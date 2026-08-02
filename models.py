from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any


class RestartMode(str, Enum):
    NEVER = "never"
    ON_FAILURE = "on_failure"
    ALWAYS = "always"


class HealthCheckType(str, Enum):
    PROCESS = "process"
    TCP = "tcp"
    HTTP = "http"
    COMMAND = "command"


class ScheduleType(str, Enum):
    NONE = "none"
    INTERVAL = "interval"
    DAILY = "daily"
    CRON = "cron"
    ONCE = "once"


@dataclass(slots=True)
class RestartPolicy:
    mode: str = RestartMode.ON_FAILURE.value
    skip_exit_codes: list[int] = field(default_factory=lambda: [0])
    initial_delay_seconds: float = 5.0
    backoff_multiplier: float = 2.0
    max_delay_seconds: float = 300.0
    max_attempts: int = 5
    window_seconds: float = 600.0
    stable_reset_seconds: float = 600.0
    jitter_ratio: float = 0.1

    @classmethod
    def from_dict(cls, value: Any) -> "RestartPolicy":
        raw = value if isinstance(value, dict) else {}
        return cls(**{key: raw[key] for key in cls.__dataclass_fields__ if key in raw})

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.mode not in {item.value for item in RestartMode}:
            errors.append("restart_policy.mode 값이 올바르지 않습니다.")
        if self.initial_delay_seconds < 0 or self.max_delay_seconds < self.initial_delay_seconds:
            errors.append("재시작 지연 시간 범위가 올바르지 않습니다.")
        if self.backoff_multiplier < 1 or self.max_attempts < 1 or self.window_seconds <= 0:
            errors.append("재시작 횟수 또는 백오프 설정이 올바르지 않습니다.")
        if not 0 <= self.jitter_ratio <= 1:
            errors.append("jitter_ratio는 0~1이어야 합니다.")
        return errors


@dataclass(slots=True)
class HealthCheck:
    type: str = HealthCheckType.PROCESS.value
    enabled: bool = False
    interval_seconds: float = 30.0
    timeout_seconds: float = 5.0
    failure_threshold: int = 3
    startup_grace_seconds: float = 30.0
    host: str | None = None
    port: int | None = None
    url: str | None = None
    command: list[str] = field(default_factory=list)
    expected_status: list[int] = field(default_factory=lambda: [200])

    @classmethod
    def from_dict(cls, value: Any) -> "HealthCheck":
        raw = value if isinstance(value, dict) else {}
        return cls(**{key: raw[key] for key in cls.__dataclass_fields__ if key in raw})

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.type not in {item.value for item in HealthCheckType}:
            errors.append("health_check.type 값이 올바르지 않습니다.")
        if self.interval_seconds <= 0 or self.timeout_seconds <= 0 or self.failure_threshold < 1:
            errors.append("상태 확인 주기·제한시간·실패 횟수가 올바르지 않습니다.")
        if self.enabled and self.type == HealthCheckType.TCP.value:
            if not self.host or not isinstance(self.port, int) or not 1 <= self.port <= 65535:
                errors.append("TCP 상태 확인에는 host와 1~65535 port가 필요합니다.")
        if self.enabled and self.type == HealthCheckType.HTTP.value and not str(self.url or "").startswith(("http://", "https://")):
            errors.append("HTTP 상태 확인 URL이 올바르지 않습니다.")
        if self.enabled and self.type == HealthCheckType.COMMAND.value and not self.command:
            errors.append("명령 상태 확인에는 command가 필요합니다.")
        return errors


