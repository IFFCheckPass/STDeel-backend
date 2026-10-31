"""知识点：掌握度 / 薄弱知识点。

知识掌握是派生数据，写入侧（解题记录）会调用 services.recompute_mastery 重算，
这里只负责查询与聚合。
"""

import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import KnowledgeMastery
from schemas import KnowledgeMasteryItem, KnowledgeMasteryList, KnowledgePointOut
from utils import decode_user_id

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/knowledge", tags=["knowledge"])


def _aggregate_globally(rows) -> list[KnowledgeMasteryItem]:
    """不传 user_id 时按知识点做全局汇总（管理台视角）。"""
    bucket: dict[str, list[int]] = {}
    for row in rows:
        if row.user_id is None:
            continue
        stat = bucket.setdefault(row.knowledge_point, [0, 0])
        stat[0] += int(row.correct_count or 0)
        stat[1] += int(row.wrong_count or 0)

    items: list[KnowledgeMasteryItem] = []
    for point, (correct, wrong) in bucket.items():
        total = correct + wrong
        items.append(
            KnowledgeMasteryItem(
                knowledge_point=point,
                correct_count=correct,
                wrong_count=wrong,
                total_count=total,
                error_rate=(wrong / total) if total else 0.0,
            )
        )
    items.sort(key=lambda item: (item.error_rate, item.total_count), reverse=True)
    return items


@router.get("/mastery", response_model=KnowledgeMasteryList, summary="知识点掌握情况")
async def list_mastery(
    user_id: str | None = Query(None, description="不传则返回全局聚合"),
    db: AsyncSession = Depends(get_db),
):
    user_id_number = decode_user_id(user_id)
    if user_id_number:
        rows = (
            await db.execute(
                select(KnowledgeMastery)
                .where(KnowledgeMastery.user_id == user_id_number)
                .order_by(KnowledgeMastery.error_rate.desc(), KnowledgeMastery.total_count.desc())
            )
        ).scalars().all()
        items = [KnowledgeMasteryItem.model_validate(row) for row in rows]
    else:
        rows = (await db.execute(select(KnowledgeMastery))).scalars().all()
        items = _aggregate_globally(rows)
    return KnowledgeMasteryList(items=items, total=len(items))


@router.get("/weak", response_model=KnowledgeMasteryList, summary="薄弱知识点（错误率 >= threshold）")
async def list_weak_points(
    user_id: str | None = Query(None, description="不传则返回全局聚合"),
    threshold: float = Query(0.5, ge=0.0, le=1.0),
    db: AsyncSession = Depends(get_db),
):
    user_id_number = decode_user_id(user_id)
    if user_id_number:
        rows = (
            await db.execute(
                select(KnowledgeMastery)
                .where(
                    KnowledgeMastery.user_id == user_id_number,
                    KnowledgeMastery.total_count > 0,
                    KnowledgeMastery.error_rate >= threshold,
                )
                .order_by(KnowledgeMastery.error_rate.desc())
            )
        ).scalars().all()
        items = [KnowledgeMasteryItem.model_validate(row) for row in rows]
    else:
        rows = (await db.execute(select(KnowledgeMastery))).scalars().all()
        items = [item for item in _aggregate_globally(rows) if item.total_count > 0 and item.error_rate >= threshold]
    return KnowledgeMasteryList(items=items, total=len(items))


@router.get("/points", response_model=list[KnowledgePointOut])
async def list_points(db: AsyncSession = Depends(get_db)):
    """所有出现过的知识点（distinct）。"""
    rows = (
        await db.execute(
            select(KnowledgeMastery.knowledge_point)
            .distinct()
            .order_by(KnowledgeMastery.knowledge_point)
        )
    ).all()
    return [
        KnowledgePointOut(id=index, knowledge_point=row[0])
        for index, row in enumerate(rows, start=1)
    ]


@router.get("/summary", summary="知识点总览（管理台用）")
async def knowledge_summary(db: AsyncSession = Depends(get_db)):
    total_points = (
        await db.execute(select(func.count(func.distinct(KnowledgeMastery.knowledge_point))))
    ).scalar_one()
    total_rows = (await db.execute(select(func.count()).select_from(KnowledgeMastery))).scalar_one()
    return {"total_points": total_points, "total_mastery_rows": total_rows}
