from __future__ import annotations

import base64
import hashlib
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

try:
    import paramiko
except ImportError:  # The engine reports a configuration error until the optional runtime is installed.
    paramiko = None  # type: ignore

try:
    import telnetlib
except ImportError:
    import simple_telnet as telnetlib

from event_logging import EventLogger
from models import RemoteJobDefinition
from storage import Repository


@dataclass
class _RemoteRuntime:
    definition: RemoteJobDefinition
    running: bool = False
    cancel_event: threading.Event = field(default_factory=threading.Event)
    lock: threading.RLock = field(default_factory=threading.RLock)
    state: str = "idle"
    result: str = "-"
    last_run: str | None = None
    failure_reason: str | None = None
    queued: bool = False


class _FingerprintPolicy:
    def __init__(self, expected: str):
        self.expected = expected.strip()

    def missing_host_key(self, _client, hostname, key) -> None:
        digest = base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode("ascii").rstrip("=")
        actual = f"SHA256:{digest}"
        if actual.casefold() != self.expected.casefold():
            raise paramiko.SSHException(f"SSH 호스트 키 불일치 ({hostname}): 예상 {self.expected}, 실제 {actual}")


def fetch_ssh_fingerprint(host: str, port: int = 22, timeout: float = 10.0) -> str:
    if paramiko is None:
        raise RuntimeError("SSH 기능을 사용하려면 paramiko가 필요합니다.")
    sock = socket.create_connection((host, port), timeout=timeout)
    transport = paramiko.Transport(sock)
    try:
        transport.start_client(timeout=timeout)
        key = transport.get_remote_server_key()
        digest = base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode("ascii").rstrip("=")
        return f"SHA256:{digest}"
    finally:
        transport.close()
        sock.close()


