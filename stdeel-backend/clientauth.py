"""客户端（App）令牌：给换机同步等敏感接口做 per-user 鉴权（2026-11-14 安全审计后新增）。

设计
- 令牌格式 `st_<43 字符 urlsafe>`；数据库只存 SHA-256 哈希，明文只在生成时返回一次。
- 客户端在请求头 `X-STDeel-Token` 里携带；也兼容 `?client_token=`（老/简易客户端）。
- 生成/轮换在管理台做（`POST /users/client-token`，需管理员登录），轮换后旧令牌立即失效。
- 用途：`GET/PUT /users/api-key`（换机同步，涉及明文 AI Key）必须带令牌；
  其它客户端接口暂保持兼容（见 docs/API.md §11）。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

TOKEN_PREFIX = "st_"
TOKEN_BYTES = 32
HASH_LEN = 64


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def issue(user) -> str:
    """给用户生成新令牌（覆盖旧的），返回明文；调用方负责 flush。"""
    token = new_token()
    user.client_token_hash = hash_token(token)
    return token


def verify(user, token: str | None) -> bool:
    stored = str(getattr(user, "client_token_hash", "") or "")
    candidate = str(token or "").strip()
    if not stored or not candidate:
        return False
    return hmac.compare_digest(hash_token(candidate), stored)


def from_request(request) -> str:
    header = request.headers.get("x-stdeel-token")
    if header:
        return str(header).strip()
    return str(request.query_params.get("client_token") or "").strip()
