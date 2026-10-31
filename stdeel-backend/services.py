"""领域服务。

把「怎么做」从 HTTP 层剥离出来，路由只负责解析/校验/拼响应：
- 解题记录幂等 upsert（local_id 优先，batch_key 兜底）；
- 知识点掌握重算（从 solve_records 聚合，天然幂等）；
- 标准答案匹配（精确哈希 + 关键词探测 + 相似度）。
"""

import hashlib
import logging
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import Select, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from config import BATCH_DEDUPE_WINDOW_SECONDS, MATCH_MAX_CANDIDATES, MATCH_THRESHOLD
from models import AnswerLibrary, KnowledgeMastery, SolveRecord
from utils import (
    DEFAULT_ACTION,
    DEFAULT_FEEDBACK,
    DEFAULT_SOURCE,
    DEFAULT_SUBJECT,
    JUDGED_FEEDBACK,
    dump_json_list,
    normalize_action,
    normalize_feedback,
    normalize_question_text,
    question_hash,
)

logger = logging.getLogger(__name__)

# 新建记录时的默认值（payload 里为 None 的键会用这里的值）
DEFAULT_VALUES: dict[str, Any] = {
    "local_id": None,
    "batch_key": None,
    "question_text": "",
    "answer": "",
    "solution": "",
    "knowledge_points": "[]",
    "subject": DEFAULT_SUBJECT,
    "ai_model": "",
    "latency_ms": 0,
    "tokens_used": 0,
    "matched": False,
    "action_type": DEFAULT_ACTION,
    "user_feedback": DEFAULT_FEEDBACK,
    "source": DEFAULT_SOURCE,
    "confidence": None,
    "question_no": 0,
    "image_path": "",
}


# ==========================================================================
# 解题记录
# ==========================================================================
def merge_feedback(old: Any, new: Any) -> str:
    """合并用户判定：已是「正确/部分错误/错误」时，不被迟到的 none 冲掉。

    这是 v0.9.11 的回归点（状态被清空），后端必须自己兜住。
    """
    old_value = normalize_feedback(old)
    new_value = normalize_feedback(new)
    if old_value in JUDGED_FEEDBACK and new_value == DEFAULT_FEEDBACK:
        return old_value
    return new_value


def merge_action(old: Any, new: Any) -> str:
    """合并题目动作：迟到的 solve 不覆盖 retry / detail / 已判定的状态。

    客户端重传时可能带着「上传那一刻」的旧 action_type（solve），
    若直接覆盖会把「疑问 / 重答 / 已判定」的语义冲掉。
    """
    old_value = normalize_action(old)
    new_value = normalize_action(new)
    if new_value == DEFAULT_ACTION and old_value != DEFAULT_ACTION:
        return old_value
    return new_value


def compute_batch_key(user_id: int, source: str, image_path: str, index: int, question_text: str) -> str:
    """批量上传缺 local_id 时的去重指纹：同一批 + 同一位置 + 同一题干。"""
    raw = f"{user_id}|{source}|{image_path}|{index}|{question_hash(question_text)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _new_record(user_id: int, payload: dict[str, Any], now: datetime) -> SolveRecord:
    values: dict[str, Any] = {}
    for key, default in DEFAULT_VALUES.items():
        value = payload.get(key)
        if key == "knowledge_points":
            values[key] = dump_json_list(value) if value is not None else default
        else:
            values[key] = default if value is None else value
    record = SolveRecord(user_id=user_id, created_at=now, updated_at=now, **values)
    return record


def _merge_into(record: SolveRecord, payload: dict[str, Any]) -> None:
    """把非空字段合并进已有记录（重传不得丢状态、不得清空内容）。"""
    for key, value in payload.items():
        if value is None:
            continue
        if key == "knowledge_points":
            if value:
                record.knowledge_points = dump_json_list(value)
        elif key in ("answer", "solution", "ai_model", "image_path"):
            if value:
                setattr(record, key, value)
        elif key == "question_text":
            if value:
                record.question_text = value
        elif key == "subject":
            # 客户端单条补传没有 subject，别把已分类的题目降级成「未分类」
            if value and not (
                value == DEFAULT_SUBJECT and (record.subject or DEFAULT_SUBJECT) != DEFAULT_SUBJECT
            ):
                record.subject = value
        elif key in ("latency_ms", "tokens_used", "question_no"):
            if value:
                setattr(record, key, value)
        elif key == "confidence":
            record.confidence = value
        elif key == "matched":
            record.matched = bool(record.matched or value)
        elif key == "user_feedback":
            record.user_feedback = merge_feedback(record.user_feedback, value)
        elif key == "action_type":
            record.action_type = merge_action(record.action_type, value)
        elif key == "source":
            record.source = value
        elif key == "local_id":
            record.local_id = value
        elif key == "batch_key":
            record.batch_key = value


