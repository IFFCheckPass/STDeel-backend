"""管理台鉴权：密码哈希、会话令牌、登录限流与「设置密码」命令行。

设计要点
- 密码只存「加盐 PBKDF2-SHA256」哈希，明文不落盘、不进日志、不进命令行参数。
- 会话是无状态签名 Cookie（HMAC-SHA256），改密码即时让所有旧会话失效。
- 认证配置默认在 BASE_DIR/admin_auth.json（0600、已 gitignore），文件 mtime 变化
  自动重载——改完密码不用重启服务。
- 未配置密码时一律拒绝而不是放行（fail-closed），避免忘了设置就裸奔。

命令行
    python adminauth.py status          # 只看「已配置 / 未配置」
    python adminauth.py set-password    # 交互式设置（输入不回显）
    python adminauth.py check           # 校验某个密码是否正确（本机排错用）

线上设置密码（文件属主必须是服务账号 www）：
    sudo -u www /www/server/pyporject_evn/3.11.2venv/bin/python /www/wwwroot/stdeel-backend/stdeel-backend/adminauth.py set-password
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import json
import logging
import os
import secrets
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import config

logger = logging.getLogger("stdeel.adminauth")

ALGORITHM = "pbkdf2_sha256"
# 这台 X200（Core2Duo）上 200k 次约 0.28s：登录可接受，暴力破解成本也够高。
# 配合「5 次失败锁 15 分钟」的限流，在线爆破基本不可行。
ITERATIONS = 200_000
MIN_PASSWORD_LENGTH = 8
AUTH_FILE_VERSION = 1
SECRET_BYTES = 32

_cache_lock = threading.Lock()
_cache: dict = {"mtime": None, "data": None}


# --------------------------------------------------------------------------
# 配置文件读写
# --------------------------------------------------------------------------
def auth_path() -> Path:
    return Path(config.ADMIN_AUTH_FILE)


def _hex_bytes(value: str, length: int) -> bytes | None:
    try:
        raw = bytes.fromhex(value)
    except (TypeError, ValueError):
        return None
    return raw if len(raw) == length else None


def _validate(raw) -> dict | None:
    """校验配置结构；任何字段不对都返回 None（宁可拒绝，也不静默放行）。"""
    if not isinstance(raw, dict):
        return None
    password_hash = raw.get("password_hash")
    session_secret = raw.get("session_secret")
    if not isinstance(password_hash, str) or not password_hash.startswith(ALGORITHM + "$"):
        return None
    if not isinstance(session_secret, str) or _hex_bytes(session_secret, SECRET_BYTES) is None:
        return None
    parts = password_hash.split("$")
    if len(parts) != 4 or not parts[1].isdigit() or not parts[2] or not parts[3]:
        return None
    return raw


def load_auth() -> dict | None:
    """读取认证配置；按 mtime 缓存，文件被改后下一次请求即生效。"""
    path = auth_path()
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        with _cache_lock:
            _cache.update(mtime=None, data=None)
        return None

    with _cache_lock:
        if _cache.get("mtime") == mtime:
            return _cache.get("data")

    data: dict | None = None
    try:
        data = _validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        logger.error("管理台认证配置读取失败（按未配置处理）：%s", path)
        logger.debug("读取异常: %s", exc)
        data = None
    if data is None:
        logger.error("管理台认证配置格式不正确（按未配置处理）：%s", path)

    with _cache_lock:
        _cache.update(mtime=mtime, data=data)
    return data


def is_configured() -> bool:
    return load_auth() is not None


# --------------------------------------------------------------------------
# 密码哈希
# --------------------------------------------------------------------------
def hash_password(password: str, *, iterations: int = ITERATIONS, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "$".join([ALGORITHM, str(iterations), salt.hex(), digest.hex()])


def verify_password(password: str) -> bool:
    """校验密码；未配置或格式异常一律 False。"""
    auth = load_auth()
    if auth is None:
        return False
    try:
        _, iterations, salt_hex, expected_hex = str(auth["password_hash"]).split("$")
        salt = bytes.fromhex(salt_hex)
        digest = hashlib.pbkdf2_hmac("sha256", str(password or "").encode("utf-8"), salt, int(iterations))
    except (KeyError, TypeError, ValueError):
        return False
    return hmac.compare_digest(digest.hex(), expected_hex)


def write_password(password: str) -> dict:
    """原子写入新密码（同时轮换 session_secret，旧会话全部失效）。"""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError("密码至少 " + str(MIN_PASSWORD_LENGTH) + " 位")

    path = auth_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = {
        "version": AUTH_FILE_VERSION,
        "password_hash": hash_password(password),
        "session_secret": secrets.token_hex(SECRET_BYTES),
        "updated_at": now,
    }

    # 同目录临时文件 + os.replace：避免写一半被读到；0600 只给服务账号自己看
    fd, tmp_name = tempfile.mkstemp(prefix=".admin_auth.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write(os.linesep)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    with _cache_lock:
        _cache.update(mtime=None, data=None)
    return {"updated_at": now, "path": str(path)}


# --------------------------------------------------------------------------
# 会话令牌（无状态签名 Cookie）
# --------------------------------------------------------------------------
def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes | None:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError):
        return None


def _password_version(auth: dict) -> str:
    """密码派生指纹：改密码后旧令牌自动失效。"""
    return hashlib.sha256(str(auth["password_hash"]).encode("utf-8")).hexdigest()[:16]


def _sign(secret: bytes, body: str) -> str:
    return hmac.new(secret, body.encode("ascii"), hashlib.sha256).hexdigest()


def create_token() -> str | None:
    auth = load_auth()
    if auth is None:
        return None
    secret = _hex_bytes(str(auth["session_secret"]), SECRET_BYTES)
    if secret is None:
        return None
    payload = {
        "v": 1,
        "pv": _password_version(auth),
        "exp": int(time.time() + int(config.ADMIN_SESSION_TTL_SECONDS)),
        "nonce": secrets.token_hex(8),
    }
    body = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return body + "." + _sign(secret, body)


def token_valid(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    auth = load_auth()
    if auth is None:
        return False
    secret = _hex_bytes(str(auth["session_secret"]), SECRET_BYTES)
    if secret is None:
        return False

    body, signature = token.rsplit(".", 1)
    if not hmac.compare_digest(signature, _sign(secret, body)):
        return False

    raw = _b64decode(body)
    if raw is None:
        return False
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    try:
        expired = int(payload["exp"]) <= int(time.time())
    except (KeyError, TypeError, ValueError):
        return False
    if expired:
        return False
    return hmac.compare_digest(str(payload.get("pv", "")), _password_version(auth))


# --------------------------------------------------------------------------
# 登录限流（进程内，够用：服务是单 worker）
# --------------------------------------------------------------------------
class LoginLimiter:
    """按来源 IP 记录失败次数，窗口内超限即锁定。"""

    def __init__(self, max_failures: int, lock_seconds: int) -> None:
        self.max_failures = max(1, int(max_failures))
        self.lock_seconds = max(1, int(lock_seconds))
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}

    @staticmethod
    def _key(ip: str | None) -> str:
        return (ip or "").strip() or "unknown"

    def _prune(self, now: float, stamps: list[float]) -> list[float]:
        return [stamp for stamp in stamps if now - stamp < self.lock_seconds]

    def locked_for(self, ip: str | None) -> int:
        """还剩多少秒锁定；0 表示未锁定。"""
        key = self._key(ip)
        now = time.time()
        with self._lock:
            stamps = self._prune(now, self._failures.get(key, []))
            if stamps:
                self._failures[key] = stamps
            else:
                self._failures.pop(key, None)
            if len(stamps) < self.max_failures:
                return 0
            return max(1, int(self.lock_seconds - (now - stamps[-1])))

    def register_failure(self, ip: str | None) -> None:
        key = self._key(ip)
        now = time.time()
        with self._lock:
            stamps = self._prune(now, self._failures.get(key, []))
            stamps.append(now)
            self._failures[key] = stamps
            # 顺手清理过期 key，防止长时间运行时字典无限增长
            if len(self._failures) > 4096:
                for stale in [k for k, v in self._failures.items() if not v or now - v[-1] > self.lock_seconds]:
                    self._failures.pop(stale, None)

    def reset(self, ip: str | None) -> None:
        with self._lock:
            self._failures.pop(self._key(ip), None)


limiter = LoginLimiter(config.ADMIN_LOGIN_MAX_FAILURES, config.ADMIN_LOGIN_LOCK_SECONDS)


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------
def _read_password_twice() -> str:
    first = getpass.getpass("新的管理台密码（输入不回显，至少 " + str(MIN_PASSWORD_LENGTH) + " 位）：")
    if first != getpass.getpass("再输入一次："):
        print("两次输入不一致，已取消。", file=sys.stderr)
        raise SystemExit(2)
    if len(first) < MIN_PASSWORD_LENGTH:
        print("密码至少 " + str(MIN_PASSWORD_LENGTH) + " 位，已取消。", file=sys.stderr)
        raise SystemExit(2)
    return first


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command = argv[0] if argv else "status"

    if command == "status":
        auth = load_auth()
        print("配置文件：" + str(auth_path()))
        print("状态：" + ("已配置" if auth else "未配置（管理台会被拒绝访问）"))
        if auth:
            print("更新时间：" + str(auth.get("updated_at") or "未知"))
        return 0

    if command in ("set-password", "reset-password"):
        info = write_password(_read_password_twice())
        print("已写入 " + info["path"] + "（只保存加盐哈希，明文未落盘）")
        print("已生效：服务下次请求会自动重载，所有旧会话立即失效。")
        return 0

    if command == "check":
        ok = verify_password(getpass.getpass("要校验的密码："))
        print("通过" if ok else "不通过")
        return 0 if ok else 1

    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
