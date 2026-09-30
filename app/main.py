import json
import logging
import re
from typing import Any, Dict, Optional
from urllib.parse import quote

from fastapi import (BackgroundTasks, FastAPI, File, HTTPException, Query,
                     Request, Response, UploadFile)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

try:
    from .config import settings
    from .downloader import download_cv
    from .extractor import extract_text, get_paddle_ocr, check_tesseract_available
    from .forwarder import forward_to_tina_crm
    from .store import TempCvStore
except ImportError:
    from config import settings
    from downloader import download_cv
    from extractor import extract_text, get_paddle_ocr, check_tesseract_available
    from forwarder import forward_to_tina_crm
    from store import TempCvStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("topcv-middleware")

app = FastAPI(
    title="TopCV & OCR Middleware for Tina CRM",
    description="Standalone microservice for downloading protected CVs from TopCV, OCR/text extraction, and forwarding to Tina CRM.",
    version="1.1.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_incoming_requests(request: Request, call_next):
    """
    Nginx in front of this service may rewrite, prefix or redirect the incoming
    path, so the raw path is logged to make the routing visible.
    """
    logger.info(f"===> INCOMING [{request.method}] path='{request.url.path}' "
                f"query='{request.url.query}' host='{request.headers.get('host', '')}'")
    response = await call_next(request)
    return response


cv_store = TempCvStore(
    root=settings.TMP_CV_DIR,
    ttl_seconds=settings.CV_LINK_TTL_SECONDS,
)
cv_store.start_sweeper()

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")
_CV_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_IMAGE_MAGIC = (b"\xff\xd8\xff", b"\x89PNG", b"GIF8", b"BM", b"II*\x00", b"MM\x00*")
_HTML_MARKERS = (b"<!doctype html", b"<html", b"<HTML")


class TopCVWebhookPayload(BaseModel):
    candidate_name: Optional[str] = Field(None, description="Tên ứng viên")
    candidate_email: Optional[str] = Field(None, description="Email ứng viên")
    candidate_phone: Optional[str] = Field(None, description="Số điện thoại")
    job_id: Optional[str] = Field(None, description="ID tin tuyển dụng trên TopCV")
    job_title: Optional[str] = Field(None, description="Tên vị trí tuyển dụng")
    apply_at: Optional[str] = Field(None, description="Thời gian ứng tuyển")
    download_url: Optional[str] = Field(None, description="Link onetime-download của TopCV")
    url: Optional[str] = None
    cv_download_url: Optional[str] = None
    cv_url: Optional[str] = None
    cv_file: Optional[Dict[str, Any]] = None
    data: Optional[Dict[str, Any]] = None
    pm_email: Optional[str] = Field(None, description="Email người phụ trách")
    source: Optional[str] = Field("TOPCV", description="Nguồn ứng viên")
    cv_text: Optional[str] = Field(None, description="Text CV có sẵn")

    class Config:
        extra = "allow"


def _content_disposition(filename: str) -> str:
    ascii_name = filename.encode("ascii", "replace").decode("ascii").replace('"', "")
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(filename)}'


def _resolve_range(range_header: str, size: int):
    match = _RANGE_RE.fullmatch(range_header.strip())
    if not match:
        return None

    raw_start, raw_end = match.group(1), match.group(2)
    if not raw_start and not raw_end:
        return None

    if raw_start:
        start = int(raw_start)
        end = int(raw_end) if raw_end else size - 1
    else:
        length = int(raw_end)
        start = max(0, size - length)
        end = size - 1

    end = min(end, size - 1)
    if start > end or start >= size:
        return None
    return start, end


def _reject_soft_blocked(file_bytes: bytes, content_type: str, filename: Optional[str]):
    """
    Cloudflare serves a JS challenge page with HTTP 200, which would otherwise be
    cached and forwarded to Tina CRM as if it were the CV.
    """
    if not file_bytes:
        raise RuntimeError("Empty response body from download_url")

    head = file_bytes.lstrip()[:512]
    lowered = head.lower()
    if any(marker in lowered for marker in _HTML_MARKERS):
        raise RuntimeError(
            "download_url returned an HTML page instead of a CV file "
            "(likely a Cloudflare challenge, not a valid file)"
        )

    looks_binary = file_bytes.lstrip().startswith(b"%PDF") or head.startswith(_IMAGE_MAGIC)
    if not looks_binary:
        logger.warning(
            f"Unexpected content-type={content_type!r} and no PDF/image magic bytes; "
            "treating as plain text"
        )
    return file_bytes, content_type, filename


def _safe_filename(filename: Optional[str]) -> str:
    if not filename:
        return "cv.pdf"
    cleaned = re.sub(r"[\r\n\x00]", "", str(filename)).strip().strip('"')
    cleaned = cleaned.replace("\\", "/").split("/")[-1]
    if not cleaned:
        return "cv.pdf"
    if not cleaned.lower().endswith((".pdf", ".doc", ".docx", ".png", ".jpg", ".jpeg")):
        cleaned += ".pdf"
    return cleaned[:120]


