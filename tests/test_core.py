from __future__ import annotations

import json
import queue
import socketserver
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path

import psutil

from config_loader import load_config
from process_manager import ProcessManager
from schedule_utils import advance_due, initial_next_due
from telnet_worker import TelnetJobManager


class ScheduleTests(unittest.TestCase):
    def test_daily_schedule_uses_next_day_after_missed_time(self) -> None:
        now = datetime(2026, 7, 29, 5, 0, 0)
        due = initial_next_due("daily", now=now, daily_time="04:00")
        self.assertEqual(due, datetime(2026, 7, 30, 4, 0, 0))

    def test_interval_advance_skips_backfill(self) -> None:
        due = datetime(2026, 7, 29, 1, 0, 0)
        now = datetime(2026, 7, 29, 2, 7, 0)
        next_due = advance_due(due, "interval", now=now, interval_minutes=30)
        self.assertEqual(next_due, datetime(2026, 7, 29, 2, 30, 0))


class ConfigTests(unittest.TestCase):
    def test_valid_config_is_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            script = base / "run.py"
            script.write_text("print('ok')\n", encoding="utf-8")
            config = {
                "services": [{
                    "name": "demo",
                    "working_directory": str(base),
                    "python_executable": sys.executable,
                    "script": "run.py",
                    "arguments": [],
                    "auto_start": False,
                    "schedule_type": "none",
                    "restart_time": None,
                    "restart_interval_minutes": None,
                }],
                "telnet_jobs": [{
                    "name": "job",
                    "host": "127.0.0.1",
                    "port": 23,
                    "username": "user",
                    "password": "pw",
                    "login_prompt": "login:",
                    "password_prompt": "Password:",
                    "shell_prompt": "$",
                    "commands": ["date"],
                    "connect_timeout_seconds": 1,
                    "command_timeout_seconds": 1,
                    "auto_run": False,
                    "schedule_type": "none",
                }],
            }
            path = base / "config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            result = load_config(path)
            self.assertFalse(result.global_errors)
            self.assertTrue(result.services[0]["_enabled"])
            self.assertTrue(result.telnet_jobs[0]["_enabled"])

    def test_missing_config_returns_error_without_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = load_config(Path(tmp) / "missing.json")
            self.assertEqual(result.services, [])
            self.assertTrue(result.global_errors)

    def test_invalid_item_is_disabled_without_global_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"services": [{"name": "bad"}], "telnet_jobs": []}), encoding="utf-8")
            result = load_config(path)
            self.assertEqual(len(result.services), 1)
            self.assertFalse(result.services[0]["_enabled"])
            self.assertGreater(len(result.services[0]["_errors"]), 0)


class ProcessManagerTests(unittest.TestCase):
    def test_start_log_and_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            script = base / "service.py"
            script.write_text(
                "import subprocess, sys, time\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                "print(f'CHILD:{child.pid}', flush=True)\n"
                "print('READY', flush=True)\n"
                "while True: time.sleep(0.1)\n",
                encoding="utf-8",
            )
            events: queue.Queue = queue.Queue()
            service = {
                "name": "demo",
                "working_directory": str(base),
                "python_executable": sys.executable,
                "script": "service.py",
                "arguments": [],
                "_enabled": True,
                "_errors": [],
            }
            manager = ProcessManager([service], events)
            manager.start_async("demo")

            saw_running = False
            saw_ready = False
            child_pid = None
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not (saw_running and saw_ready and child_pid):
                try:
                    event = events.get(timeout=0.2)
                except queue.Empty:
                    continue
                if event.get("type") == "service_state" and event.get("state") == "실행 중":
                    saw_running = True
                if event.get("type") == "log" and event.get("message") == "READY":
                    saw_ready = True
                if event.get("type") == "log" and str(event.get("message", "")).startswith("CHILD:"):
                    child_pid = int(event["message"].split(":", 1)[1])
            self.assertTrue(saw_running)
            self.assertTrue(saw_ready)
            self.assertIsNotNone(child_pid)
            self.assertTrue(psutil.pid_exists(child_pid))

            manager.stop_async("demo")
            deadline = time.monotonic() + 7
            while time.monotonic() < deadline and manager.any_running():
                time.sleep(0.1)
            self.assertFalse(manager.any_running())
            child_deadline = time.monotonic() + 3
            while child_pid is not None and time.monotonic() < child_deadline and psutil.pid_exists(child_pid):
                time.sleep(0.1)
            self.assertFalse(psutil.pid_exists(child_pid))


class _TelnetHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        self.wfile.write(b"login:")
        self.wfile.flush()
        self.rfile.readline()
        self.wfile.write(b"Password:")
        self.wfile.flush()
        self.rfile.readline()
        self.wfile.write(b"$")
        self.wfile.flush()
        while True:
            line = self.rfile.readline()
            if not line:
                return
            command = line.strip()
            if command == b"exit":
                return
            self.wfile.write(b"ran:" + command + b"\r\n$")
            self.wfile.flush()


class TelnetManagerTests(unittest.TestCase):
    def test_login_command_and_success(self) -> None:
        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _TelnetHandler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            events: queue.Queue = queue.Queue()
            job = {
                "name": "mock",
                "host": "127.0.0.1",
                "port": server.server_address[1],
                "username": "user",
                "password": "pw",
                "login_prompt": "login:",
                "password_prompt": "Password:",
                "shell_prompt": "$",
                "commands": ["date"],
                "connect_timeout_seconds": 2,
                "command_timeout_seconds": 2,
                "_enabled": True,
                "_errors": [],
            }
            manager = TelnetJobManager([job], events)
            self.assertTrue(manager.run_async("mock", manual=True))
            self.assertFalse(manager.run_async("mock", manual=True))

            success = False
            output_found = False
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not (success and output_found):
                try:
                    event = events.get(timeout=0.2)
                except queue.Empty:
                    continue
                if event.get("type") == "telnet_state" and event.get("state") == "대기" and event.get("result") == "성공":
                    success = True
                if event.get("type") == "log" and "ran:date" in event.get("message", ""):
                    output_found = True
            self.assertTrue(success)
            self.assertTrue(output_found)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
