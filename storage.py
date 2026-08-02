from __future__ import annotations

import json
import os
import shutil
import threading
import zipfile
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from config_loader import load_config
from credentials import protect_secret, unprotect_secret
from models import EventRecord, RemoteJobDefinition, ServiceDefinition, new_id

SCHEMA_VERSION = 1
EVENT_ROTATE_BYTES = 10 * 1024 * 1024


class StorageCorruptionError(RuntimeError):
    """Raised when a JSON document and its last-known-good backup are unusable."""


def default_data_directory() -> Path:
    override = os.environ.get("SERVICE_MANAGER_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    local_app_data = os.environ.get("LOCALAPPDATA")
    root = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return root / "PythonServiceManager"


class JsonRepository:
    def __init__(
        self,
        data_dir: str | Path | None = None,
        config_path: str | Path | None = None,
        *,
        event_rotate_bytes: int = EVENT_ROTATE_BYTES,
    ):
        self.data_dir = Path(data_dir).expanduser().resolve() if data_dir else default_data_directory()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = Path(config_path).expanduser().resolve() if config_path else self.data_dir / "config.json"
        self.state_path = self.data_dir / "state.json"
        self.credentials_path = self.data_dir / "credentials.json"
        self.events_path = self.data_dir / "events.jsonl"
        self.events_directory = self.data_dir / "events"
        self.events_directory.mkdir(parents=True, exist_ok=True)
        self.event_rotate_bytes = max(1024, int(event_rotate_bytes))
        self.startup_warnings: list[str] = []
        self._lock = threading.RLock()

        self._state = self._load_document(
            self.state_path,
            {"schema_version": SCHEMA_VERSION, "runtime_states": {}},
            self._validate_state_document,
        )
        self._credentials = self._load_document(
            self.credentials_path,
            {"schema_version": SCHEMA_VERSION, "credentials": {}},
            self._validate_credentials_document,
        )
        self._config = self._load_or_migrate_config()
        self._event_id = self._find_latest_event_id()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _validate_version(document: dict[str, Any], label: str) -> None:
        version = document.get("schema_version")
        if not isinstance(version, int):
            raise ValueError(f"{label}에 schema_version이 없습니다.")
        if version > SCHEMA_VERSION:
            raise ValueError(f"{label}은 더 최신 버전의 프로그램에서 생성되었습니다.")
        if version < SCHEMA_VERSION:
            raise ValueError(f"{label} 스키마 버전 {version}은 지원되지 않습니다.")

    @classmethod
    def _validate_config_document(cls, document: dict[str, Any]) -> None:
        cls._validate_version(document, "config.json")
        services = document.get("services")
        remote_jobs = document.get("remote_jobs")
        settings = document.get("settings")
        if not isinstance(services, list) or not isinstance(remote_jobs, list) or not isinstance(settings, dict):
            raise ValueError("config.json의 services, remote_jobs, settings 형식이 올바르지 않습니다.")
        errors: list[str] = []
        for raw in services:
            if not isinstance(raw, dict):
                errors.append("서비스 항목은 객체여야 합니다.")
                continue
            errors.extend(ServiceDefinition.from_dict(raw).validate(require_paths=False))
        for raw in remote_jobs:
            if not isinstance(raw, dict):
                errors.append("원격 작업 항목은 객체여야 합니다.")
                continue
            errors.extend(RemoteJobDefinition.from_dict(raw).validate())
        if errors:
            raise ValueError("\n".join(errors))

    @classmethod
    def _validate_state_document(cls, document: dict[str, Any]) -> None:
        cls._validate_version(document, "state.json")
        if not isinstance(document.get("runtime_states"), dict):
            raise ValueError("state.json의 runtime_states는 객체여야 합니다.")

    @classmethod
    def _validate_credentials_document(cls, document: dict[str, Any]) -> None:
        cls._validate_version(document, "credentials.json")
        credentials = document.get("credentials")
        if not isinstance(credentials, dict):
            raise ValueError("credentials.json의 credentials는 객체여야 합니다.")
        for credential_id, value in credentials.items():
            if not isinstance(credential_id, str) or not isinstance(value, dict) or not isinstance(value.get("protected_value"), str):
                raise ValueError("credentials.json에 올바르지 않은 자격증명 항목이 있습니다.")

    def _load_document(
        self,
        path: Path,
        default: dict[str, Any],
        validator: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        if not path.exists():
            self._atomic_write(path, default, create_backup=False)
            return deepcopy(default)
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                raise ValueError("최상위 값은 객체여야 합니다.")
            validator(document)
            return document
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            backup = path.with_suffix(path.suffix + ".bak")
            try:
                recovered = json.loads(backup.read_text(encoding="utf-8"))
                if not isinstance(recovered, dict):
                    raise ValueError("백업의 최상위 값은 객체여야 합니다.")
                validator(recovered)
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as backup_exc:
                raise StorageCorruptionError(
                    f"{path.name}을 읽을 수 없고 정상 백업도 없습니다: {exc} (백업: {backup_exc})"
                ) from exc
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            corrupt = path.with_name(f"{path.name}.corrupt-{stamp}")
            shutil.copy2(path, corrupt)
            shutil.copy2(backup, path)
            message = f"손상된 {path.name}을 {backup.name}에서 복구했습니다. 손상본: {corrupt.name}"
            self.startup_warnings.append(message)
            return recovered

    def _load_or_migrate_config(self) -> dict[str, Any]:
        if not self.path.exists():
            default = {"schema_version": SCHEMA_VERSION, "services": [], "remote_jobs": [], "settings": {}}
            self._atomic_write(self.path, default, create_backup=False)
            return default
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return self._load_document(
                self.path,
                {"schema_version": SCHEMA_VERSION, "services": [], "remote_jobs": [], "settings": {}},
                self._validate_config_document,
            )
        if isinstance(raw, dict) and "schema_version" in raw:
            return self._load_document(
                self.path,
                {"schema_version": SCHEMA_VERSION, "services": [], "remote_jobs": [], "settings": {}},
                self._validate_config_document,
            )
        return self._migrate_legacy_document(self.path)

    def _migrate_legacy_document(self, source: Path) -> dict[str, Any]:
        result = load_config(source)
        if result.global_errors:
            raise StorageCorruptionError("기존 config.json을 변환할 수 없습니다: " + "\n".join(result.global_errors))
        services: list[ServiceDefinition] = []
        jobs: list[RemoteJobDefinition] = []
        for raw in result.services:
            if raw.get("_enabled"):
                services.append(ServiceDefinition.from_dict(raw, base_dir=source.parent))
        for raw in result.telnet_jobs:
            if not raw.get("_enabled"):
                continue
            password = raw.get("password")
            job = RemoteJobDefinition.from_dict({**raw, "protocol": "telnet", "legacy_telnet_confirmed": True})
            if password:
                job.credential_id = self._save_credential_locked(f"{job.name} 자격증명", str(password))
            jobs.append(job)
        document = {
            "schema_version": SCHEMA_VERSION,
            "services": [item.to_dict() for item in services],
            "remote_jobs": [item.to_dict() for item in jobs],
            "settings": {},
        }
        self._validate_config_document(document)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = source.with_name(f"{source.stem}.pre-json-{stamp}{source.suffix}.bak")
        shutil.copy2(source, backup)
        self._atomic_write(self.credentials_path, self._credentials)
        self._atomic_write(source, document)
        self.startup_warnings.append(f"기존 JSON 설정을 새 형식으로 변환했습니다. 원본 백업: {backup.name}")
        return document

    @staticmethod
    def _copy_json(value: Any) -> Any:
        return json.loads(json.dumps(value, ensure_ascii=False))

    def _atomic_write(self, path: Path, document: dict[str, Any], *, create_backup: bool = True) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(document, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            if create_backup and path.is_file():
                shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def is_empty(self) -> bool:
        with self._lock:
            return not self._config["services"] and not self._config["remote_jobs"]

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._copy_json(self._config["settings"].get(key, default))

    def set_setting(self, key: str, value: Any) -> None:
        with self._lock:
            updated = deepcopy(self._config)
            updated["settings"][key] = self._copy_json(value)
            self._atomic_write(self.path, updated)
            self._config = updated

    def list_services(self) -> list[ServiceDefinition]:
        with self._lock:
            return sorted(
                (ServiceDefinition.from_dict(deepcopy(raw)) for raw in self._config["services"]),
                key=lambda item: item.name.casefold(),
            )

    def get_service(self, resource_id: str) -> ServiceDefinition | None:
        with self._lock:
            raw = next((item for item in self._config["services"] if item.get("id") == resource_id), None)
            return ServiceDefinition.from_dict(deepcopy(raw)) if raw else None

    def upsert_service(self, service: ServiceDefinition) -> None:
        errors = service.validate()
        if errors:
            raise ValueError("\n".join(errors))
        with self._lock:
            updated = deepcopy(self._config)
            items = updated["services"]
            index = next((index for index, item in enumerate(items) if item.get("id") == service.id), None)
            if index is None:
                items.append(service.to_dict())
            else:
                items[index] = service.to_dict()
            self._atomic_write(self.path, updated)
            self._config = updated

    def delete_service(self, resource_id: str) -> bool:
        with self._lock:
            updated = deepcopy(self._config)
            before = len(updated["services"])
            updated["services"] = [item for item in updated["services"] if item.get("id") != resource_id]
            if len(updated["services"]) == before:
                return False
            self._atomic_write(self.path, updated)
            self._config = updated
            return True

    def list_remote_jobs(self) -> list[RemoteJobDefinition]:
        with self._lock:
            return sorted(
                (RemoteJobDefinition.from_dict(deepcopy(raw)) for raw in self._config["remote_jobs"]),
                key=lambda item: item.name.casefold(),
            )

    def get_remote_job(self, resource_id: str) -> RemoteJobDefinition | None:
        with self._lock:
            raw = next((item for item in self._config["remote_jobs"] if item.get("id") == resource_id), None)
            return RemoteJobDefinition.from_dict(deepcopy(raw)) if raw else None

    def upsert_remote_job(self, job: RemoteJobDefinition, *, allow_unverified_ssh: bool = False) -> None:
        errors = job.validate()
        if allow_unverified_ssh:
            errors = [error for error in errors if "호스트 키" not in error]
        if errors:
            raise ValueError("\n".join(errors))
        with self._lock:
            updated = deepcopy(self._config)
            items = updated["remote_jobs"]
            index = next((index for index, item in enumerate(items) if item.get("id") == job.id), None)
            if index is None:
                items.append(job.to_dict())
            else:
                items[index] = job.to_dict()
            self._atomic_write(self.path, updated)
            self._config = updated

    def delete_remote_job(self, resource_id: str) -> bool:
        with self._lock:
            updated = deepcopy(self._config)
            removed = next((item for item in updated["remote_jobs"] if item.get("id") == resource_id), None)
            if removed is None:
                return False
            updated["remote_jobs"] = [item for item in updated["remote_jobs"] if item.get("id") != resource_id]
            self._atomic_write(self.path, updated)
            self._config = updated
            credential_id = removed.get("credential_id")
            if credential_id and not any(item.get("credential_id") == credential_id for item in updated["remote_jobs"]):
                credentials = deepcopy(self._credentials)
                if credentials["credentials"].pop(credential_id, None) is not None:
                    self._atomic_write(self.credentials_path, credentials)
                    self._credentials = credentials
            return True

    def _save_credential_locked(
        self,
        name: str,
        secret: str,
        *,
        kind: str = "password",
        credential_id: str | None = None,
    ) -> str:
        credential_id = credential_id or new_id()
        now = self._now()
        previous = self._credentials["credentials"].get(credential_id, {})
        self._credentials["credentials"][credential_id] = {
            "id": credential_id,
            "name": name,
            "kind": kind,
            "protected_value": protect_secret(secret),
            "created_at": previous.get("created_at", now),
            "updated_at": now,
        }
        return credential_id

    def save_credential(
        self,
        name: str,
        secret: str,
        *,
        kind: str = "password",
        credential_id: str | None = None,
    ) -> str:
        with self._lock:
            updated = deepcopy(self._credentials)
            original = self._credentials
            self._credentials = updated
            try:
                credential_id = self._save_credential_locked(name, secret, kind=kind, credential_id=credential_id)
                self._atomic_write(self.credentials_path, updated)
            except Exception:
                self._credentials = original
                raise
            return credential_id

    def get_credential(self, credential_id: str | None) -> str | None:
        if not credential_id:
            return None
        with self._lock:
            value = self._credentials["credentials"].get(credential_id)
            return unprotect_secret(value["protected_value"]) if value else None

    def save_runtime_state(self, resource_id: str, state: dict[str, Any]) -> None:
        with self._lock:
            updated = deepcopy(self._state)
            updated["runtime_states"][resource_id] = self._copy_json(state)
            self._atomic_write(self.state_path, updated)
            self._state = updated

    def get_runtime_states(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return deepcopy(self._state["runtime_states"])

    def _event_files(self) -> list[Path]:
        files = list(self.events_directory.glob("events-*.jsonl"))
        if self.events_path.is_file():
            files.append(self.events_path)
        return files

    def _find_latest_event_id(self) -> int:
        latest = 0
        for path in self._event_files():
            try:
                with path.open("r", encoding="utf-8") as stream:
                    for line in stream:
                        try:
                            latest = max(latest, int(json.loads(line).get("id") or 0))
                        except (TypeError, ValueError, json.JSONDecodeError):
                            continue
            except OSError:
                continue
        return latest

    def _rotate_events_if_needed(self) -> None:
        if not self.events_path.is_file() or self.events_path.stat().st_size == 0:
            return
        modified = datetime.fromtimestamp(self.events_path.stat().st_mtime).date()
        if modified == datetime.now().date() and self.events_path.stat().st_size < self.event_rotate_bytes:
            return
        stamp = datetime.fromtimestamp(self.events_path.stat().st_mtime).strftime("%Y%m%d")
        index = 1
        while True:
            target = self.events_directory / f"events-{stamp}-{index}.jsonl"
            if not target.exists():
                os.replace(self.events_path, target)
                return
            index += 1

    def add_event(
        self,
        level: str,
        source_type: str,
        source_name: str,
        event_type: str,
        message: str,
        *,
        source_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> int:
        with self._lock:
            self._rotate_events_if_needed()
            self._event_id += 1
            event = {
                "id": self._event_id,
                "timestamp": self._now(),
                "level": level,
                "source_type": source_type,
                "source_id": source_id,
                "source_name": source_name,
                "event_type": event_type,
                "message": message,
                "details": details or {},
            }
            with self.events_path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
                stream.flush()
            return self._event_id

    def query_events(
        self,
        *,
        limit: int = 500,
        after_id: int = 0,
        source_id: str | None = None,
        level: str | None = None,
        keyword: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> list[EventRecord]:
        matches: list[EventRecord] = []
        normalized_keyword = keyword.casefold() if keyword else None
        with self._lock:
            files = list(self._event_files())
        for path in files:
            try:
                with path.open("r", encoding="utf-8") as stream:
                    for line in stream:
                        try:
                            raw = json.loads(line)
                            event_id = int(raw.get("id") or 0)
                            timestamp = str(raw.get("timestamp") or "")
                            if event_id <= after_id or (source_id and raw.get("source_id") != source_id):
                                continue
                            if level and str(raw.get("level", "")).upper() != level.upper():
                                continue
                            if start_time and timestamp < start_time:
                                continue
                            if end_time and timestamp > end_time:
                                continue
                            haystack = " ".join(
                                str(raw.get(key, "")) for key in ("source_name", "event_type", "message", "details")
                            ).casefold()
                            if normalized_keyword and normalized_keyword not in haystack:
                                continue
                            matches.append(
                                EventRecord(
                                    id=event_id,
                                    timestamp=timestamp,
                                    level=str(raw.get("level") or "INFO"),
                                    source_type=str(raw.get("source_type") or "system"),
                                    source_id=raw.get("source_id"),
                                    source_name=str(raw.get("source_name") or ""),
                                    event_type=str(raw.get("event_type") or ""),
                                    message=str(raw.get("message") or ""),
                                    details=dict(raw.get("details") or {}),
                                )
                            )
                        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                            continue
            except OSError:
                continue
        matches.sort(key=lambda item: int(item.id or 0), reverse=True)
        return matches[: max(1, min(int(limit), 5000))]

    @staticmethod
    def _line_count(path: Path) -> int:
        try:
            with path.open("rb") as stream:
                return sum(1 for _ in stream)
        except OSError:
            return 0

    def prune_events(self, *, retention_days: int = 30, max_records: int = 100_000) -> int:
        removed = 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        with self._lock:
            archives = sorted(self.events_directory.glob("events-*.jsonl"), key=lambda item: item.stat().st_mtime)
            for path in list(archives):
                try:
                    modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                    if modified < cutoff:
                        removed += self._line_count(path)
                        path.unlink()
                        archives.remove(path)
                except OSError:
                    continue
            active_count = self._line_count(self.events_path)
            archive_counts = [(path, self._line_count(path)) for path in archives]
            total = active_count + sum(count for _, count in archive_counts)
            for path, count in archive_counts:
                if total <= max_records:
                    break
                try:
                    path.unlink()
                    total -= count
                    removed += count
                except OSError:
                    continue
        return removed

    def export_json(self, path: str | Path) -> None:
        with self._lock:
            data = {
                "schema_version": SCHEMA_VERSION,
                "services": deepcopy(self._config["services"]),
                "remote_jobs": deepcopy(self._config["remote_jobs"]),
                "settings": deepcopy(self._config["settings"]),
            }
        target = Path(path)
        self._atomic_write(target, data)

    def import_json(self, path: str | Path) -> dict[str, int]:
        source = Path(path)
        data = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("가져오기 파일의 최상위 값은 객체여야 합니다.")
        raw_services = data.get("services", [])
        raw_jobs = data.get("remote_jobs", data.get("telnet_jobs", []))
        if not isinstance(raw_services, list) or not isinstance(raw_jobs, list):
            raise ValueError("services와 remote_jobs는 배열이어야 합니다.")
        services = [ServiceDefinition.from_dict(dict(raw), base_dir=source.parent) for raw in raw_services]
        jobs: list[tuple[RemoteJobDefinition, str | None]] = []
        for value in raw_jobs:
            raw = dict(value)
            password = raw.pop("password", None)
            jobs.append((RemoteJobDefinition.from_dict(raw), str(password) if password is not None else None))
        validation_errors = [error for item in services for error in item.validate()]
        validation_errors.extend(error for item, _ in jobs for error in item.validate() if "호스트 키" not in error)
        if validation_errors:
            raise ValueError("\n".join(validation_errors))
        with self._lock:
            config = deepcopy(self._config)
            credentials = deepcopy(self._credentials)
            service_map = {item.get("id"): item for item in config["services"]}
            job_map = {item.get("id"): item for item in config["remote_jobs"]}
            for service in services:
                service_map[service.id] = service.to_dict()
            for job, password in jobs:
                if password:
                    credential_id = job.credential_id or new_id()
                    job.credential_id = credential_id
                    now = self._now()
                    previous = credentials["credentials"].get(credential_id, {})
                    credentials["credentials"][credential_id] = {
                        "id": credential_id,
                        "name": f"{job.name} 자격증명",
                        "kind": "password",
                        "protected_value": protect_secret(password),
                        "created_at": previous.get("created_at", now),
                        "updated_at": now,
                    }
                job_map[job.id] = job.to_dict()
            config["services"] = list(service_map.values())
            config["remote_jobs"] = list(job_map.values())
            if isinstance(data.get("settings"), dict):
                config["settings"].update(deepcopy(data["settings"]))
            self._validate_config_document(config)
            self._validate_credentials_document(credentials)
            self._atomic_write(self.credentials_path, credentials)
            self._atomic_write(self.path, config)
            self._credentials = credentials
            self._config = config
        return {"services": len(services), "remote_jobs": len(jobs)}

    def migrate_legacy_config(self, path: str | Path) -> dict[str, int] | None:
        source = Path(path)
        if not source.is_file() or not self.is_empty():
            return None
        return self.import_json(source)

    def backup(self) -> Path:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_directory = self.data_dir / "backups"
        backup_directory.mkdir(parents=True, exist_ok=True)
        target = backup_directory / f"service-manager-{stamp}.zip"
        with self._lock, zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            candidates = [self.path, self.state_path, self.credentials_path, self.events_path]
            candidates.extend(self.events_directory.glob("events-*.jsonl"))
            for path in candidates:
                if path.is_file():
                    try:
                        relative = path.relative_to(self.data_dir)
                    except ValueError:
                        relative = Path("configuration") / path.name
                    archive.write(path, relative.as_posix())
        return target


# Keep the existing import surface for the engine and managers while using JSON only.
Repository = JsonRepository
