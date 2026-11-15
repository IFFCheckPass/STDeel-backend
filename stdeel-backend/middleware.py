"""ASGI 中间件。

客户端允许把 Base URL 配成任意路径前缀（默认 https://api.stdeel.com/api/v1，
也可能被用户配成 https://host/stapi）。这里把前缀剥掉再交给路由，
使同一套路由在根路径和任意前缀下都可用；/healthz 也因此在两种地址下都通。

2026-11-14 安全审计后的加固（只读审计发现的问题）：
- AdminAuthMiddleware 的白名单补上 /answer-library 的管理端 CRUD 与 /knowledge/summary；
- SecurityHeadersMiddleware：全局安全响应头 + 管理台 CSP；
- HttpsRedirectMiddleware：经 Cloudflare 的明文 HTTP 请求 308 跳 HTTPS（内网直连 HTTP 不受影响）；
- BodyLimitMiddleware：请求体上限（上传单独放宽），防止内存/磁盘 DoS；
- RateLimitMiddleware：按来源 IP 的滑动窗口，重点限制 /users/api-key 枚举；
- 来源 IP 只在「直连来自 127.0.0.1（Cloudflare Tunnel / 本机）」时才信任
  CF-Connecting-IP / X-Forwarded-For，避免局域网直连伪造头部绕过登录限流。
"""

from __future__ import annotations

import json
import re
import time
from typing import Awaitable, Callable, Iterable
from urllib.parse import quote

import adminauth
import config
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


# --------------------------------------------------------------------------
# 请求头 / 来源 IP 工具（供限流、访问日志、登录限流共用）
# --------------------------------------------------------------------------
def scope_header(scope: Scope, name: str) -> str | None:
    wanted = name.lower().encode("latin-1")
    for header_name, header_value in scope.get("headers") or []:
        if header_name.lower() == wanted:
            return header_value.decode("latin-1")
    return None


def socket_client_ip(scope: Scope) -> str:
    client = scope.get("client") or ("", 0)
    ip = str(client[0] or "")
    if ip.startswith("::ffff:"):
        ip = ip[7:]
    return ip


def is_trusted_proxy(scope: Scope) -> bool:
    """只有本机回环连接（Cloudflare Tunnel / 本机反代）才可信转发头。"""
    return socket_client_ip(scope) in ("127.0.0.1", "::1", "")


def scope_client_ip(scope: Scope) -> str:
    """真实来源 IP：可信代理连接看 CF-Connecting-IP，其次 X-Forwarded-For，最后 socket。"""
    if is_trusted_proxy(scope):
        cf = scope_header(scope, "cf-connecting-ip")
        if cf:
            return cf.split(",")[0].strip()
        forwarded = scope_header(scope, "x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return socket_client_ip(scope) or "unknown"


def scope_is_https(scope: Scope) -> bool:
    if scope.get("scheme") == "https":
        return True
    proto = (scope_header(scope, "x-forwarded-proto") or "").split(",")[0].strip().lower()
    return proto == "https"


def _send_simple(send, status: int, headers: list[tuple[bytes, bytes]], body: bytes) -> Awaitable[None]:
    async def _run() -> None:
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    return _run()


# --------------------------------------------------------------------------
# 路径前缀
# --------------------------------------------------------------------------
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


# --------------------------------------------------------------------------
# 访问日志（含敏感路径的来源 IP，便于发现枚举/扫描）
# --------------------------------------------------------------------------
SENSITIVE_LOG_PATHS: frozenset[str] = frozenset({"/users/api-key", "/users/api-key-masked", "/webui/login"})


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
            elif path in SENSITIVE_LOG_PATHS or path.rstrip("/") in SENSITIVE_LOG_PATHS:
                self.logger.info(
                    "%s %s -> %s (%.1fms) ip=%s",
                    scope.get("method"),
                    path,
                    status_code["value"],
                    elapsed_ms,
                    scope_client_ip(scope),
                )
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
# 2026-11-14：补上此前遗漏的管理端接口（标准答案库 CRUD、知识点总览）。
# 客户端契约只使用 POST /answer-library、POST /answer-library/match，以及
# GET /knowledge/mastery|weak|points，所以下面这些不会影响客户端。
ADMIN_API_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("GET", re.compile(r"^/users$")),
    ("GET", re.compile(r"^/users/stats$")),
    ("GET", re.compile(r"^/users/[^/]+/records$")),
    ("GET", re.compile(r"^/users/[^/]+/mastery$")),
    ("GET", re.compile(r"^/users/[^/]+$")),
    ("DELETE", re.compile(r"^/users/[^/]+$")),
    ("GET", re.compile(r"^/answer-library$")),
    ("GET", re.compile(r"^/answer-library/[^/]+$")),
    ("PUT", re.compile(r"^/answer-library/[^/]+$")),
    ("DELETE", re.compile(r"^/answer-library/[^/]+$")),
    ("GET", re.compile(r"^/knowledge/summary$")),
    ("POST", re.compile(r"^/users/client-token$")),
)
# 无条件放行的路径：登录页、首启设置密码、登出（登出本来就要能在会话失效时清 Cookie）
PUBLIC_OPEN_EXACT = frozenset({ADMIN_LOGIN_PATH, "/webui/setup", "/webui/logout"})
# 客户端要用的接口：即使长得像管理端也放行（换机同步要读自己的 API Key）
CLIENT_OPEN_EXACT = frozenset({"/users/api-key", "/users/register"})


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


