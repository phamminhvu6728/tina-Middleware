import logging
from typing import Dict, Any
import httpx

try:
    from .config import settings
except ImportError:
    from config import settings

logger = logging.getLogger(__name__)

def forward_to_tina_crm(payload: Dict[str, Any], webhook_url: str = None) -> Dict[str, Any]:
    target_url = webhook_url or settings.TINA_WEBHOOK_URL
    logger.info(f"Forwarding processed payload to Tina CRM: {target_url}")
    
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "TopCV-OCR-Middleware/1.0"
    }

    try:
        with httpx.Client(timeout=settings.FORWARD_TIMEOUT_SECONDS) as client:
            response = client.post(target_url, json=payload, headers=headers)
            logger.info(f"Tina CRM responded with status {response.status_code}")
            return {
                "success": response.is_success,
                "status_code": response.status_code,
                "response_body": response.text[:500]
            }
    except Exception as e:
        logger.error(f"Failed to forward to Tina CRM: {e}")
        return {
            "success": False,
            "error": str(e)
        }
