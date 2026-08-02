from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from config_loader import load_config
from credentials import protect_secret, unprotect_secret
from models import EventRecord, RemoteJobDefinition, ServiceDefinition, new_id

SCHEMA_VERSION = 1


def default_data_directory() -> Path:
    override = os.environ.get("SERVICE_MANAGER_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    root = Path(os.environ.get("PROGRAMDATA", Path.home()))
    return root / "PythonServiceManager"


class Repository:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else default_data_directory() / "service-manager.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._lock, self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS services (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS remote_jobs (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS credentials (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    protected_value TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime_state (
                    resource_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    level TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_id TEXT,
                    source_name TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_events_time ON events(timestamp DESC);
                CREATE INDEX IF NOT EXISTS idx_events_source ON events(source_type, source_id, timestamp DESC);
                """
            )
            current = db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
            if current is None:
                db.execute("INSERT INTO metadata(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
            elif int(current[0]) > SCHEMA_VERSION:
                raise RuntimeError("이 데이터베이스는 더 최신 버전의 프로그램에서 생성되었습니다.")

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def is_empty(self) -> bool:
        with self._connect() as db:
            count = db.execute("SELECT (SELECT COUNT(*) FROM services) + (SELECT COUNT(*) FROM remote_jobs)").fetchone()[0]
            return int(count) == 0

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._connect() as db:
            row = db.execute("SELECT value FROM metadata WHERE key=?", (f"setting:{key}",)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return default

    def set_setting(self, key: str, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False)
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (f"setting:{key}", payload),
            )

    def list_services(self) -> list[ServiceDefinition]:
        with self._connect() as db:
            return [ServiceDefinition.from_dict(json.loads(row["payload"])) for row in db.execute("SELECT payload FROM services ORDER BY name")]

    def get_service(self, resource_id: str) -> ServiceDefinition | None:
        with self._connect() as db:
            row = db.execute("SELECT payload FROM services WHERE id=?", (resource_id,)).fetchone()
            return ServiceDefinition.from_dict(json.loads(row[0])) if row else None

    def upsert_service(self, service: ServiceDefinition) -> None:
        errors = service.validate()
        if errors:
            raise ValueError("\n".join(errors))
        now = self._now()
        payload = json.dumps(service.to_dict(), ensure_ascii=False)
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO services(id,name,enabled,payload,created_at,updated_at) VALUES(?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name, enabled=excluded.enabled,
                payload=excluded.payload, updated_at=excluded.updated_at""",
                (service.id, service.name, int(service.enabled), payload, now, now),
            )

    def delete_service(self, resource_id: str) -> bool:
        with self._lock, self._connect() as db:
            return db.execute("DELETE FROM services WHERE id=?", (resource_id,)).rowcount > 0

    def list_remote_jobs(self) -> list[RemoteJobDefinition]:
        with self._connect() as db:
            return [RemoteJobDefinition.from_dict(json.loads(row["payload"])) for row in db.execute("SELECT payload FROM remote_jobs ORDER BY name")]

    def get_remote_job(self, resource_id: str) -> RemoteJobDefinition | None:
        with self._connect() as db:
            row = db.execute("SELECT payload FROM remote_jobs WHERE id=?", (resource_id,)).fetchone()
            return RemoteJobDefinition.from_dict(json.loads(row[0])) if row else None

    def upsert_remote_job(self, job: RemoteJobDefinition, *, allow_unverified_ssh: bool = False) -> None:
        errors = job.validate()
        if allow_unverified_ssh:
            errors = [error for error in errors if "호스트 키" not in error]
        if errors:
            raise ValueError("\n".join(errors))
        now = self._now()
        payload = json.dumps(job.to_dict(), ensure_ascii=False)
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO remote_jobs(id,name,enabled,payload,created_at,updated_at) VALUES(?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name, enabled=excluded.enabled,
                payload=excluded.payload, updated_at=excluded.updated_at""",
                (job.id, job.name, int(job.enabled), payload, now, now),
            )

    def delete_remote_job(self, resource_id: str) -> bool:
        with self._lock, self._connect() as db:
            return db.execute("DELETE FROM remote_jobs WHERE id=?", (resource_id,)).rowcount > 0

    def save_credential(self, name: str, secret: str, *, kind: str = "password", credential_id: str | None = None) -> str:
        credential_id = credential_id or new_id()
        now = self._now()
        protected = protect_secret(secret)
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO credentials(id,name,kind,protected_value,created_at,updated_at) VALUES(?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name, kind=excluded.kind,
                protected_value=excluded.protected_value, updated_at=excluded.updated_at""",
                (credential_id, name, kind, protected, now, now),
            )
        return credential_id

    def get_credential(self, credential_id: str | None) -> str | None:
        if not credential_id:
            return None
        with self._connect() as db:
            row = db.execute("SELECT protected_value FROM credentials WHERE id=?", (credential_id,)).fetchone()
        return unprotect_secret(row[0]) if row else None

    def save_runtime_state(self, resource_id: str, state: dict[str, Any]) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO runtime_state(resource_id,payload,updated_at) VALUES(?,?,?)
                ON CONFLICT(resource_id) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at""",
                (resource_id, json.dumps(state, ensure_ascii=False), self._now()),
            )

    def get_runtime_states(self) -> dict[str, dict[str, Any]]:
        with self._connect() as db:
            return {row["resource_id"]: json.loads(row["payload"]) for row in db.execute("SELECT resource_id,payload FROM runtime_state")}

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
        with self._lock, self._connect() as db:
            cursor = db.execute(
                """INSERT INTO events(timestamp,level,source_type,source_id,source_name,event_type,message,details)
                VALUES(?,?,?,?,?,?,?,?)""",
                (self._now(), level, source_type, source_id, source_name, event_type, message, json.dumps(details or {}, ensure_ascii=False)),
            )
            return int(cursor.lastrowid)

    def query_events(self, *, limit: int = 500, after_id: int = 0, source_id: str | None = None, level: str | None = None, keyword: str | None = None) -> list[EventRecord]:
        clauses = ["id > ?"]
        values: list[Any] = [after_id]
        if source_id:
            clauses.append("source_id = ?")
            values.append(source_id)
        if level:
            clauses.append("level = ?")
            values.append(level)
        if keyword:
            clauses.append("message LIKE ?")
            values.append(f"%{keyword}%")
        values.append(max(1, min(int(limit), 5000)))
        sql = f"SELECT * FROM events WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT ?"
        with self._connect() as db:
            rows = db.execute(sql, values).fetchall()
        return [
            EventRecord(
                id=row["id"], timestamp=row["timestamp"], level=row["level"], source_type=row["source_type"],
                source_id=row["source_id"], source_name=row["source_name"], event_type=row["event_type"],
                message=row["message"], details=json.loads(row["details"]),
            ) for row in rows
        ]

    def prune_events(self, *, retention_days: int = 30, max_records: int = 100_000) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        with self._lock, self._connect() as db:
            removed = db.execute("DELETE FROM events WHERE timestamp < ?", (cutoff.isoformat(),)).rowcount
            overflow = db.execute("SELECT MAX(COUNT(*) - ?, 0) FROM events", (max_records,)).fetchone()[0]
            if overflow:
                removed += db.execute(
                    "DELETE FROM events WHERE id IN (SELECT id FROM events ORDER BY id ASC LIMIT ?)", (int(overflow),)
                ).rowcount
            return int(removed)

    def export_json(self, path: str | Path) -> None:
        data = {
            "schema_version": SCHEMA_VERSION,
            "services": [item.to_dict() for item in self.list_services()],
            "remote_jobs": [item.to_dict() for item in self.list_remote_jobs()],
        }
        target = Path(path)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, target)

    def import_json(self, path: str | Path) -> dict[str, int]:
        source = Path(path)
        data = json.loads(source.read_text(encoding="utf-8"))
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
        validation_errors.extend(
            error for item, _ in jobs for error in item.validate() if "호스트 키" not in error
        )
        if validation_errors:
            raise ValueError("\n".join(validation_errors))
        now = self._now()
        with self._lock, self._connect() as db:
            for service in services:
                db.execute(
                    """INSERT INTO services(id,name,enabled,payload,created_at,updated_at) VALUES(?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET name=excluded.name,enabled=excluded.enabled,payload=excluded.payload,updated_at=excluded.updated_at""",
                    (service.id, service.name, int(service.enabled), json.dumps(service.to_dict(), ensure_ascii=False), now, now),
                )
            for job, password in jobs:
                if password:
                    credential_id = job.credential_id or new_id()
                    job.credential_id = credential_id
                    db.execute(
                        """INSERT INTO credentials(id,name,kind,protected_value,created_at,updated_at) VALUES(?,?,?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET name=excluded.name,kind=excluded.kind,protected_value=excluded.protected_value,updated_at=excluded.updated_at""",
                        (credential_id, f"{job.name} 자격증명", "password", protect_secret(password), now, now),
                    )
                db.execute(
                    """INSERT INTO remote_jobs(id,name,enabled,payload,created_at,updated_at) VALUES(?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET name=excluded.name,enabled=excluded.enabled,payload=excluded.payload,updated_at=excluded.updated_at""",
                    (job.id, job.name, int(job.enabled), json.dumps(job.to_dict(), ensure_ascii=False), now, now),
                )
        return {"services": len(services), "remote_jobs": len(jobs)}

    def migrate_legacy_config(self, path: str | Path) -> dict[str, int] | None:
        source = Path(path)
        if not source.is_file() or not self.is_empty():
            return None
        result = load_config(source)
        if result.global_errors:
            raise ValueError("\n".join(result.global_errors))
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = source.with_name(f"{source.stem}.pre-commercial-{stamp}{source.suffix}.bak")
        shutil.copy2(source, backup)
        services: list[ServiceDefinition] = []
        jobs: list[tuple[RemoteJobDefinition, str | None]] = []
        for raw in result.services:
            if not raw.get("_enabled"):
                continue
            services.append(ServiceDefinition.from_dict(raw, base_dir=source.parent))
        for raw in result.telnet_jobs:
            if not raw.get("_enabled"):
                continue
            password = raw.get("password")
            job = RemoteJobDefinition.from_dict({**raw, "protocol": "telnet", "legacy_telnet_confirmed": True})
            jobs.append((job, str(password) if password else None))
        now = self._now()
        with self._lock, self._connect() as db:
            for service in services:
                db.execute(
                    "INSERT INTO services(id,name,enabled,payload,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (service.id, service.name, int(service.enabled), json.dumps(service.to_dict(), ensure_ascii=False), now, now),
                )
            for job, password in jobs:
                if password:
                    job.credential_id = new_id()
                    db.execute(
                        "INSERT INTO credentials(id,name,kind,protected_value,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                        (job.credential_id, f"{job.name} 자격증명", "password", protect_secret(password), now, now),
                    )
                db.execute(
                    "INSERT INTO remote_jobs(id,name,enabled,payload,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (job.id, job.name, int(job.enabled), json.dumps(job.to_dict(), ensure_ascii=False), now, now),
                )
        self.add_event("INFO", "system", "시스템", "config_migrated", f"기존 JSON 설정을 가져왔습니다. 백업: {backup}")
        return {"services": len(services), "remote_jobs": len(jobs)}

    def backup(self) -> Path:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = self.path.with_name(f"{self.path.stem}-{stamp}.bak{self.path.suffix}")
        with self._lock, self._connect() as source, sqlite3.connect(target) as destination:
            source.backup(destination)
        return target