class AdminAuthMiddleware:
    """管理台鉴权的第一道闸。

    - 页面类（/webui、/docs、/redoc）：未登录 303 去 /webui/login。
    - 接口类（/openapi.json、管理端 JSON 接口）：未登录 401 JSON。
    - 未配置密码时同样拒绝（fail-closed），只是提示改成「先去设置密码」。
    - 客户端同步接口（注册、解题记录、答案库读写、知识点、API Key 同步）不受影响。
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
            await self.app(scope, receive, send)
            return

        if verdict == "page":
            target = ADMIN_LOGIN_PATH
            if path not in ("", "/"):
                target += "?next=" + quote(path, safe="/")
            await _finish(
                send,
                303,
                [(b"location", target.encode("latin-1")), (b"content-length", b"0"), (b"cache-control", b"no-store")],
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
                (b"cache-control", b"no-store"),
            ],
            body,
        )


# --------------------------------------------------------------------------
# 安全响应头 + 管理台 CSP
# --------------------------------------------------------------------------
ADMIN_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "font-src 'self' data:; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "object-src 'none'"
)
HSTS_VALUE = "max-age=31536000"


class SecurityHeadersMiddleware:
    """给所有 HTTP 响应补安全头（不覆盖已存在的同名头）。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = strip_api_prefix(scope.get("path", ""))
        https = scope_is_https(scope)

        async def send_wrapper(message: Message) -> None:
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])
                existing = {key.decode("latin-1").lower() for key, _ in headers}

                def add(name: str, value: str) -> None:
                    if name not in existing:
                        headers.append((name.encode("latin-1"), value.encode("latin-1")))
                        existing.add(name)

                add("x-content-type-options", "nosniff")
                add("x-frame-options", "DENY")
                add("referrer-policy", "no-referrer")
                if not path.startswith("/uploads"):
                    add("cache-control", "no-store")
                if https:
                    add("strict-transport-security", HSTS_VALUE)
                if path == "/webui" or path.startswith("/webui/"):
                    add("content-security-policy", ADMIN_CSP)
                message = dict(message)
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


