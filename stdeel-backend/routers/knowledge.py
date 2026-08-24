import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import KnowledgeMastery
from schemas import (
    KnowledgePointCreate,
    KnowledgePointOut,
    KnowledgeMasteryItem,
    KnowledgeMasteryList,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/knowledge", tags=["knowledge"])


@router.get("/points", response_model=list[KnowledgePointOut])
async def list_points(db: AsyncSession = Depends(get_db)):
    """列出所有出现过的 knowledge_point (distinct). id 为顺序号。"""
    result = await db.execute(
        select(KnowledgeMastery.knowledge_point)
        .distinct()
        .order_by(KnowledgeMastery.knowledge_point)
    )
    points = [r[0] for r in result.all()]
    return [KnowledgePointOut(id=i + 1, knowledge_point=p) for i, p in enumerate(points)]


@router.post("/points", response_model=KnowledgePointOut)
async def create_point(body: KnowledgePointCreate, db: AsyncSession = Depends(get_db)):
    """检查并确认知识点存在(不实际插入, 由 mastery 自动维护)。"""
    existing = await db.execute(
        select(KnowledgeMastery).where(KnowledgeMastery.knowledge_point == body.knowledge_point)
    )
    if existing.scalar_one_or_none():
        logger.info("知识点已存在: %s", body.knowledge_point)
    return KnowledgePointOut(id=0, knowledge_point=body.knowledge_point)


@router.get("/mastery", response_model=KnowledgeMasteryList)
async def list_mastery(
    user_id: int | None = Query(None, description="可选: 按 user 隔离; 不传则全局聚合"),
    db: AsyncSession = Depends(get_db),
):
    if user_id is not None:
        stmt = (
            select(KnowledgeMastery)
            .where(KnowledgeMastery.user_id == user_id)
            .order_by(KnowledgeMastery.error_rate.desc(), KnowledgeMastery.total_count.desc())
        )
        rows = (await db.execute(stmt)).scalars().all()
        items = [KnowledgeMasteryItem.model_validate(r) for r in rows]
    else:
        # 全局聚合
        stmt = (
            select(
                func.sum(KnowledgeMastery.correct_count).label("correct_count"),
                func.sum(KnowledgeMastery.wrong_count).label("wrong_count"),
                KnowledgeMastery.knowledge_point.label("knowledge_point"),
            )
            .group_by(KnowledgeMastery.knowledge_point)
        )
        aggregated = (await db.execute(stmt)).all()
        items = []
        for r in aggregated:
            c = int(r.correct_count or 0)
            w = int(r.wrong_count or 0)
            t = c + w
            er = (w / t) if t > 0 else 0.0
            items.append(
                KnowledgeMasteryItem(
                    id=None,
                    user_id=None,
                    knowledge_point=r.knowledge_point,
                    correct_count=c,
                    wrong_count=w,
                    total_count=t,
                    error_rate=er,
                    updated_at=None,
                )
            )
        items.sort(key=lambda x: (x.error_rate, x.total_count), reverse=True)
    return KnowledgeMasteryList(items=items, total=len(items))


@router.get("/mastery/{point:path}", response_model=KnowledgeMasteryItem)
async def get_mastery(
    point: str,
    user_id: int | None = Query(None, description="可选: 按 user 隔离"),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(KnowledgeMastery).where(KnowledgeMastery.knowledge_point == point)
    if user_id is not None:
        stmt = stmt.where(KnowledgeMastery.user_id == user_id)
    result = await db.execute(stmt)
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="知识点不存在")
    return KnowledgeMasteryItem.model_validate(row)


@router.get("/weak", response_model=KnowledgeMasteryList)
async def get_weak_points(
    threshold: float = Query(0.5, ge=0.0, le=1.0),
    user_id: int | None = Query(None, description="可选: 按 user 隔离"),
    db: AsyncSession = Depends(get_db),
):
    if user_id is not None:
        stmt = (
            select(KnowledgeMastery)
            .where(KnowledgeMastery.user_id == user_id)
            .where(KnowledgeMastery.error_rate >= threshold)
            .where(KnowledgeMastery.total_count > 0)
            .order_by(KnowledgeMastery.error_rate.desc())
        )
        rows = (await db.execute(stmt)).scalars().all()
        items = [KnowledgeMasteryItem.model_validate(r) for r in rows]
    else:
        # 全局聚合
        stmt = (
            select(
                func.sum(KnowledgeMastery.correct_count).label("correct_count"),
                func.sum(KnowledgeMastery.wrong_count).label("wrong_count"),
                KnowledgeMastery.knowledge_point.label("knowledge_point"),
            )
            .group_by(KnowledgeMastery.knowledge_point)
        )
        aggregated = (await db.execute(stmt)).all()
        items = []
        for r in aggregated:
            c = int(r.correct_count or 0)
            w = int(r.wrong_count or 0)
            t = c + w
            if t == 0:
                continue
            er = w / t
            if er < threshold:
                continue
            items.append(
                KnowledgeMasteryItem(
                    id=None,
                    user_id=None,
                    knowledge_point=r.knowledge_point,
                    correct_count=c,
                    wrong_count=w,
                    total_count=t,
                    error_rate=er,
                    updated_at=None,
                )
            )
        items.sort(key=lambda x: x.error_rate, reverse=True)
    return KnowledgeMasteryList(items=items, total=len(items))
