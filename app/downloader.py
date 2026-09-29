import logging
import re
from typing import Tuple, Optional
from curl_cffi import requests as cffi_requests
import httpx

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,application/pdf,*/*;q=0.8",
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://tuyendung.topcv.vn/",
}

def extract_filename_from_headers(headers: dict) -> Optional[str]:
    content_disp = headers.get("content-disposition", "") or headers.get("Content-Disposition", "")
    if content_disp:
        match = re.search(r'filename\*?=(?:UTF-8\'\')?["\']?([^"\';\r\n]+)', content_disp)
        if match:
            return match.group(1).strip()
    return None

def download_cv(url: str, timeout: int = 30) -> Tuple[bytes, str, Optional[str]]:
    """
    Downloads CV file from TopCV (or ATS URL) bypassing Cloudflare Bot Management.
    Returns: (file_bytes, content_type, filename)
    """
    logger.info(f"Downloading CV from URL: {url[:80]}...")
    
    # 1. Primary method: curl_cffi with Chrome 124 browser impersonation
    try:
        session = cffi_requests.Session()
        res = session.get(
            url,
            impersonate="chrome124",
            headers=DEFAULT_HEADERS,
            timeout=timeout,
            allow_redirects=True
        )
        if res.status_code == 200 and len(res.content) > 0:
            content_type = res.headers.get("content-type", "application/pdf")
            filename = extract_filename_from_headers(dict(res.headers))
            logger.info(f"curl_cffi download success: {len(res.content)} bytes, filename: {filename}")
            return res.content, content_type, filename
        else:
            logger.warning(f"curl_cffi returned status {res.status_code}: {res.content[:200]}")
    except Exception as e:
        logger.error(f"curl_cffi download failed: {e}")

    # 2. Secondary fallback: httpx
    try:
        logger.info("Falling back to httpx for download...")
        with httpx.Client(timeout=timeout, follow_redirects=True, headers=DEFAULT_HEADERS) as client:
            res = client.get(url)
            if res.status_code == 200:
                content_type = res.headers.get("content-type", "application/pdf")
                filename = extract_filename_from_headers(dict(res.headers))
                logger.info(f"httpx download success: {len(res.content)} bytes")
                return res.content, content_type, filename
            else:
                logger.error(f"httpx download failed with status {res.status_code}")
    except Exception as e:
        logger.error(f"httpx download exception: {e}")

    raise RuntimeError(f"Unable to download CV from URL: {url}")
