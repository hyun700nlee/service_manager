from __future__ import annotations

import logging
import logging.handlers
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from credentials import SecretRedactor
from storage import Repository


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z가-힣._-]+", "_", value).strip("._")
    return cleaned[:80] or "unnamed"


class EventLogger:
    def __init__(
        self,
        repository: Repository,
        *,
        log_directory: str | Path | None = None,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
        retention_days: int = 30,
        max_total_bytes: int = 1024 * 1024 * 1024,
    ):
        self.repository = repository
        self.log_directory = Path(log_directory or repository.data_dir / "logs")
        self.log_directory.mkdir(parents=True, exist_ok=True)
        self.event_callback = event_callback
        self.retention_days = retention_days
        self.max_total_bytes = max_total_bytes
        self.redactor = SecretRedactor()
        self._handlers: dict[tuple[str, str, str], logging.Handler] = {}
        self._lock = threading.RLock()
        self._closed = False

    def register_secret(self, value: str | None) -> None:
        self.redactor.register(value)

    def _handler(self, source_type: str, source_id: str | None, source_name: str, stream: str) -> logging.Handler:
        key = (source_type, source_id or source_name, stream)
        with self._lock:
            existing = self._handlers.get(key)
            if existing:
                return existing
            directory = self.log_directory / _safe_name(source_id) if source_id else self.log_directory / source_type
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{_safe_name(stream)}.log"
            handler = logging.handlers.RotatingFileHandler(
                path, maxBytes=10 * 1024 * 1024, backupCount=10, encoding="utf-8", errors="replace"
            )
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            self._handlers[key] = handler
            return handler

    def emit(
        self,
        level: str,
        source_type: str,
        source_name: str,
        event_type: str,
        message: str,
        *,
        source_id: str | None = None,
        details: dict[str, Any] | None = None,
        stream: str = "system",
    ) -> int | None:
        if self._closed:
            return None
        safe_message = self.redactor.redact(message)
        safe_details = {key: self.redactor.redact(str(value)) for key, value in (details or {}).items()}
        event_id: int | None = None
        try:
            event_id = self.repository.add_event(
                level.upper(), source_type, source_name, event_type, safe_message,
                source_id=source_id, details=safe_details,
            )
        except (OSError, ValueError):
            pass
        with self._lock:
            if self._closed:
                return event_id
            try:
                record = logging.LogRecord(
                    name="service-manager", level=getattr(logging, level.upper(), logging.INFO),
                    pathname="", lineno=0, msg=f"[{event_type}] [{stream}] {safe_message}", args=(), exc_info=None,
                )
                self._handler(source_type, source_id, source_name, stream).emit(record)
            except (OSError, ValueError):
                pass
        payload = {
            "id": event_id, "timestamp": datetime.now().isoformat(), "level": level.upper(),
            "source_type": source_type, "source_id": source_id, "source_name": source_name,
            "event_type": event_type, "message": safe_message, "details": safe_details, "stream": stream,
        }
        if self.event_callback:
            try:
                self.event_callback(payload)
            except Exception:
                pass
        return event_id

    def cleanup(self) -> None:
        cutoff = datetime.now() - timedelta(days=self.retention_days)
        files = [item for item in self.log_directory.rglob("*.log*") if item.is_file()]
        for path in files:
            try:
                if datetime.fromtimestamp(path.stat().st_mtime) < cutoff:
                    path.unlink()
            except OSError:
                continue
        files = sorted(
            [item for item in self.log_directory.rglob("*.log*") if item.is_file()],
            key=lambda item: item.stat().st_mtime,
        )
        total = sum(item.stat().st_size for item in files)
        for path in files:
            if total <= self.max_total_bytes:
                break
            try:
                size = path.stat().st_size
                path.unlink()
                total -= size
            except OSError:
                continue

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for handler in self._handlers.values():
                handler.close()
            self._handlers.clear()