async def _find_by_local_id(db: AsyncSession, user_id: int, local_id: int) -> SolveRecord | None:
    result = await db.execute(
        select(SolveRecord)
        .where(SolveRecord.user_id == user_id, SolveRecord.local_id == local_id)
        .order_by(SolveRecord.id.desc())
        .limit(1)
    )
    return result.scalars().first()


async def _find_by_batch_key(
    db: AsyncSession, user_id: int, batch_key: str, since: datetime
) -> SolveRecord | None:
    result = await db.execute(
        select(SolveRecord)
        .where(
            SolveRecord.user_id == user_id,
            SolveRecord.batch_key == batch_key,
            SolveRecord.created_at >= since,
        )
        .order_by(SolveRecord.id.desc())
        .limit(1)
    )
    return result.scalars().first()


async def upsert_solve_record(
    db: AsyncSession,
    user_id: int,
    payload: dict[str, Any],
    now: datetime | None = None,
) -> tuple[SolveRecord, bool]:
    """按 (user_id, local_id) 幂等写入；没有 local_id 时用 batch_key + 时间窗口兜底。

    返回 (记录, 是否新建)。
    """
    now = now or datetime.utcnow()
    local_id = payload.get("local_id")
    batch_key = payload.get("batch_key")

    record: SolveRecord | None = None
    if local_id:
        record = await _find_by_local_id(db, user_id, int(local_id))
    if record is None and batch_key:
        record = await _find_by_batch_key(
            db, user_id, batch_key, now - timedelta(seconds=BATCH_DEDUPE_WINDOW_SECONDS)
        )

    if record is None:
        record = _new_record(user_id, payload, now)
        db.add(record)
        await db.flush()
        return record, True

    _merge_into(record, payload)
    record.updated_at = now
    await db.flush()
    return record, False


# ==========================================================================
# 知识点掌握（派生聚合，重算保证幂等）
# ==========================================================================
MASTERY_AGGREGATE_SQL = text(
    """
    SELECT
        kp.value AS point,
        COALESCE(NULLIF(TRIM(r.subject), ''), :default_subject) AS subject,
        SUM(CASE WHEN r.user_feedback IN ('correct', 'partial') THEN 1 ELSE 0 END) AS correct_cnt,
        SUM(CASE WHEN r.user_feedback IN ('wrong', 'partial') THEN 1 ELSE 0 END) AS wrong_cnt
    FROM solve_records AS r
    JOIN json_each(
            CASE WHEN json_valid(r.knowledge_points) THEN r.knowledge_points ELSE '[]' END
         ) AS kp
    WHERE r.user_id = :user_id
    GROUP BY kp.value, subject
    """
)


async def recompute_mastery(db: AsyncSession, user_id: int) -> int:
    """按 solve_records 重新聚合该用户的知识点掌握情况。

    口径（需求 §3.3）：correct -> correct+1；partial -> correct+1 且 wrong+1；
    wrong -> wrong+1。因为每次全量重算受影响用户，重复 PATCH 不会重复计数。
    """
    if not user_id:
        return 0

    rows = (
        await db.execute(
            MASTERY_AGGREGATE_SQL,
            {"user_id": user_id, "default_subject": DEFAULT_SUBJECT},
        )
    ).all()

    aggregate: dict[str, dict[str, Any]] = {}
    for point, subject, correct_cnt, wrong_cnt in rows:
        name = str(point).strip() if point is not None else ""
        if not name:
            continue
        entry = aggregate.setdefault(name, {"correct": 0, "wrong": 0, "subjects": {}})
        correct = int(correct_cnt or 0)
        wrong = int(wrong_cnt or 0)
        entry["correct"] += correct
        entry["wrong"] += wrong
        subject_name = (subject or DEFAULT_SUBJECT).strip() or DEFAULT_SUBJECT
        bucket = entry["subjects"].setdefault(subject_name, [0, 0])
        bucket[0] += correct
        bucket[1] += wrong

    existing_rows = (
        await db.execute(select(KnowledgeMastery).where(KnowledgeMastery.user_id == user_id))
    ).scalars().all()
    existing = {row.knowledge_point: row for row in existing_rows}

    for name, entry in aggregate.items():
        correct = entry["correct"]
        wrong = entry["wrong"]
        total = correct + wrong
        # 一个知识点若跨科目出现，取出现次数最多的科目
        subject_name = max(entry["subjects"].items(), key=lambda kv: sum(kv[1]))[0]
        row = existing.pop(name, None)
        if row is None:
            row = KnowledgeMastery(user_id=user_id, knowledge_point=name)
            db.add(row)
        row.subject = subject_name
        row.correct_count = correct
        row.wrong_count = wrong
        row.total_count = total
        row.error_rate = (wrong / total) if total else 0.0
        row.updated_at = datetime.utcnow()

    for stale in existing.values():
        await db.delete(stale)

    await db.flush()
    return len(aggregate)


