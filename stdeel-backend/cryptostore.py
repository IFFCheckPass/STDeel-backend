"""API Key 加密存储（纯标准库，不增加依赖）。

为什么加密：api_keys 列原来原样存明文 AI Key，DB 文件被拷走/误传就等于泄露所有用户的 Key；
管理台也不该把完整 Key 摊在页面上。

实现
- 主密钥 32 字节：优先环境变量 STDEEL_API_KEY_SECRET（64 位 hex，便于迁移/测试），否则读
  BASE_DIR/api_key.key（0600，首次使用自动生成，已 gitignore；可用 STDEEL_API_KEY_SECRET_FILE 换路径）。
- 子密钥：k_enc = HMAC(主密钥, "stdeel:subkey:enc")，k_mac = HMAC(主密钥, "stdeel:subkey:mac")。
- 加密：PRF 流密码（HMAC-SHA256 计数器模式）异或明文，每次随机 16 字节 nonce。
- 完整性：encrypt-then-MAC，HMAC-SHA256 覆盖版本号 + nonce + 密文，解密前先验。
- 密文 token：v1:<nonce_b64>:<cipher_b64>:<mac_b64>。
- 展示：只保留前 3 位和后 3 位（短于 7 位全掩码），中间段不落明文。

边界：这层防「数据库被拷走/误传」，不防拿到服务器 root 的人——主密钥就在同一台机器上，
所以 api_key.key 必须 0600 且与数据库一起纳入 root-only 备份。

命令行
    python cryptostore.py status   # 只看密钥是否已配置、路径在哪（不打印密钥）
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import sys
import tempfile
import threading
from pathlib import Path

import config

logger = logging.getLogger("stdeel.cryptostore")

KEY_BYTES = 32
NONCE_BYTES = 16
MIN_KEEP = 3
MASK_ALL = "******"
TOKEN_VERSION = "v1"

_lock = threading.Lock()
_cache: dict = {"path": None, "mtime": None, "key": None}


# --------------------------------------------------------------------------
# 主密钥
# --------------------------------------------------------------------------
def key_file_path() -> Path:
    return Path(config.API_KEY_SECRET_FILE)


def _parse_hex(text: str) -> bytes:
    try:
        raw = bytes.fromhex(str(text or "").strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("API Key 主密钥必须是 64 位十六进制字符串") from exc
    if len(raw) != KEY_BYTES:
        raise ValueError("API Key 主密钥长度不对（需要 32 字节）")
    return raw


def _write_key_file(path: Path, key: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".api_key.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(key.hex())
            handle.write(os.linesep)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    logger.info("已生成新的 API Key 主密钥文件：%s（0600，勿外传，需与数据库一起备份）", path)


def master_key() -> bytes:
    env_value = os.getenv("STDEEL_API_KEY_SECRET", "").strip()
    if env_value:
        return _parse_hex(env_value)

    path = key_file_path()
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        mtime = None

    with _lock:
        if (
            mtime is not None
            and _cache.get("path") == str(path)
            and _cache.get("mtime") == mtime
        ):
            return _cache["key"]

        if mtime is None:
            key = secrets.token_bytes(KEY_BYTES)
            _write_key_file(path, key)
            mtime = path.stat().st_mtime_ns
        else:
            key = _parse_hex(path.read_text(encoding="utf-8"))
        _cache.update(path=str(path), mtime=mtime, key=key)
        return key


def is_configured() -> bool:
    """只看密钥是否可用（不打印、不返回密钥本身）。"""
    try:
        master_key()
    except (OSError, ValueError):
        return False
    return True


def _subkeys() -> tuple[bytes, bytes]:
    key = master_key()
    k_enc = hmac.new(key, b"stdeel:subkey:enc", hashlib.sha256).digest()
    k_mac = hmac.new(key, b"stdeel:subkey:mac", hashlib.sha256).digest()
    return k_enc, k_mac


# --------------------------------------------------------------------------
# 编解码 / 加解密
# --------------------------------------------------------------------------
def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(str(text) + padding)


def _keystream(k_enc: bytes, nonce: bytes, length: int) -> bytes:
    chunks: list[bytes] = []
    counter = 0
    while sum(len(chunk) for chunk in chunks) < length:
        chunks.append(hmac.new(k_enc, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest())
        counter += 1
    return b"".join(chunks)[:length]


def _xor(data: bytes, stream: bytes) -> bytes:
    return bytes(left ^ right for left, right in zip(data, stream))


def encrypt_api_key(plaintext: str) -> str:
    k_enc, k_mac = _subkeys()
    data = str(plaintext or "").encode("utf-8")
    nonce = secrets.token_bytes(NONCE_BYTES)
    cipher = _xor(data, _keystream(k_enc, nonce, len(data)))
    mac = hmac.new(k_mac, TOKEN_VERSION.encode("ascii") + nonce + cipher, hashlib.sha256).digest()
    return ":".join([TOKEN_VERSION, _b64encode(nonce), _b64encode(cipher), _b64encode(mac)])


def decrypt_api_key(token: str) -> str:
    parts = str(token or "").split(":")
    if len(parts) != 4 or parts[0] != TOKEN_VERSION:
        raise ValueError("不是有效的 v1 加密串")
    nonce = _b64decode(parts[1])
    cipher = _b64decode(parts[2])
    mac = _b64decode(parts[3])
    k_enc, k_mac = _subkeys()
    expected = hmac.new(k_mac, TOKEN_VERSION.encode("ascii") + nonce + cipher, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected):
        raise ValueError("API Key 解密失败：完整性校验不通过（主密钥可能已更换）")
    return _xor(cipher, _keystream(k_enc, nonce, len(cipher))).decode("utf-8")


# --------------------------------------------------------------------------
# 落库条目与展示
# --------------------------------------------------------------------------
def mask(secret: str) -> str:
    text = str(secret or "")
    if len(text) < MIN_KEEP * 2 + 1:
        return MASK_ALL
    return text[:MIN_KEEP] + MASK_ALL + text[-MIN_KEEP:]


def make_entry(api_key: str, name: str = "", enabled: bool = True) -> dict:
    """明文 Key -> 落库条目（密文 + 前3/后3 掩码素材）。"""
    key = str(api_key or "").strip()
    entry: dict = {
        "name": str(name or ""),
        "enabled": bool(enabled),
        "cipher": encrypt_api_key(key),
    }
    if len(key) >= MIN_KEEP * 2 + 1:
        entry["prefix"] = key[:MIN_KEEP]
        entry["suffix"] = key[-MIN_KEEP:]
    else:
        entry["prefix"] = ""
        entry["suffix"] = ""
    return entry


def masked_entry(entry: dict) -> str:
    """落库条目 -> 展示用掩码（不需要解密）。"""
    prefix = str(entry.get("prefix") or "")
    suffix = str(entry.get("suffix") or "")
    if prefix or suffix:
        return prefix + MASK_ALL + suffix
    return MASK_ALL


def plain_from_entry(entry: dict) -> str:
    """落库条目 -> 明文（客户端换机同步用）。兼容还没迁移的旧条目。"""
    cipher = entry.get("cipher")
    if cipher:
        return decrypt_api_key(str(cipher))
    return str(entry.get("api_key") or "")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command = argv[0] if argv else "status"
    if command == "status":
        print("密钥文件：" + str(key_file_path()))
        print("状态：" + ("已配置" if is_configured() else "未配置（首次保存 Key 时会自动生成）"))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
