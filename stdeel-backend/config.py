"""集中配置。

所有可变项都可以用环境变量覆盖，方便本机调试、验收测试和线上部署使用
同一份代码而互不干扰。
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _env(name: str, default: str = "") -> str:
    """读取环境变量，空字符串视为未设置。"""
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


APP_NAME = "思谛 STDeel 后端服务"
APP_VERSION = "2.0.0"

# ---- 数据库 --------------------------------------------------------------
# 默认落在项目目录下的 app.db；测试可用 STDEEL_DB_PATH 指到临时文件。
DATABASE_PATH = Path(_env("STDEEL_DB_PATH", str(BASE_DIR / "app.db")))
DATABASE_URL = _env("STDEEL_DATABASE_URL", f"sqlite+aiosqlite:///{DATABASE_PATH}")

# ---- 文件上传 ------------------------------------------------------------
UPLOAD_DIR = Path(_env("STDEEL_UPLOAD_DIR", str(BASE_DIR / "uploads")))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_BYTES = int(_env("STDEEL_MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
ALLOWED_UPLOAD_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

# ---- 业务参数 ------------------------------------------------------------
# 批量上传缺少 local_id 时，用「内容指纹 + 时间窗口」兜底去重的时间窗口（秒）。
BATCH_DEDUPE_WINDOW_SECONDS = int(_env("STDEEL_BATCH_DEDUPE_WINDOW", "900"))

# 标准答案模糊匹配的命中阈值（客户端也会再判一次 >= 0.85）。
MATCH_THRESHOLD = float(_env("STDEEL_MATCH_THRESHOLD", "0.85"))
# 参与相似度计算的候选上限，避免大题库时接口变慢。
MATCH_MAX_CANDIDATES = int(_env("STDEEL_MATCH_MAX_CANDIDATES", "200"))

# ---- 管理台鉴权 ----------------------------------------------------------
# 管理密码只以「加盐 PBKDF2-SHA256」哈希保存，不落明文。默认文件在项目目录下，
# 已 gitignore 且部署脚本会保留（chmod 600），也可用环境变量指到别处。
# 未配置时 /webui、/docs、/redoc 与管理端 JSON 接口一律拒绝访问（fail-closed）。
ADMIN_AUTH_FILE = Path(_env("STDEEL_ADMIN_AUTH_FILE", str(BASE_DIR / "admin_auth.json")))
# 会话 Cookie：改密码即失效，无需手动踢人
ADMIN_COOKIE_NAME = _env("STDEEL_ADMIN_COOKIE", "stdeel_admin")
ADMIN_SESSION_TTL_HOURS = float(_env("STDEEL_ADMIN_SESSION_TTL", "12"))
ADMIN_SESSION_TTL_SECONDS = int(ADMIN_SESSION_TTL_HOURS * 3600)
# 登录失败限流：窗口内失败 N 次即锁定（秒）。单进程内存计数，重启清零。
ADMIN_LOGIN_MAX_FAILURES = int(_env("STDEEL_ADMIN_LOGIN_MAX_FAILURES", "5"))
ADMIN_LOGIN_LOCK_SECONDS = int(_env("STDEEL_ADMIN_LOGIN_LOCK", "900"))

# ---- API Key 加密 --------------------------------------------------------
# api_keys 列只存密文（前缀/后缀各 3 位单独存，用于展示）；主密钥 32 字节 hex，
# 默认在项目目录 api_key.key（0600、已 gitignore），也可用 STDEEL_API_KEY_SECRET（64 位 hex）覆盖。
# 注意：丢失主密钥 = 已保存的用户 API Key 无法解密，必须与数据库一起备份。
API_KEY_SECRET_FILE = Path(_env("STDEEL_API_KEY_SECRET_FILE", str(BASE_DIR / "api_key.key")))

# ---- 请求体 / 限流 / API Key 同步开关（2026-11-14 安全加固） ---------------
# 请求体上限：普通接口 2MB；上传接口 = 单文件上限 + 1MB multipart 余量。
MAX_BODY_BYTES = int(_env("STDEEL_MAX_BODY_BYTES", str(2 * 1024 * 1024)))
MAX_BODY_BYTES_UPLOAD = int(_env("STDEEL_MAX_BODY_BYTES_UPLOAD", str(MAX_UPLOAD_BYTES + 1024 * 1024)))
# 按来源 IP 的滑动窗口限流（单 worker 内存计数；0/负数视为关闭）。
RATE_LIMIT_ENABLED = _env("STDEEL_RATE_LIMIT", "1") != "0"
RATE_LIMIT_PER_MINUTE = int(_env("STDEEL_RATE_LIMIT_PER_MIN", "240"))
RATE_LIMIT_API_KEY_PER_MINUTE = int(_env("STDEEL_RATE_LIMIT_API_KEY_PER_MIN", "30"))
# 换机同步 API Key 的总开关：设 0 可临时关闭（客户端将收到 403），用于 App 升级窗口期。
API_KEY_SYNC_ENABLED = _env("STDEEL_API_KEY_SYNC", "1") != "0"

# ---- Web ----------------------------------------------------------------
# 客户端不带自定义 Header，故默认放开 CORS；生产环境可改成具体来源。
CORS_ORIGINS = [o.strip() for o in _env("STDEEL_CORS_ORIGINS", "*").split(",") if o.strip()]

LOG_LEVEL = _env("LOG_LEVEL", "INFO").upper()
