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

# ---- Web ----------------------------------------------------------------
# 客户端不带自定义 Header，故默认放开 CORS；生产环境可改成具体来源。
CORS_ORIGINS = [o.strip() for o in _env("STDEEL_CORS_ORIGINS", "*").split(",") if o.strip()]

LOG_LEVEL = _env("LOG_LEVEL", "INFO").upper()
