"""标准答案库：上传（按题干 hash upsert）与匹配。

契约陷阱（需求 §4.9）：
- 客户端判定条件是 hit == true 且 similarity >= 0.85；
- match 响应里的 answer.knowledge_points 必须是 **JSON 字符串**，
  而 GET /solve-records 里同名字段是 **数组**，两者不能混。
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import AnswerLibrary
from schemas import (
    AnswerLibraryCreate,
    AnswerLibraryList,
    AnswerLibraryMatch,
    AnswerLibraryOut,
    AnswerLibraryUpdate,
    MatchAnswer,
    MatchResult,
)
from services import match_answer_library
from utils import (
    DEFAULT_SUBJECT,
    as_clean_text,
    as_text,
    decode_user_id,
    encode_user_id,
    load_json_list,
    normalize_question_text,
    question_hash,
    utcnow,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/answer-library", tags=["answer-library"])


def _dump_points(raw) -> str:
    return json.dumps(load_json_list(raw), ensure_ascii=False)


@router.post("", response_model=AnswerLibraryOut, summary="上传标准答案（按 user + 题干 hash 幂等 upsert）")
async def upsert_answer(body: AnswerLibraryCreate | None = None, db: AsyncSession = Depends(get_db)):
    payload = body.model_dump() if body is not None else {}
    question_text = as_text(payload.get("question_text"))
    if not normalize_question_text(question_text):
        raise HTTPException(status_code=400, detail="question_text 不能为空")

    user_id = decode_user_id(payload.get("user_id"))
    # 统一用后端自己的规范化哈希，避免客户端算法差异导致匹配不上
    digest = question_hash(question_text)
    answer = as_text(payload.get("answer"))
    solution = as_text(payload.get("solution"))
    subject = as_clean_text(payload.get("subject"), DEFAULT_SUBJECT)
    points = _dump_points(payload.get("knowledge_points"))

    stmt = select(AnswerLibrary).where(AnswerLibrary.question_hash == digest)
    if user_id:
        stmt = stmt.where(AnswerLibrary.user_id == user_id)
    else:
        stmt = stmt.where(AnswerLibrary.user_id.is_(None))
    record = (await db.execute(stmt.order_by(AnswerLibrary.id.desc()).limit(1))).scalar_one_or_none()

    created = record is None
    if record is None:
        record = AnswerLibrary(
            user_id=user_id,
            question_text=question_text,
            question_hash=digest,
            answer=answer,
            solution=solution,
            knowledge_points=points,
            subject=subject,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        db.add(record)
    else:
        # 只覆盖有内容的字段，避免一次不完整的重传把答案清空
        record.question_text = question_text
        if answer:
            record.answer = answer
        if solution:
            record.solution = solution
        if points != "[]":
            record.knowledge_points = points
        if subject:
            record.subject = subject
        record.updated_at = utcnow()

    await db.flush()
    logger.info(
        "标准答案%s: user=%s id=%s hash=%s",
        "新增" if created else "更新",
        encode_user_id(user_id),
        record.id,
        digest[:12],
    )
    return AnswerLibraryOut.model_validate(record)


@router.post("/match", response_model=MatchResult, summary="按题干匹配标准答案")
async def match_answer(body: AnswerLibraryMatch | None = None, db: AsyncSession = Depends(get_db)):
    payload = body.model_dump() if body is not None else {}
    user_id = decode_user_id(payload.get("user_id"))
    result = await match_answer_library(
        db,
        user_id,
        payload.get("question_text"),
        payload.get("question_hash"),
    )
    record: AnswerLibrary | None = result["answer"]
    answer = None
    if record is not None:
        answer = MatchAnswer(
            id=record.id,
            answer=record.answer or "",
            solution=record.solution or "",
            subject=record.subject or DEFAULT_SUBJECT,
            # 注意：这里必须是 JSON 字符串，客户端会 jsonDecode
            knowledge_points=json.dumps(load_json_list(record.knowledge_points), ensure_ascii=False),
        )
    logger.info(
        "标准答案匹配: user=%s hit=%s similarity=%s answer_id=%s",
        encode_user_id(user_id),
        result["hit"],
        result["similarity"],
        record.id if record else None,
    )
    return MatchResult(
        hit=result["hit"],
        similarity=result["similarity"],
        answer=answer,
        matched=result["hit"],
    )


# --------------------------------------------------------------------------
# 管理端接口
# --------------------------------------------------------------------------
@router.get("", response_model=AnswerLibraryList)
async def list_answers(
    user_id: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(AnswerLibrary)
    user_id_number = decode_user_id(user_id)
    if user_id_number:
        stmt = stmt.where(AnswerLibrary.user_id == user_id_number)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(AnswerLibrary.updated_at.desc(), AnswerLibrary.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return AnswerLibraryList(
        items=[AnswerLibraryOut.model_validate(row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{record_id}", response_model=AnswerLibraryOut)
async def get_answer(record_id: int, db: AsyncSession = Depends(get_db)):
    record = await db.get(AnswerLibrary, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    return AnswerLibraryOut.model_validate(record)


@router.put("/{record_id}", response_model=AnswerLibraryOut)
async def update_answer(
    record_id: int,
    body: AnswerLibraryUpdate | None = None,
    db: AsyncSession = Depends(get_db),
):
    record = await db.get(AnswerLibrary, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="记录不存在")

    payload = body.model_dump() if body is not None else {}
    question_text = as_text(payload.get("question_text"))
    if payload.get("question_text") is not None:
        if not normalize_question_text(question_text):
            raise HTTPException(status_code=400, detail="question_text 不能为空")
        record.question_text = question_text
        record.question_hash = question_hash(question_text)
    if payload.get("answer") is not None:
        record.answer = as_text(payload.get("answer"))
    if payload.get("solution") is not None:
        record.solution = as_text(payload.get("solution"))
    if payload.get("knowledge_points") is not None:
        record.knowledge_points = _dump_points(payload.get("knowledge_points"))
    if payload.get("subject") is not None:
        record.subject = as_clean_text(payload.get("subject"), DEFAULT_SUBJECT)
    record.updated_at = utcnow()
    await db.flush()
    return AnswerLibraryOut.model_validate(record)


@router.delete("/{record_id}")
async def delete_answer(record_id: int, db: AsyncSession = Depends(get_db)):
    record = await db.get(AnswerLibrary, record_id)
    if record is None:
        return {"ok": True, "deleted": False, "id": record_id}
    await db.delete(record)
    await db.flush()
    logger.info("标准答案删除: id=%s", record_id)
    return {"ok": True, "deleted": True, "id": record_id}
