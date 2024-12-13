from fastapi import FastAPI, HTTPException, Request
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

# mount templates and static files
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
        self.base_directory.mkdir(exist_ok=True)
        
        # Sample resume data
        self.sample_resumes = {
            "john_doe.txt": """
            John Doe
            Senior Software Engineer

            Experience:
            - Lead Developer at TechCorp (2019-Present)
              * Led team of 5 developers on cloud migration project
              * Implemented MLOps pipeline reducing deployment time by 60%
              * Mentored junior developers and conducted code reviews
            
            Skills:
            - Programming: Python, Java, Go
            - Cloud & DevOps: Kubernetes, Docker, AWS
            - Machine Learning: TensorFlow, PyTorch, MLOps
            """,
            "jane_smith.txt": """
            Jane Smith
            AI Research Engineer

            Experience:
            - AI Research Lead at DataMinds (2020-Present)
              * Published 3 papers on NLP architectures
              * Developed novel attention mechanism improving accuracy by 25%
              * Led research team of 3 PhD candidates
            
            Skills:
            - Deep Learning: PyTorch, TensorFlow
            - NLP: Transformers, BERT, GPT
            - Research: Paper Writing, Experimentation
            """
        }
        
        # Initialize resumes
        self.initialize_resumes()
    
    def initialize_resumes(self):
        for filename, content in self.sample_resumes.items():
            filepath = self.base_directory / filename
            filepath.write_text(content, encoding="utf-8")
    
    def get_resume_paths(self) -> List[str]:
        return [str(path) for path in self.base_directory.glob("*.txt")]

class AssistantManager:
    def __init__(self):
        self.config = AzureConfig.from_env()
        self.kernel = Kernel()
        self.agent = None
        self.thread_id = None
        
    async def initialize(self):
        if not self.agent:
            resume_manager = ResumeManager()
            resume_paths = resume_manager.get_resume_paths()
            
            self.agent = await AzureAssistantAgent.create(
                kernel=self.kernel,
                deployment_name=self.config.deployment_name,
                endpoint=self.config.endpoint,
                api_key=self.config.api_key,
                api_version=self.config.api_version,
                name="HR_Resume_Analyzer",
                instructions="""
                You are an expert HR assistant specialized in analyzing resumes and providing 
                detailed candidate evaluations. Always analyze the resumes thoroughly and 
                provide specific evidence for your conclusions.
                """,
                enable_file_search=True,
                vector_store_filenames=resume_paths,
                ai_model_id=self.config.deployment_name,
                temperature=0.7,
            )
            
            self.thread_id = await self.agent.create_thread()
    
    async def analyze(self, query: str) -> Dict:
        if not self.agent or not self.thread_id:
            await self.initialize()
        
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
assistant_manager = AssistantManager()

class Query(BaseModel):
    text: str

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {"request": request}
    )

@app.post("/analyze")
async def analyze_resumes(query: Query):
    try:
        result = await assistant_manager.analyze(query.text)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
