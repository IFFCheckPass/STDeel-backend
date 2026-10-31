"""Pydantic 模型。

请求模型统一用宽松类型（Any + extra="allow"）：客户端历史版本的字段类型
并不完全一致，宁可后端自己收敛，也不要因为它多传/少传一个字段就 422。
响应模型严格定义，保证契约里列出的字段一定存在。
"""

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_serializer, model_validator

from utils import DEFAULT_SUBJECT, encode_user_id, iso_utc, load_json_list


# ==========================================================================
# 请求体
# ==========================================================================
class UserRegister(BaseModel):
    """POST /users/register：device_id 与 username 二选一（或都传）。"""

    model_config = ConfigDict(extra="allow")

    device_id: Any = None
    username: Any = None
    platform: Any = None


class ApiKeyPayload(BaseModel):
    """PUT /users/api-key：全量覆盖式保存 Key 列表。"""

    model_config = ConfigDict(extra="allow")

    user_id: Any = None
    api_keys: Any = None


class FeedbackPayload(BaseModel):
    """PATCH /solve-records/{id}/feedback。"""

    model_config = ConfigDict(extra="allow")

    user_id: Any = None
    feedback: Any = None
    user_feedback: Any = None


class DeleteRecordPayload(BaseModel):
    """DELETE /solve-records/{id} 的可选请求体。"""

    model_config = ConfigDict(extra="allow")

    user_id: Any = None


class SolveRecordCreate(BaseModel):
    """POST /solve-records：同一路由兼容「批量」与「单条补传」两种形态。"""

    model_config = ConfigDict(extra="allow")

    # 公共
    user_id: Any = None
    ai_model: Any = None
    latency_ms: Any = None
    tokens_used: Any = None
    source: Any = None
    image_path: Any = None

    # 形态 A（批量）
    questions: Any = None

    # 形态 B（单条）以及 questions[] 内的同名字段
    local_id: Any = None
    question_text: Any = None
    content: Any = None
    answer: Any = None
    solution: Any = None
    knowledge_points: Any = None
    subject: Any = None
    matched: Any = None
    action_type: Any = None
    user_feedback: Any = None
    confidence: Any = None
    question_no: Any = None


class AnswerLibraryCreate(BaseModel):
    """POST /answer-library。"""

    model_config = ConfigDict(extra="allow")

    user_id: Any = None
    question_text: Any = None
    question_hash: Any = None
    answer: Any = None
    solution: Any = None
    knowledge_points: Any = None
    subject: Any = None


class AnswerLibraryUpdate(BaseModel):
    """PUT /answer-library/{id}（管理端用）。"""

    model_config = ConfigDict(extra="allow")

    question_text: Any = None
    answer: Any = None
    solution: Any = None
    knowledge_points: Any = None
    subject: Any = None


class AnswerLibraryMatch(BaseModel):
    """POST /answer-library/match。"""

    model_config = ConfigDict(extra="allow")

    user_id: Any = None
    question_text: Any = None
    question_hash: Any = None


# ==========================================================================
# 响应体
# ==========================================================================
class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    username: Optional[str] = None
    device_id: Optional[str] = None
    platform: str = "android"
    created_at: Optional[datetime] = None
    last_active_at: Optional[datetime] = None

    @model_validator(mode="before")
    @classmethod
    def _from_orm(cls, data: Any):
        if isinstance(data, dict):
            return data
        user_id = encode_user_id(getattr(data, "id", None))
        return {
            "id": user_id,
            "user_id": user_id,
            "username": getattr(data, "username", None),
            "device_id": getattr(data, "device_id", None),
            "platform": getattr(data, "platform", None) or "android",
            "created_at": getattr(data, "created_at", None),
            "last_active_at": getattr(data, "last_active_at", None),
        }

    @field_serializer("created_at", "last_active_at")
    def _serialize_time(self, value: Optional[datetime]) -> Optional[str]:
        return iso_utc(value)


class UserStats(BaseModel):
    total_users: int
    today_new: int
    active_7d: int
    active_30d: int


class UserList(BaseModel):
    items: list[UserOut]
    total: int
    page: int
    page_size: int


