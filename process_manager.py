from __future__ import annotations

import locale
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil


@dataclass
class ServiceRuntime:
    config: dict[str, Any]
    process: subprocess.Popen[str] | None = None
    start_time: datetime | None = None
    stop_requested: bool = False
    state: str = "중지"
    data_lock: threading.RLock = field(default_factory=threading.RLock)
    operation_lock: threading.Lock = field(default_factory=threading.Lock)


class ProcessManager:
    def __init__(self, services: list[dict[str, Any]], event_queue: queue.Queue):
        self.event_queue = event_queue
        self.runtimes = {item["name"]: ServiceRuntime(item) for item in services}
        self._shutdown = threading.Event()

    def _emit(self, event_type: str, **payload: Any) -> None:
        self.event_queue.put({"type": event_type, **payload})

    def _log(self, name: str, message: str, stream: str = "system") -> None:
        self._emit(
            "log",
            source_type="service",
            name=name,
            message=message,
            stream=stream,
            timestamp=datetime.now(),
        )

    def initialize_states(self) -> None:
        for name, runtime in self.runtimes.items():
            if runtime.config.get("_enabled", False):
                self._emit("service_state", name=name, state="중지", pid=None, last_start=None)
            else:
                runtime.state = "오류"
                self._emit("service_state", name=name, state="오류", pid=None, last_start=None)
                for error in runtime.config.get("_errors", []):
                    self._log(name, f"설정 오류: {error}", "stderr")

    def start_async(self, name: str) -> None:
        runtime = self.runtimes.get(name)
        if runtime is None:
            return
        threading.Thread(target=self._start_worker, args=(runtime,), daemon=True).start()

    def stop_async(self, name: str) -> None:
        runtime = self.runtimes.get(name)
        if runtime is None:
            return
        threading.Thread(target=self._stop_worker, args=(runtime,), daemon=True).start()

    def restart_async(self, name: str) -> None:
        runtime = self.runtimes.get(name)
        if runtime is None:
            return
        threading.Thread(target=self._restart_worker, args=(runtime,), daemon=True).start()

    def start_all(self, delay_seconds: float = 0.0) -> None:
        def worker() -> None:
            for runtime in self.runtimes.values():
                if self._shutdown.is_set():
                    break
                if runtime.config.get("_enabled", False):
                    self._start_worker(runtime)
                    if delay_seconds > 0:
                        self._shutdown.wait(delay_seconds)

        threading.Thread(target=worker, daemon=True).start()

    def stop_all_async(self) -> None:
        def worker() -> None:
            for runtime in self.runtimes.values():
                self._stop_worker(runtime)

        threading.Thread(target=worker, daemon=True).start()

    def _start_worker(self, runtime: ServiceRuntime) -> None:
        with runtime.operation_lock:
            self._start_internal(runtime)

    def _start_internal(self, runtime: ServiceRuntime) -> bool:
        config = runtime.config
        name = config["name"]
        if self._shutdown.is_set():
            return False
        if not config.get("_enabled", False):
            self._log(name, "설정 오류가 있어 서비스를 시작할 수 없습니다.", "stderr")
            return False

        with runtime.data_lock:
            if runtime.process is not None and runtime.process.poll() is None:
                self._log(name, "이미 실행 중인 서비스입니다.")
                return False

        cwd = Path(config["working_directory"])
        python_executable = Path(config["python_executable"])
        script_path = cwd / config["script"]
        if not cwd.is_dir() or not python_executable.is_file() or not script_path.is_file():
            runtime.state = "오류"
            self._emit("service_state", name=name, state="오류", pid=None, last_start=runtime.start_time)
            self._log(name, "실행 경로를 다시 확인하십시오.", "stderr")
            return False

        command = [str(python_executable), "-u", config["script"], *config.get("arguments", [])]
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]

        self._log(name, f"서비스 시작: {' '.join(command)}")
        try:
            process = subprocess.Popen(
                command,
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding=locale.getpreferredencoding(False),
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            with runtime.data_lock:
                runtime.process = None
                runtime.state = "오류"
            self._emit("service_state", name=name, state="오류", pid=None, last_start=runtime.start_time)
            self._log(name, f"프로세스 생성 실패: {exc}", "stderr")
            return False

        started_at = datetime.now()
        with runtime.data_lock:
            runtime.process = process
            runtime.start_time = started_at
            runtime.stop_requested = False
            runtime.state = "실행 중"

        self._emit(
            "service_state",
            name=name,
            state="실행 중",
            pid=process.pid,
            last_start=started_at,
        )

        if process.stdout is not None:
            threading.Thread(
                target=self._read_stream,
                args=(runtime, process, process.stdout, "stdout"),
                daemon=True,
            ).start()
        if process.stderr is not None:
            threading.Thread(
                target=self._read_stream,
                args=(runtime, process, process.stderr, "stderr"),
                daemon=True,
            ).start()
        threading.Thread(target=self._monitor_process, args=(runtime, process), daemon=True).start()
        return True

    def _read_stream(self, runtime: ServiceRuntime, process: subprocess.Popen[str], stream, stream_name: str) -> None:
        try:
            for line in iter(stream.readline, ""):
                if line == "":
                    break
                self._log(runtime.config["name"], line.rstrip("\r\n"), stream_name)
                if process.poll() is not None:
                    # Drain remaining buffered lines, then exit naturally.
                    continue
        except (OSError, ValueError) as exc:
            if process.poll() is None:
                self._log(runtime.config["name"], f"출력 읽기 오류: {exc}", "stderr")
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _monitor_process(self, runtime: ServiceRuntime, process: subprocess.Popen[str]) -> None:
        name = runtime.config["name"]
        try:
            return_code = process.wait()
        except Exception as exc:  # Defensive: monitor failure must not kill the manager.
            self._log(name, f"프로세스 종료 감시 오류: {exc}", "stderr")
            return

        with runtime.data_lock:
            if runtime.process is not process:
                return
            requested = runtime.stop_requested
            runtime.process = None
            runtime.stop_requested = False
            runtime.state = "중지" if requested or return_code == 0 else "오류"
            state = runtime.state
            last_start = runtime.start_time

        self._emit("service_state", name=name, state=state, pid=None, last_start=last_start)
        if requested:
            self._log(name, "서비스가 종료되었습니다.")
        elif return_code == 0:
            self._log(name, "서비스가 정상 종료되었습니다.")
        else:
            self._log(name, f"서비스가 오류 코드 {return_code}로 종료되었습니다.", "stderr")

    def _stop_worker(self, runtime: ServiceRuntime) -> None:
        with runtime.operation_lock:
            self._stop_internal(runtime)

    def _stop_internal(self, runtime: ServiceRuntime) -> bool:
        name = runtime.config["name"]
        with runtime.data_lock:
            process = runtime.process
            if process is None or process.poll() is not None:
                runtime.process = None
                runtime.stop_requested = False
                runtime.state = "중지"
                self._emit(
                    "service_state",
                    name=name,
                    state=runtime.state,
                    pid=None,
                    last_start=runtime.start_time,
                )
                self._log(name, "서비스가 이미 중지되어 있습니다.")
                return False
            runtime.stop_requested = True
            pid = process.pid

        self._log(name, f"프로세스 트리 종료 요청 (PID {pid})")
        try:
            self._terminate_process_tree(pid)
        except psutil.NoSuchProcess:
            self._log(name, "종료 대상 PID가 이미 존재하지 않습니다.")
        except (psutil.Error, OSError) as exc:
            self._log(name, f"프로세스 종료 실패: {exc}", "stderr")
            try:
                process.kill()
            except OSError:
                pass

        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                self._log(name, "프로세스를 완전히 종료하지 못했습니다.", "stderr")
                return False
        return True

    @staticmethod
    def _terminate_process_tree(pid: int) -> None:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        for child in reversed(children):
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(children, timeout=3)
        for child in alive:
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        try:
            parent.terminate()
            parent.wait(timeout=3)
        except psutil.TimeoutExpired:
            parent.kill()
            parent.wait(timeout=3)

    def _restart_worker(self, runtime: ServiceRuntime) -> None:
        with runtime.operation_lock:
            with runtime.data_lock:
                running = runtime.process is not None and runtime.process.poll() is None
            if running:
                self._log(runtime.config["name"], "서비스 재시작을 시작합니다.")
                self._stop_internal(runtime)
                if self._shutdown.wait(2.0):
                    return
            self._start_internal(runtime)

    def get_snapshot(self, name: str) -> dict[str, Any]:
        runtime = self.runtimes[name]
        with runtime.data_lock:
            process = runtime.process
            return {
                "state": runtime.state,
                "pid": process.pid if process is not None and process.poll() is None else None,
                "last_start": runtime.start_time,
            }

    def any_running(self) -> bool:
        for runtime in self.runtimes.values():
            with runtime.data_lock:
                if runtime.process is not None and runtime.process.poll() is None:
                    return True
        return False

    def shutdown(self, stop_services: bool) -> None:
        self._shutdown.set()
        if stop_services:
            self.stop_all_async()