# --------------------------------------------------------------------------
# 经 Cloudflare 的 HTTP 强制跳 HTTPS（内网直连 HTTP 不受影响）
# --------------------------------------------------------------------------
class HttpsRedirectMiddleware:
    """只有明确带了 X-Forwarded-Proto: http 的请求（即公网经 Cloudflare 的明文请求）才跳转。

    内网直连（例如 http://192.168.2.60:8500）没有这个头，保持原样，不影响本地调试。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive, send) -> None:
        if scope.get("type") == "http":
            proto = (scope_header(scope, "x-forwarded-proto") or "").split(",")[0].strip().lower()
            host = scope_header(scope, "host") or ""
            if proto == "http" and host:
                path = scope.get("path") or "/"
                query = (scope.get("query_string") or b"").decode("latin-1")
                location = "https://" + host + path + (("?" + query) if query else "")
                await _finish_redirect(send, location)
                return
        await self.app(scope, receive, send)


async def _finish_redirect(send, location: str) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 308,
            "headers": [
                (b"location", location.encode("latin-1")),
                (b"content-length", b"0"),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": b""})


# --------------------------------------------------------------------------
# 请求体上限（防内存/磁盘 DoS）
# --------------------------------------------------------------------------
class BodyTooLarge(Exception):
    pass


class BodyLimitMiddleware:
    """限制请求体大小；上传接口用更宽的上限。超限直接 413。"""

    def __init__(self, app: ASGIApp, *, default_limit: int, upload_limit: int) -> None:
        self.app = app
        self.default_limit = max(1, int(default_limit))
        self.upload_limit = max(1, int(upload_limit))

    async def __call__(self, scope: Scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = strip_api_prefix(scope.get("path", ""))
        limit = self.upload_limit if path == "/files/upload" else self.default_limit

        content_length = scope_header(scope, "content-length")
        if content_length and content_length.isdigit() and int(content_length) > limit:
            await self._reject(send)
            return

        received = {"value": 0}
        started = {"value": False}

        async def send_wrapper(message: Message) -> None:
            if message.get("type") == "http.response.start":
                started["value"] = True
            await send(message)

        async def receive_wrapper() -> Message:
            message = await receive()
            if message.get("type") == "http.request":
                received["value"] += len(message.get("body") or b"")
                if received["value"] > limit:
                    raise BodyTooLarge()
            return message

        try:
            await self.app(scope, receive_wrapper, send_wrapper)
        except BodyTooLarge:
            if not started["value"]:
                await self._reject(send)

    @staticmethod
    async def _reject(send) -> None:
        body = json.dumps({"detail": "请求体过大"}, ensure_ascii=False).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


# --------------------------------------------------------------------------
# 简单限流（按来源 IP 的滑动窗口；单 worker 内存实现）
# --------------------------------------------------------------------------
class RateLimitMiddleware:
    """默认每分钟 N 次；/users/api-key 单独更低，压枚举速度。"""

    def __init__(self, app: ASGIApp, *, per_minute: int, api_key_per_minute: int, enabled: bool = True) -> None:
        self.app = app
        self.per_minute = max(1, int(per_minute))
        self.api_key_per_minute = max(1, int(api_key_per_minute))
        self.enabled = bool(enabled)
        self._hits: dict[str, list[float]] = {}

    def _limit_for(self, path: str) -> int:
        return self.api_key_per_minute if path == "/users/api-key" else self.per_minute

    def _allow(self, key: str, limit: int) -> tuple[bool, int]:
        now = time.monotonic()
        window = 60.0
        stamps = [stamp for stamp in self._hits.get(key, []) if now - stamp < window]
        if len(stamps) >= limit:
            retry = max(1, int(window - (now - stamps[0])))
            self._hits[key] = stamps
            return False, retry
        stamps.append(now)
        self._hits[key] = stamps
        if len(self._hits) > 8192:
            for stale in [item for item, values in self._hits.items() if not values or now - values[-1] > window]:
                self._hits.pop(stale, None)
        return True, 0

    async def __call__(self, scope: Scope, receive, send) -> None:
        if not self.enabled or scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = strip_api_prefix(scope.get("path", ""))
        if path in ("/healthz", "/health"):
            await self.app(scope, receive, send)
            return

        ip = scope_client_ip(scope)
        allowed, retry = self._allow(ip + "|" + path, self._limit_for(path))
        if not allowed:
            body = json.dumps({"detail": "请求过于频繁，请稍后再试"}, ensure_ascii=False).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 429,
                    "headers": [
                        (b"content-type", b"application/json; charset=utf-8"),
                        (b"content-length", str(len(body)).encode()),
                        (b"retry-after", str(retry).encode("latin-1")),
                        (b"cache-control", b"no-store"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)