class SolveRecordOut(BaseModel):
    """GET /solve-records 的条目：字段与需求 §4.7 的「客户端实际读取字段」一一对应。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: Optional[str] = None
    local_id: Optional[int] = None
    question_text: str = ""
    answer: str = ""
    solution: str = ""
    knowledge_points: list[str] = Field(default_factory=list)
    subject: str = DEFAULT_SUBJECT
    ai_model: str = ""
    latency_ms: int = 0
    tokens_used: int = 0
    matched: bool = False
    action_type: str = "solve"
    user_feedback: str = "none"
    source: str = "ai"
    confidence: Optional[float] = None
    question_no: int = 0
    image_path: str = ""
    created_at: Optional[datetime] = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def status(self) -> str:
        """老客户端读 status：与 action_type 保持同值，二者都不丢。"""
        return self.action_type

    @model_validator(mode="before")
    @classmethod
    def _from_orm(cls, data: Any):
        if isinstance(data, dict):
            return data
        return {
            "id": getattr(data, "id", None),
            "user_id": encode_user_id(getattr(data, "user_id", None)),
            "local_id": getattr(data, "local_id", None),
            "question_text": getattr(data, "question_text", None) or "",
            "answer": getattr(data, "answer", None) or "",
            "solution": getattr(data, "solution", None) or "",
            "knowledge_points": load_json_list(getattr(data, "knowledge_points", None)),
            "subject": getattr(data, "subject", None) or DEFAULT_SUBJECT,
            "ai_model": getattr(data, "ai_model", None) or "",
            "latency_ms": getattr(data, "latency_ms", 0) or 0,
            "tokens_used": getattr(data, "tokens_used", 0) or 0,
            "matched": bool(getattr(data, "matched", False)),
            "action_type": getattr(data, "action_type", None) or "solve",
            "user_feedback": getattr(data, "user_feedback", None) or "none",
            "source": getattr(data, "source", None) or "ai",
            "confidence": getattr(data, "confidence", None),
            "question_no": getattr(data, "question_no", 0) or 0,
            "image_path": getattr(data, "image_path", None) or "",
            "created_at": getattr(data, "created_at", None),
        }

    @field_serializer("created_at")
    def _serialize_time(self, value: Optional[datetime]) -> Optional[str]:
        return iso_utc(value)


class SolveRecordList(BaseModel):
    total: int
    items: list[SolveRecordOut]


class AnswerLibraryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: Optional[str] = None
    question_text: str = ""
    question_hash: str = ""
    answer: str = ""
    solution: str = ""
    knowledge_points: list[str] = Field(default_factory=list)
    subject: str = DEFAULT_SUBJECT
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @model_validator(mode="before")
    @classmethod
    def _from_orm(cls, data: Any):
        if isinstance(data, dict):
            return data
        return {
            "id": getattr(data, "id", None),
            "user_id": encode_user_id(getattr(data, "user_id", None)),
            "question_text": getattr(data, "question_text", None) or "",
            "question_hash": getattr(data, "question_hash", None) or "",
            "answer": getattr(data, "answer", None) or "",
            "solution": getattr(data, "solution", None) or "",
            "knowledge_points": load_json_list(getattr(data, "knowledge_points", None)),
            "subject": getattr(data, "subject", None) or DEFAULT_SUBJECT,
            "created_at": getattr(data, "created_at", None),
            "updated_at": getattr(data, "updated_at", None),
        }

    @field_serializer("created_at", "updated_at")
    def _serialize_time(self, value: Optional[datetime]) -> Optional[str]:
        return iso_utc(value)


class AnswerLibraryList(BaseModel):
    items: list[AnswerLibraryOut]
    total: int
    page: int
    page_size: int


class MatchAnswer(BaseModel):
    """match 响应里的 answer 对象：knowledge_points 必须是 JSON 字符串。"""

    id: Optional[int] = None
    answer: str = ""
    solution: str = ""
    subject: str = DEFAULT_SUBJECT
    knowledge_points: str = "[]"


class MatchResult(BaseModel):
    hit: bool
    similarity: float = 0.0
    answer: Optional[MatchAnswer] = None
    # 老客户端读 matched，保留同义字段
    matched: bool = False


class KnowledgePointOut(BaseModel):
    id: int
    knowledge_point: str


class KnowledgeMasteryItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: Optional[int] = None
    user_id: Optional[str] = None
    knowledge_point: str
    subject: str = DEFAULT_SUBJECT
    correct_count: int = 0
    wrong_count: int = 0
    total_count: int = 0
    error_rate: float = 0.0
    updated_at: Optional[datetime] = None

    @model_validator(mode="before")
    @classmethod
    def _from_orm(cls, data: Any):
        if isinstance(data, dict):
            return data
        return {
            "id": getattr(data, "id", None),
            "user_id": encode_user_id(getattr(data, "user_id", None)),
            "knowledge_point": getattr(data, "knowledge_point", None) or "",
            "subject": getattr(data, "subject", None) or DEFAULT_SUBJECT,
            "correct_count": getattr(data, "correct_count", 0) or 0,
            "wrong_count": getattr(data, "wrong_count", 0) or 0,
            "total_count": getattr(data, "total_count", 0) or 0,
            "error_rate": getattr(data, "error_rate", 0.0) or 0.0,
            "updated_at": getattr(data, "updated_at", None),
        }

    @field_serializer("updated_at")
    def _serialize_time(self, value: Optional[datetime]) -> Optional[str]:
        return iso_utc(value)


class KnowledgeMasteryList(BaseModel):
    items: list[KnowledgeMasteryItem]
    total: int


class ApiKeyOut(BaseModel):
    id: int
    user_id: str
    api_key: str
    name: str = ""
    enabled: bool = True


class FileUploadOut(BaseModel):
    path: str
    url: str


class BatchUploadResult(BaseModel):
    """批量上传响应：顺序与 questions 一致，元素只含数字 id。"""

    items: list[dict[str, int]]


class SingleUploadResult(BaseModel):
    id: int
