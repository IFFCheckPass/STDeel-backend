from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict


class UserRegister(BaseModel):
    username: str
    device_id: Optional[str] = None


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    device_id: Optional[str] = None
    created_at: datetime
    last_active_at: datetime


class UserStats(BaseModel):
    total_users: int
    today_new: int
    active_7d: int
    active_30d: int


class UserList(BaseModel):
    items: List[UserOut]
    total: int
    page: int
    page_size: int


class SolveRecordCreate(BaseModel):
    user_id: Optional[int] = None
    question_text: str
    answer: Optional[str] = None
    solution: Optional[str] = None
    knowledge_points: Optional[str] = None
    ai_model: Optional[str] = None
    latency_ms: Optional[int] = None
    tokens_used: Optional[int] = None
    matched: bool = False
    image_path: Optional[str] = None


class SolveRecordOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: Optional[int] = None
    question_text: str
    answer: Optional[str] = None
    solution: Optional[str] = None
    knowledge_points: Optional[str] = None
    ai_model: Optional[str] = None
    latency_ms: Optional[int] = None
    tokens_used: Optional[int] = None
    matched: bool = False
    user_feedback: Optional[str] = None
    image_path: Optional[str] = None
    created_at: datetime


class FeedbackUpdate(BaseModel):
    user_feedback: Optional[str] = None


class SolveRecordList(BaseModel):
    items: List[SolveRecordOut]
    total: int
    page: int
    page_size: int


class KnowledgePointCreate(BaseModel):
    knowledge_point: str


class KnowledgePointOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    knowledge_point: str


class KnowledgeMasteryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    knowledge_point: str
    correct_count: int
    wrong_count: int
    total_count: int
    error_rate: float
    updated_at: datetime


class AnswerLibraryCreate(BaseModel):
    question_text: str
    answer: Optional[str] = None
    solution: Optional[str] = None
    knowledge_points: Optional[str] = None
    source: str = "manual"


class AnswerLibraryUpdate(BaseModel):
    question_text: Optional[str] = None
    answer: Optional[str] = None
    solution: Optional[str] = None
    knowledge_points: Optional[str] = None
    source: Optional[str] = None


class AnswerLibraryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    question_text: str
    question_hash: Optional[str] = None
    answer: Optional[str] = None
    solution: Optional[str] = None
    knowledge_points: Optional[str] = None
    source: str
    created_at: datetime
    updated_at: datetime


class AnswerLibraryList(BaseModel):
    items: List[AnswerLibraryOut]
    total: int
    page: int
    page_size: int


class MatchRequest(BaseModel):
    question_text: str
    threshold: float = 0.85


class MatchResult(BaseModel):
    matched: bool
    similarity: float
    answer: Optional[AnswerLibraryOut] = None


class FileUploadOut(BaseModel):
    path: str
    url: str
