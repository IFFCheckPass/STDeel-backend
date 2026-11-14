"""思谛 STDeel 后端入口。

架构：客户端直连 AI 推理，后端只做账号归属与数据同步（见 backend_requirements.md）。
启动顺序：建表 -> 幂等迁移 -> （升级时）重算知识点掌握 -> 对外服务。
"""

import ipaddress
import logging
from contextlib import asynccontextmanager
from html import escape as html_escape
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import adminauth
from config import (
    ADMIN_COOKIE_NAME,
    ADMIN_SESSION_TTL_SECONDS,
    APP_NAME,
    APP_VERSION,
    BASE_DIR,
    CORS_ORIGINS,
    DATABASE_PATH,
    LOG_LEVEL,
    UPLOAD_DIR,
)
from database import Base, async_session, engine
from middleware import AccessLogMiddleware, AdminAuthMiddleware, PathPrefixMiddleware
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

# 中间件注册顺序：后注册的在外层（CORS -> 路径前缀 -> 管理台鉴权 -> 访问日志 -> 路由）
app.add_middleware(AccessLogMiddleware, logger=logger)
app.add_middleware(AdminAuthMiddleware)
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
LOGIN_INDEX = BASE_DIR / "webui" / "login.html"

# 未配置密码时登录页会原样展示这条命令；不含任何凭据，只告诉在哪里设密码
SETUP_COMMAND = (
    "sudo -u www /www/server/pyporject_evn/3.11.2venv/bin/python "
    "/www/wwwroot/stdeel-backend/stdeel-backend/adminauth.py set-password"
)

_LOGIN_NOTICES = {
    "1": "密码不正确",
    "locked": "尝试次数过多，已暂时锁定，请稍后再试",
    "denied": "来源校验失败，请直接从管理台登录",
    "setup_invalid": "两次输入不一致，或密码长度不足 8 位",
    "setup_lan": "首次设置密码只允许在本机或内网直连时进行；也可在服务器执行 adminauth.py set-password",
}


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


# --------------------------------------------------------------------------
# 管理台登录（页面本身公开，不含任何业务数据）
# --------------------------------------------------------------------------
def _client_ip(request: Request) -> str:
    """真实来源 IP：Cloudflare Tunnel 场景下看 CF-Connecting-IP，其次 X-Forwarded-For。"""
    forwarded = request.headers.get("x-forwarded-for", "")
    return (
        request.headers.get("cf-connecting-ip")
        or (forwarded.split(",")[0].strip() if forwarded else "")
        or (request.client.host if request.client else "")
    ).strip()


def _is_https(request: Request) -> bool:
    """Cloudflare Tunnel 到源站是 HTTP，靠 X-Forwarded-Proto 判断对外是否 HTTPS。"""
    forwarded = request.headers.get("x-forwarded-proto", "")
    if forwarded.split(",")[0].strip().lower() == "https":
        return True
    return request.url.scheme == "https"


def _is_local_client(request: Request) -> bool:
    """判断是否「本机/内网直连」。

    首次设置管理密码只允许内网直连：公网（Cloudflare Tunnel）来源会被
    CF-Connecting-IP / cdn-loop 或非私有 IP 识别出来并被拒绝，避免被人抢注密码。
    """
    if request.headers.get("cf-connecting-ip") or request.headers.get("cdn-loop"):
        return False
    host = request.client.host if request.client else ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def _safe_next(raw: str) -> str:
    """只允许站内相对路径，避免开放重定向。"""
    text = (raw or "").strip()
    if not text.startswith("/") or text.startswith("//"):
        return "/webui"
    return text


def _login_page_html(request: Request, next_path: str, error: str) -> str:
    configured = adminauth.is_configured()
    notices: list[str] = []
    if configured:
        body_class = ""
    elif _is_local_client(request):
        # 首启：允许在内网直连时自己设密码（公网来源会被 _is_local_client 挡掉）
        body_class = "setup"
        notices.append(
            '<div class="notice"><strong>首次使用：设置管理密码</strong><br>'
            "设置后每次进入管理台都要输入它；服务器只保存加盐哈希，不保存明文。</div>"
        )
    else:
        body_class = "locked"
        notices.append(
            '<div class="notice"><strong>尚未配置管理密码</strong><br>'
            "管理台已拒绝公网访问。请在服务器执行下面的命令设置密码（输入不回显）："
            '<pre class="cmd">' + html_escape(SETUP_COMMAND) + "</pre></div>"
        )
    if error in _LOGIN_NOTICES:
        notices.append('<div class="notice">' + html_escape(_LOGIN_NOTICES[error]) + "</div>")

    template = (
        LOGIN_INDEX.read_text(encoding="utf-8")
        if LOGIN_INDEX.is_file()
        else '<!DOCTYPE html><meta charset="utf-8"><body><h1>管理台登录页缺失</h1></body>'
    )
    return (
        template.replace("%%BODY_CLASS%%", body_class)
        .replace("%%NOTICE%%", "".join(notices))
        .replace("%%NEXT%%", html_escape(next_path, quote=True))
    )


