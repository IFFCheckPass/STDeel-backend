"""文件上传（当前客户端未使用，保留供图片题扩展）。"""

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, UploadFile

from config import ALLOWED_UPLOAD_EXTENSIONS, MAX_UPLOAD_BYTES, UPLOAD_DIR
from schemas import FileUploadOut

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/files", tags=["files"])


@router.post("/upload", response_model=FileUploadOut)
async def upload_file(file: UploadFile, request: Request):
    suffix = ("." + file.filename.rsplit(".", 1)[-1].lower()) if file.filename and "." in file.filename else ""
    if suffix not in ALLOWED_UPLOAD_EXTENSIONS:
        raise HTTPException(status_code=400, detail="不支持的文件类型")

    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail=f"文件大小超过 {MAX_UPLOAD_BYTES // 1024 // 1024}MB")

    date_dir = datetime.utcnow().strftime("%Y%m")
    target_dir = UPLOAD_DIR / date_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{uuid.uuid4().hex}{suffix}"
    (target_dir / filename).write_bytes(content)

    rel_path = f"/uploads/{date_dir}/{filename}"
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    logger.info("文件上传: %s", rel_path)
    return FileUploadOut(path=rel_path, url=f"{scheme}://{host}{rel_path}")
