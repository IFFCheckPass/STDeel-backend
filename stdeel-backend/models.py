from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String, Text, UniqueConstraint, func

from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(255), nullable=False, unique=True)
    device_id = Column(String(255), nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    last_active_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class AnswerLibrary(Base):
    __tablename__ = "answer_library"

    id = Column(Integer, primary_key=True, autoincrement=True)
    question_text = Column(Text, nullable=False)
    question_hash = Column(String(128), nullable=True)
    answer = Column(Text, nullable=True)
    solution = Column(Text, nullable=True)
    knowledge_points = Column(Text, nullable=True)
    source = Column(String(50), default="manual")
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class SolveRecord(Base):
    __tablename__ = "solve_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=True)
    question_text = Column(Text, nullable=False)
    answer = Column(Text, nullable=True)
    solution = Column(Text, nullable=True)
    knowledge_points = Column(Text, nullable=True)
    ai_model = Column(String(128), nullable=True)
    latency_ms = Column(Integer, nullable=True)
    tokens_used = Column(Integer, nullable=True)
    matched = Column(Boolean, default=False)
    user_feedback = Column(String(20), nullable=True)
    image_path = Column(String(512), nullable=True)
    created_at = Column(DateTime, server_default=func.now())


class UserApiKey(Base):
    """用户 AI api-key(跨端同步: 账号不变/换机不重配, 一用户多 key)。"""

    __tablename__ = "user_api_keys"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True)
    api_key = Column(Text, nullable=False)
    name = Column(String(128), nullable=True)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class KnowledgeMastery(Base):
    __tablename__ = "knowledge_mastery"
    __table_args__ = (
        UniqueConstraint("user_id", "knowledge_point", name="uq_knowledge_mastery_user_point"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=True)
    knowledge_point = Column(String(255), nullable=False)
    correct_count = Column(Integer, default=0)
    wrong_count = Column(Integer, default=0)
    total_count = Column(Integer, default=0)
    error_rate = Column(Float, default=0.0)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
