import json
from datetime import datetime
from typing import List, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


class UserRegister(BaseModel):
    """注册请求体: 全部字段可选, device_id 与 username 任一即可, 都不传则生成匿名用户。"""

    model_config = ConfigDict(extra="ignore")

    username: Optional[str] = None
    device_id: Optional[str] = None


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    username: str
    device_id: Optional[str] = None
    created_at: datetime
    last_active_at: datetime

    @model_validator(mode="before")
    @classmethod
    def _populate_user_id(cls, data):
        if hasattr(data, "id") and getattr(data, "user_id", None) is None:
            try:
                data.user_id = data.id
            except Exception:
                pass
        elif isinstance(data, dict) and "id" in data and "user_id" not in data:
            data["user_id"] = data["id"]
        return data


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

    @field_validator("knowledge_points", mode="before")
    @classmethod
    def _jsonify_knowledge_points(cls, v):
        """兼容: 前端若传数组, 落库前序列化为 JSON 字符串。"""
        if isinstance(v, (list, tuple)):
            return json.dumps(v, ensure_ascii=False)
        return v


class SolveRecordOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: Optional[int] = None
    question_text: str
    answer: Optional[str] = None
    solution: Optional[str] = None
    knowledge_points: Optional[List[str]] = None
    ai_model: Optional[str] = None
    latency_ms: Optional[int] = None
    tokens_used: Optional[int] = None
    matched: bool = False
    user_feedback: Optional[str] = None
    image_path: Optional[str] = None
    created_at: datetime

    @field_validator("knowledge_points", mode="before")
    @classmethod
    def _parse_knowledge_points(cls, v):
        """把库中的 JSON 字符串解析为数组, 供前端直接消费。"""
        if isinstance(v, str):
            try:
                return json.loads(v)
            except (json.JSONDecodeError, TypeError):
                return None
        return v


class FeedbackUpdate(BaseModel):
    user_feedback: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("user_feedback", "feedback"),
    )

    model_config = ConfigDict(populate_by_name=True)


class SolveRecordList(BaseModel):
    items: List[SolveRecordOut]
    total: int
    page: Optional[int] = None
    page_size: Optional[int] = None


class KnowledgePointCreate(BaseModel):
    knowledge_point: str


class KnowledgePointOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    knowledge_point: str


class KnowledgeMasteryItem(BaseModel):
    """单个 mastery 记录(支持按 user 维度)"""
    model_config = ConfigDict(from_attributes=True)

    id: Optional[int] = None
    user_id: Optional[int] = None
    knowledge_point: str
    correct_count: int = 0
    wrong_count: int = 0
    total_count: int = 0
    error_rate: float = 0.0
    updated_at: Optional[datetime] = None


class KnowledgeMasteryList(BaseModel):
    items: List[KnowledgeMasteryItem]
    total: int


# 兼容旧引用
KnowledgeMasteryOut = KnowledgeMasteryItem


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


class ApiKeyItem(BaseModel):
    """单个 api-key 条目。"""

    api_key: str
    name: Optional[str] = None
    enabled: Optional[bool] = True


class ApiKeyBatchUpsert(BaseModel):
    """批量上报该用户的 api-key 列表(全量覆盖, 对齐前端 {user_id, api_keys})。"""

    user_id: int
    api_keys: List[ApiKeyItem] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _coerce_api_keys(cls, data):
        """兼容: api_keys 元素既可是 {api_key,...} 对象, 也可是纯字符串 key。"""
        if isinstance(data, dict) and isinstance(data.get("api_keys"), list):
            data["api_keys"] = [
                ({"api_key": k} if isinstance(k, str) else k)
                for k in data["api_keys"]
            ]
        return data


class ApiKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    api_key: str
    name: Optional[str] = None
    enabled: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
