import logging
from typing import Optional, Dict, Any
from fastapi import FastAPI, BackgroundTasks, UploadFile, File, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

try:
    from .config import settings
    from .downloader import download_cv
    from .extractor import extract_text, get_paddle_ocr, check_tesseract_available
    from .forwarder import forward_to_tina_crm
except ImportError:
    from config import settings
    from downloader import download_cv
    from extractor import extract_text, get_paddle_ocr, check_tesseract_available
    from forwarder import forward_to_tina_crm

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("topcv-middleware")

app = FastAPI(
    title="TopCV & OCR Middleware for Tina CRM",
    description="Standalone microservice for downloading protected CVs from TopCV, OCR/text extraction, and forwarding to Tina CRM.",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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

    if not cv_text and download_url:
        try:
            file_bytes, content_type, filename = download_cv(download_url, timeout=settings.DOWNLOAD_TIMEOUT_SECONDS)
            cv_text, extraction_method = extract_text(file_bytes, content_type)
            logger.info(f"Extracted {len(cv_text)} chars from {filename or 'CV'} using {extraction_method}")
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
        "extraction_method": extraction_method,
        "forward_result": forward_result
    }

@app.get("/health")
def health_check():
    return {
        "status": "healthy",
        "service": "topcv-ocr-middleware",
        "tina_webhook_target": settings.TINA_WEBHOOK_URL,
        "ocr_engines": {
            "paddleocr": get_paddle_ocr() is not None,
            "tesseract": check_tesseract_available()
        }
    }

@app.post("/webhook/topcv")
async def topcv_webhook(
    payload: TopCVWebhookPayload,
    sync: bool = Query(True, description="Đồng bộ (True) hoặc Bất đồng bộ nền (False)"),
    background_tasks: BackgroundTasks = None
):
    data = payload.model_dump(exclude_none=True)
    logger.info(f"Received TopCV webhook for candidate: {data.get('candidate_name', 'Unknown')}")

    if not sync:
        background_tasks.add_task(process_and_forward, data)
        return {
            "status": "queued",
            "message": "Candidate CV processing started in background",
            "candidate_name": data.get("candidate_name")
        }

    result = process_and_forward(data)
    return result

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
