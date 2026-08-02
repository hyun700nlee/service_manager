from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from credentials import SecretRedactor, protect_secret, unprotect_secret
from engine import ServiceManagerEngine
from event_logging import EventLogger
from health_checks import HealthChecker
from instance_lock import AlreadyRunningError, EngineInstanceLock
from models import HealthCheck, RestartPolicy, Schedule, ServiceDefinition
from scheduler import EngineScheduler
from storage import Repository, StorageCorruptionError, default_data_directory
from supervisor import ServiceSupervisor


class ModelAndStorageTests(unittest.TestCase):
    @staticmethod
    def _service(base: Path, name: str = "worker") -> ServiceDefinition:
        script = base / f"{name}.py"
        script.write_text("print('ok')\n", encoding="utf-8")
        return ServiceDefinition(
            name=name,
            executable=sys.executable,
            arguments=["-u", str(script)],
            working_directory=str(base),
        )

    def test_json_files_round_trip_and_secret_free_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repository = Repository(base)
            service = self._service(base)
            repository.upsert_service(service)
            credential_id = repository.save_credential("test", "plain-secret")

            reloaded = Repository(base)
            self.assertEqual(reloaded.get_service(service.id).name, "worker")
            self.assertEqual(reloaded.get_credential(credential_id), "plain-secret")
            self.assertTrue((base / "config.json").is_file())
            self.assertTrue((base / "state.json").is_file())
            self.assertTrue((base / "credentials.json").is_file())

            credential_text = (base / "credentials.json").read_text(encoding="utf-8")
            self.assertNotIn("plain-secret", credential_text)
            target = base / "export.json"
            repository.export_json(target)
            exported = target.read_text(encoding="utf-8")
            self.assertIn('"schema_version"', exported)
            self.assertNotIn("plain-secret", exported)
            self.assertNotIn("runtime_states", exported)
            self.assertNotIn("_enabled", exported)

    def test_default_data_directory_uses_local_app_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_local = os.environ.get("LOCALAPPDATA")
            old_override = os.environ.pop("SERVICE_MANAGER_DATA_DIR", None)
            os.environ["LOCALAPPDATA"] = tmp
            try:
                self.assertEqual(default_data_directory(), Path(tmp) / "PythonServiceManager")
            finally:
                if old_local is None:
                    os.environ.pop("LOCALAPPDATA", None)
                else:
                    os.environ["LOCALAPPDATA"] = old_local
                if old_override is not None:
                    os.environ["SERVICE_MANAGER_DATA_DIR"] = old_override

    def test_backup_contains_json_documents_and_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repository = Repository(base)
            repository.upsert_service(self._service(base))
            repository.save_runtime_state("test", {"desired_running": True})
            repository.save_credential("test", "backup-secret")
            repository.add_event("INFO", "system", "test", "backup", "ready")
            backup = repository.backup()
            with zipfile.ZipFile(backup) as archive:
                names = set(archive.namelist())
                self.assertTrue({"config.json", "state.json", "credentials.json", "events.jsonl"} <= names)
                self.assertNotIn("backup-secret", archive.read("credentials.json").decode("utf-8"))

    def test_dpapi_and_redaction(self) -> None:
        envelope = protect_secret("private-value")
        self.assertEqual(unprotect_secret(envelope), "private-value")
        redactor = SecretRedactor()
        redactor.register("private-value")
        self.assertEqual(redactor.redact("token=private-value"), "token=***")
        self.assertEqual(redactor.redact("Authorization: Bearer abc123"), "Authorization: Bearer ***")
        self.assertEqual(redactor.redact("api_key=unregistered"), "api_key=***")

    def test_legacy_config_is_backed_up_and_password_is_encrypted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            script = base / "run.py"
            script.write_text("print('ok')\n", encoding="utf-8")
            config = {
                "services": [{
                    "name": "legacy",
                    "working_directory": str(base),
                    "python_executable": sys.executable,
                    "script": "run.py",
                    "arguments": [],
                    "schedule_type": "none",
                }],
                "telnet_jobs": [{
                    "name": "legacy-job",
                    "host": "127.0.0.1",
                    "port": 23,
                    "username": "u",
                    "password": "secret",
                    "login_prompt": "login:",
                    "password_prompt": "Password:",
                    "shell_prompt": "$",
                    "commands": ["date"],
                    "connect_timeout_seconds": 1,
                    "command_timeout_seconds": 1,
                    "schedule_type": "none",
                }],
            }
            (base / "config.json").write_text(json.dumps(config), encoding="utf-8")
            repository = Repository(base)
            self.assertEqual(len(repository.list_services()), 1)
            job = repository.list_remote_jobs()[0]
            self.assertEqual(repository.get_credential(job.credential_id), "secret")
            self.assertNotIn("secret", (base / "credentials.json").read_text(encoding="utf-8"))
            self.assertEqual(len(list(base.glob("config.pre-json-*.json.bak"))), 1)

    def test_corrupt_config_recovers_from_last_good_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repository = Repository(base)
            first = self._service(base, "first")
            second = self._service(base, "second")
            repository.upsert_service(first)
            repository.upsert_service(second)
            (base / "config.json").write_text("{broken", encoding="utf-8")

            recovered = Repository(base)
            self.assertIsNotNone(recovered.get_service(first.id))
            self.assertTrue(recovered.startup_warnings)
            self.assertEqual(len(list(base.glob("config.json.corrupt-*"))), 1)

    def test_unsupported_schema_without_backup_fails_safely(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            base.mkdir(exist_ok=True)
            (base / "config.json").write_text(
                json.dumps({"schema_version": 999, "services": [], "remote_jobs": [], "settings": {}}),
                encoding="utf-8",
            )
            with self.assertRaises(StorageCorruptionError):
                Repository(base)

    def test_concurrent_state_writes_remain_valid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repository = Repository(base)

            def writer(index: int) -> None:
                for value in range(20):
                    repository.save_runtime_state(f"item-{index}", {"value": value})

            threads = [threading.Thread(target=writer, args=(index,)) for index in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            state = json.loads((base / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(len(state["runtime_states"]), 4)
            self.assertTrue(all(item["value"] == 19 for item in state["runtime_states"].values()))

    def test_jsonl_events_rotate_and_skip_corrupt_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repository = Repository(base, event_rotate_bytes=1024)
            for index in range(30):
                repository.add_event("INFO", "service", "worker", "output", f"{index}-" + "x" * 100)
            with (base / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write("not-json\n")
            events = repository.query_events(limit=100, keyword="worker")
            self.assertEqual(len(events), 30)
            archives = list((base / "events").glob("events-*.jsonl"))
            self.assertTrue(archives)
            old = (datetime.now() - timedelta(days=10)).timestamp()
            for archive in archives:
                os.utime(archive, (old, old))
            self.assertGreater(repository.prune_events(retention_days=1), 0)


class HealthAndRecoveryTests(unittest.TestCase):
    def test_tcp_health_check(self) -> None:
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        try:
            host, port = server.getsockname()
            result = HealthChecker().check(
                HealthCheck(enabled=True, type="tcp", host=host, port=port, timeout_seconds=1)
            )
            self.assertTrue(result.healthy)
        finally:
            server.close()

    def test_crash_loop_opens_circuit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            script = base / "fail.py"
            script.write_text("raise SystemExit(9)\n", encoding="utf-8")
            repository = Repository(base)
            service = ServiceDefinition(
                name="crasher",
                executable=sys.executable,
                arguments=[str(script)],
                working_directory=str(base),
                restart_policy=RestartPolicy(
                    initial_delay_seconds=0.02,
                    max_delay_seconds=0.05,
                    max_attempts=3,
                    window_seconds=5,
                    jitter_ratio=0,
                ),
            )
            repository.upsert_service(service)
            logger = EventLogger(repository)
            supervisor = ServiceSupervisor(repository, logger)
            try:
                supervisor.start(service.id)
                deadline = time.monotonic() + 5
                state = {}
                while time.monotonic() < deadline:
                    state = supervisor.snapshots()[0]["runtime"]
                    if state["circuit_open"]:
                        break
                    time.sleep(0.05)
                self.assertTrue(state["circuit_open"])
                self.assertGreaterEqual(state["restart_count"], 2)
            finally:
                supervisor.shutdown()
                logger.close()

    def test_desired_running_state_survives_engine_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            script = base / "wait.py"
            script.write_text("import time\nwhile True: time.sleep(0.1)\n", encoding="utf-8")
            repository = Repository(base)
            service = ServiceDefinition(
                name="persistent",
                executable=sys.executable,
                arguments=[str(script)],
                working_directory=str(base),
                stop_timeout_seconds=0.2,
            )
            repository.upsert_service(service)
            logger = EventLogger(repository)
            supervisor = ServiceSupervisor(repository, logger)
            supervisor.start(service.id)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not supervisor.snapshots()[0]["runtime"]["pid"]:
                time.sleep(0.05)
            self.assertTrue(supervisor.snapshots()[0]["runtime"]["pid"])
            supervisor.shutdown()
            logger.close()
            self.assertTrue(repository.get_runtime_states()[service.id]["desired_running"])

            logger2 = EventLogger(repository)
            supervisor2 = ServiceSupervisor(repository, logger2)
            try:
                supervisor2.start_auto_services()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and not supervisor2.snapshots()[0]["runtime"]["pid"]:
                    time.sleep(0.05)
                self.assertTrue(supervisor2.snapshots()[0]["runtime"]["pid"])
                supervisor2.stop(service.id)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and supervisor2.snapshots()[0]["runtime"]["pid"]:
                    time.sleep(0.05)
                self.assertFalse(repository.get_runtime_states()[service.id]["desired_running"])
            finally:
                supervisor2.shutdown()
                logger2.close()


class SchedulerAndInstanceTests(unittest.TestCase):
    def test_interval_schedule_is_persisted(self) -> None:
        class Noop:
            def restart(self, _resource_id):
                return True

            def run(self, _resource_id, *, manual=False):
                return True

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            script = base / "run.py"
            script.write_text("print('ok')\n", encoding="utf-8")
            repository = Repository(base)
            service = ServiceDefinition(
                name="scheduled",
                executable=sys.executable,
                arguments=[str(script)],
                working_directory=str(base),
                schedule=Schedule(type="interval", interval_minutes=5),
            )
            repository.upsert_service(service)
            logger = EventLogger(repository)
            scheduler = EngineScheduler(repository, Noop(), Noop(), logger)
            try:
                due = scheduler.next_due(service.id)
                self.assertIsNotNone(due)
                self.assertEqual(repository.get_runtime_states()[f"schedule:{service.id}"]["next_due"], due)
            finally:
                scheduler.shutdown()
                logger.close()

    def test_instance_lock_is_scoped_to_data_directory(self) -> None:
        with tempfile.TemporaryDirectory() as first_tmp, tempfile.TemporaryDirectory() as second_tmp:
            first = EngineInstanceLock(first_tmp)
            duplicate = EngineInstanceLock(first_tmp)
            independent = EngineInstanceLock(second_tmp)
            first.acquire()
            try:
                with self.assertRaises(AlreadyRunningError):
                    duplicate.acquire()
                independent.acquire()
                independent.release()
            finally:
                first.release()


class EngineApiTests(unittest.TestCase):
    def test_embedded_engine_crud_without_ipc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            script = base / "run.py"
            script.write_text("print('ok')\n", encoding="utf-8")
            engine = ServiceManagerEngine(data_dir=base)
            try:
                item = ServiceDefinition(
                    name="api",
                    executable=sys.executable,
                    arguments=[str(script)],
                    working_directory=str(base),
                )
                saved = engine.dispatch({"command": "upsert_service", "item": item.to_dict()})
                self.assertTrue(saved["ok"], saved)
                listed = engine.dispatch({"command": "list"})
                self.assertEqual(listed["services"][0]["definition"]["name"], "api")
                exported = base / "export.json"
                self.assertTrue(engine.dispatch({"command": "export", "path": str(exported)})["ok"])
                self.assertTrue(exported.is_file())
            finally:
                engine.shutdown()


if __name__ == "__main__":
    unittest.main()
