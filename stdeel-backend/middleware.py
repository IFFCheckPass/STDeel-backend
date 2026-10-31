"""ASGI 中间件。

客户端允许把 Base URL 配成任意路径前缀（默认 https://api.stdeel.com/api/v1，
也可能被用户配成 https://host/stapi）。这里把前缀剥掉再交给路由，
使同一套路由在根路径和任意前缀下都可用；/healthz 也因此在两种地址下都通。
"""

from __future__ import annotations

import time
from typing import Awaitable, Callable, Iterable

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
