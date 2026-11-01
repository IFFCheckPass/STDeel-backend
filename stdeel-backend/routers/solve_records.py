"""解题记录：上传（批量 / 单条补传）、反馈、删除、拉取。

契约要点（backend_requirements.md §4.3 ~ §4.7）：
- 同一路由按 body 是否含 questions 数组区分两种形态；
- 批量响应必须按 questions 顺序返回等量「数字 id」；
- 单条补传按 (user_id, local_id) 幂等，重复上传不产生新行；
- PATCH / DELETE 幂等；DELETE 记录不存在也返回 2xx；
- GET 必须回传 action_type 与 user_feedback（v0.9.11 回归点）。
"""

import hashlib
import logging
from datetime import timedelta

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import DeletedRecord, SolveRecord, User
from schemas import (
    DeleteRecordPayload,
    FeedbackPayload,
    SolveRecordCreate,
    SolveRecordList,
    SolveRecordOut,
)
from services import compute_batch_key, recompute_mastery, upsert_bulk, upsert_solve_record
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
    iso_utc,
    iso_utc_ms,
    normalize_question_text,
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


BULK_STATUS_VALUES = ("inserted", "updated", "duplicated")


def _bulk_payload(raw: dict, device_id: str) -> dict:
    """把同步 v2 的单条记录整理成 upsert 用的 payload。"""
    if not isinstance(raw, dict):
        raise ValueError("record 必须是对象")

    local_id = as_int(raw.get("local_id"), 0) or None
    client_ref = as_clean_text(raw.get("client_ref"))
    if not client_ref and device_id and local_id:
        client_ref = f"{device_id}:{local_id}"

    question_text = as_text(raw.get("question_text"))
    created_at = parse_client_datetime(raw.get("created_at"))
    content_key = as_clean_text(raw.get("content_key")).lower()
    if not content_key and question_text:
        stamp = int(created_at.timestamp() * 1000) if created_at else int(utcnow().timestamp() * 1000)
        content_key = hashlib.sha256(
            f"{normalize_question_text(question_text)}|{stamp}".encode("utf-8")
        ).hexdigest()

    payload = {
        "client_ref": client_ref or None,
        "content_key": content_key or None,
        "local_id": local_id,
        "question_text": question_text,
        "answer": as_text(raw.get("answer")),
        "solution": as_text(raw.get("solution")),
        "knowledge_points": raw.get("knowledge_points"),
        "subject": normalize_subject(raw.get("subject")),
        "ai_model": as_clean_text(raw.get("ai_model")),
        "latency_ms": as_int(raw.get("latency_ms")),
        "tokens_used": as_int(raw.get("tokens_used")),
        "matched": as_bool(raw.get("matched"), False),
        "action_type": normalize_action(raw.get("action_type")),
        "user_feedback": normalize_feedback(raw.get("user_feedback")),
        "source": normalize_source(raw.get("source")),
        "image_path": as_clean_text(raw.get("image_path")),
    }
    return {"payload": payload, "created_at": created_at}


@router.post("/bulk", summary="同步 v2：批量全量上传，按 client_ref / content_key 去重")
async def bulk_upload(body: dict | None = Body(None), db: AsyncSession = Depends(get_db)):
    payload = body or {}
    user = await _require_user(db, payload.get("user_id"))
    device_id = as_clean_text(payload.get("device_id"))
    records = payload.get("records")
    if not isinstance(records, list):
        raise HTTPException(status_code=400, detail="records 必须是数组")

    now = utcnow()
    items: list[dict] = []
    failed: list[dict] = []

    for raw in records[:500]:
        try:
            prepared = _bulk_payload(raw, device_id)
            # 每条一个 SAVEPOINT：单条失败不污染整批事务
            async with db.begin_nested():
                record, status = await upsert_bulk(
                    db, user.id, prepared["payload"], now, created_at=prepared["created_at"]
                )
            items.append(
                {"client_ref": prepared["payload"].get("client_ref"), "id": record.id, "status": status}
            )
        except Exception as exc:  # noqa: BLE001 - 单条失败要逐条回报
            logger.warning("bulk 单条失败: %s", exc)
            failed.append(
                {
                    "client_ref": (raw or {}).get("client_ref") if isinstance(raw, dict) else None,
                    "reason": f"bad_record: {str(exc)[:160]}",
                }
            )

    if items:
        await recompute_mastery(db, user.id)

    logger.info(
        "bulk 同步: user=%s 请求=%s 成功=%s 失败=%s 状态=%s",
        encode_user_id(user.id),
        len(records),
        len(items),
        len(failed),
        {status: sum(1 for item in items if item["status"] == status) for status in BULK_STATUS_VALUES},
    )
    return {"server_time": iso_utc_ms(now), "items": items, "failed": failed}


