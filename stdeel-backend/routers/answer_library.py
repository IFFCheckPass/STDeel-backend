import hashlib
import logging
from difflib import SequenceMatcher

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, text, func
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import AnswerLibrary
from schemas import (
    AnswerLibraryCreate,
    AnswerLibraryUpdate,
    AnswerLibraryOut,
    AnswerLibraryList,
    MatchRequest,
    MatchResult,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/answer-library", tags=["answer-library"])


def _calc_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _calc_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


@router.post("", response_model=AnswerLibraryOut)
async def create_answer(body: AnswerLibraryCreate, db: AsyncSession = Depends(get_db)):
    record = AnswerLibrary(
        question_text=body.question_text,
        question_hash=_calc_hash(body.question_text),
        answer=body.answer,
        solution=body.solution,
        knowledge_points=body.knowledge_points,
        source=body.source,
    )
    db.add(record)
    await db.flush()
    logger.info("标准答案创建: id=%s", record.id)
    return record


@router.get("", response_model=AnswerLibraryList)
async def list_answers(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    total = await db.execute(select(func.count()).select_from(AnswerLibrary))
    items = await db.execute(
        select(AnswerLibrary)
        .order_by(AnswerLibrary.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return AnswerLibraryList(items=items.scalars().all(), total=total.scalar_one(), page=page, page_size=page_size)


@router.get("/{record_id}", response_model=AnswerLibraryOut)
async def get_answer(record_id: int, db: AsyncSession = Depends(get_db)):
    record = await db.get(AnswerLibrary, record_id)
    if not record:
        raise HTTPException(status_code=404, detail="记录不存在")
    return record


@router.put("/{record_id}", response_model=AnswerLibraryOut)
async def update_answer(record_id: int, body: AnswerLibraryUpdate, db: AsyncSession = Depends(get_db)):
    record = await db.get(AnswerLibrary, record_id)
    if not record:
        raise HTTPException(status_code=404, detail="记录不存在")
    data = body.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(record, k, v)
    if "question_text" in data:
        record.question_hash = _calc_hash(data["question_text"])
    record.updated_at = func.now()
    await db.flush()
    logger.info("标准答案更新: id=%s", record_id)
    return record


@router.delete("/{record_id}")
async def delete_answer(record_id: int, db: AsyncSession = Depends(get_db)):
    record = await db.get(AnswerLibrary, record_id)
    if not record:
        raise HTTPException(status_code=404, detail="记录不存在")
    await db.delete(record)
    await db.flush()
    logger.info("标准答案删除: id=%s", record_id)
    return {"detail": "删除成功"}


@router.post("/match", response_model=MatchResult)
async def match_answer(body: MatchRequest, db: AsyncSession = Depends(get_db)):
    target_hash = _calc_hash(body.question_text)

    exact_stmt = select(AnswerLibrary).where(AnswerLibrary.question_hash == target_hash)
    exact_result = await db.execute(exact_stmt)
    exact_match = exact_result.scalar_one_or_none()
    if exact_match:
        logger.info("精确匹配命中: id=%s", exact_match.id)
        return MatchResult(matched=True, similarity=1.0, answer=exact_match)

    candidates = []

    try:
        fts_query = body.question_text.strip()
        fts_query = " ".join(fts_query.split())
        fts_sql = text(
            "SELECT al.id FROM answer_library al "
            "JOIN answer_library_fts f ON f.rowid = al.id "
            "WHERE answer_library_fts MATCH :q "
            "ORDER BY rank LIMIT 10"
        ).bindparams(q=fts_query)
        fts_result = await db.execute(fts_sql)
        fts_ids = [row[0] for row in fts_result.fetchall()]
        if fts_ids:
            objs = await db.execute(select(AnswerLibrary).where(AnswerLibrary.id.in_(fts_ids)))
            candidates.extend(objs.scalars().all())
    except Exception as exc:
        logger.warning("FTS5 查询失败: %s", exc)

    if not candidates:
        keyword_stmt = select(AnswerLibrary).limit(100)
        kw_result = await db.execute(keyword_stmt)
        all_records = kw_result.scalars().all()
        target = body.question_text
        target_chars = [c for c in target if len(c.strip()) > 0]
        if target_chars:
            keyword = "".join(target_chars[:3]) if len(target_chars) >= 3 else target
            like_stmt = select(AnswerLibrary).where(AnswerLibrary.question_text.contains(keyword)).limit(10)
            like_result = await db.execute(like_stmt)
            candidates = like_result.scalars().all()
            if not candidates:
                candidates = all_records

    if not candidates:
        return MatchResult(matched=False, similarity=0.0, answer=None)

    seen_ids = set()
    uniq_candidates = []
    for c in candidates:
        if c.id not in seen_ids:
            seen_ids.add(c.id)
            uniq_candidates.append(c)
    candidates = uniq_candidates

    best_record = None
    best_sim = 0.0
    target = body.question_text
    for rec in candidates:
        sim = _calc_similarity(target, rec.question_text)
        if sim > best_sim:
            best_sim = sim
            best_record = rec

    if best_record and best_sim >= body.threshold:
        return MatchResult(matched=True, similarity=round(best_sim, 4), answer=best_record)
    return MatchResult(matched=False, similarity=round(best_sim, 4), answer=None)
