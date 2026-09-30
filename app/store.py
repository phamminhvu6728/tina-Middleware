import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

SUFFIX = ".cv"
META_SUFFIX = ".json"


class TempCvStore:
    """
    Short-lived filesystem cache for CV files.

    A file is written once, served via GET /cv/{cv_id}, and deleted by a
    background sweeper once its TTL passes. Nothing is kept long term and no
    database is involved.
    """

    def __init__(self, root: str, ttl_seconds: int, sweep_interval_seconds: int = 60):
        self.root = Path(root)
        self.ttl_seconds = ttl_seconds
        self.sweep_interval_seconds = sweep_interval_seconds
        self.root.mkdir(parents=True, exist_ok=True)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        logger.info(f"TempCvStore ready at {self.root} (ttl={ttl_seconds}s)")

    def save(self, data: bytes, filename: str, content_type: str, meta: Dict[str, Any]) -> str:
        cv_id = secrets.token_hex(16)
        target = self.root / (cv_id + SUFFIX)
        tmp = self.root / (cv_id + SUFFIX + ".part")

        with self._lock:
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, target)

            record = dict(meta)
            record.update({
                "cv_id": cv_id,
                "filename": filename,
                "content_type": content_type,
                "size": len(data),
                "created_at": int(time.time()),
                "expires_at": int(time.time()) + self.ttl_seconds,
            })
            meta_path = self.root / (cv_id + META_SUFFIX)
            meta_tmp = self.root / (cv_id + META_SUFFIX + ".part")
            with open(meta_tmp, "w", encoding="utf-8") as fh:
                fh.write(_json_dumps(record))
            os.replace(meta_tmp, meta_path)

        logger.info(f"cv_id={cv_id} cached {len(data)} bytes, expires in {self.ttl_seconds}s")
        return cv_id

    def load(self, cv_id: str) -> Optional[Tuple[Path, Dict[str, Any]]]:
        if not _is_valid_id(cv_id):
            return None

        meta_path = self.root / (cv_id + META_SUFFIX)
        data_path = self.root / (cv_id + SUFFIX)
        if not meta_path.is_file() or not data_path.is_file():
            return None

        try:
            import json
            with open(meta_path, "r", encoding="utf-8") as fh:
                record = json.load(fh)
        except Exception as e:
            logger.error(f"cv_id={cv_id} meta unreadable: {e}")
            return None

        if int(record.get("expires_at", 0)) < time.time():
            self.delete(cv_id)
            return None

        return data_path, record

    def delete(self, cv_id: str) -> None:
        if not _is_valid_id(cv_id):
            return
        with self._lock:
            for path in (self.root / (cv_id + SUFFIX), self.root / (cv_id + META_SUFFIX)):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                except OSError as e:
                    logger.error(f"cv_id={cv_id} delete failed for {path.name}: {e}")

    def sweep(self) -> int:
        now = time.time()
        removed = 0
        try:
            entries = list(self.root.iterdir())
        except OSError as e:
            logger.error(f"sweep cannot list {self.root}: {e}")
            return 0

        for path in entries:
            if path.suffix != META_SUFFIX:
                continue
            try:
                import json
                with open(path, "r", encoding="utf-8") as fh:
                    record = json.load(fh)
            except Exception:
                path.unlink(missing_ok=True)
                continue

            if now >= int(record.get("expires_at", 0)):
                self.delete(record.get("cv_id", ""))
                removed += 1

        if removed:
            logger.info(f"sweep removed {removed} expired CV file(s)")
        return removed

    def start_sweeper(self) -> None:
        def loop():
            while not self._stop.wait(self.sweep_interval_seconds):
                self.sweep()

        thread = threading.Thread(target=loop, name="cv-cache-sweeper", daemon=True)
        thread.start()

    def stop(self) -> None:
        self._stop.set()


def _is_valid_id(cv_id: str) -> bool:
    return bool(cv_id) and len(cv_id) == 32 and all(c in "0123456789abcdef" for c in cv_id)


def _json_dumps(data: Dict[str, Any]) -> str:
    import json
    return json.dumps(data, ensure_ascii=False)


_store: Optional[TempCvStore] = None


def get_store() -> TempCvStore:
    global _store
    if _store is None:
        from .config import settings
        _store = TempCvStore(
            root=os.getenv("TMP_CV_DIR", "/tmp/tina-middleware-cv"),
            ttl_seconds=int(os.getenv("CV_LINK_TTL_SECONDS", "3600")),
        )
        _store.start_sweeper()
    return _store
