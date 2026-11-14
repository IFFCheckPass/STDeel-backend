"""文件上传（当前客户端未使用，保留供图片题扩展）。"""

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, UploadFile

from config import ALLOWED_UPLOAD_EXTENSIONS, MAX_UPLOAD_BYTES, UPLOAD_DIR
from schemas import FileUploadOut

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/files", tags=["files"])

# 流式落盘的块大小；绝不再把整文件读进内存（2026-11-14 安全加固）
CHUNK_BYTES = 1024 * 1024


def _safe_host(request: Request) -> str:
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    host = str(host or "").split(",")[0].strip()
    # 只允许 host[:port] 形态，防止把伪造头原样拼进返回 URL
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-:[]")
    if not host or any(ch not in allowed for ch in host):
        return request.url.netloc
    return host


@router.post("/upload", response_model=FileUploadOut)
async def upload_file(file: UploadFile, request: Request):
    suffix = ("." + file.filename.rsplit(".", 1)[-1].lower()) if file.filename and "." in file.filename else ""
    if suffix not in ALLOWED_UPLOAD_EXTENSIONS:
        raise HTTPException(status_code=400, detail="不支持的文件类型")

    date_dir = datetime.utcnow().strftime("%Y%m")
    target_dir = UPLOAD_DIR / date_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{uuid.uuid4().hex}{suffix}"
    target = target_dir / filename

    total = 0
    try:
        with target.open("wb") as handle:
            while True:
                chunk = await file.read(CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=400,
                        detail=f"文件大小超过 {MAX_UPLOAD_BYTES // 1024 // 1024}MB",
                    )
                handle.write(chunk)
    except BaseException:
        # 任何失败（含超限）都不留半截文件
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    rel_path = f"/uploads/{date_dir}/{filename}"
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = _safe_host(request)
    logger.info("文件上传: %s (%d bytes)", rel_path, total)
    return FileUploadOut(path=rel_path, url=f"{scheme}://{host}{rel_path}")
