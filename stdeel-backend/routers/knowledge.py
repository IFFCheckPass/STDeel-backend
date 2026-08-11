import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import KnowledgeMastery
from schemas import KnowledgePointCreate, KnowledgePointOut, KnowledgeMasteryOut

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/knowledge", tags=["knowledge"])


@router.get("/points", response_model=list[KnowledgePointOut])
async def list_points(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(KnowledgeMastery.id.label("id"), KnowledgeMastery.knowledge_point).order_by(KnowledgeMastery.knowledge_point))
    return [KnowledgePointOut(id=r.id, knowledge_point=r.knowledge_point) for r in result.all()]


@router.post("/points", response_model=KnowledgePointOut)
async def create_point(body: KnowledgePointCreate, db: AsyncSession = Depends(get_db)):
    existing = await db.execute(
        select(KnowledgeMastery).where(KnowledgeMastery.knowledge_point == body.knowledge_point)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="知识点已存在")
    kp = KnowledgeMastery(knowledge_point=body.knowledge_point, correct_count=0, wrong_count=0, total_count=0, error_rate=0.0)
    db.add(kp)
    await db.flush()
    logger.info("知识点创建: %s", body.knowledge_point)
    return KnowledgePointOut(id=kp.id, knowledge_point=kp.knowledge_point)


@router.get("/mastery", response_model=list[KnowledgeMasteryOut])
async def list_mastery(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(KnowledgeMastery).order_by(KnowledgeMastery.error_rate.desc(), KnowledgeMastery.total_count.desc())
    )
    return result.scalars().all()


@router.get("/mastery/{point}", response_model=KnowledgeMasteryOut)
async def get_mastery(point: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(KnowledgeMastery).where(KnowledgeMastery.knowledge_point == point)
    )
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="知识点不存在")
    return row


@router.get("/weak", response_model=list[KnowledgeMasteryOut])
async def get_weak_points(threshold: float = 0.5, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(KnowledgeMastery)
        .where(KnowledgeMastery.error_rate > threshold)
        .where(KnowledgeMastery.total_count > 0)
        .order_by(KnowledgeMastery.error_rate.desc())
    )
    return result.scalars().all()
