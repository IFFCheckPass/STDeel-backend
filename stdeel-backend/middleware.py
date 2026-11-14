"""ASGI 中间件。

客户端允许把 Base URL 配成任意路径前缀（默认 https://api.stdeel.com/api/v1，
也可能被用户配成 https://host/stapi）。这里把前缀剥掉再交给路由，
使同一套路由在根路径和任意前缀下都可用；/healthz 也因此在两种地址下都通。
"""

from __future__ import annotations

import json
import re
import time
from typing import Awaitable, Callable, Iterable
from urllib.parse import quote

import adminauth
from config import ADMIN_COOKIE_NAME

Scope = dict
Message = dict
ASGIApp = Callable[[Scope, Callable[[], Awaitable[Message]], Callable[[Message], Awaitable[None]]], Awaitable[None]]

# 路由的一级路径段；只要请求路径里出现这些段，就把它们之前的部分当成前缀丢掉。
KNOWN_ROOTS: frozenset[str] = frozenset(
    {
        "users",
        "solve-records",
        "answer-library",
        "knowledge",
        "files",
        "healthz",
        "health",
        "webui",
        "uploads",
        "docs",
        "redoc",
        "openapi.json",
    }
)


def strip_api_prefix(path: str) -> str:
    """把任意前缀剥掉：

    /solve-records            -> /solve-records
    /api/v1/solve-records     -> /solve-records
    /stapi/users/register     -> /users/register
    /stapi/healthz            -> /healthz
    /foo/bar                  -> /foo/bar（没有已知路由段，原样返回）
    """
    if not path or path == "/":
        return path
    segments = [segment for segment in path.split("/") if segment]
    if not segments or segments[0] in KNOWN_ROOTS:
        return path
    for index, segment in enumerate(segments):
        if segment in KNOWN_ROOTS:
            return "/" + "/".join(segments[index:])
    return path


class PathPrefixMiddleware:
    """纯 ASGI 中间件：改写 scope["path"] 后继续传递。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive, send) -> None:
        if scope.get("type") in ("http", "websocket"):
            raw_path = scope.get("path", "")
            rewritten = strip_api_prefix(raw_path)
            if rewritten != raw_path:
                scope = dict(scope)
                scope["path"] = rewritten
                scope["raw_path"] = rewritten.encode("utf-8")
        await self.app(scope, receive, send)


class AccessLogMiddleware:
    """记录方法、路径、状态码与耗时；健康检查降级为 debug 以免刷日志。"""

    def __init__(self, app: ASGIApp, logger) -> None:
        self.app = app
        self.logger = logger

    async def __call__(self, scope: Scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status_code = {"value": 500}

        async def send_wrapper(message: Message) -> None:
            if message.get("type") == "http.response.start":
                status_code["value"] = message.get("status", 500)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            path = scope.get("path", "")
            elapsed_ms = (time.perf_counter() - started) * 1000
            if path in ("/healthz", "/health"):
                self.logger.debug("%s %s -> %s (%.1fms)", scope.get("method"), path, status_code["value"], elapsed_ms)
            else:
                self.logger.info(
                    "%s %s -> %s (%.1fms)",
                    scope.get("method"),
                    path,
                    status_code["value"],
                    elapsed_ms,
                )


# --------------------------------------------------------------------------
# 管理台鉴权（第二道闸：中间件先挡住未登录访问）
# --------------------------------------------------------------------------
ADMIN_LOGIN_PATH = "/webui/login"
# 必须登录才能看的「页面」：未登录时 303 去登录页，而不是弹 401
ADMIN_PAGE_EXACT = frozenset({"/webui", "/docs", "/redoc"})
ADMIN_PAGE_PREFIXES = ("/webui/", "/docs/")
# 必须登录才能拿的 JSON：未登录直接 401
ADMIN_JSON_EXACT = frozenset({"/openapi.json"})
ADMIN_API_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("GET", re.compile(r"^/users$")),
    ("GET", re.compile(r"^/users/stats$")),
    ("GET", re.compile(r"^/users/[^/]+/records$")),
    ("GET", re.compile(r"^/users/[^/]+/mastery$")),
    ("GET", re.compile(r"^/users/[^/]+$")),
    ("DELETE", re.compile(r"^/users/[^/]+$")),
)
# 无条件放行的路径：登录页、登出（登出本来就要能在会话失效时清 Cookie）
PUBLIC_OPEN_EXACT = frozenset({ADMIN_LOGIN_PATH, "/webui/logout"})
# 客户端要用的接口：即使长得像管理端也放行（换机同步要读自己的 API Key）
CLIENT_OPEN_EXACT = frozenset({"/users/api-key", "/users/register"})

SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"cache-control", b"no-store"),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
)


def classify_admin_scope(method: str, path: str) -> str:
    """把请求分成三类：public（放行）/ page（页面，未登录跳登录页）/ api（接口，未登录 401）。"""
    if path in PUBLIC_OPEN_EXACT or path.startswith(ADMIN_LOGIN_PATH + "/"):
        return "public"
    if path in CLIENT_OPEN_EXACT:
        return "public"
    if path in ADMIN_PAGE_EXACT or path.startswith(ADMIN_PAGE_PREFIXES):
        return "page"
    if path in ADMIN_JSON_EXACT:
        return "api"
    for rule_method, pattern in ADMIN_API_RULES:
        if method == rule_method and pattern.match(path):
            return "api"
    return "public"


def _cookie_value(scope: Scope, name: str) -> str | None:
    for header_name, header_value in scope.get("headers") or []:
        if header_name == b"cookie":
            for item in header_value.decode("latin-1").split(";"):
                key, _, value = item.strip().partition("=")
                if key == name:
                    return value
    return None


async def _finish(send, status: int, headers: list[tuple[bytes, bytes]], body: bytes) -> None:
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def _with_security_headers(send):
    async def wrapped(message: Message) -> None:
        if message.get("type") == "http.response.start":
            message = dict(message)
            message["headers"] = list(message.get("headers") or []) + list(SECURITY_HEADERS)
        await send(message)

    return wrapped


class AdminAuthMiddleware:
    """管理台鉴权的第一道闸。

    - 页面类（/webui、/docs、/redoc）：未登录 303 去 /webui/login。
    - 接口类（/openapi.json、管理端 JSON 接口）：未登录 401 JSON。
    - 未配置密码时同样拒绝（fail-closed），只是提示改成「先去设置密码」。
    - 客户端同步接口（注册、解题记录、答案库、知识点、API Key 同步）不受影响。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "GET")
        verdict = classify_admin_scope(method, path)
        if verdict == "public":
            await self.app(scope, receive, send)
            return

        configured = adminauth.is_configured()
        authenticated = configured and adminauth.token_valid(_cookie_value(scope, ADMIN_COOKIE_NAME))
        if authenticated:
            await self.app(scope, receive, _with_security_headers(send))
            return

        if verdict == "page":
            target = ADMIN_LOGIN_PATH
            if path not in ("", "/"):
                target += "?next=" + quote(path, safe="/")
            await _finish(
                send,
                303,
                [(b"location", target.encode("latin-1")), (b"content-length", b"0")] + list(SECURITY_HEADERS),
                b"",
            )
            return

        if not configured:
            detail = "管理台密码未配置：请在服务器执行 python adminauth.py set-password 后重试"
            status = 503
        else:
            detail = "需要登录管理台"
            status = 401
        body = json.dumps({"detail": detail}, ensure_ascii=False).encode("utf-8")
        await _finish(
            send,
            status,
            [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
            ]
            + list(SECURITY_HEADERS),
            body,
        )