# --------------------------------------------------------------------------
# 读取
# --------------------------------------------------------------------------
@router.get("", response_model=SolveRecordList, summary="拉取解题记录（窗口 / 全量 / 增量 / 分页）")
async def list_records(
    user_id: str = Query(..., description="用户 id，支持 u_1 或 1"),
    correct_days: int = Query(30, ge=1, le=3660),
    wrong_days: int = Query(90, ge=1, le=3660),
    all_records: int = Query(0, alias="all", description="1=返回该用户全部记录，忽略 30/90 天窗口"),
    updated_since: str | None = Query(None, description="只返回 updated_at >= 该时间的记录（增量）"),
    limit: int | None = Query(None, ge=1, le=200, description="默认 200；不传新参数时保持旧行为（不限）"),
    cursor: str | None = Query(None, description="上一页返回的 next_cursor"),
    include_deleted: int = Query(0, description="1=附带后端已删除的 id 列表"),
    db: AsyncSession = Depends(get_db),
) -> SolveRecordList:
    now = utcnow()
    user_id_number = decode_user_id(user_id)
    user = await db.get(User, user_id_number) if user_id_number else None
    if user is None:
        logger.warning("拉取记录：用户不存在 user_id=%s", user_id)
        return SolveRecordList(
            total=0, items=[], next_cursor=None, deleted=[], server_time=iso_utc_ms(now)
        )

    since = parse_client_datetime(updated_since)
    base = select(SolveRecord).where(SolveRecord.user_id == user.id)
    if since is not None:
        # 增量模式：按 updated_at，不看创建时间窗口
        base = base.where(SolveRecord.updated_at >= since)
    elif not all_records:
        # 兼容旧行为：未标注的记录始终返回；correct/partial 30 天、wrong 90 天
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
        base = base.where(window)

    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()

    # 用了 all / updated_since / cursor 就走分页（默认 200）；否则保持旧版「一次给完」
    effective_limit = limit
    if effective_limit is None and (all_records or since is not None or cursor):
        effective_limit = 200
    offset = 0
    if cursor:
        try:
            offset = max(0, int(cursor))
        except ValueError:
            raise HTTPException(status_code=400, detail="cursor 非法")

    stmt = base.order_by(SolveRecord.created_at.desc(), SolveRecord.id.desc())
    if effective_limit is not None:
        stmt = stmt.offset(offset).limit(effective_limit)
    rows = (await db.execute(stmt)).scalars().all()

    next_cursor = None
    if effective_limit is not None and len(rows) == effective_limit and offset + len(rows) < total:
        next_cursor = str(offset + len(rows))

    deleted: list[int] = []
    if include_deleted:
        tomb = select(DeletedRecord.record_id).where(DeletedRecord.user_id == user.id)
        if since is not None:
            tomb = tomb.where(DeletedRecord.deleted_at >= since)
        deleted = [
            int(record_id)
            for record_id in (await db.execute(tomb.order_by(DeletedRecord.id.desc()).limit(500))).scalars().all()
        ]

    return SolveRecordList(
        total=total,
        items=[SolveRecordOut.model_validate(row) for row in rows],
        next_cursor=next_cursor,
        deleted=deleted,
        server_time=iso_utc_ms(now),
    )


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
    db.add(DeletedRecord(user_id=owner, record_id=record_id, deleted_at=utcnow()))
    await db.flush()
    await recompute_mastery(db, owner)
    logger.info("删除记录: user=%s record=%s", encode_user_id(owner), record_id)
    return {"ok": True, "deleted": True, "id": record_id}
