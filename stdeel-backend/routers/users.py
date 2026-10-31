"""账号相关路由：注册、AI Key 同步、管理端用户列表。"""

import json
import logging
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import KnowledgeMastery, SolveRecord, User
from schemas import (
    ApiKeyOut,
    ApiKeyPayload,
    KnowledgeMasteryItem,
    KnowledgeMasteryList,
    SolveRecordOut,
    UserList,
    UserOut,
    UserRegister,
    UserStats,
)
from utils import as_bool, as_clean_text, decode_user_id, encode_user_id, iso_utc, utcnow

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/users", tags=["users"])


# --------------------------------------------------------------------------
# 内部工具
# --------------------------------------------------------------------------
def _public_user(user: User) -> dict:
    public_id = encode_user_id(user.id)
    return {
        "user_id": public_id,
        "id": public_id,
        "username": user.username,
        "device_id": user.device_id,
        "platform": user.platform or "android",
        "created_at": iso_utc(user.created_at),
        "last_active_at": iso_utc(user.last_active_at),
    }


async def _find_by_device(db: AsyncSession, device_id: str) -> User | None:
    result = await db.execute(select(User).where(User.device_id == device_id))
    return result.scalars().first()


async def _find_by_username(db: AsyncSession, username: str) -> User | None:
    result = await db.execute(select(User).where(User.username == username))
    return result.scalars().first()


async def _require_user(db: AsyncSession, raw_user_id, *, touch: bool = False) -> User:
    user_id = decode_user_id(raw_user_id)
    if user_id is None:
        raise HTTPException(status_code=400, detail="user_id 缺失或格式不正确")
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail=f"用户不存在: {user_id}")
    if touch:
        user.last_active_at = utcnow()
    return user