def _expires_iso(ttl_seconds: int) -> str:
    from datetime import datetime, timedelta, timezone
    tz = timezone(timedelta(hours=7))
    return (datetime.now(tz) + timedelta(seconds=ttl_seconds)).isoformat()


def _serve_cv(cv_id: str, request: Request):
    found = cv_store.load(cv_id)
    if not found:
        raise HTTPException(status_code=404, detail="CV link expired or does not exist")

    data_path, record = found
    size = int(record.get("size", data_path.stat().st_size))
    filename = record.get("filename", "cv.pdf")
    content_type = record.get("content_type", "application/pdf")
    if "text/" in content_type and filename.lower().endswith(".pdf"):
        content_type = "application/pdf"

    base_headers = {
        "Content-Disposition": _content_disposition(filename),
        "Cache-Control": "private, max-age=60",
        "Accept-Ranges": "bytes",
    }

    range_header = request.headers.get("range")
    if range_header:
        resolved = _resolve_range(range_header, size)
        if resolved is None:
            return Response(
                status_code=416,
                headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
            )
        start, end = resolved
        with open(data_path, "rb") as fh:
            fh.seek(start)
            chunk = fh.read(end - start + 1)
        headers = dict(base_headers)
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        return Response(content=chunk, status_code=206, media_type=content_type,
                        headers=headers)

    return FileResponse(path=str(data_path), media_type=content_type, headers=base_headers)


def process_and_forward(payload_dict: Dict[str, Any], webhook_url: Optional[str] = None) -> Dict[str, Any]:
    candidate_name = (
        payload_dict.get("candidate_name") or
        payload_dict.get("name") or
        payload_dict.get("fullName") or
        (payload_dict.get("data") or {}).get("candidate_name") or
        "Ứng viên"
    )
    candidate_email = (
        payload_dict.get("candidate_email") or
        payload_dict.get("email") or
        (payload_dict.get("data") or {}).get("candidate_email") or
        ""
    )
    candidate_phone = (
        payload_dict.get("candidate_phone") or
        payload_dict.get("phone") or
        (payload_dict.get("data") or {}).get("candidate_phone") or
        ""
    )
    job_id = (
        payload_dict.get("job_id") or
        payload_dict.get("jobId") or
        (payload_dict.get("data") or {}).get("job_id") or
        ""
    )
    job_title = (
        payload_dict.get("job_title") or
        payload_dict.get("jobTitle") or
        (payload_dict.get("data") or {}).get("job_title") or
        "Vị trí tuyển dụng"
    )
    apply_at = (
        payload_dict.get("apply_at") or
        payload_dict.get("applied_at") or
        (payload_dict.get("data") or {}).get("apply_at") or
        ""
    )
    pm_email = (
        payload_dict.get("pm_email") or
        payload_dict.get("hr_email") or
        (payload_dict.get("data") or {}).get("pm_email") or
        settings.DEFAULT_PM_EMAIL
    )

    download_url = (
        payload_dict.get("download_url") or
        payload_dict.get("url") or
        payload_dict.get("cv_download_url") or
        payload_dict.get("cv_url") or
        (payload_dict.get("cv_file") or {}).get("url") or
        (payload_dict.get("data") or {}).get("download_url") or
        ""
    )

    cv_text = payload_dict.get("cv_text") or (payload_dict.get("data") or {}).get("cv_text") or ""
    extraction_method = "direct_input"
    cv_file_url = ""
    cv_filename = ""
    cv_size = 0
    cv_link_expires_at = ""

    if not cv_text and download_url:
        try:
            file_bytes, content_type, filename = download_cv(download_url, timeout=settings.DOWNLOAD_TIMEOUT_SECONDS)
            file_bytes, content_type, filename = _reject_soft_blocked(file_bytes, content_type, filename)
            cv_text, extraction_method = extract_text(file_bytes, content_type)
            logger.info(f"Extracted {len(cv_text)} chars from {filename or 'CV'} using {extraction_method}")

            safe_name = _safe_filename(filename)
            cv_id = cv_store.save(
                file_bytes,
                filename=safe_name,
                content_type=content_type,
                meta={
                    "candidate_name": candidate_name,
                    "candidate_email": candidate_email,
                    "job_title": job_title,
                    "source": payload_dict.get("source", "TOPCV"),
                },
            )
            cv_filename = safe_name
            cv_size = len(file_bytes)
            cv_link_expires_at = _expires_iso(cv_store.ttl_seconds)
            cv_file_url = f"{settings.PUBLIC_BASE_URL.rstrip('/')}/cv/{cv_id}"
            logger.info(f"Cached CV as {cv_id}, link expires at {cv_link_expires_at}")
        except Exception as e:
            logger.error(f"Failed to download/extract CV: {e}")
            cv_text = f"Lỗi tải hoặc bóc tách CV từ download_url: {e}"
            extraction_method = "error"

    tina_payload = {
        "candidate_name": candidate_name,
        "candidate_email": candidate_email,
        "candidate_phone": candidate_phone,
        "job_id": job_id,
        "job_title": job_title,
        "apply_at": apply_at,
        "download_url": download_url,
        "pm_email": pm_email,
        "cv_text": cv_text,
        "cv_file_url": cv_file_url,
        "cv_filename": cv_filename,
        "cv_size": cv_size,
        "cv_link_expires_at": cv_link_expires_at,
        "source": payload_dict.get("source", "TOPCV"),
        "ocr_method": extraction_method
    }

    forward_result = forward_to_tina_crm(tina_payload, webhook_url=webhook_url)

    return {
        "status": "completed",
        "candidate_name": candidate_name,
        "candidate_email": candidate_email,
        "job_title": job_title,
        "cv_text_length": len(cv_text),
        "cv_file_url": cv_file_url,
        "extraction_method": extraction_method,
        "forward_result": forward_result
    }


