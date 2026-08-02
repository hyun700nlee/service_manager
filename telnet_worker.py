from __future__ import annotations

import queue
import socket
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

try:
    import telnetlib  # Python 3.10-3.12
except ImportError:  # Python 3.13+
    import simple_telnet as telnetlib


class TelnetStepError(RuntimeError):
    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage


@dataclass
class TelnetRuntime:
    config: dict[str, Any]
    running: bool = False
    connection: Any = None
    last_run: datetime | None = None
    state: str = "대기"
    result: str = "-"
    lock: threading.Lock = field(default_factory=threading.Lock)
    cancel_event: threading.Event = field(default_factory=threading.Event)


class TelnetJobManager:
    def __init__(self, jobs: list[dict[str, Any]], event_queue: queue.Queue):
        self.event_queue = event_queue
        self.runtimes = {item["name"]: TelnetRuntime(item) for item in jobs}
        self._shutdown = threading.Event()

    def _emit(self, event_type: str, **payload: Any) -> None:
        self.event_queue.put({"type": event_type, **payload})

    def _log(self, name: str, message: str, stream: str = "system") -> None:
        self._emit(
            "log",
            source_type="telnet",
            name=name,
            message=message,
            stream=stream,
            timestamp=datetime.now(),
        )

    def initialize_states(self) -> None:
        for name, runtime in self.runtimes.items():
            if runtime.config.get("_enabled", False):
                self._emit(
                    "telnet_state",
                    name=name,
                    state="대기",
                    last_run=None,
                    result="-",
                )
            else:
                runtime.state = "실패"
                runtime.result = "설정 오류"
                self._emit(
                    "telnet_state",
                    name=name,
                    state="실패",
                    last_run=None,
                    result="설정 오류",
                )
                for error in runtime.config.get("_errors", []):
                    self._log(name, f"설정 오류: {error}", "stderr")

    def run_async(self, name: str, *, manual: bool = False) -> bool:
        runtime = self.runtimes.get(name)
        if runtime is None:
            return False
        with runtime.lock:
            if runtime.running:
                self._log(name, "해당 작업이 이미 실행 중입니다.")
                return False
            if not runtime.config.get("_enabled", False):
                self._log(name, "설정 오류가 있어 작업을 실행할 수 없습니다.", "stderr")
                return False
            if self._shutdown.is_set():
                return False
            runtime.running = True
            runtime.cancel_event.clear()
            runtime.state = "실행 중"
            runtime.result = "수동 실행" if manual else "예약 실행"

        self._emit(
            "telnet_state",
            name=name,
            state="실행 중",
            last_run=runtime.last_run,
            result=runtime.result,
        )
        threading.Thread(target=self._run_worker, args=(runtime,), daemon=True).start()
        return True

    def _run_worker(self, runtime: TelnetRuntime) -> None:
        config = runtime.config
        name = config["name"]
        started_at = datetime.now()
        stage = "초기화"
        tn = None
        self._log(name, f"Telnet 작업 시작: {config['host']}:{config.get('port', 23)}")

        try:
            stage = "서버 접속"
            tn = telnetlib.Telnet(
                config["host"],
                int(config.get("port", 23)),
                timeout=float(config["connect_timeout_seconds"]),
            )
            with runtime.lock:
                runtime.connection = tn
            self._check_cancel(runtime)

            stage = "로그인 프롬프트 대기"
            self._read_until(
                runtime,
                tn,
                config["login_prompt"],
                float(config["connect_timeout_seconds"]),
                stage,
            )
            tn.write(config["username"].encode("utf-8") + b"\n")

            stage = "비밀번호 프롬프트 대기"
            self._read_until(
                runtime,
                tn,
                config["password_prompt"],
                float(config["connect_timeout_seconds"]),
                stage,
            )
            tn.write(config["password"].encode("utf-8") + b"\n")

            stage = "쉘 프롬프트 대기"
            self._read_until(
                runtime,
                tn,
                config["shell_prompt"],
                float(config["command_timeout_seconds"]),
                stage,
            )
            self._log(name, "로그인 및 쉘 프롬프트 확인 완료")

            for index, command in enumerate(config["commands"], start=1):
                self._check_cancel(runtime)
                stage = f"명령 {index} 실행: {command}"
                self._log(name, f"> {command}")
                tn.write(command.encode("utf-8") + b"\n")
                output = self._read_until(
                    runtime,
                    tn,
                    config["shell_prompt"],
                    float(config["command_timeout_seconds"]),
                    stage,
                )
                self._log_received(name, output)

            stage = "접속 종료"
            try:
                tn.write(b"exit\n")
            except (EOFError, OSError):
                pass

            finished_at = datetime.now()
            with runtime.lock:
                runtime.last_run = finished_at
                runtime.state = "대기"
                runtime.result = "성공"
            self._emit(
                "telnet_state",
                name=name,
                state="대기",
                last_run=finished_at,
                result="성공",
            )
            self._log(name, "Telnet 작업이 정상 완료되었습니다.")

        except TelnetStepError as exc:
            self._mark_failure(runtime, started_at, exc.stage, str(exc))
        except (socket.timeout, TimeoutError) as exc:
            self._mark_failure(runtime, started_at, stage, f"시간 초과: {exc}")
        except (ConnectionError, EOFError, OSError) as exc:
            self._mark_failure(runtime, started_at, stage, f"연결 오류: {exc}")
        except Exception as exc:  # Keep a malformed server response from killing the manager.
            self._mark_failure(runtime, started_at, stage, f"예상하지 못한 오류: {exc}")
        finally:
            if tn is not None:
                try:
                    tn.close()
                except OSError:
                    pass
            with runtime.lock:
                runtime.connection = None
                runtime.running = False

    def _read_until(self, runtime: TelnetRuntime, tn, prompt: str, timeout: float, stage: str) -> bytes:
        self._check_cancel(runtime)
        expected = prompt.encode("utf-8")
        try:
            data = tn.read_until(expected, timeout=timeout)
        except EOFError as exc:
            raise TelnetStepError(stage, "서버가 연결을 종료했습니다.") from exc
        self._check_cancel(runtime)
        if expected not in data:
            raise TelnetStepError(stage, f"프롬프트를 제한 시간 내에 찾지 못했습니다: {prompt!r}")
        return data

    def _check_cancel(self, runtime: TelnetRuntime) -> None:
        if runtime.cancel_event.is_set() or self._shutdown.is_set():
            raise TelnetStepError("종료 요청", "프로그램 종료로 작업이 취소되었습니다.")

    def _log_received(self, name: str, data: bytes) -> None:
        text = data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
        for line in text.splitlines():
            if line.strip():
                self._log(name, line)

    def _mark_failure(self, runtime: TelnetRuntime, started_at: datetime, stage: str, message: str) -> None:
        failed_at = datetime.now()
        with runtime.lock:
            runtime.last_run = failed_at
            runtime.state = "실패"
            runtime.result = "실패"
        self._emit(
            "telnet_state",
            name=runtime.config["name"],
            state="실패",
            last_run=failed_at,
            result="실패",
        )
        self._log(runtime.config["name"], f"작업명: {runtime.config['name']}", "stderr")
        self._log(runtime.config["name"], f"실패 시각: {failed_at:%Y-%m-%d %H:%M:%S}", "stderr")
        self._log(runtime.config["name"], f"실패 단계: {stage}", "stderr")
        self._log(runtime.config["name"], f"오류 내용: {message}", "stderr")

    def get_snapshot(self, name: str) -> dict[str, Any]:
        runtime = self.runtimes[name]
        with runtime.lock:
            return {
                "state": runtime.state,
                "last_run": runtime.last_run,
                "result": runtime.result,
                "running": runtime.running,
            }

    def shutdown(self) -> None:
        self._shutdown.set()
        for runtime in self.runtimes.values():
            with runtime.lock:
                runtime.cancel_event.set()
                connection = runtime.connection
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    pass
