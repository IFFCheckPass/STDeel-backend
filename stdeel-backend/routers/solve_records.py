"""解题记录：上传（批量 / 单条补传）、反馈、删除、拉取。

契约要点（backend_requirements.md §4.3 ~ §4.7）：
- 同一路由按 body 是否含 questions 数组区分两种形态；
- 批量响应必须按 questions 顺序返回等量「数字 id」；
- 单条补传按 (user_id, local_id) 幂等，重复上传不产生新行；
- PATCH / DELETE 幂等；DELETE 记录不存在也返回 2xx；
- GET 必须回传 action_type 与 user_feedback（v0.9.11 回归点）。
"""

import logging
from datetime import timedelta

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import SolveRecord, User
from schemas import (
    DeleteRecordPayload,
    FeedbackPayload,
    SolveRecordCreate,
    SolveRecordList,
    SolveRecordOut,
)
from services import compute_batch_key, recompute_mastery, upsert_solve_record
from utils import (
    FEEDBACK_VALUES,
    as_bool,
    as_clean_text,
    as_float,
    as_int,
    as_text,
    decode_user_id,
    encode_user_id,
    normalize_action,
    normalize_feedback,
    normalize_source,
    normalize_subject,
    parse_client_datetime,
    utcnow,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/solve-records", tags=["solve-records"])


async def _require_user(db: AsyncSession, raw_user_id) -> User:
    user_id = decode_user_id(raw_user_id)
    if user_id is None:
        raise HTTPException(status_code=400, detail="user_id 缺失或格式不正确")
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail=f"用户不存在: {user_id}")
    return user


# --------------------------------------------------------------------------
# 写入
# --------------------------------------------------------------------------
async def _create_batch(db: AsyncSession, user: User, payload: dict, questions: list) -> dict:
    """形态 A：一次上传一组的题目。"""
    now = utcnow()
    context = {
        "ai_model": as_clean_text(payload.get("ai_model")),
        "latency_ms": as_int(payload.get("latency_ms")),
        "tokens_used": as_int(payload.get("tokens_used")),
        "source": normalize_source(payload.get("source")),
        "image_path": as_clean_text(payload.get("image_path")),
    }

    items: list[dict[str, int]] = []
    for index, raw_question in enumerate(questions):
        question = raw_question if isinstance(raw_question, dict) else {}
        question_text = as_text(question.get("content")) or as_text(question.get("question_text"))
        # questions[].id 是客户端本地 SQLite 主键，这里只拿来做去重，不当后端主键
        local_id = as_int(question.get("id"), 0) or None
        question_payload = {
            "local_id": local_id,
            "question_text": question_text,
            "answer": as_text(question.get("answer")),
            "solution": as_text(question.get("solution")),
            "knowledge_points": question.get("knowledge_points"),
            "subject": normalize_subject(question.get("subject")),
            "question_no": as_int(question.get("question_no")),
            "confidence": as_float(question.get("confidence")),
            "action_type": normalize_action(question.get("action_type")),
            "user_feedback": normalize_feedback(question.get("user_feedback")),
            "matched": as_bool(question.get("matched"), False),
            "ai_model": context["ai_model"],
            "latency_ms": context["latency_ms"],
            "tokens_used": context["tokens_used"],
            "source": context["source"],
            "image_path": context["image_path"],
        }
        if local_id is None:
            # 没有本地主键时的兜底去重指纹（内容 + 批次位置）
            question_payload["batch_key"] = compute_batch_key(
                user.id, context["source"], context["image_path"], index, question_text
            )

        question_created = parse_client_datetime(
            question.get("created_at") or payload.get("created_at")
        )
        record, created = await upsert_solve_record(
            db, user.id, question_payload, now, created_at=question_created
        )
        items.append({"id": record.id})
        logger.info(
            "解题记录%s: user=%s record=%s local_id=%s action=%s feedback=%s points=%s",
            "创建" if created else "复用",
            encode_user_id(user.id),
            record.id,
            local_id,
            record.action_type,
            record.user_feedback,
            record.knowledge_points,
        )

    await recompute_mastery(db, user.id)
    return {"items": items}


async def _create_single(db: AsyncSession, user: User, payload: dict) -> dict:
    """形态 B：离线补传的单条扁平结构。"""
    now = utcnow()
    local_id = as_int(payload.get("local_id"), 0) or None
    record_payload = {
        "local_id": local_id,
        "question_text": as_text(payload.get("question_text")) or as_text(payload.get("content")),
        "answer": as_text(payload.get("answer")),
        "solution": as_text(payload.get("solution")),
        "subject": normalize_subject(payload.get("subject")),
        "ai_model": as_clean_text(payload.get("ai_model")),
        "latency_ms": as_int(payload.get("latency_ms")),
        "tokens_used": as_int(payload.get("tokens_used")),
        "matched": as_bool(payload.get("matched"), False),
        "action_type": normalize_action(payload.get("action_type")),
        "user_feedback": normalize_feedback(payload.get("user_feedback")),
        "image_path": as_clean_text(payload.get("image_path")),
        # 契约里形态 B 没列这些字段，但客户端愿意带就收下（组卷要用知识点）
        "knowledge_points": payload.get("knowledge_points"),
        "source": normalize_source(payload.get("source"), "") or None,
        "question_no": as_int(payload.get("question_no")) or None,
    }

    created_at = parse_client_datetime(
        payload.get("created_at") or payload.get("solved_at")
    )
    record, created = await upsert_solve_record(db, user.id, record_payload, now, created_at=created_at)
    await recompute_mastery(db, user.id)
    logger.info(
        "解题记录补传%s: user=%s record=%s local_id=%s action=%s feedback=%s",
        "创建" if created else "复用",
        encode_user_id(user.id),
        record.id,
        local_id,
        record.action_type,
        record.user_feedback,
    )
    return {"id": record.id}


