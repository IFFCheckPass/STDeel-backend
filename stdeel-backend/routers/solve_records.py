import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import SolveRecord, KnowledgeMastery
from schemas import (
    SolveRecordCreate,
    SolveRecordOut,
    FeedbackUpdate,
    SolveRecordList,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/solve-records", tags=["solve-records"])


async def _update_mastery(
    session: AsyncSession,
    user_id: int | None,
    knowledge_points_str: str | None,
    old_feedback: str | None,
    new_feedback: str | None,
) -> None:
    if not knowledge_points_str or new_feedback not in ("correct", "wrong"):
        return
    try:
        points = json.loads(knowledge_points_str)
    except (json.JSONDecodeError, TypeError):
        return
    if not isinstance(points, list):
        return

    delta_correct = 0
    delta_wrong = 0
    if old_feedback == "correct":
        delta_correct -= 1
    elif old_feedback == "wrong":
        delta_wrong -= 1
    if new_feedback == "correct":
        delta_correct += 1
    elif new_feedback == "wrong":
        delta_wrong += 1

    if delta_correct == 0 and delta_wrong == 0:
        return

    for point in points:
        existing_result = await session.execute(
            select(KnowledgeMastery)
            .where(KnowledgeMastery.user_id == user_id)
            .where(KnowledgeMastery.knowledge_point == point)
        )
        existing = existing_result.scalar_one_or_none()
        if not existing:
            existing = KnowledgeMastery(
                user_id=user_id,
                knowledge_point=point,
                correct_count=0,
                wrong_count=0,
                total_count=0,
                error_rate=0.0,
            )
            session.add(existing)
            await session.flush()
        existing.correct_count = max(0, existing.correct_count + delta_correct)
        existing.wrong_count = max(0, existing.wrong_count + delta_wrong)
        existing.total_count = existing.correct_count + existing.wrong_count
        existing.error_rate = (
            existing.wrong_count / existing.total_count if existing.total_count > 0 else 0.0
        )
        existing.updated_at = func.now()


@router.post("", response_model=SolveRecordOut)
async def create_record(body: SolveRecordCreate, db: AsyncSession = Depends(get_db)):
    record = SolveRecord(**body.model_dump())
    db.add(record)
    await db.flush()
    logger.info("解题记录创建: id=%s, user_id=%s", record.id, record.user_id)
    return record


@router.get("", response_model=SolveRecordList)
async def list_records(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user_id: int = Query(None),
    knowledge: str = Query(None),
    start_date: str = Query(None),
    end_date: str = Query(None),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(SolveRecord)
    if user_id is not None:
        stmt = stmt.where(SolveRecord.user_id == user_id)
    if knowledge:
        stmt = stmt.where(SolveRecord.knowledge_points.contains(knowledge))
    if start_date:
        stmt = stmt.where(SolveRecord.created_at >= start_date)
    if end_date:
        stmt = stmt.where(SolveRecord.created_at <= end_date)
    total_stmt = select(func.count()).select_from(stmt.subquery())
    total = await db.execute(total_stmt)
    items_stmt = stmt.order_by(SolveRecord.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    items = await db.execute(items_stmt)
    return SolveRecordList(items=items.scalars().all(), total=total.scalar_one(), page=page, page_size=page_size)


@router.get("/{record_id}", response_model=SolveRecordOut)
async def get_record(record_id: int, db: AsyncSession = Depends(get_db)):
    record = await db.get(SolveRecord, record_id)
    if not record:
        raise HTTPException(status_code=404, detail="记录不存在")
    return record


@router.patch("/{record_id}/feedback", response_model=SolveRecordOut)
async def update_feedback(record_id: int, body: FeedbackUpdate, db: AsyncSession = Depends(get_db)):
    record = await db.get(SolveRecord, record_id)
    if not record:
        raise HTTPException(status_code=404, detail="记录不存在")
    old_feedback = record.user_feedback
    new_feedback = body.user_feedback
    if old_feedback == new_feedback:
        logger.info("反馈更新(未变): record=%s, feedback=%s", record_id, new_feedback)
        return record
    await _update_mastery(db, record.user_id, record.knowledge_points, old_feedback, new_feedback)
    record.user_feedback = new_feedback
    await db.flush()
    logger.info("反馈更新: record=%s, feedback=%s", record_id, new_feedback)
    return record
