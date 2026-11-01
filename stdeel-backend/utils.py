"""纯函数工具：对外 ID 编解码、题干规范化、枚举收敛、时间序列化。

这里刻意不依赖数据库，方便单独测试和复用。
"""

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

# 对外暴露的 user_id 形如 "u_1"；同时兼容纯数字（老客户端曾收到纯数字）。
USER_ID_PREFIX = "u_"

DEFAULT_SUBJECT = "未分类"
DEFAULT_ACTION = "solve"
DEFAULT_FEEDBACK = "none"
DEFAULT_SOURCE = "ai"

FEEDBACK_NONE = "none"
FEEDBACK_VALUES = ("none", "correct", "partial", "wrong")
JUDGED_FEEDBACK = ("correct", "partial", "wrong")
ACTION_VALUES = ("solve", "retry", "detail", "correct", "partial", "wrong")
SOURCE_VALUES = ("ai", "answer_library", "error")

_WS_RE = re.compile(r"\s+")


# --------------------------------------------------------------------------
# user_id
# --------------------------------------------------------------------------
def encode_user_id(user_id: Any) -> str | None:
    """把内部自增 id 编码成对外字符串 id。"""
    if user_id is None:
        return None
    try:
        number = int(user_id)
    except (TypeError, ValueError):
        return str(user_id)
    return f"{USER_ID_PREFIX}{number}"


def decode_user_id(value: Any) -> int | None:
    """解析客户端传来的 user_id：支持 12、"12"、"u_12"、"U_12"。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and value > 0 else None
    text = str(value).strip()
    if not text:
        return None
    if text[:2].lower() == USER_ID_PREFIX:
        text = text[2:].strip()
    try:
        number = int(text)
    except ValueError:
        return None
    return number if number > 0 else None


# --------------------------------------------------------------------------
# 题干规范化与哈希
# --------------------------------------------------------------------------
def normalize_question_text(text: Any) -> str:
    """题干规范化：trim + 连续空白压缩为单个空格（需求 §3.4）。"""
    if text is None:
        return ""
    return _WS_RE.sub(" ", str(text).strip())


def question_hash(text: Any) -> str:
    """题干规范化后的 SHA-256 小写 hex。"""
    return hashlib.sha256(normalize_question_text(text).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# 通用类型收敛（客户端历史版本字段类型不完全一致，尽量宽容）
# --------------------------------------------------------------------------
def as_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def as_clean_text(value: Any, default: str = "") -> str:
    """去首尾空白；空字符串视为「未提供」返回 default。"""
    text = as_text(value, default).strip()
    return text if text else default


def as_int(value: Any, default: int = 0) -> int:
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def as_float(value: Any, default: float | None = None) -> float | None:
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "y", "on"):
        return True
    if text in ("false", "0", "no", "n", "off", ""):
        return False
    return default


# --------------------------------------------------------------------------
# 枚举收敛
# --------------------------------------------------------------------------
def normalize_feedback(value: Any, default: str = DEFAULT_FEEDBACK) -> str:
    text = as_clean_text(value).lower()
    if not text or text in ("null", "none"):
        return FEEDBACK_NONE if text in ("null", "none") else default
    return text if text in FEEDBACK_VALUES else default


def normalize_action(value: Any, default: str = DEFAULT_ACTION) -> str:
    text = as_clean_text(value).lower()
    if not text or text == "null":
        return default
    return text if text in ACTION_VALUES else default


def normalize_source(value: Any, default: str = DEFAULT_SOURCE) -> str:
    text = as_clean_text(value).lower()
    if not text:
        return default
    return text if text in SOURCE_VALUES else text


def normalize_subject(value: Any, default: str = DEFAULT_SUBJECT) -> str:
    """科目按文档是枚举，但后端保持宽容：非空就原样保存，空则归为「未分类」。"""
    return as_clean_text(value, default)


# --------------------------------------------------------------------------
# knowledge_points
# --------------------------------------------------------------------------
def parse_json_list(value: Any) -> list[str]:
    """把数组 / JSON 字符串 / 逗号分隔字符串统一成 list[str]。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, dict):
        return []
    text = str(value).strip()
    if not text:
        return []
    if text[0] in "[{":
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
        if isinstance(parsed, dict):
            return []
        if isinstance(parsed, str):
            return [parsed.strip()] if parsed.strip() else []
    parts = re.split(r"[,，、;；]", text)
    return [part.strip() for part in parts if part.strip()]


def dump_json_list(items: Any) -> str:
    """统一落库格式：JSON 数组字符串。"""
    return json.dumps(parse_json_list(items), ensure_ascii=False)


def load_json_list(raw: Any) -> list[str]:
    """从库里读出 JSON 数组字符串，异常时返回空数组。"""
    return parse_json_list(raw)


# --------------------------------------------------------------------------
# 时间
# --------------------------------------------------------------------------
def utcnow() -> datetime:
    """统一使用 naive UTC（SQLite 存储友好），序列化时补 Z。"""
    return datetime.utcnow()


def iso_utc_ms(value: datetime | None) -> str | None:
    """ISO-8601 UTC，带毫秒（同步 v2 的 server_time 用）。"""
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.isoformat(timespec="milliseconds") + "Z"


def parse_client_datetime(value: Any) -> datetime | None:
    """解析客户端传来的原始时间：ISO-8601（带 Z / 偏移）、秒或毫秒时间戳。

    用于「补传的历史记录保留原解题时间」——否则一次批量补传会让 200 多条记录
    的时间戳全挤在同一秒，按时间衰减算薄弱点就失真了。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        stamp = float(value)
        if stamp > 1e11:  # 毫秒
            stamp /= 1000.0
        try:
            return datetime.utcfromtimestamp(stamp)
        except (OSError, ValueError, OverflowError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return parse_client_datetime(int(text))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def iso_utc(value: datetime | None) -> str | None:
    """输出 ISO-8601 UTC 字符串，如 2026-10-31T12:00:00Z。"""
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.isoformat(timespec="seconds") + "Z"