@router.post("", summary="上传解题记录（形态 A 批量 / 形态 B 单条补传）")
async def create_records(body: SolveRecordCreate | None = None, db: AsyncSession = Depends(get_db)):
    payload = body.model_dump() if body is not None else {}
    user = await _require_user(db, payload.get("user_id"))
    questions = payload.get("questions")
    if isinstance(questions, list):
        return await _create_batch(db, user, payload, questions)
    return await _create_single(db, user, payload)


# --------------------------------------------------------------------------
# 读取
# --------------------------------------------------------------------------
@router.get("", response_model=SolveRecordList, summary="拉取解题记录（正确 30 天 / 错误 90 天 取并集）")
async def list_records(
    user_id: str = Query(..., description="用户 id，支持 u_1 或 1"),
    correct_days: int = Query(30, ge=1, le=3660),
    wrong_days: int = Query(90, ge=1, le=3660),
    limit: int | None = Query(None, ge=1, le=10000, description="可选：限制返回条数，避免一次拉太多"),
    db: AsyncSession = Depends(get_db),
) -> SolveRecordList:
    user_id_number = decode_user_id(user_id)
    user = await db.get(User, user_id_number) if user_id_number else None
    if user is None:
        logger.warning("拉取记录：用户不存在 user_id=%s", user_id)
        return SolveRecordList(total=0, items=[])

    now = utcnow()
    # 未标注的记录始终返回，否则换机后「还没点反馈」的历史会凭空消失
    window = or_(
        SolveRecord.user_feedback.is_(None),
        SolveRecord.user_feedback == "none",
        and_(
            SolveRecord.user_feedback.in_(("correct", "partial")),
            SolveRecord.created_at >= now - timedelta(days=correct_days),
        ),
        and_(
            SolveRecord.user_feedback == "wrong",
            SolveRecord.created_at >= now - timedelta(days=wrong_days),
        ),
    )

    base = select(SolveRecord).where(SolveRecord.user_id == user.id, window)
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    stmt = base.order_by(SolveRecord.created_at.desc(), SolveRecord.id.desc())
    if limit is not None:
        stmt = stmt.limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return SolveRecordList(total=total, items=[SolveRecordOut.model_validate(row) for row in rows])


@router.get("/{record_id}", response_model=SolveRecordOut)
async def get_record(record_id: int, db: AsyncSession = Depends(get_db)):
    record = await db.get(SolveRecord, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    return SolveRecordOut.model_validate(record)


# --------------------------------------------------------------------------
# 反馈 / 删除（幂等）
# --------------------------------------------------------------------------
@router.patch("/{record_id}/feedback", summary="更新题目反馈（幂等，不重复计数）")
async def update_feedback(
    record_id: int,
    body: FeedbackPayload | None = None,
    db: AsyncSession = Depends(get_db),
):
    payload = body.model_dump() if body is not None else {}
    raw_feedback = payload.get("feedback")
    if raw_feedback is None:
        raw_feedback = payload.get("user_feedback")
    feedback = as_clean_text(raw_feedback).lower()
    if feedback not in FEEDBACK_VALUES:
        raise HTTPException(
            status_code=400,
            detail=f"feedback 必须是 {'/'.join(FEEDBACK_VALUES)} 之一",
        )

    record = await db.get(SolveRecord, record_id)
    user_id = decode_user_id(payload.get("user_id"))
    if record is None or (user_id is not None and record.user_id != user_id):
        # 记录已被删除 / 不属于该用户：仍返回 2xx，避免客户端墓碑永远清不掉
        logger.warning("反馈更新跳过（记录不存在或不属于该用户）: record=%s user=%s", record_id, user_id)
        return {"ok": True, "missing": True, "id": record_id}

    changed = record.user_feedback != feedback
    if changed:
        record.user_feedback = feedback
        record.updated_at = utcnow()
        await db.flush()
        await recompute_mastery(db, record.user_id)

    logger.info(
        "反馈更新%s: user=%s record=%s feedback=%s action=%s",
        "" if changed else "(未变化)",
        encode_user_id(record.user_id),
        record.id,
        feedback,
        record.action_type,
    )
    return {
        "ok": True,
        "id": record.id,
        "user_feedback": record.user_feedback,
        "action_type": record.action_type,
    }


@router.delete("/{record_id}", summary="删除记录（幂等，记录不存在也返回 2xx）")
async def delete_record(
    record_id: int,
    user_id: str | None = Query(None, description="可选：仅删除属于该用户的记录"),
    body: DeleteRecordPayload | None = Body(None),
    db: AsyncSession = Depends(get_db),
):
    owner_id = decode_user_id(user_id)
    if owner_id is None and body is not None:
        owner_id = decode_user_id(body.model_dump().get("user_id"))

    record = await db.get(SolveRecord, record_id)
    if record is None:
        return {"ok": True, "deleted": False, "id": record_id}
    if owner_id is not None and record.user_id != owner_id:
        logger.warning("删除被拒绝（不属于该用户）: record=%s user=%s", record_id, owner_id)
        return {"ok": True, "deleted": False, "id": record_id}

    owner = record.user_id
    await db.delete(record)
    await db.flush()
    await recompute_mastery(db, owner)
    logger.info("删除记录: user=%s record=%s", encode_user_id(owner), record_id)
    return {"ok": True, "deleted": True, "id": record_id}
