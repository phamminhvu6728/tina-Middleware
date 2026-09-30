import os
from pydantic import BaseModel

class Settings(BaseModel):
    PORT: int = int(os.getenv("PORT", "8000"))
    HOST: str = os.getenv("HOST", "0.0.0.0")
    TINA_WEBHOOK_URL: str = os.getenv(
        "TINA_WEBHOOK_URL",
        "http://localhost:3000/webhooks/workflows/5b8996b9-379d-4af4-87ab-f9215a4ebb32/85b47d46-39c1-4db7-8a97-2dd8634ef000"
    )
    DEFAULT_PM_EMAIL: str = os.getenv("DEFAULT_PM_EMAIL", "tuyendung@tinasoft.vn")
    DOWNLOAD_TIMEOUT_SECONDS: int = int(os.getenv("DOWNLOAD_TIMEOUT_SECONDS", "30"))
    FORWARD_TIMEOUT_SECONDS: int = int(os.getenv("FORWARD_TIMEOUT_SECONDS", "30"))
    PUBLIC_BASE_URL: str = os.getenv("PUBLIC_BASE_URL", "")
    TMP_CV_DIR: str = os.getenv("TMP_CV_DIR", "/tmp/tina-middleware-cv")
    CV_LINK_TTL_SECONDS: int = int(os.getenv("CV_LINK_TTL_SECONDS", "3600"))

settings = Settings()
