from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class IngestResponse(BaseModel):
    filename: str
    chunks_ingested: int
    chunks: Optional[int] = None
    pages: Optional[int] = 1
    message: str


class SearchPolicyResult(BaseModel):
    id: str
    text: str
    source: str
    section: Optional[str] = "General"
    chunk_index: int
    score: float


class EmployeeResponse(BaseModel):
    id: Optional[int] = None
    emp_code: Optional[str] = None
    name: str
    email: Optional[str] = None
    department: str
    designation: str
    manager_id: Optional[int] = None
    role: Optional[str] = None
    join_date: Optional[str] = None
    work_location: Optional[str] = None
    status: Optional[str] = None

    model_config = {"from_attributes": True}


class HealthResponse(BaseModel):
    status: str
    database: str
    db_type: str = "sqlite"  # 'postgresql' or 'sqlite'
    vectorstore: str
    qdrant_mode: str = "local"  # 'cloud' or 'local'
    model_name: str
    ingested_chunks: int = 0
    tables: Dict[str, int] = Field(default_factory=dict)


class ChatRequest(BaseModel):
    session_id: str = Field(..., description="Unique conversation session ID")
    message: str = Field(..., description="User message text")
    retry: Optional[bool] = Field(False, description="Whether this request is retrying a failed turn")

    model_config = {"extra": "ignore"}


class MemoryResponse(BaseModel):
    id: str
    user_id: str
    text: str
    created_at: Optional[str] = None


class ChatMessageResponse(BaseModel):
    id: int
    session_id: str
    user_id: Optional[str] = None
    role: str
    content: str
    created_at: Optional[str] = None
