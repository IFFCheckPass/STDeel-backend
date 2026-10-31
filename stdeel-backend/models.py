"""ORM 模型。

设计要点（对应 backend_requirements.md 第 3 章）：
- users 是唯一的账号主体，device_id / username 均可空且唯一；
- solve_records 用 (user_id, local_id) 做单条补传幂等，缺失时用 batch_key 兜底；
- knowledge_mastery 是 solve_records 的派生聚合，由 services.recompute_mastery 重算；
- answer_library 按 (user_id, question_hash) 隔离，user_id 为空的历史数据视为共享题库。
"""

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String, Text, func

from database import Base


def _utcnow() -> datetime:
    return datetime.utcnow()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # 跨设备找回数据的唯一凭据，两者都允许为空（仅设备注册时 username 为空）
    device_id = Column(String(255), nullable=True, unique=True, index=True)
    username = Column(String(255), nullable=True, unique=True, index=True)
    platform = Column(String(32), nullable=False, default="android", server_default="android")
    # 全量覆盖式保存：[{api_key, name, enabled}, ...]
    api_keys = Column(Text, nullable=False, default="[]", server_default="[]")
    created_at = Column(DateTime, nullable=False, default=_utcnow, server_default=func.now())
    last_active_at = Column(DateTime, nullable=False, default=_utcnow, server_default=func.now(), onupdate=_utcnow)


class SolveRecord(Base):
    __tablename__ = "solve_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True)
    # 客户端本地 SQLite 主键；批量上传取自 questions[].id，单条补传取自 local_id
    local_id = Column(Integer, nullable=True)
    # 无 local_id 时的批量去重指纹（内容 + 位置 + 时间窗口）
    batch_key = Column(String(64), nullable=True)

    question_text = Column(Text, nullable=False, default="")
    answer = Column(Text, nullable=False, default="")
    solution = Column(Text, nullable=False, default="")
    knowledge_points = Column(Text, nullable=False, default="[]")
    subject = Column(String(32), nullable=False, default="未分类")

    ai_model = Column(String(128), nullable=False, default="")
    latency_ms = Column(Integer, nullable=False, default=0)
    tokens_used = Column(Integer, nullable=False, default=0)
    matched = Column(Boolean, nullable=False, default=False)

    # 题目最近动作 / 用户判定，二者都必须落库并原样回传
    action_type = Column(String(16), nullable=False, default="solve")
    user_feedback = Column(String(16), nullable=False, default="none")
    source = Column(String(16), nullable=False, default="ai")

    confidence = Column(Float, nullable=True)
    question_no = Column(Integer, nullable=False, default=0)
    image_path = Column(Text, nullable=False, default="")

    created_at = Column(DateTime, nullable=False, default=_utcnow, server_default=func.now(), index=True)
    updated_at = Column(DateTime, nullable=False, default=_utcnow, server_default=func.now(), onupdate=_utcnow)


class AnswerLibrary(Base):
    __tablename__ = "answer_library"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=True, index=True)
    question_text = Column(Text, nullable=False, default="")
    question_hash = Column(String(64), nullable=True, index=True)
    answer = Column(Text, nullable=False, default="")
    solution = Column(Text, nullable=False, default="")
    knowledge_points = Column(Text, nullable=False, default="[]")
    subject = Column(String(32), nullable=False, default="未分类")
    created_at = Column(DateTime, nullable=False, default=_utcnow, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, default=_utcnow, server_default=func.now(), onupdate=_utcnow)


class KnowledgeMastery(Base):
    __tablename__ = "knowledge_mastery"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True)
    knowledge_point = Column(String(255), nullable=False)
    subject = Column(String(32), nullable=False, default="未分类")
    correct_count = Column(Integer, nullable=False, default=0)
    wrong_count = Column(Integer, nullable=False, default=0)
    total_count = Column(Integer, nullable=False, default=0)
    error_rate = Column(Float, nullable=False, default=0.0)
    updated_at = Column(DateTime, nullable=False, default=_utcnow, server_default=func.now(), onupdate=_utcnow)
