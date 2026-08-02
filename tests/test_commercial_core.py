from __future__ import annotations

import json
import socket
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from credentials import SecretRedactor, protect_secret, unprotect_secret
from engine import ServiceManagerEngine
from event_logging import EventLogger
from health_checks import HealthChecker
from instance_lock import AlreadyRunningError, EngineInstanceLock
from ipc import EngineClient, EngineIpcServer
from models import HealthCheck, RestartPolicy, Schedule, ServiceDefinition
from scheduler import EngineScheduler
from storage import Repository
from supervisor import ServiceSupervisor


class ModelAndStorageTests(unittest.TestCase):
    def test_service_round_trip_and_secret_free_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            script = base / "worker.py"
            script.write_text("print('ok')\n", encoding="utf-8")
            repository = Repository(base / "manager.db")
            service = ServiceDefinition(
                name="worker", executable=sys.executable, arguments=["-u", str(script)], working_directory=str(base)
            )
            repository.upsert_service(service)
            self.assertEqual(repository.get_service(service.id).name, "worker")
            target = base / "export.json"
            repository.export_json(target)
            exported = target.read_text(encoding="utf-8")
            self.assertIn('"schema_version"', exported)
            self.assertNotIn("password", exported.casefold())
            self.assertNotIn("_enabled", exported)

    def test_dpapi_and_redaction(self) -> None:
        envelope = protect_secret("private-value")
        self.assertEqual(unprotect_secret(envelope), "private-value")
        redactor = SecretRedactor()
        redactor.register("private-value")
        self.assertEqual(redactor.redact("token=private-value"), "token=***")
        self.assertEqual(redactor.redact("Authorization: Bearer abc123"), "Authorization: Bearer ***")
        self.assertEqual(redactor.redact("api_key=unregistered"), "api_key=***")

    def test_legacy_migration_backs_up_and_encrypts_password(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            script = base / "run.py"
            script.write_text("print('ok')\n", encoding="utf-8")
            config = {
                "services": [{
                    "name": "legacy", "working_directory": str(base), "python_executable": sys.executable,
                    "script": "run.py", "arguments": [], "schedule_type": "none",
                }],
                "telnet_jobs": [{
                    "name": "legacy-job", "host": "127.0.0.1", "port": 23, "username": "u", "password": "secret",
                    "login_prompt": "login:", "password_prompt": "Password:", "shell_prompt": "$", "commands": ["date"],
                    "connect_timeout_seconds": 1, "command_timeout_seconds": 1, "schedule_type": "none",
                }],
            }
            path = base / "config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            repository = Repository(base / "manager.db")
            result = repository.migrate_legacy_config(path)
            self.assertEqual(result, {"services": 1, "remote_jobs": 1})
            self.assertEqual(repository.get_credential(repository.list_remote_jobs()[0].credential_id), "secret")
            self.assertEqual(len(list(base.glob("config.pre-commercial-*.json.bak"))), 1)


class HealthAndRecoveryTests(unittest.TestCase):
    def test_tcp_health_check(self) -> None:
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        try:
            host, port = server.getsockname()
            result = HealthChecker().check(HealthCheck(enabled=True, type="tcp", host=host, port=port, timeout_seconds=1))
            self.assertTrue(result.healthy)
        finally:
            server.close()

    def test_crash_loop_opens_circuit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            script = base / "fail.py"
            script.write_text("raise SystemExit(9)\n", encoding="utf-8")
            repository = Repository(base / "manager.db")
            service = ServiceDefinition(
                name="crasher", executable=sys.executable, arguments=[str(script)], working_directory=str(base),
                restart_policy=RestartPolicy(initial_delay_seconds=0.02, max_delay_seconds=0.05, max_attempts=3, window_seconds=5, jitter_ratio=0),
            )
            repository.upsert_service(service)
            logger = EventLogger(repository, log_directory=base / "logs")
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
            repository = Repository(base / "manager.db")
            service = ServiceDefinition(
                name="persistent", executable=sys.executable, arguments=[str(script)], working_directory=str(base),
                stop_timeout_seconds=0.2,
            )
            repository.upsert_service(service)
            logger = EventLogger(repository, log_directory=base / "logs")
            supervisor = ServiceSupervisor(repository, logger)
            supervisor.start(service.id)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not supervisor.snapshots()[0]["runtime"]["pid"]:
                time.sleep(0.05)
            self.assertTrue(supervisor.snapshots()[0]["runtime"]["pid"])
            supervisor.shutdown()
            logger.close()
            self.assertTrue(repository.get_runtime_states()[service.id]["desired_running"])

            logger2 = EventLogger(repository, log_directory=base / "logs2")
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
            repository = Repository(base / "manager.db")
            service = ServiceDefinition(
                name="scheduled", executable=sys.executable, arguments=[str(script)], working_directory=str(base),
                schedule=Schedule(type="interval", interval_minutes=5),
            )
            repository.upsert_service(service)
            logger = EventLogger(repository, log_directory=base / "logs")
            scheduler = EngineScheduler(repository, Noop(), Noop(), logger)
            try:
                due = scheduler.next_due(service.id)
                self.assertIsNotNone(due)
                self.assertEqual(repository.get_runtime_states()[f"schedule:{service.id}"]["next_due"], due)
            finally:
                scheduler.shutdown()
                logger.close()

    def test_engine_instance_lock_rejects_second_owner(self) -> None:
        first = EngineInstanceLock()
        second = EngineInstanceLock()
        first.acquire()
        try:
            with self.assertRaises(AlreadyRunningError):
                second.acquire()
        finally:
            first.release()


class EngineApiTests(unittest.TestCase):
    def test_crud_and_authenticated_ipc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            script = base / "run.py"
            script.write_text("print('ok')\n", encoding="utf-8")
            engine = ServiceManagerEngine(database_path=base / "manager.db")
            address = rf"\\.\pipe\ServiceManagerTest-{uuid.uuid4()}"
            key = b"test-auth-key-32-bytes-long-value"
            server = EngineIpcServer(engine.dispatch, address=address, authkey=key)
            server.start()
            try:
                client = EngineClient(address=address, authkey=key)
                item = ServiceDefinition(name="api", executable=sys.executable, arguments=[str(script)], working_directory=str(base))
                saved = client.request("upsert_service", item=item.to_dict())
                self.assertTrue(saved["ok"], saved)
                listed = client.request("list")
                self.assertEqual(listed["services"][0]["definition"]["name"], "api")
                exported = base / "export.json"
                self.assertTrue(client.request("export", path=str(exported))["ok"])
                self.assertTrue(exported.is_file())
            finally:
                server.stop()
                engine.shutdown()


if __name__ == "__main__":
    unittest.main()
