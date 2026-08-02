from __future__ import annotations

import locale
import os
import random
import re
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import psutil

from event_logging import EventLogger
from health_checks import HealthChecker
from job_object import JobObject
from models import RestartMode, RuntimeState, ServiceDefinition
from storage import Repository


@dataclass
class _Runtime:
    definition: ServiceDefinition
    process: subprocess.Popen[str] | None = None
    job_object: JobObject | None = None
    state: RuntimeState | None = None
    desired_running: bool = False
    stop_requested: bool = False
    started_monotonic: float | None = None
    failures: deque[float] = field(default_factory=deque)
    restart_generation: int = 0
    health_failures: int = 0
    last_health_check: float = 0.0
    operation_lock: threading.RLock = field(default_factory=threading.RLock)

    def __post_init__(self) -> None:
        self.state = RuntimeState(resource_id=self.definition.id)


class ServiceSupervisor:
    def __init__(self, repository: Repository, logger: EventLogger):
        self.repository = repository
        self.logger = logger
        self._runtimes: dict[str, _Runtime] = {}
        self._lock = threading.RLock()
        self._shutdown = threading.Event()
        self._health_checker = HealthChecker()
        self.reload()
        self._health_thread = threading.Thread(target=self._health_loop, name="health-monitor", daemon=True)
        self._health_thread.start()

    def reload(self) -> None:
        definitions = {item.id: item for item in self.repository.list_services()}
        persisted = self.repository.get_runtime_states()
        with self._lock:
            for resource_id, definition in definitions.items():
                for key, value in definition.environment.items():
                    if re.search(r"(?i)(password|passwd|token|api[_-]?key|secret)", key):
                        self.logger.register_secret(value)
                if resource_id in self._runtimes:
                    runtime = self._runtimes[resource_id]
                    previous = runtime.definition
                    runtime.definition = definition
                    if runtime.process is not None and runtime.process.poll() is None and self._execution_signature(previous) != self._execution_signature(definition):
                        assert runtime.state is not None
                        runtime.state.restart_required = True
                        self._persist(runtime)
                else:
                    runtime = _Runtime(definition)
                    saved = persisted.get(resource_id, {})
                    if runtime.state is not None:
                        for key in RuntimeState.__dataclass_fields__:
                            if key in saved:
                                setattr(runtime.state, key, saved[key])
                        runtime.state.pid = None
                        runtime.state.state = "stopped"
                    runtime.desired_running = bool(saved.get("desired_running", False))
                    self._runtimes[resource_id] = runtime
            for resource_id in set(self._runtimes) - set(definitions):
                runtime = self._runtimes[resource_id]
                if not runtime.process or runtime.process.poll() is not None:
                    del self._runtimes[resource_id]

    def start_auto_services(self) -> None:
        for runtime in list(self._runtimes.values()):
            if runtime.definition.enabled and (runtime.definition.auto_start or runtime.desired_running):
                self.start(runtime.definition.id, manual=not runtime.desired_running)

    def start(self, resource_id: str, *, manual: bool = True) -> bool:
        runtime = self._get(resource_id)
        if not runtime:
            return False
        if manual:
            with runtime.operation_lock:
                runtime.failures.clear()
                runtime.restart_generation += 1
                assert runtime.state is not None
                runtime.state.circuit_open = False
                runtime.state.failure_reason = None
        runtime.desired_running = True
        if runtime.state is not None:
            runtime.state.desired_running = True
            self._persist(runtime)
        threading.Thread(target=self._start_worker, args=(runtime,), name=f"start-{resource_id}", daemon=True).start()
        return True

    def _start_worker(self, runtime: _Runtime) -> None:
        with runtime.operation_lock:
            definition = runtime.definition
            assert runtime.state is not None
            if self._shutdown.is_set() or not definition.enabled or runtime.state.circuit_open:
                return
            if runtime.process is not None and runtime.process.poll() is None:
                return
            for dependency in definition.dependencies:
                dep = self._get(dependency)
                dependency_unready = dep is None or dep.process is None or dep.process.poll() is not None
                if dep is not None and dep.definition.health_check.enabled and dep.state is not None:
                    dependency_unready = dependency_unready or dep.state.health != "healthy"
                if dependency_unready:
                    self._set_state(runtime, "waiting_dependency", failure_reason=f"의존 서비스 대기: {dependency}")
                    self.logger.emit("WARNING", "service", definition.name, "dependency_wait", f"의존 서비스가 실행 중이 아닙니다: {dependency}", source_id=definition.id)
                    generation = runtime.restart_generation

                    def retry_dependency() -> None:
                        if not self._shutdown.wait(5) and runtime.desired_running and runtime.restart_generation == generation:
                            self._start_worker(runtime)

                    threading.Thread(target=retry_dependency, daemon=True).start()
                    return
            errors = definition.validate()
            if errors:
                self._set_state(runtime, "configuration_error", failure_reason="; ".join(errors))
                self.logger.emit("ERROR", "service", definition.name, "configuration_error", "; ".join(errors), source_id=definition.id)
                return
            command = [definition.executable, *definition.arguments]
            environment = os.environ.copy()
            environment.update(definition.environment)
            creationflags = 0
            if os.name == "nt":
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
            self.logger.emit("INFO", "service", definition.name, "start_requested", f"서비스 시작: {self.logger.redactor.redact(' '.join(command))}", source_id=definition.id)
            try:
                job = JobObject(f"ServiceManager-{definition.id}")
                process = subprocess.Popen(
                    command, cwd=definition.working_directory, env=environment, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    encoding=locale.getpreferredencoding(False), errors="replace", bufsize=1,
                    creationflags=creationflags,
                )
                if os.name == "nt":
                    job.assign(int(process._handle))  # type: ignore[attr-defined]
            except (OSError, subprocess.SubprocessError) as exc:
                try:
                    job.close()
                except (NameError, OSError):
                    pass
                self._set_state(runtime, "failed", failure_reason=str(exc))
                self.logger.emit("ERROR", "service", definition.name, "start_failed", f"프로세스 생성 실패: {exc}", source_id=definition.id)
                self._schedule_recovery(runtime, exit_code=None)
                return
            runtime.process = process
            runtime.job_object = job
            runtime.stop_requested = False
            runtime.started_monotonic = time.monotonic()
            runtime.health_failures = 0
            runtime.state.pid = process.pid
            runtime.state.started_at = datetime.now(timezone.utc).isoformat()
            runtime.state.health = "starting"
            runtime.state.restart_required = False
            self._set_state(runtime, "running")
            if runtime.failures:
                self.logger.emit("INFO", "service", definition.name, "recovered", "서비스가 자동복구되었습니다.", source_id=definition.id)
            for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
                if stream is not None:
                    threading.Thread(target=self._read_stream, args=(runtime, process, stream, name), daemon=True).start()
            threading.Thread(target=self._monitor, args=(runtime, process), name=f"monitor-{definition.id}", daemon=True).start()

    def stop(self, resource_id: str, *, manual: bool = True) -> bool:
        runtime = self._get(resource_id)
        if not runtime:
            return False
        if manual:
            runtime.desired_running = False
            runtime.restart_generation += 1
            if runtime.state is not None:
                runtime.state.desired_running = False
                self._persist(runtime)
        threading.Thread(target=self._stop_worker, args=(runtime,), name=f"stop-{resource_id}", daemon=True).start()
        return True

    def _stop_worker(self, runtime: _Runtime) -> None:
        with runtime.operation_lock:
            process = runtime.process
            if process is None or process.poll() is not None:
                self._set_state(runtime, "stopped")
                return
            runtime.stop_requested = True
            definition = runtime.definition
            self._set_state(runtime, "stopping")
            self.logger.emit("INFO", "service", definition.name, "stop_requested", f"종료 요청 (PID {process.pid})", source_id=definition.id)
            if definition.stop_command:
                try:
                    subprocess.run(definition.stop_command, cwd=definition.working_directory, timeout=definition.stop_timeout_seconds / 2, check=False)
                except (OSError, subprocess.SubprocessError):
                    pass
            elif os.name == "nt":
                try:
                    process.send_signal(getattr(__import__("signal"), "CTRL_BREAK_EVENT"))
                except OSError:
                    pass
            else:
                process.terminate()
            try:
                process.wait(timeout=definition.stop_timeout_seconds)
            except subprocess.TimeoutExpired:
                if runtime.job_object:
                    runtime.job_object.close()
                else:
                    self._kill_tree(process.pid)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()

    def restart(self, resource_id: str) -> bool:
        runtime = self._get(resource_id)
        if not runtime:
            return False
        runtime.desired_running = True
        if runtime.state is not None:
            runtime.state.desired_running = True
        runtime.restart_generation += 1
        threading.Thread(target=self._restart_worker, args=(runtime,), daemon=True).start()
        return True

    def _restart_worker(self, runtime: _Runtime) -> None:
        self._stop_worker(runtime)
        if not self._shutdown.wait(1):
            self._start_worker(runtime)

    def _read_stream(self, runtime: _Runtime, process: subprocess.Popen[str], stream, stream_name: str) -> None:
        try:
            for line in iter(stream.readline, ""):
                if line:
                    self.logger.emit(
                        "ERROR" if stream_name == "stderr" else "INFO", "service", runtime.definition.name,
                        "process_output", line.rstrip("\r\n"), source_id=runtime.definition.id, stream=stream_name,
                    )
                if process.poll() is not None and not line:
                    break
        except (OSError, ValueError) as exc:
            self.logger.emit("WARNING", "service", runtime.definition.name, "stream_error", str(exc), source_id=runtime.definition.id)
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _monitor(self, runtime: _Runtime, process: subprocess.Popen[str]) -> None:
        try:
            exit_code = process.wait()
        except Exception as exc:
            self.logger.emit("ERROR", "service", runtime.definition.name, "monitor_error", str(exc), source_id=runtime.definition.id)
            return
        with runtime.operation_lock:
            if runtime.process is not process:
                return
            requested = runtime.stop_requested
            runtime.process = None
            runtime.stop_requested = False
            runtime.state.pid = None  # type: ignore[union-attr]
            runtime.state.last_exit_code = exit_code  # type: ignore[union-attr]
            if runtime.job_object:
                runtime.job_object.close()
                runtime.job_object = None
            if requested:
                self._set_state(runtime, "stopped")
                self.logger.emit("INFO", "service", runtime.definition.name, "stopped", "서비스가 종료되었습니다.", source_id=runtime.definition.id, details={"exit_code": exit_code})
                return
            level = "INFO" if exit_code == 0 else "ERROR"
            self._set_state(runtime, "stopped" if exit_code == 0 else "failed", failure_reason=None if exit_code == 0 else f"종료 코드 {exit_code}")
            self.logger.emit(level, "service", runtime.definition.name, "exited", f"서비스가 종료 코드 {exit_code}로 종료되었습니다.", source_id=runtime.definition.id, details={"exit_code": exit_code})
            self._schedule_recovery(runtime, exit_code=exit_code)

    def _schedule_recovery(self, runtime: _Runtime, *, exit_code: int | None, reason: str | None = None) -> None:
        policy = runtime.definition.restart_policy
        if not runtime.desired_running or policy.mode == RestartMode.NEVER.value:
            return
        if policy.mode == RestartMode.ON_FAILURE.value and exit_code in policy.skip_exit_codes:
            return
        now = time.monotonic()
        while runtime.failures and now - runtime.failures[0] > policy.window_seconds:
            runtime.failures.popleft()
        runtime.failures.append(now)
        assert runtime.state is not None
        if len(runtime.failures) >= policy.max_attempts:
            runtime.state.circuit_open = True
            runtime.state.failure_reason = reason or f"{policy.window_seconds:g}초 동안 {len(runtime.failures)}회 실패"
            runtime.state.next_action_at = None
            self._set_state(runtime, "circuit_open")
            self.logger.emit("CRITICAL", "service", runtime.definition.name, "circuit_open", "반복 실패로 자동 재시작을 중단했습니다.", source_id=runtime.definition.id)
            return
        attempt = len(runtime.failures)
        base = min(policy.max_delay_seconds, policy.initial_delay_seconds * policy.backoff_multiplier ** (attempt - 1))
        delay = max(0, base * (1 + random.uniform(-policy.jitter_ratio, policy.jitter_ratio)))
        generation = runtime.restart_generation
        runtime.state.next_action_at = datetime.fromtimestamp(time.time() + delay, timezone.utc).isoformat()
        runtime.state.restart_count += 1
        self._set_state(runtime, "waiting_restart")
        self.logger.emit("WARNING", "service", runtime.definition.name, "restart_scheduled", f"{delay:.1f}초 후 자동 재시작합니다.", source_id=runtime.definition.id, details={"attempt": attempt})

        def delayed() -> None:
            if self._shutdown.wait(delay):
                return
            if runtime.restart_generation == generation and runtime.desired_running and not runtime.state.circuit_open:
                runtime.state.next_action_at = None
                self._start_worker(runtime)

        threading.Thread(target=delayed, daemon=True).start()

    def _health_loop(self) -> None:
        while not self._shutdown.wait(1):
            now = time.monotonic()
            for runtime in list(self._runtimes.values()):
                process = runtime.process
                check = runtime.definition.health_check
                if not check.enabled or process is None or process.poll() is not None or runtime.started_monotonic is None:
                    continue
                if now - runtime.started_monotonic < check.startup_grace_seconds or now - runtime.last_health_check < check.interval_seconds:
                    continue
                runtime.last_health_check = now
                environment = os.environ.copy()
                environment.update(runtime.definition.environment)
                result = self._health_checker.check(check, cwd=runtime.definition.working_directory, environment=environment)
                assert runtime.state is not None
                runtime.state.health = "healthy" if result.healthy else "unhealthy"
                if result.healthy:
                    runtime.health_failures = 0
                    if now - runtime.started_monotonic >= runtime.definition.restart_policy.stable_reset_seconds:
                        runtime.failures.clear()
                else:
                    runtime.health_failures += 1
                    self.logger.emit("WARNING", "service", runtime.definition.name, "health_failed", result.message, source_id=runtime.definition.id, details={"consecutive": runtime.health_failures})
                    if runtime.health_failures >= check.failure_threshold:
                        self.logger.emit("ERROR", "service", runtime.definition.name, "health_recovery", "상태 확인 실패 임계값에 도달해 재시작합니다.", source_id=runtime.definition.id)
                        self.restart(runtime.definition.id)
                self._persist(runtime)
                if runtime.definition.runtime_limit_seconds and now - runtime.started_monotonic >= runtime.definition.runtime_limit_seconds:
                    self.logger.emit("WARNING", "service", runtime.definition.name, "runtime_limit", "최대 실행시간에 도달했습니다.", source_id=runtime.definition.id)
                    runtime.desired_running = False
                    runtime.state.desired_running = False
                    self.stop(runtime.definition.id, manual=False)

    def _set_state(self, runtime: _Runtime, state: str, *, failure_reason: str | None = None) -> None:
        assert runtime.state is not None
        runtime.state.state = state
        if failure_reason is not None:
            runtime.state.failure_reason = failure_reason
        self._persist(runtime)

    def _persist(self, runtime: _Runtime) -> None:
        assert runtime.state is not None
        from dataclasses import asdict
        try:
            self.repository.save_runtime_state(runtime.definition.id, asdict(runtime.state))
        except (OSError, ValueError):
            pass

    def _get(self, resource_id: str) -> _Runtime | None:
        with self._lock:
            return self._runtimes.get(resource_id)

    @staticmethod
    def _execution_signature(definition: ServiceDefinition) -> tuple[Any, ...]:
        return (
            definition.executable, tuple(definition.arguments), definition.working_directory,
            tuple(sorted(definition.environment.items())), tuple(definition.stop_command),
            definition.stop_timeout_seconds, definition.priority,
        )

    def snapshots(self) -> list[dict[str, Any]]:
        from dataclasses import asdict
        with self._lock:
            snapshots: list[dict[str, Any]] = []
            for runtime in self._runtimes.values():
                if runtime.state is None:
                    continue
                state = asdict(runtime.state)
                process = runtime.process
                if process is not None and process.poll() is None:
                    try:
                        managed = psutil.Process(process.pid)
                        state["cpu_percent"] = managed.cpu_percent(interval=None)
                        state["memory_bytes"] = managed.memory_info().rss
                    except psutil.Error:
                        state["cpu_percent"] = None
                        state["memory_bytes"] = None
                snapshots.append({"definition": runtime.definition.to_dict(), "runtime": state})
            return snapshots

    @staticmethod
    def _kill_tree(pid: int) -> None:
        try:
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
            parent.terminate()
            try:
                parent.wait(timeout=3)
            except psutil.TimeoutExpired:
                parent.kill()
        except psutil.NoSuchProcess:
            pass

    def shutdown(self) -> None:
        self._shutdown.set()
        for runtime in list(self._runtimes.values()):
            runtime.desired_running = False
            runtime.restart_generation += 1
            self._stop_worker(runtime)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if all(runtime.process is None for runtime in self._runtimes.values()):
                break
            time.sleep(0.02)