@app.get("/webui/login", include_in_schema=False)
async def webui_login_page(request: Request, error: str = "", next: str = "/webui"):
    next_path = _safe_next(next)
    if adminauth.token_valid(request.cookies.get(ADMIN_COOKIE_NAME)):
        return RedirectResponse(url=next_path, status_code=303)
    return HTMLResponse(
        _login_page_html(request, next_path, error),
        headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"},
    )


@app.post("/webui/login", include_in_schema=False)
async def webui_login_submit(request: Request):
    form = await request.form()
    password = str(form.get("password") or "")
    next_path = _safe_next(str(form.get("next") or "/webui"))

    # 简单 CSRF 兜底：带 Origin 的提交必须和当前 Host 一致
    origin = request.headers.get("origin", "")
    if origin:
        origin_netloc = urlsplit(origin).netloc
        if origin_netloc and origin_netloc != request.headers.get("host", ""):
            logger.warning("管理台登录来源不合法: origin=%s", origin_netloc)
            return RedirectResponse(url="/webui/login?error=denied", status_code=303)

    if not adminauth.is_configured():
        return RedirectResponse(url="/webui/login?error=unconfigured", status_code=303)

    ip = _client_ip(request)
    if adminauth.limiter.locked_for(ip):
        logger.warning("管理台登录被限流: ip=%s", ip)
        return RedirectResponse(
            url="/webui/login?error=locked&next=" + quote(next_path, safe="/"), status_code=303
        )

    if not adminauth.verify_password(password):
        adminauth.limiter.register_failure(ip)
        logger.warning("管理台登录失败: ip=%s", ip)
        return RedirectResponse(url="/webui/login?error=1&next=" + quote(next_path, safe="/"), status_code=303)

    token = adminauth.create_token()
    if not token:
        return RedirectResponse(url="/webui/login?error=unconfigured", status_code=303)

    adminauth.limiter.reset(ip)
    logger.info("管理台登录成功: ip=%s", ip)
    response = RedirectResponse(url=next_path, status_code=303)
    response.set_cookie(
        key=ADMIN_COOKIE_NAME,
        value=token,
        max_age=ADMIN_SESSION_TTL_SECONDS,
        path="/",
        httponly=True,
        samesite="lax",
        secure=_is_https(request),
    )
    return response


@app.post("/webui/setup", include_in_schema=False)
async def webui_setup_submit(request: Request):
    """首启设置管理密码：只允许本机/内网直连，公网来源一律拒绝（防抢注）。"""
    if adminauth.is_configured():
        return RedirectResponse(url="/webui/login", status_code=303)

    # 与登录相同的 CSRF 兜底
    origin = request.headers.get("origin", "")
    if origin:
        origin_netloc = urlsplit(origin).netloc
        if origin_netloc and origin_netloc != request.headers.get("host", ""):
            return RedirectResponse(url="/webui/login?error=denied", status_code=303)

    if not _is_local_client(request):
        logger.warning("拒绝公网来源的首启设置密码请求: ip=%s", _client_ip(request))
        return RedirectResponse(url="/webui/login?error=setup_lan", status_code=303)

    form = await request.form()
    next_path = _safe_next(str(form.get("next") or "/webui"))
    password = str(form.get("password") or "")
    confirm = str(form.get("confirm") or "")
    if password != confirm or len(password) < 8:
        return RedirectResponse(
            url="/webui/login?error=setup_invalid&next=" + quote(next_path, safe="/"), status_code=303
        )

    adminauth.write_password(password)
    logger.info("管理台密码已首次设置: ip=%s", _client_ip(request))
    response = RedirectResponse(url=next_path, status_code=303)
    token = adminauth.create_token()
    if token:
        response.set_cookie(
            key=ADMIN_COOKIE_NAME,
            value=token,
            max_age=ADMIN_SESSION_TTL_SECONDS,
            path="/",
            httponly=True,
            samesite="lax",
            secure=_is_https(request),
        )
    return response


@app.post("/webui/logout", include_in_schema=False)
async def webui_logout():
    response = RedirectResponse(url="/webui/login", status_code=303)
    response.delete_cookie(ADMIN_COOKIE_NAME, path="/")
    return response


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