def _load_api_keys(user: User) -> list[dict]:
    try:
        data = json.loads(user.api_keys or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [
        item
        for item in data
        if isinstance(item, dict) and str(item.get("api_key") or "").strip()
    ]


def _normalize_api_keys(raw) -> list[dict]:
    """把 [{api_key,name,enabled}] / ["sk-xxx"] / JSON 字符串统一成规范列表。"""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []

    items: list[dict] = []
    for entry in raw:
        if isinstance(entry, str):
            key = entry.strip()
            if key:
                items.append({"api_key": key, "name": "", "enabled": True})
            continue
        if not isinstance(entry, dict):
            continue
        key = as_clean_text(entry.get("api_key") or entry.get("key"))
        if not key:
            continue
        items.append(
            {
                "api_key": key,
                "name": as_clean_text(entry.get("name")),
                "enabled": as_bool(entry.get("enabled"), True),
            }
        )
    return items


# --------------------------------------------------------------------------
# 客户端接口
# --------------------------------------------------------------------------
@router.post("/register", summary="注册或复用用户（设备注册 / 用户名绑定）")
async def register(body: UserRegister | None = None, db: AsyncSession = Depends(get_db)):
    payload = body.model_dump() if body is not None else {}
    device_id = as_clean_text(payload.get("device_id"))
    username = as_clean_text(payload.get("username"))
    platform = as_clean_text(payload.get("platform"), "android")

    if not device_id and not username:
        raise HTTPException(status_code=400, detail="device_id 与 username 至少提供一个")

    user: User | None = None
    created = False
    if device_id:
        user = await _find_by_device(db, device_id)
    if user is None and username:
        user = await _find_by_username(db, username)

    if user is None:
        user = User(device_id=device_id or None, username=username or None, platform=platform)
        db.add(user)
        try:
            await db.flush()
            created = True
        except IntegrityError:
            # 并发注册同一 device_id/username：回滚后复用已存在的账号
            await db.rollback()
            user = None
            if device_id:
                user = await _find_by_device(db, device_id)
            if user is None and username:
                user = await _find_by_username(db, username)
            if user is None:
                logger.exception("注册冲突且无法复用: device=%s username=%s", device_id, username)
                raise HTTPException(status_code=409, detail="注册冲突，请重试")

    # 补绑缺失的身份字段（不抢占别人的用户名/设备号）
    if device_id and not user.device_id:
        conflict = await _find_by_device(db, device_id)
        if conflict is None:
            user.device_id = device_id
    if username and not user.username:
        conflict = await _find_by_username(db, username)
        if conflict is None:
            user.username = username
    if platform:
        user.platform = platform
    user.last_active_at = utcnow()
    await db.flush()

    logger.info(
        "用户注册/复用: user=%s created=%s device=%s username=%s",
        encode_user_id(user.id),
        created,
        user.device_id,
        user.username,
    )
    return _public_user(user)


@router.put("/api-key", summary="全量覆盖保存该用户的 AI API Key")
async def put_api_keys(body: ApiKeyPayload | None = None, db: AsyncSession = Depends(get_db)):
    payload = body.model_dump() if body is not None else {}
    user = await _require_user(db, payload.get("user_id"), touch=True)
    items = _normalize_api_keys(payload.get("api_keys"))
    user.api_keys = json.dumps(items, ensure_ascii=False)
    await db.flush()
    # 注意：日志只记数量，不记 Key 内容
    logger.info("api-key 全量覆盖: user=%s count=%s", encode_user_id(user.id), len(items))
    return {"ok": True, "user_id": encode_user_id(user.id), "count": len(items)}


@router.get("/api-key", response_model=list[ApiKeyOut], summary="拉取该用户的 AI API Key（换机同步/管理台）")
async def get_api_keys(user_id: str = Query(..., description="用户 id，支持 u_1 或 1"), db: AsyncSession = Depends(get_db)):
    user = await _require_user(db, user_id)
    public_id = encode_user_id(user.id)
    return [
        ApiKeyOut(
            id=index,
            user_id=public_id,
            api_key=item["api_key"],
            name=str(item.get("name") or ""),
            enabled=bool(item.get("enabled", True)),
        )
        for index, item in enumerate(_load_api_keys(user), start=1)
    ]


# --------------------------------------------------------------------------
# 管理端接口（webui 使用）
# --------------------------------------------------------------------------
@router.get("/stats", response_model=UserStats)
async def user_stats(db: AsyncSession = Depends(get_db)):
    now = utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    total = (await db.execute(select(func.count()).select_from(User))).scalar_one()
    today_new = (
        await db.execute(select(func.count()).select_from(User).where(User.created_at >= today_start))
    ).scalar_one()
    active_7d = (
        await db.execute(
            select(func.count()).select_from(User).where(User.last_active_at >= now - timedelta(days=7))
        )
    ).scalar_one()
    active_30d = (
        await db.execute(
            select(func.count()).select_from(User).where(User.last_active_at >= now - timedelta(days=30))
        )
    ).scalar_one()
    return UserStats(total_users=total, today_new=today_new, active_7d=active_7d, active_30d=active_30d)


@router.get("", response_model=UserList)
async def list_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=1000),
    search: str = Query(""),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(User)
    if search:
        like = f"%{search}%"
        stmt = stmt.where(or_(User.username.ilike(like), User.device_id.ilike(like)))
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(stmt.order_by(User.id.desc()).offset((page - 1) * page_size).limit(page_size))
    ).scalars().all()
    return UserList(
        items=[UserOut.model_validate(row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{user_id}", response_model=UserOut)
async def get_user(user_id: str, db: AsyncSession = Depends(get_db)):
    user = await _require_user(db, user_id)
    return UserOut.model_validate(user)


@router.get("/{user_id}/records", response_model=list[SolveRecordOut])
async def get_user_records(
    user_id: str,
    limit: int = Query(200, ge=1, le=2000),
    db: AsyncSession = Depends(get_db),
):
    user = await _require_user(db, user_id)
    rows = (
        await db.execute(
            select(SolveRecord)
            .where(SolveRecord.user_id == user.id)
            .order_by(SolveRecord.created_at.desc(), SolveRecord.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [SolveRecordOut.model_validate(row) for row in rows]


@router.get("/{user_id}/mastery", response_model=KnowledgeMasteryList)
async def get_user_mastery(user_id: str, db: AsyncSession = Depends(get_db)):
    user = await _require_user(db, user_id)
    rows = (
        await db.execute(
            select(KnowledgeMastery)
            .where(KnowledgeMastery.user_id == user.id)
            .order_by(KnowledgeMastery.error_rate.desc(), KnowledgeMastery.total_count.desc())
        )
    ).scalars().all()
    items = [KnowledgeMasteryItem.model_validate(row) for row in rows]
    return KnowledgeMasteryList(items=items, total=len(items))


@router.delete("/{user_id}", summary="删除用户并级联清理其数据（不可恢复）")
async def delete_user(user_id: str, db: AsyncSession = Depends(get_db)):
    user = await _require_user(db, user_id)
    public_id = encode_user_id(user.id)
    await db.execute(delete(SolveRecord).where(SolveRecord.user_id == user.id))
    await db.execute(delete(KnowledgeMastery).where(KnowledgeMastery.user_id == user.id))
    await db.delete(user)
    await db.flush()
    logger.info("用户删除(级联): user=%s username=%s", public_id, user.username)
    return {"ok": True, "deleted_user_id": public_id}
