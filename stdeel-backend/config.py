import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

DATABASE_PATH = BASE_DIR / "app.db"
DATABASE_URL = f"sqlite+aiosqlite:///{DATABASE_PATH}"

UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

DOMAIN = os.getenv("APP_DOMAIN", "http://127.0.0.1:8500")

API_PREFIX = ""

ALLOWED_ORIGINS = ["*"]

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