def _is_ping() -> dict:
    return {"status": "ok", "message": "TopCV Webhook Endpoint Ready"}


@app.get("/health")
def health_check():
    return {
        "status": "healthy",
        "service": "topcv-ocr-middleware",
        "tina_webhook_target": settings.TINA_WEBHOOK_URL,
        "cv_link": {
            "public_base_url": settings.PUBLIC_BASE_URL,
            "ttl_seconds": settings.CV_LINK_TTL_SECONDS,
            "cache_dir": settings.TMP_CV_DIR
        },
        "ocr_engines": {
            "paddleocr": get_paddle_ocr() is not None,
            "tesseract": check_tesseract_available()
        }
    }


@app.get("/cv/{cv_id}")
def get_cv(cv_id: str, request: Request):
    return _serve_cv(cv_id, request)


@app.post("/cv/{cv_id}/release", status_code=204)
def release_cv(cv_id: str):
    cv_store.delete(cv_id)
    return Response(status_code=204)


@app.get("/webhook/topcv")
async def topcv_webhook_ping():
    return _is_ping()


@app.post("/webhook/topcv")
async def topcv_webhook(
    payload: Optional[TopCVWebhookPayload] = None,
    sync: bool = Query(True, description="Đồng bộ (True) hoặc Bất đồng bộ nền (False)"),
    background_tasks: BackgroundTasks = None
):
    if not payload:
        return {"status": "ok", "message": "Ping received"}

    data = payload.model_dump(exclude_none=True)
    logger.info(f"Received TopCV webhook for candidate: {data.get('candidate_name', 'Unknown')}")

    if not sync:
        background_tasks.add_task(process_and_forward, data)
        return {
            "status": "queued",
            "message": "Candidate CV processing started in background",
            "candidate_name": data.get("candidate_name")
        }

    return process_and_forward(data)


@app.post("/extract-url")
def extract_from_url(url: str = Query(..., description="URL file CV")):
    try:
        file_bytes, content_type, filename = download_cv(url)
        text, method = extract_text(file_bytes, content_type)
        return {
            "status": "success",
            "filename": filename,
            "content_type": content_type,
            "char_count": len(text),
            "method": method,
            "text_preview": text[:500],
            "full_text": text
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/extract-file")
async def extract_from_file(file: UploadFile = File(...)):
    file_bytes = await file.read()
    text, method = extract_text(file_bytes, file.content_type)
    return {
        "filename": file.filename,
        "content_type": file.content_type,
        "char_count": len(text),
        "method": method,
        "text_preview": text[:500],
        "full_text": text
    }


@app.api_route("/{full_path:path}", methods=["GET", "POST"])
async def catch_all(full_path: str, request: Request, background_tasks: BackgroundTasks):
    """
    Catch-all registered last on purpose.

    Nginx in front of this service may redirect or rewrite the path (observed:
    /webhook/topcv returning 301 to another host, and the request landing under an
    unexpected prefix). Guessing the path is unreliable, so any path that does not
    match a real route is treated as a TopCV webhook. The raw path is logged above
    so the actual routing can be read off the logs.
    """
    segments = [s for s in full_path.split("/") if s]
    if segments and _CV_ID_RE.match(segments[-1]) and cv_store.load(segments[-1]):
        logger.info(f"Serving cached CV {segments[-1]} via prefix '/{full_path}'")
        return _serve_cv(segments[-1], request)

    if request.method == "GET":
        return _is_ping()

    raw = await request.body()
    if not raw.strip():
        return {"status": "ok", "message": "Ping received"}

    try:
        data = json.loads(raw)
    except Exception as e:
        logger.error(f"Unparseable body on '/{full_path}': {e}")
        raise HTTPException(status_code=400, detail=f"Body is not valid JSON: {e}")

    if not isinstance(data, dict):
        raise HTTPException(status_code=400,
                            detail="Body must be a JSON object")

    logger.info(f"Treating '/{full_path}' as TopCV webhook, "
                f"keys={sorted(data.keys())[:12]}")

    sync = request.query_params.get("sync", "true").lower()
    if sync in ("false", "0", "no"):
        background_tasks.add_task(process_and_forward, data)
        return {
            "status": "queued",
            "message": "Candidate CV processing started in background",
            "candidate_name": data.get("candidate_name")
        }

    return process_and_forward(data)