class RemoteJobManager:
    def __init__(self, repository: Repository, logger: EventLogger):
        self.repository = repository
        self.logger = logger
        self._runtimes: dict[str, _RemoteRuntime] = {}
        self._shutdown = threading.Event()
        self.reload()

    def reload(self) -> None:
        definitions = {item.id: item for item in self.repository.list_remote_jobs()}
        for resource_id, definition in definitions.items():
            if resource_id in self._runtimes:
                self._runtimes[resource_id].definition = definition
            else:
                self._runtimes[resource_id] = _RemoteRuntime(definition)
        for resource_id in set(self._runtimes) - set(definitions):
            if not self._runtimes[resource_id].running:
                del self._runtimes[resource_id]

    def run(self, resource_id: str, *, manual: bool = True) -> bool:
        runtime = self._runtimes.get(resource_id)
        if runtime is None or not runtime.definition.enabled or self._shutdown.is_set():
            return False
        with runtime.lock:
            if runtime.running:
                policy = runtime.definition.schedule.overlap_policy
                if policy == "queue":
                    runtime.queued = True
                    self.logger.emit("INFO", "remote_job", runtime.definition.name, "overlap_queued", "현재 작업 완료 후 한 번 더 실행합니다.", source_id=resource_id)
                elif policy == "replace":
                    runtime.queued = True
                    runtime.cancel_event.set()
                    self.logger.emit("WARNING", "remote_job", runtime.definition.name, "overlap_replacing", "현재 작업을 취소하고 새 실행을 대기시킵니다.", source_id=resource_id)
                else:
                    self.logger.emit("WARNING", "remote_job", runtime.definition.name, "overlap_skipped", "작업이 이미 실행 중이어서 중복 실행을 건너뜁니다.", source_id=resource_id)
                return False
            errors = runtime.definition.validate()
            if errors:
                runtime.state = "configuration_error"
                runtime.failure_reason = "; ".join(errors)
                self.logger.emit("ERROR", "remote_job", runtime.definition.name, "configuration_error", runtime.failure_reason, source_id=resource_id)
                return False
            runtime.running = True
            runtime.state = "running"
            runtime.result = "수동 실행" if manual else "예약 실행"
            runtime.cancel_event.clear()
        threading.Thread(target=self._run_worker, args=(runtime,), name=f"remote-{resource_id}", daemon=True).start()
        return True

    def _run_worker(self, runtime: _RemoteRuntime) -> None:
        definition = runtime.definition
        started = time.monotonic()
        last_error: Exception | None = None
        for attempt in range(1, definition.retry_attempts + 1):
            if self._shutdown.is_set() or runtime.cancel_event.is_set():
                last_error = RuntimeError("작업이 취소되었습니다.")
                break
            try:
                password = self.repository.get_credential(definition.credential_id)
                self.logger.register_secret(password)
                self.logger.emit("INFO", "remote_job", definition.name, "job_started", f"{definition.protocol.upper()} 작업 시작: {definition.host}:{definition.port}", source_id=definition.id, details={"attempt": attempt})
                if definition.protocol == "ssh":
                    self._run_ssh(runtime, password, started)
                else:
                    self._run_telnet(runtime, password or "", started)
                self._complete(runtime, True, "성공", None)
                return
            except Exception as exc:
                last_error = exc
                self.logger.emit("ERROR", "remote_job", definition.name, "attempt_failed", f"시도 {attempt} 실패: {exc}", source_id=definition.id)
                if attempt < definition.retry_attempts and not self._shutdown.wait(definition.retry_delay_seconds):
                    continue
                break
        self._complete(runtime, False, "실패", str(last_error or "알 수 없는 오류"))

    def _run_ssh(self, runtime: _RemoteRuntime, password: str | None, started: float) -> None:
        if paramiko is None:
            raise RuntimeError("SSH 기능을 사용하려면 paramiko가 필요합니다.")
        definition = runtime.definition
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(_FingerprintPolicy(definition.host_key_fingerprint or ""))
        kwargs: dict[str, Any] = {
            "hostname": definition.host,
            "port": definition.port,
            "username": definition.username,
            "timeout": definition.connect_timeout_seconds,
            "banner_timeout": definition.connect_timeout_seconds,
            "auth_timeout": definition.connect_timeout_seconds,
            "look_for_keys": definition.auth_method in {"agent", "key"},
            "allow_agent": definition.auth_method == "agent",
        }
        if definition.auth_method == "password":
            kwargs["password"] = password
        elif definition.auth_method == "key":
            kwargs["key_filename"] = definition.private_key_path
            kwargs["passphrase"] = password
        try:
            client.connect(**kwargs)
            for index, command in enumerate(definition.commands, 1):
                self._check_deadline(runtime, started)
                self.logger.emit("INFO", "remote_job", definition.name, "command_started", f"> {command}", source_id=definition.id, details={"step": index})
                stdin, stdout, stderr = client.exec_command(command, timeout=definition.command_timeout_seconds)
                stdin.close()
                output = stdout.read().decode(definition.encoding, errors="replace")
                error = stderr.read().decode(definition.encoding, errors="replace")
                exit_code = stdout.channel.recv_exit_status()
                self._log_output(definition, output, "stdout")
                self._log_output(definition, error, "stderr")
                self._assert_patterns(definition, output + "\n" + error)
                if exit_code != 0:
                    raise RuntimeError(f"명령 {index}이 종료 코드 {exit_code}로 실패했습니다.")
        finally:
            client.close()

    def _run_telnet(self, runtime: _RemoteRuntime, password: str, started: float) -> None:
        definition = runtime.definition
        tn = telnetlib.Telnet(definition.host, definition.port, timeout=definition.connect_timeout_seconds)
        try:
            self._read_until(tn, definition.login_prompt, definition.connect_timeout_seconds, definition)
            tn.write(definition.username.encode(definition.encoding) + definition.newline.encode(definition.encoding))
            self._read_until(tn, definition.password_prompt, definition.connect_timeout_seconds, definition)
            tn.write(password.encode(definition.encoding) + definition.newline.encode(definition.encoding))
            self._read_until(tn, definition.shell_prompt, definition.command_timeout_seconds, definition)
            for index, command in enumerate(definition.commands, 1):
                self._check_deadline(runtime, started)
                self.logger.emit("INFO", "remote_job", definition.name, "command_started", f"> {command}", source_id=definition.id, details={"step": index})
                tn.write(command.encode(definition.encoding) + definition.newline.encode(definition.encoding))
                data = self._read_until(tn, definition.shell_prompt, definition.command_timeout_seconds, definition)
                output = data.decode(definition.encoding, errors="replace")
                self._log_output(definition, output, "stdout")
                self._assert_patterns(definition, output)
            try:
                tn.write(b"exit\n")
            except (OSError, EOFError):
                pass
        finally:
            tn.close()

    @staticmethod
    def _read_until(tn, prompt: str, timeout: float, definition: RemoteJobDefinition) -> bytes:
        expected = prompt.encode(definition.encoding)
        data = tn.read_until(expected, timeout=timeout)
        if expected not in data:
            raise TimeoutError(f"프롬프트를 제한시간 내에 찾지 못했습니다: {prompt!r}")
        return data

    @staticmethod
    def _assert_patterns(definition: RemoteJobDefinition, output: str) -> None:
        if definition.failure_pattern and re.search(definition.failure_pattern, output, re.MULTILINE):
            raise RuntimeError("실패 패턴이 출력에서 발견되었습니다.")
        if definition.success_pattern and not re.search(definition.success_pattern, output, re.MULTILINE):
            raise RuntimeError("성공 패턴을 출력에서 찾지 못했습니다.")

    def _log_output(self, definition: RemoteJobDefinition, output: str, stream: str) -> None:
        for line in output.replace("\r\n", "\n").replace("\r", "\n").splitlines():
            if line.strip():
                self.logger.emit("ERROR" if stream == "stderr" else "INFO", "remote_job", definition.name, "command_output", line, source_id=definition.id, stream=stream)

    @staticmethod
    def _check_deadline(runtime: _RemoteRuntime, started: float) -> None:
        if runtime.cancel_event.is_set():
            raise RuntimeError("작업이 취소되었습니다.")
        if time.monotonic() - started > runtime.definition.total_timeout_seconds:
            raise TimeoutError("전체 실행 제한시간을 초과했습니다.")

    def _complete(self, runtime: _RemoteRuntime, success: bool, result: str, reason: str | None) -> None:
        with runtime.lock:
            runtime.running = False
            runtime.state = "idle" if success else "failed"
            runtime.result = result
            runtime.failure_reason = reason
            runtime.last_run = datetime.now(timezone.utc).isoformat()
            queued = runtime.queued
            runtime.queued = False
        self.repository.save_runtime_state(runtime.definition.id, self._snapshot(runtime))
        self.logger.emit("INFO" if success else "ERROR", "remote_job", runtime.definition.name, "job_completed" if success else "job_failed", "원격 작업이 완료되었습니다." if success else f"원격 작업 실패: {reason}", source_id=runtime.definition.id)
        if queued and not self._shutdown.is_set():
            self.run(runtime.definition.id, manual=False)

    @staticmethod
    def _snapshot(runtime: _RemoteRuntime) -> dict[str, Any]:
        return {
            "resource_id": runtime.definition.id, "state": runtime.state, "running": runtime.running,
            "result": runtime.result, "last_run": runtime.last_run, "failure_reason": runtime.failure_reason,
        }

    def snapshots(self) -> list[dict[str, Any]]:
        return [{"definition": item.definition.to_dict(), "runtime": self._snapshot(item)} for item in self._runtimes.values()]

    def shutdown(self) -> None:
        self._shutdown.set()
        for runtime in self._runtimes.values():
            runtime.cancel_event.set()
