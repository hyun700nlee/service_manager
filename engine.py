from __future__ import annotations

import csv
import shutil
import sys
import threading
from pathlib import Path
from typing import Any

from event_logging import EventLogger
from models import RemoteJobDefinition, ServiceDefinition
from notifications import NotificationDispatcher
from remote_jobs import RemoteJobManager, fetch_ssh_fingerprint
from scheduler import EngineScheduler
from storage import Repository
from supervisor import ServiceSupervisor


class ServiceManagerEngine:
    def __init__(self, *, data_dir: str | Path | None = None, config_path: str | Path | None = None):
        self.repository = Repository(data_dir=data_dir, config_path=config_path)
        self.logger = EventLogger(self.repository)
        self.notifications = NotificationDispatcher(self.repository)
        self.logger.event_callback = self.notifications.handle_event
        for warning in self.repository.startup_warnings:
            self.logger.emit("WARNING", "system", "시스템", "storage_recovered", warning)
        self.supervisor = ServiceSupervisor(self.repository, self.logger)
        self.remote_jobs = RemoteJobManager(self.repository, self.logger)
        self.scheduler = EngineScheduler(self.repository, self.supervisor, self.remote_jobs, self.logger)
        self._shutdown = threading.Event()
        self._maintenance_thread = threading.Thread(target=self._maintenance_loop, name="maintenance", daemon=True)
        self._maintenance_thread.start()
        self.supervisor.start_auto_services()
        self.logger.emit("INFO", "system", "시스템", "engine_started", "서비스 관리자 엔진이 시작되었습니다.")

    def dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        command = str(request.get("command", ""))
        try:
            if command == "ping":
                return {"ok": True, "version": "1.0.0"}
            if command == "list":
                return self._list_all()
            if command == "get":
                kind, resource_id = str(request["kind"]), str(request["id"])
                if kind not in {"service", "remote_job"}:
                    raise ValueError("kind는 service 또는 remote_job이어야 합니다.")
                item = self.repository.get_service(resource_id) if kind == "service" else self.repository.get_remote_job(resource_id)
                return {"ok": item is not None, "item": item.to_dict() if item else None}
            if command == "events":
                events = self.repository.query_events(
                    limit=int(request.get("limit", 500)), after_id=int(request.get("after_id", 0)),
                    source_id=request.get("source_id"), level=request.get("level"), keyword=request.get("keyword"),
                    start_time=request.get("start_time"), end_time=request.get("end_time"),
                )
                from dataclasses import asdict
                return {"ok": True, "events": [asdict(item) for item in events]}
            if command == "export_events":
                events = self.repository.query_events(limit=int(request.get("limit", 5000)))
                path = Path(str(request["path"]))
                with path.open("w", encoding="utf-8-sig", newline="") as file:
                    writer = csv.writer(file)
                    writer.writerow(("id", "timestamp", "level", "source_type", "source_name", "event_type", "message"))
                    for event in reversed(events):
                        writer.writerow((event.id, event.timestamp, event.level, event.source_type, event.source_name, event.event_type, event.message))
                return {"ok": True, "path": str(path)}
            if command in {"start", "stop", "restart"}:
                operation = getattr(self.supervisor, command)
                return {"ok": bool(operation(str(request["id"])))}
            if command == "run":
                return {"ok": self.remote_jobs.run(str(request["id"]))}
            if command == "upsert_service":
                item = ServiceDefinition.from_dict(dict(request["item"]))
                self.repository.upsert_service(item)
                self._reload()
                self.logger.emit("INFO", "service", item.name, "configuration_saved", "서비스 설정이 저장되었습니다.", source_id=item.id)
                return {"ok": True, "item": item.to_dict()}
            if command in {"create", "update"} and request.get("kind") == "service":
                item = ServiceDefinition.from_dict(dict(request["item"]))
                existing = self.repository.get_service(item.id)
                if command == "create" and existing is not None:
                    raise ValueError("같은 ID의 서비스가 이미 존재합니다.")
                if command == "update" and existing is None:
                    raise ValueError("수정할 서비스를 찾을 수 없습니다.")
                self.repository.upsert_service(item)
                self._reload()
                return {"ok": True, "item": item.to_dict()}
            if command == "delete_service":
                resource_id = str(request["id"])
                snapshots = {item["definition"]["id"]: item for item in self.supervisor.snapshots()}
                if snapshots.get(resource_id, {}).get("runtime", {}).get("pid"):
                    raise RuntimeError("실행 중인 서비스는 삭제할 수 없습니다.")
                deleted = self.repository.delete_service(resource_id)
                self._reload()
                return {"ok": deleted}
            if command == "upsert_remote_job":
                item = RemoteJobDefinition.from_dict(dict(request["item"]))
                secret = request.get("secret")
                if secret:
                    item.credential_id = self.repository.save_credential(f"{item.name} 자격증명", str(secret), credential_id=item.credential_id)
                    self.logger.register_secret(str(secret))
                self.repository.upsert_remote_job(item)
                self._reload()
                self.logger.emit("INFO", "remote_job", item.name, "configuration_saved", "원격 작업 설정이 저장되었습니다.", source_id=item.id)
                return {"ok": True, "item": item.to_dict()}
            if command in {"create", "update"} and request.get("kind") == "remote_job":
                item = RemoteJobDefinition.from_dict(dict(request["item"]))
                existing = self.repository.get_remote_job(item.id)
                if command == "create" and existing is not None:
                    raise ValueError("같은 ID의 원격 작업이 이미 존재합니다.")
                if command == "update" and existing is None:
                    raise ValueError("수정할 원격 작업을 찾을 수 없습니다.")
                secret = request.get("secret")
                if secret:
                    item.credential_id = self.repository.save_credential(f"{item.name} 자격증명", str(secret), credential_id=item.credential_id)
                self.repository.upsert_remote_job(item)
                self._reload()
                return {"ok": True, "item": item.to_dict()}
            if command == "delete":
                kind = request.get("kind")
                resource_id = str(request["id"])
                if kind == "service":
                    snapshots = {item["definition"]["id"]: item for item in self.supervisor.snapshots()}
                    if snapshots.get(resource_id, {}).get("runtime", {}).get("pid"):
                        raise RuntimeError("실행 중인 서비스는 삭제할 수 없습니다.")
                    deleted = self.repository.delete_service(resource_id)
                elif kind == "remote_job":
                    deleted = self.repository.delete_remote_job(resource_id)
                else:
                    raise ValueError("kind는 service 또는 remote_job이어야 합니다.")
                self._reload()
                return {"ok": deleted}
            if command == "test":
                kind = request.get("kind")
                if kind == "service":
                    errors = ServiceDefinition.from_dict(dict(request["item"])).validate()
                    return {"ok": not errors, "errors": errors}
                item = RemoteJobDefinition.from_dict(dict(request["item"]))
                errors = item.validate()
                return {"ok": not errors, "errors": errors}
            if command == "delete_remote_job":
                deleted = self.repository.delete_remote_job(str(request["id"]))
                self._reload()
                return {"ok": deleted}
            if command == "fetch_ssh_fingerprint":
                fingerprint = fetch_ssh_fingerprint(str(request["host"]), int(request.get("port", 22)), float(request.get("timeout", 10)))
                return {"ok": True, "fingerprint": fingerprint}
            if command == "import":
                result = self.repository.import_json(str(request["path"]))
                self._reload()
                return {"ok": True, "result": result}
            if command == "export":
                self.repository.export_json(str(request["path"]))
                return {"ok": True}
            if command == "backup":
                return {"ok": True, "path": str(self.repository.backup())}
            if command == "get_settings":
                return {"ok": True, "notifications": self.repository.get_setting("notifications", {})}
            if command == "update_settings":
                settings = dict(request.get("notifications") or {})
                if request.get("smtp_password"):
                    settings["smtp_credential_id"] = self.repository.save_credential("SMTP 자격증명", str(request["smtp_password"]), credential_id=settings.get("smtp_credential_id"))
                if request.get("webhook_url"):
                    settings["webhook_credential_id"] = self.repository.save_credential("Webhook URL", str(request["webhook_url"]), kind="webhook", credential_id=settings.get("webhook_credential_id"))
                self.repository.set_setting("notifications", settings)
                self.logger.emit("INFO", "system", "시스템", "settings_saved", "알림 설정이 저장되었습니다.")
                return {"ok": True, "notifications": settings}
            if command == "diagnostics":
                default_path = self.repository.data_dir / "diagnostics.zip"
                return {"ok": True, "path": str(self._diagnostics(str(request.get("path") or default_path)))}
            raise ValueError(f"지원하지 않는 명령: {command}")
        except Exception as exc:
            self.logger.emit("ERROR", "system", "시스템", "command_failed", f"{command} 실패: {exc}")
            return {"ok": False, "error": str(exc)}

    def _list_all(self) -> dict[str, Any]:
        services = self.supervisor.snapshots()
        jobs = self.remote_jobs.snapshots()
        for item in services:
            item["runtime"]["next_due"] = self.scheduler.next_due(item["definition"]["id"])
        for item in jobs:
            item["runtime"]["next_due"] = self.scheduler.next_due(item["definition"]["id"])
        return {"ok": True, "services": services, "remote_jobs": jobs}

    def _reload(self) -> None:
        self.supervisor.reload()
        self.remote_jobs.reload()
        self.scheduler.reload()

    def _diagnostics(self, path: str) -> Path:
        target = Path(path)
        base = target.with_suffix("")
        base.mkdir(parents=True, exist_ok=True)
        export = base / "configuration-redacted.json"
        self.repository.export_json(export)
        info = base / "system.txt"
        info.write_text(
            f"Python: {sys.version}\nPlatform: {sys.platform}\nData directory: {self.repository.data_dir}\n"
            f"Configuration: {self.repository.path}\nStorage: JSON\n",
            encoding="utf-8",
        )
        events = self.repository.query_events(limit=1000)
        import json
        from dataclasses import asdict
        (base / "recent-events.json").write_text(json.dumps([asdict(item) for item in events], ensure_ascii=False, indent=2), encoding="utf-8")
        archive = shutil.make_archive(str(target.with_suffix("")), "zip", base)
        return Path(archive)

    def wait(self) -> None:
        self._shutdown.wait()

    def _maintenance_loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                self.logger.cleanup()
                self.repository.prune_events()
            except (OSError, ValueError):
                pass
            if self._shutdown.wait(3600):
                return

    def shutdown(self) -> None:
        if self._shutdown.is_set():
            return
        self._shutdown.set()
        self.scheduler.shutdown()
        self.remote_jobs.shutdown()
        self.supervisor.shutdown()
        self.notifications.shutdown()
        self.logger.emit("INFO", "system", "시스템", "engine_stopped", "서비스 관리자 엔진이 종료되었습니다.")
        self.logger.close()
