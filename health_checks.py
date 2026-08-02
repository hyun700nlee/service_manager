from __future__ import annotations

import socket
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass

from models import HealthCheck, HealthCheckType


@dataclass(slots=True)
class HealthResult:
    healthy: bool
    message: str


class HealthChecker:
    def check(self, config: HealthCheck, *, cwd: str | None = None, environment: dict[str, str] | None = None) -> HealthResult:
        try:
            if not config.enabled or config.type == HealthCheckType.PROCESS.value:
                return HealthResult(True, "프로세스 실행 중")
            if config.type == HealthCheckType.TCP.value:
                with socket.create_connection((str(config.host), int(config.port or 0)), timeout=config.timeout_seconds):
                    return HealthResult(True, f"TCP 연결 성공: {config.host}:{config.port}")
            if config.type == HealthCheckType.HTTP.value:
                request = urllib.request.Request(str(config.url), headers={"User-Agent": "ServiceManager-HealthCheck/1"})
                with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:
                    status = int(response.status)
                healthy = status in config.expected_status
                return HealthResult(healthy, f"HTTP 상태 {status}")
            if config.type == HealthCheckType.COMMAND.value:
                completed = subprocess.run(
                    config.command, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                    capture_output=True, text=True, timeout=config.timeout_seconds, check=False,
                )
                message = (completed.stdout or completed.stderr or f"종료 코드 {completed.returncode}").strip()
                return HealthResult(completed.returncode == 0, message[-500:])
            return HealthResult(False, f"지원하지 않는 상태 확인 유형: {config.type}")
        except (OSError, subprocess.SubprocessError, urllib.error.URLError) as exc:
            return HealthResult(False, str(exc))

