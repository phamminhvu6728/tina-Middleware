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
    Short-lived, single use cache for CV files.

    A file is written once, served via GET /cv/{cv_id}, then removed. Removal
    happens on the first full download, or by the background sweeper when the
    TTL passes. The cache also holds at most `max_files` entries, evicting the
    file closest to expiry when it overflows, so a burst of webhooks can never
    fill the disk. Nothing is kept long term and no database is involved.
    """

    def __init__(self, root: str, ttl_seconds: int, max_files: int = 10,
                 sweep_interval_seconds: int = 10):
        self.root = Path(root)
        self.ttl_seconds = ttl_seconds
        self.max_files = max(1, max_files)
        self.sweep_interval_seconds = sweep_interval_seconds
        self.root.mkdir(parents=True, exist_ok=True)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        logger.info(f"TempCvStore ready at {self.root} "
                    f"(ttl={ttl_seconds}s, max_files={self.max_files})")

    def save(self, data: bytes, filename: str, content_type: str, meta: Dict[str, Any]) -> str:
        cv_id = secrets.token_hex(16)
        target = self.root / (cv_id + SUFFIX)
        tmp = self.root / (cv_id + SUFFIX + ".part")

        with self._lock:
            self._evict_overflow_locked(1)

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

        logger.info(f"cv_id={cv_id} cached {len(data)} bytes, "
                    f"expires in {self.ttl_seconds}s, {self.count()}/{self.max_files} in use")
        return cv_id

    def load(self, cv_id: str, consume: bool = False) -> Optional[Tuple[bytes, Dict[str, Any]]]:
        """
        Return (bytes, record) for a live CV, or None when it is missing or expired.

        With consume=True the entry is deleted under the same lock that verifies
        it, so two concurrent downloads of the same link can never both win.
        """
        if not _is_valid_id(cv_id):
            return None

        with self._lock:
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
                self._delete_locked(cv_id)
                return None

            try:
                data = data_path.read_bytes()
            except OSError as e:
                logger.error(f"cv_id={cv_id} read failed: {e}")
                return None

            if consume:
                self._delete_locked(cv_id)
                logger.info(f"cv_id={cv_id} served and consumed, "
                            f"{self._count_locked()}/{self.max_files} in use")

            return data, record

    def delete(self, cv_id: str) -> None:
        if not _is_valid_id(cv_id):
            return
        with self._lock:
            self._delete_locked(cv_id)

    def exists(self, cv_id: str) -> bool:
        if not _is_valid_id(cv_id):
            return False
        with self._lock:
            meta_path = self.root / (cv_id + META_SUFFIX)
            data_path = self.root / (cv_id + SUFFIX)
            if not meta_path.is_file() or not data_path.is_file():
                return False
            try:
                import json
                with open(meta_path, "r", encoding="utf-8") as fh:
                    record = json.load(fh)
            except Exception:
                return False
            if int(record.get("expires_at", 0)) < time.time():
                self._delete_locked(cv_id)
                return False
            return True

    def count(self) -> int:
        with self._lock:
            return self._count_locked()

    def _delete_locked(self, cv_id: str) -> None:
        for path in (self.root / (cv_id + SUFFIX), self.root / (cv_id + META_SUFFIX)):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                logger.error(f"cv_id={cv_id} delete failed for {path.name}: {e}")

    def _live_records_locked(self) -> Dict[str, int]:
        """cv_id -> expires_at for every entry that is still on disk."""
        import json
        records: Dict[str, int] = {}
        try:
            entries = list(self.root.iterdir())
        except OSError as e:
            logger.error(f"cannot list {self.root}: {e}")
            return records

        for path in entries:
            if path.suffix != META_SUFFIX:
                continue
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    record = json.load(fh)
                records[record.get("cv_id", path.stem)] = int(record.get("expires_at", 0))
            except Exception:
                path.unlink(missing_ok=True)
        return records

    def _count_locked(self) -> int:
        return len(self._live_records_locked())

    def _evict_overflow_locked(self, incoming: int) -> None:
        """
        Drop the entries closest to expiry so the cache never holds more than
        max_files. Called before writing, with the lock already held.
        """
        records = self._live_records_locked()
        overflow = len(records) + incoming - self.max_files
        if overflow <= 0:
            return

        for cv_id, _expires in sorted(records.items(), key=lambda kv: kv[1])[:overflow]:
            logger.warning(f"cv_id={cv_id} evicted to keep the cache at "
                           f"{self.max_files} file(s); its download link stops working")
            self._delete_locked(cv_id)

    def sweep(self) -> int:
        now = time.time()
        removed = 0
        with self._lock:
            for cv_id, expires_at in self._live_records_locked().items():
                if now >= expires_at:
                    self._delete_locked(cv_id)
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
            root=settings.TMP_CV_DIR,
            ttl_seconds=settings.CV_LINK_TTL_SECONDS,
            max_files=settings.CV_MAX_CACHED_FILES,
        )
        _store.start_sweeper()
    return _store
