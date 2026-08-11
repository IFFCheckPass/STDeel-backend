import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import User, SolveRecord, KnowledgeMastery
from schemas import UserRegister, UserOut, UserStats, UserList, SolveRecordOut, KnowledgeMasteryOut

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/users", tags=["users"])


@router.post("/register", response_model=UserOut)
async def register(body: UserRegister, db: AsyncSession = Depends(get_db)):
    existing = await db.execute(select(User).where(User.username == body.username))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="用户名已存在")
    user = User(username=body.username, device_id=body.device_id)
    db.add(user)
    await db.flush()
    logger.info("新用户注册: id=%s, username=%s", user.id, user.username)
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


@router.get("/{user_id}/mastery", response_model=list[KnowledgeMasteryOut])
async def get_user_mastery(user_id: int, db: AsyncSession = Depends(get_db)):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    result = await db.execute(select(KnowledgeMastery).order_by(KnowledgeMastery.error_rate.desc()))
    return result.scalars().all()