async def recompute_all_mastery(db: AsyncSession) -> int:
    """全量重算（迁移升级后用一次，保证历史聚合与明细一致）。"""
    user_ids = (await db.execute(text("SELECT DISTINCT user_id FROM solve_records WHERE user_id IS NOT NULL"))).all()
    count = 0
    for (user_id,) in user_ids:
        await recompute_mastery(db, int(user_id))
        count += 1
    return count


# ==========================================================================
# 标准答案匹配
# ==========================================================================
def _apply_library_scope(stmt: Select, user_id: int | None) -> Select:
    """用户自己的题库 + user_id 为空的历史共享数据。"""
    if user_id:
        return stmt.where(
            or_(AnswerLibrary.user_id == user_id, AnswerLibrary.user_id.is_(None))
        )
    return stmt.where(AnswerLibrary.user_id.is_(None))


async def _collect_candidates(
    db: AsyncSession, user_id: int | None, normalized_text: str
) -> list[AnswerLibrary]:
    """先用 LIKE 探测缩小范围，再用相似度精算，避免全表遍历。"""
    probes: set[str] = set()
    length = len(normalized_text)
    for size in (12, 8):
        if length >= size:
            probes.add(normalized_text[:size])
            probes.add(normalized_text[-size:])
            if length > size * 2:
                middle = (length - size) // 2
                probes.add(normalized_text[middle : middle + size])

    candidates: list[AnswerLibrary] = []
    seen: set[int] = set()

    if probes:
        conditions = [AnswerLibrary.question_text.contains(probe) for probe in probes]
        stmt = _apply_library_scope(select(AnswerLibrary).where(or_(*conditions)), user_id).limit(80)
        for row in (await db.execute(stmt)).scalars().all():
            if row.id not in seen:
                seen.add(row.id)
                candidates.append(row)

    remain = MATCH_MAX_CANDIDATES - len(candidates)
    if remain > 0:
        stmt = (
            _apply_library_scope(select(AnswerLibrary), user_id)
            .order_by(AnswerLibrary.updated_at.desc(), AnswerLibrary.id.desc())
            .limit(remain)
        )
        for row in (await db.execute(stmt)).scalars().all():
            if row.id not in seen:
                seen.add(row.id)
                candidates.append(row)

    return candidates


async def match_answer_library(
    db: AsyncSession,
    user_id: int | None,
    question_text_value: Any,
    provided_hash: Any = None,
    threshold: float = MATCH_THRESHOLD,
) -> dict[str, Any]:
    """返回 {hit, similarity, answer}；answer 为 ORM 对象或 None。"""
    normalized = normalize_question_text(question_text_value)
    target_hash = str(provided_hash or "").strip().lower()
    if not target_hash and normalized:
        target_hash = question_hash(normalized)

    if target_hash:
        stmt = _apply_library_scope(
            select(AnswerLibrary).where(AnswerLibrary.question_hash == target_hash), user_id
        ).order_by(AnswerLibrary.id.desc())
        exact = (await db.execute(stmt.limit(1))).scalar_one_or_none()
        if exact is not None:
            return {"hit": True, "similarity": 1.0, "answer": exact}

    if not normalized:
        return {"hit": False, "similarity": 0.0, "answer": None}

    best: AnswerLibrary | None = None
    best_similarity = 0.0
    for candidate in await _collect_candidates(db, user_id, normalized):
        similarity = SequenceMatcher(
            None, normalized, normalize_question_text(candidate.question_text)
        ).ratio()
        if similarity > best_similarity:
            best_similarity = similarity
            best = candidate

    if best is not None and best_similarity >= threshold:
        return {"hit": True, "similarity": round(best_similarity, 4), "answer": best}
    return {"hit": False, "similarity": round(best_similarity, 4), "answer": None}
