import logging

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from config import API_PREFIX, ALLOWED_ORIGINS, UPLOAD_DIR, LOG_LEVEL, BASE_DIR
from database import engine, Base, async_session
from models import User, AnswerLibrary, SolveRecord, KnowledgeMastery
from routers import users, solve_records, knowledge, answer_library, files

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s %(levelname)s %(name)s - %(message)s")
logger = logging.getLogger("stdeel")

SYSTEM_USERNAME = "__system__"
SYSTEM_DEVICE_ID = "__system__"

FTS5_SETUP_SQLS = [
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS answer_library_fts USING fts5(
        question_text,
        content='answer_library',
        content_rowid='id'
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS answer_library_ai AFTER INSERT ON answer_library BEGIN
        INSERT INTO answer_library_fts(rowid, question_text) VALUES (new.id, new.question_text);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS answer_library_ad AFTER DELETE ON answer_library BEGIN
        INSERT INTO answer_library_fts(answer_library_fts, rowid, question_text) VALUES('delete', old.id, old.question_text);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS answer_library_au AFTER UPDATE ON answer_library BEGIN
        INSERT INTO answer_library_fts(answer_library_fts, rowid, question_text) VALUES('delete', old.id, old.question_text);
        INSERT INTO answer_library_fts(rowid, question_text) VALUES (new.id, new.question_text);
    END
    """,
]


async def _ensure_system_user(db: AsyncSession) -> int:
    """确保系统用户存在, 返回其 id. 迁移后的老数据会归属此用户。"""
    result = await db.execute(select(User).where(User.username == SYSTEM_USERNAME))
    sys_user = result.scalar_one_or_none()
    if sys_user:
        return sys_user.id
    sys_user = User(username=SYSTEM_USERNAME, device_id=SYSTEM_DEVICE_ID)
    db.add(sys_user)
    await db.flush()
    logger.info("创建系统用户: id=%s (用于汇总历史数据)", sys_user.id)
    return sys_user.id


async def _migrate_existing_mastery_to_system(db: AsyncSession, sys_user_id: int) -> None:
    """迁移: 将 user_id 为空的 mastery 行绑定到系统用户下。"""
    result = await db.execute(select(KnowledgeMastery).where(KnowledgeMastery.user_id.is_(None)))
    rows = result.scalars().all()
    if not rows:
        return
    for r in rows:
        r.user_id = sys_user_id
    await db.flush()
    logger.info("已将 %d 条历史 mastery 迁移到系统用户 (id=%s)", len(rows), sys_user_id)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("正在初始化数据库...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # 迁移: 去掉 user_api_keys 单槽位唯一索引(从一用户一 key 升级为一用户多 key)
        await conn.execute(text("DROP INDEX IF EXISTS uq_user_api_keys_user_id"))
        for stmt in FTS5_SETUP_SQLS:
            await conn.execute(text(stmt))

    async with async_session() as db:
        sys_id = await _ensure_system_user(db)
        await _migrate_existing_mastery_to_system(db, sys_id)
        await db.commit()
    logger.info("数据库初始化完成")
    yield
    logger.info("服务关闭")


app = FastAPI(
    title="思谛 STDeel 后端服务",
    version="1.1.0",
    description="前端直连 AI API + 后端纯数据库同步架构的学习软件后端",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("未处理异常: %s", exc)
    return JSONResponse(status_code=500, content={"detail": str(exc)})


@app.middleware("http")
async def request_logging(request: Request, call_next):
    logger.info("请求 %s %s", request.method, request.url.path)
    response = await call_next(call_next)
    return response


app.include_router(users.router, prefix=API_PREFIX)
app.include_router(solve_records.router, prefix=API_PREFIX)
app.include_router(knowledge.router, prefix=API_PREFIX)
app.include_router(answer_library.router, prefix=API_PREFIX)
app.include_router(files.router, prefix=API_PREFIX)

app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")

WEBUI_INDEX = BASE_DIR / "webui" / "index.html"


@app.get("/webui", include_in_schema=False)
async def webui():
    """管理台: 展示用户 / API Key / 知识点掌握情况(前端直连现有只读接口)。"""
    html = WEBUI_INDEX.read_text(encoding="utf-8") if WEBUI_INDEX.is_file() else "<h1>webui not found</h1>"
    return HTMLResponse(html)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
