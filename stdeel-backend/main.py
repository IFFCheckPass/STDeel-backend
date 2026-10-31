"""思谛 STDeel 后端入口。

架构：客户端直连 AI 推理，后端只做账号归属与数据同步（见 backend_requirements.md）。
启动顺序：建表 -> 幂等迁移 -> （升级时）重算知识点掌握 -> 对外服务。
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config import APP_NAME, APP_VERSION, BASE_DIR, CORS_ORIGINS, DATABASE_PATH, LOG_LEVEL, UPLOAD_DIR
from database import Base, async_session, engine
from middleware import AccessLogMiddleware, PathPrefixMiddleware
from migrations import run_migrations
from routers import answer_library, files, knowledge, solve_records, users
from services import recompute_all_mastery

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
logger = logging.getLogger("stdeel")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("初始化数据库: %s", DATABASE_PATH)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        upgraded = await run_migrations(conn)

    if upgraded:
        # 升级后把派生数据对齐到明细，避免历史掌握度与解题记录不一致
        async with async_session() as db:
            try:
                count = await recompute_all_mastery(db)
                await db.commit()
                logger.info("升级后已重算 %d 个用户的知识点掌握", count)
            except Exception:
                await db.rollback()
                logger.exception("升级后重算知识点掌握失败（不影响启动）")

    logger.info("%s v%s 就绪", APP_NAME, APP_VERSION)
    yield
    await engine.dispose()
    logger.info("服务已停止")


app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
    description=(
        "思谛 STDeel 后端：账号归属 + 解题记录同步 + 标准答案库 + 知识点统计。\n"
        "所有业务路由同时支持根路径与任意 Base URL 前缀（如 /api/v1、/stapi）。"
    ),
    lifespan=lifespan,
)

# 中间件注册顺序：后注册的在外层（CORS -> 路径前缀 -> 访问日志 -> 路由）
app.add_middleware(AccessLogMiddleware, logger=logger)
app.add_middleware(PathPrefixMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("未处理异常: %s %s -> %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=500, content={"detail": "服务器内部错误"})


app.include_router(users.router)
app.include_router(solve_records.router)
app.include_router(answer_library.router)
app.include_router(knowledge.router)
app.include_router(files.router)

app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")

WEBUI_INDEX = BASE_DIR / "webui" / "index.html"


@app.get("/", include_in_schema=False)
async def index():
    return {
        "service": APP_NAME,
        "version": APP_VERSION,
        "docs": "/docs",
        "health": "/healthz",
        "admin": "/webui",
        "base_url_hint": "任意路径前缀均可，例如 /api/v1",
    }


@app.get("/healthz", tags=["health"], summary="健康检查（host 根路径，忽略 Base URL 前缀）")
async def healthz():
    """客户端 5s 超时；这里不碰数据库，保证毫秒级返回。"""
    return {"status": "ok", "version": APP_VERSION}


@app.get("/health", include_in_schema=False)
async def health():
    return {"status": "ok", "version": APP_VERSION}


@app.get("/webui", include_in_schema=False)
async def webui():
    """管理台：用户 / API Key / 知识点掌握 / 解题记录。"""
    html = WEBUI_INDEX.read_text(encoding="utf-8") if WEBUI_INDEX.is_file() else "<h1>webui not found</h1>"
    return HTMLResponse(html)
