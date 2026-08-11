from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String, Text, func

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


class KnowledgeMastery(Base):
    __tablename__ = "knowledge_mastery"

    id = Column(Integer, primary_key=True, autoincrement=True)
    knowledge_point = Column(String(255), nullable=False, unique=True)
    correct_count = Column(Integer, default=0)
    wrong_count = Column(Integer, default=0)
    total_count = Column(Integer, default=0)
    error_rate = Column(Float, default=0.0)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
