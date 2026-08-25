import logging
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import User, UserApiKey, SolveRecord, KnowledgeMastery
from schemas import (
    UserRegister,
    UserOut,
    UserStats,
    UserList,
    SolveRecordOut,
    KnowledgeMasteryItem,
    KnowledgeMasteryList,
    ApiKeyUpsert,
    ApiKeyOut,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/users", tags=["users"])


async def _find_existing_user(db: AsyncSession, body: UserRegister) -> User | None:
    if body.device_id:
        result = await db.execute(select(User).where(User.device_id == body.device_id))
        user = result.scalar_one_or_none()
        if user:
            return user
    if body.username:
        result = await db.execute(select(User).where(User.username == body.username))
        user = result.scalar_one_or_none()
        if user:
            return user
    return None


@router.post("/register", response_model=UserOut)
async def register(body: UserRegister, db: AsyncSession = Depends(get_db)):
    existing = await _find_existing_user(db, body)
    if existing:
        logger.info("用户复用: id=%s (device_id=%s, username=%s)", existing.id, body.device_id, body.username)
        if body.device_id and not existing.device_id:
            existing.device_id = body.device_id
        return existing

    username = body.username
    if not username:
        username = f"anon-{secrets.token_hex(4)}"

    while True:
        check = await db.execute(select(User).where(User.username == username))
        if not check.scalar_one_or_none():
            break
        username = f"{body.username or 'anon'}-{secrets.token_hex(2)}"

    user = User(username=username, device_id=body.device_id)
    db.add(user)
    await db.flush()
    logger.info("新用户注册: id=%s, username=%s, device_id=%s", user.id, user.username, body.device_id)
    return user


@router.get("/stats", response_model=UserStats)
async def user_stats(db: AsyncSession = Depends(get_db)):
    now = datetime.utcnow()
    total = await db.execute(select(func.count()).select_from(User))
    today_new = await db.execute(
        select(func.count()).select_from(User).where(User.created_at >= now.replace(hour=0, minute=0, second=0, microsecond=0))
    )
    active_7d = await db.execute(
        select(func.count()).select_from(User).where(User.last_active_at >= now - timedelta(days=7))
    )
    active_30d = await db.execute(
        select(func.count()).select_from(User).where(User.last_active_at >= now - timedelta(days=30))
    )
    return UserStats(
        total_users=total.scalar_one(),
        today_new=today_new.scalar_one(),
        active_7d=active_7d.scalar_one(),
        active_30d=active_30d.scalar_one(),
    )


@router.get("", response_model=UserList)
async def list_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: str = Query(""),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(User)
    if search:
        stmt = stmt.where(User.username.ilike(f"%{search}%"))
    total_stmt = select(func.count()).select_from(stmt.subquery())
    total = await db.execute(total_stmt)
    items = await db.execute(stmt.offset((page - 1) * page_size).limit(page_size))
    return UserList(items=items.scalars().all(), total=total.scalar_one(), page=page, page_size=page_size)


@router.put("/api-key", response_model=ApiKeyOut)
async def upsert_api_key(body: ApiKeyUpsert, db: AsyncSession = Depends(get_db)):
    """开设/更新该用户的 api-key 槽位(单槽位, 重复 PUT 即覆盖)。"""
    user = await db.get(User, body.user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    result = await db.execute(select(UserApiKey).where(UserApiKey.user_id == body.user_id))
    row = result.scalar_one_or_none()
    if row:
        row.api_key = body.api_key
        if body.name is not None:
            row.name = body.name
        if body.enabled is not None:
            row.enabled = body.enabled
        row.updated_at = func.now()
    else:
        row = UserApiKey(
            user_id=body.user_id,
            api_key=body.api_key,
            name=body.name,
            enabled=body.enabled if body.enabled is not None else True,
        )
        db.add(row)
    await db.flush()
    logger.info("api-key 写入: user_id=%s, has_key=%s", body.user_id, bool(body.api_key))
    return row


@router.get("/api-key", response_model=list[ApiKeyOut])
async def get_api_key(user_id: int = Query(...), db: AsyncSession = Depends(get_db)):
    """拉取该用户已保存的 api-key 列表, 供换机同步。"""
    result = await db.execute(
        select(UserApiKey).where(UserApiKey.user_id == user_id).order_by(UserApiKey.id.desc())
    )
    return result.scalars().all()


@router.get("/{user_id}", response_model=UserOut)
async def get_user(user_id: int, db: AsyncSession = Depends(get_db)):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    return user


@router.get("/{user_id}/records", response_model=list[SolveRecordOut])
async def get_user_records(user_id: int, db: AsyncSession = Depends(get_db)):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    stmt = select(SolveRecord).where(SolveRecord.user_id == user_id).order_by(SolveRecord.created_at.desc())
    result = await db.execute(stmt)
    return result.scalars().all()


@router.get("/{user_id}/mastery", response_model=KnowledgeMasteryList)
async def get_user_mastery(user_id: int, db: AsyncSession = Depends(get_db)):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    result = await db.execute(
        select(KnowledgeMastery)
        .where(KnowledgeMastery.user_id == user_id)
        .order_by(KnowledgeMastery.error_rate.desc(), KnowledgeMastery.total_count.desc())
    )
    rows = result.scalars().all()
    items = [KnowledgeMasteryItem.model_validate(r) for r in rows]
    return KnowledgeMasteryList(items=items, total=len(items))