@dataclass(slots=True)
class Schedule:
    type: str = ScheduleType.NONE.value
    interval_minutes: float | None = None
    daily_time: str | None = None
    cron: str | None = None
    once_at: str | None = None
    timezone: str = "Asia/Seoul"
    misfire_policy: str = "skip"
    overlap_policy: str = "skip"

    @classmethod
    def from_dict(cls, value: Any) -> "Schedule":
        raw = value if isinstance(value, dict) else {}
        return cls(**{key: raw[key] for key in cls.__dataclass_fields__ if key in raw})

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.type not in {item.value for item in ScheduleType}:
            errors.append("schedule.type 값이 올바르지 않습니다.")
        if self.type == ScheduleType.INTERVAL.value and (self.interval_minutes is None or self.interval_minutes <= 0):
            errors.append("주기 예약에는 0보다 큰 interval_minutes가 필요합니다.")
        if self.type == ScheduleType.DAILY.value and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", self.daily_time or ""):
            errors.append("일일 예약 시간은 HH:MM 형식이어야 합니다.")
        if self.type == ScheduleType.ONCE.value:
            try:
                datetime.fromisoformat(self.once_at or "")
            except ValueError:
                errors.append("1회 예약에는 ISO 8601 once_at이 필요합니다.")
        if self.type == ScheduleType.CRON.value and not self.cron:
            errors.append("Cron 예약에는 cron 표현식이 필요합니다.")
        if self.misfire_policy not in {"skip", "run_once"}:
            errors.append("misfire_policy는 skip 또는 run_once여야 합니다.")
        if self.overlap_policy not in {"skip", "queue", "replace"}:
            errors.append("overlap_policy는 skip, queue, replace 중 하나여야 합니다.")
        return errors


def new_id() -> str:
    return str(uuid.uuid4())


