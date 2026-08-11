import logging
import os
import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, UploadFile

from config import UPLOAD_DIR, DOMAIN
from schemas import FileUploadOut

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/files", tags=["files"])

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
MAX_FILE_SIZE = 10 * 1024 * 1024


@router.post("/upload", response_model=FileUploadOut)
async def upload_file(file: UploadFile):
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="不支持的文件类型")

    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="文件大小超过 10MB")

    date_dir = datetime.utcnow().strftime("%Y%m")
    target_dir = UPLOAD_DIR / date_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{uuid.uuid4().hex}{ext}"
    target_path = target_dir / filename
    with open(target_path, "wb") as f:
        f.write(content)

    rel_path = f"/uploads/{date_dir}/{filename}"
    url = f"{DOMAIN}{rel_path}"
    logger.info("文件上传: %s", rel_path)
    return FileUploadOut(path=rel_path, url=url)
