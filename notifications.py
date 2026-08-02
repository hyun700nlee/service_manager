from __future__ import annotations

import json
import queue
import smtplib
import threading
import time
import urllib.request
from email.message import EmailMessage
from typing import Any

from storage import Repository


class NotificationDispatcher:
    """Non-blocking SMTP/webhook notifications with per-event deduplication."""

    def __init__(self, repository: Repository):
        self.repository = repository
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._last_sent: dict[tuple[str | None, str], float] = {}
        self._thread = threading.Thread(target=self._worker, name="notifications", daemon=True)
        self._thread.start()

    def handle_event(self, event: dict[str, Any]) -> None:
        settings = self.repository.get_setting("notifications", {})
        if not settings.get("enabled"):
            return
        event_type = str(event.get("event_type", ""))
        if event.get("level") not in {"ERROR", "CRITICAL"} and event_type not in {"recovered", "job_completed"}:
            return
        key = (event.get("source_id"), event_type)
        now = time.monotonic()
        dedupe = float(settings.get("dedupe_seconds", 300))
        if now - self._last_sent.get(key, 0) < dedupe:
            return
        self._last_sent[key] = now
        self._queue.put(dict(event))

    def _worker(self) -> None:
        while True:
            event = self._queue.get()
            if event is None:
                return
            settings = self.repository.get_setting("notifications", {})
            try:
                if settings.get("webhook_credential_id"):
                    self._send_webhook(settings, event)
                if settings.get("smtp_host") and settings.get("recipients"):
                    self._send_email(settings, event)
            except Exception as exc:
                try:
                    self.repository.add_event("ERROR", "system", "알림", "notification_failed", str(exc))
                except Exception:
                    pass

    def _send_webhook(self, settings: dict[str, Any], event: dict[str, Any]) -> None:
        url = self.repository.get_credential(settings.get("webhook_credential_id"))
        if not url:
            return
        payload = json.dumps({"product": "Python Service Manager", **event}, ensure_ascii=False, default=str).encode("utf-8")
        request = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json", "User-Agent": "PythonServiceManager/1"}, method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status >= 400:
                raise RuntimeError(f"Webhook 응답 오류: {response.status}")

    def _send_email(self, settings: dict[str, Any], event: dict[str, Any]) -> None:
        message = EmailMessage()
        message["Subject"] = f"[Service Manager] {event.get('level')} - {event.get('source_name')}"
        message["From"] = settings.get("sender") or settings.get("smtp_user")
        message["To"] = ", ".join(settings.get("recipients", []))
        message.set_content(
            f"시각: {event.get('timestamp')}\n대상: {event.get('source_name')}\n"
            f"이벤트: {event.get('event_type')}\n내용: {event.get('message')}\n"
        )
        password = self.repository.get_credential(settings.get("smtp_credential_id"))
        with smtplib.SMTP(settings["smtp_host"], int(settings.get("smtp_port", 587)), timeout=15) as smtp:
            if settings.get("smtp_starttls", True):
                smtp.starttls()
            if settings.get("smtp_user"):
                smtp.login(settings["smtp_user"], password or "")
            smtp.send_message(message)

    def shutdown(self) -> None:
        self._queue.put(None)
