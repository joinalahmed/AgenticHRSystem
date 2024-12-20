from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
import uvicorn
from pathlib import Path
from pydantic import BaseModel
from typing import Dict, List, Optional
import os
from dotenv import load_dotenv
import asyncio
import shutil
from datetime import datetime
from semantic_kernel.kernel import Kernel
from semantic_kernel.agents.open_ai.azure_assistant_agent import AzureAssistantAgent
from semantic_kernel.contents.chat_message_content import ChatMessageContent
from semantic_kernel.contents.streaming_annotation_content import StreamingAnnotationContent
from semantic_kernel.contents.utils.author_role import AuthorRole
from dataclasses import dataclass

# Load environment variables
load_dotenv()

# Initialize FastAPI App
app = FastAPI(title="HR Resume Search")

# Mount templates and static files
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

@dataclass
class AzureConfig:
    """Configuration for Azure OpenAI Assistant."""
    api_key: str
    endpoint: str
    deployment_name: str
    api_version: str = "2024-10-01-preview"

    @classmethod
    def from_env(cls) -> "AzureConfig":
        """Load configuration from environment variables."""
        api_key = os.getenv("AZURE_OPENAI_API_KEY")
        endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
        deployment_name = os.getenv("AZURE_OPENAI_CHAT_COMPLETION_DEPLOYED_MODEL_NAME")

        if not all([api_key, endpoint, deployment_name]):
            missing = [
                var for var, val in {
                    "AZURE_OPENAI_API_KEY": api_key,
                    "AZURE_OPENAI_ENDPOINT": endpoint,
                    "AZURE_OPENAI_CHAT_COMPLETION_DEPLOYED_MODEL_NAME": deployment_name,
                }.items() if not val
            ]
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

        return cls(
            api_key=api_key,
            endpoint=endpoint.rstrip("/"),
            deployment_name=deployment_name,
        )

class ResumeManager:
    def __init__(self):
        self.base_directory = Path("resumes")
        self.jobs_directory = Path("jobs")
        self.base_directory.mkdir(exist_ok=True)
        self.jobs_directory.mkdir(exist_ok=True)
    
    def save_uploaded_file(self, file: UploadFile, file_type: str) -> str:
        """Save uploaded file with timestamp in filename."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_filename = f"{timestamp}_{file.filename}"
        
        directory = self.jobs_directory if file_type == "job" else self.base_directory
        file_path = directory / safe_filename
        
        with file_path.open("wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        
        return safe_filename
    
    def get_all_files(self) -> Dict[str, List[str]]:
        """Get all available resume and job files."""
        return {
            "resumes": [f.name for f in self.base_directory.glob("*.txt")],
            "jobs": [f.name for f in self.jobs_directory.glob("*.txt")]
        }
    
    def get_file_content(self, filename: str, file_type: str) -> str:
        """Get content of a specific file."""
        directory = self.jobs_directory if file_type == "job" else self.base_directory
        file_path = directory / filename
        
        if not file_path.exists():
            raise FileNotFoundError(f"File {filename} not found")
        
        return file_path.read_text(encoding="utf-8")
    
    def get_file_paths(self, resume_files: List[str], job_files: List[str]) -> List[str]:
        """Get full paths of selected resume and job files."""
        resume_paths = [str(self.base_directory / filename) for filename in resume_files]
        job_paths = [str(self.jobs_directory / filename) for filename in job_files]
        return resume_paths + job_paths

class AssistantManager:
    def __init__(self):
        self.config = AzureConfig.from_env()
        self.kernel = Kernel()
        self.agent = None
        self.thread_id = None
        
    async def initialize(self, selected_files: List[str]):
        """Initialize or reinitialize the agent with selected files."""
        # Always create a new agent with the current selection of files
        self.agent = await AzureAssistantAgent.create(
            kernel=self.kernel,
            deployment_name=self.config.deployment_name,
            endpoint=self.config.endpoint,
            api_key=self.config.api_key,
            api_version=self.config.api_version,
            name="HR_Resume_Analyzer",
            instructions="""
            You are an expert HR assistant specialized in analyzing resumes and providing 
            detailed candidate evaluations. You have access to both resumes and job descriptions 
            in your vector store. When analyzing candidates, reference specific requirements 
            from the job descriptions and match them against candidate qualifications. 
            Always provide specific evidence and examples from both the resumes and job 
            descriptions to support your analysis. only give the analysis and not the citations and extra information.
            always give response as a table markdown format.
            """,
            enable_file_search=True,
            vector_store_filenames=selected_files,
            ai_model_id=self.config.deployment_name,
            temperature=0.0,
            seed=42,
        )
        
        self.thread_id = await self.agent.create_thread()
    
    async def analyze(self, query: str, resume_files: List[str], job_files: List[str]) -> Dict:
        """Analyze resumes and job descriptions with the given query."""
        # Get all selected file paths
        selected_files = resume_manager.get_file_paths(resume_files, job_files)
        
        # Initialize/reinitialize with currently selected files
        await self.initialize(selected_files)
        
        # Enhance the query to explicitly mention job descriptions if present
        if job_files:
            query = f"Please analyze the provided resumes against the job descriptions in the vector store. {query}"
        
        await self.agent.add_chat_message(
            thread_id=self.thread_id,
            message=ChatMessageContent(
                role=AuthorRole.USER,
                content=query
            )
        )
        
        response_text = ""
        citations = []
        
        async for response in self.agent.invoke_stream(thread_id=self.thread_id):
            if isinstance(response.items[0], StreamingAnnotationContent):
                citations.extend(response.items)
            else:
                response_text += response.content
        
        return {
            "analysis": response_text,
            "citations": [{"file": note.file_id, "quote": note.quote} for note in citations if isinstance(note, StreamingAnnotationContent)]
        }

# Initialize managers
resume_manager = ResumeManager()
assistant_manager = AssistantManager()

class AnalysisRequest(BaseModel):
    text: str
    resumes: List[str]
    jobs: List[str] = []

# Routes
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {"request": request}
    )

@app.post("/upload/{file_type}")
async def upload_file(file_type: str, file: UploadFile = File(...)):
    if file_type not in ["resume", "job"]:
        raise HTTPException(status_code=400, detail="Invalid file type")
    
    try:
        filename = resume_manager.save_uploaded_file(file, file_type)
        return {"filename": filename}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/files")
async def get_files():
    return resume_manager.get_all_files()

@app.post("/analyze")
async def analyze_resumes(request: AnalysisRequest):
    try:
        # Analyze with both selected resumes and job descriptions
        result = await assistant_manager.analyze(
            query=request.text,
            resume_files=request.resumes,
            job_files=request.jobs
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)