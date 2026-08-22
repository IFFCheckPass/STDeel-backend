import logging

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from config import API_PREFIX, ALLOWED_ORIGINS, UPLOAD_DIR, LOG_LEVEL
from database import engine, Base
from models import User, AnswerLibrary, SolveRecord, KnowledgeMastery
from routers import users, solve_records, knowledge, answer_library, files

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s %(levelname)s %(name)s - %(message)s")
logger = logging.getLogger("stdeel")


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("正在初始化数据库...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for stmt in FTS5_SETUP_SQLS:
            await conn.execute(text(stmt))
    logger.info("数据库初始化完成")
    yield
    logger.info("服务关闭")


app = FastAPI(
    title="思谛 STDeel 后端服务",
    version="1.0.0",
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
    response = await call_next(request)
    return response


app.include_router(users.router, prefix=API_PREFIX)
app.include_router(solve_records.router, prefix=API_PREFIX)
app.include_router(knowledge.router, prefix=API_PREFIX)
app.include_router(answer_library.router, prefix=API_PREFIX)
app.include_router(files.router, prefix=API_PREFIX)

app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