@dataclass(slots=True)
class ServiceDefinition:
    id: str = field(default_factory=new_id)
    name: str = ""
    enabled: bool = True
    executable: str = ""
    arguments: list[str] = field(default_factory=list)
    working_directory: str = ""
    environment: dict[str, str] = field(default_factory=dict)
    auto_start: bool = False
    restart_policy: RestartPolicy = field(default_factory=RestartPolicy)
    health_check: HealthCheck = field(default_factory=HealthCheck)
    schedule: Schedule = field(default_factory=Schedule)
    dependencies: list[str] = field(default_factory=list)
    stop_timeout_seconds: float = 10.0
    runtime_limit_seconds: float | None = None
    stop_command: list[str] = field(default_factory=list)
    priority: str = "normal"

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, base_dir: Path | None = None) -> "ServiceDefinition":
        base_dir = base_dir or Path.cwd()
        executable = raw.get("executable") or raw.get("python_executable") or ""
        arguments = list(raw.get("arguments") or [])
        if raw.get("script"):
            arguments = ["-u", str(raw["script"]), *map(str, arguments)]
        cwd = str(raw.get("working_directory") or base_dir)
        legacy_schedule = {
            "type": raw.get("schedule_type", "none"),
            "interval_minutes": raw.get("restart_interval_minutes"),
            "daily_time": raw.get("restart_time"),
        }
        return cls(
            id=str(raw.get("id") or new_id()),
            name=str(raw.get("name") or "").strip(),
            enabled=bool(raw.get("enabled", raw.get("_enabled", True))),
            executable=str(executable),
            arguments=[str(item) for item in arguments],
            working_directory=cwd,
            environment={str(k): str(v) for k, v in dict(raw.get("environment") or {}).items()},
            auto_start=bool(raw.get("auto_start", False)),
            restart_policy=RestartPolicy.from_dict(raw.get("restart_policy")),
            health_check=HealthCheck.from_dict(raw.get("health_check")),
            schedule=Schedule.from_dict(raw.get("schedule") or legacy_schedule),
            dependencies=[str(item) for item in raw.get("dependencies", [])],
            stop_timeout_seconds=float(raw.get("stop_timeout_seconds", 10)),
            runtime_limit_seconds=raw.get("runtime_limit_seconds"),
            stop_command=[str(item) for item in raw.get("stop_command", [])],
            priority=str(raw.get("priority", "normal")),
        )

    def validate(self, *, require_paths: bool = True) -> list[str]:
        errors: list[str] = []
        if not self.name:
            errors.append("서비스명이 필요합니다.")
        if not self.executable:
            errors.append("실행 파일이 필요합니다.")
        elif require_paths and not Path(self.executable).is_file():
            errors.append(f"실행 파일이 존재하지 않습니다: {self.executable}")
        if not self.working_directory:
            errors.append("작업 디렉터리가 필요합니다.")
        elif require_paths and not Path(self.working_directory).is_dir():
            errors.append(f"작업 디렉터리가 존재하지 않습니다: {self.working_directory}")
        if self.stop_timeout_seconds <= 0:
            errors.append("종료 제한시간은 0보다 커야 합니다.")
        errors.extend(self.restart_policy.validate())
        errors.extend(self.health_check.validate())
        errors.extend(self.schedule.validate())
        return errors

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class RemoteJobDefinition:
    id: str = field(default_factory=new_id)
    name: str = ""
    enabled: bool = True
    protocol: str = "ssh"
    host: str = ""
    port: int = 22
    username: str = ""
    credential_id: str | None = None
    auth_method: str = "password"
    private_key_path: str | None = None
    host_key_fingerprint: str | None = None
    commands: list[str] = field(default_factory=list)
    connect_timeout_seconds: float = 10.0
    command_timeout_seconds: float = 60.0
    total_timeout_seconds: float = 300.0
    retry_attempts: int = 1
    retry_delay_seconds: float = 5.0
    auto_run: bool = False
    schedule: Schedule = field(default_factory=Schedule)
    success_pattern: str | None = None
    failure_pattern: str | None = None
    login_prompt: str = "login:"
    password_prompt: str = "Password:"
    shell_prompt: str = "$"
    encoding: str = "utf-8"
    newline: str = "\n"
    legacy_telnet_confirmed: bool = False

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RemoteJobDefinition":
        protocol = str(raw.get("protocol") or ("telnet" if "login_prompt" in raw else "ssh")).lower()
        legacy_schedule = {
            "type": raw.get("schedule_type", "none"),
            "interval_minutes": raw.get("interval_minutes"),
            "daily_time": raw.get("run_time"),
        }
        values = {key: raw[key] for key in cls.__dataclass_fields__ if key in raw and key != "schedule"}
        values.update(
            id=str(raw.get("id") or new_id()),
            protocol=protocol,
            port=int(raw.get("port", 23 if protocol == "telnet" else 22)),
            schedule=Schedule.from_dict(raw.get("schedule") or legacy_schedule),
            legacy_telnet_confirmed=bool(raw.get("legacy_telnet_confirmed", protocol == "telnet")),
        )
        return cls(**values)

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.name:
            errors.append("원격 작업명이 필요합니다.")
        if self.protocol not in {"ssh", "telnet"}:
            errors.append("protocol은 ssh 또는 telnet이어야 합니다.")
        if not self.host or not 1 <= self.port <= 65535:
            errors.append("올바른 host와 port가 필요합니다.")
        if not self.username:
            errors.append("사용자 ID가 필요합니다.")
        if not self.commands:
            errors.append("한 개 이상의 명령이 필요합니다.")
        if self.protocol == "ssh" and not self.host_key_fingerprint:
            errors.append("SSH 호스트 키 지문을 확인하고 저장해야 합니다.")
        if self.protocol == "telnet" and not self.legacy_telnet_confirmed:
            errors.append("Telnet의 보안 위험을 확인해야 합니다.")
        if self.retry_attempts < 1 or self.total_timeout_seconds <= 0:
            errors.append("재시도 횟수와 전체 제한시간이 올바르지 않습니다.")
        for pattern in (self.success_pattern, self.failure_pattern):
            if pattern:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    errors.append(f"정규식이 올바르지 않습니다: {exc}")
        errors.extend(self.schedule.validate())
        return errors

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class RuntimeState:
    resource_id: str
    state: str = "stopped"
    pid: int | None = None
    started_at: str | None = None
    last_exit_code: int | None = None
    restart_count: int = 0
    circuit_open: bool = False
    failure_reason: str | None = None
    next_action_at: str | None = None
    health: str = "unknown"
    desired_running: bool = False
    restart_required: bool = False


@dataclass(slots=True)
class EventRecord:
    id: int | None
    timestamp: str
    level: str
    source_type: str
    source_id: str | None
    source_name: str
    event_type: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
