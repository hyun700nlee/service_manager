from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from schedule_utils import VALID_SCHEDULE_TYPES, parse_hhmm


@dataclass(slots=True)
class ConfigLoadResult:
    services: list[dict[str, Any]]
    telnet_jobs: list[dict[str, Any]]
    global_errors: list[str]


def _positive_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _validate_schedule(item: dict[str, Any], *, service: bool) -> list[str]:
    errors: list[str] = []
    schedule_type = item.get("schedule_type", "none")
    if schedule_type not in VALID_SCHEDULE_TYPES:
        return ["schedule_type은 none, interval, daily 중 하나여야 합니다."]

    if schedule_type == "interval":
        key = "restart_interval_minutes" if service else "interval_minutes"
        if not _positive_number(item.get(key)):
            errors.append(f"{key}는 0보다 큰 숫자여야 합니다.")
    elif schedule_type == "daily":
        key = "restart_time" if service else "run_time"
        try:
            parse_hhmm(item.get(key))
        except (TypeError, ValueError):
            errors.append(f"{key}은 HH:MM 형식이어야 합니다.")
    return errors


def _normalize_path(value: Any, base_dir: Path) -> str:
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return str(path.resolve())


def _validate_service(raw: Any, base_dir: Path) -> dict[str, Any]:
    item = dict(raw) if isinstance(raw, dict) else {}
    errors: list[str] = []

    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append("서비스명이 없습니다.")
        name = "이름 없는 서비스"
    item["name"] = str(name).strip()

    working_directory = item.get("working_directory")
    if not working_directory:
        errors.append("working_directory가 없습니다.")
    else:
        item["working_directory"] = _normalize_path(working_directory, base_dir)
        if not Path(item["working_directory"]).is_dir():
            errors.append(f"작업 디렉터리가 존재하지 않습니다: {item['working_directory']}")

    python_executable = item.get("python_executable")
    if not python_executable:
        errors.append("python_executable이 없습니다.")
    else:
        item["python_executable"] = _normalize_path(python_executable, base_dir)
        if not Path(item["python_executable"]).is_file():
            errors.append(f"Python 실행 파일이 존재하지 않습니다: {item['python_executable']}")

    script = item.get("script")
    if not isinstance(script, str) or not script.strip():
        errors.append("실행 스크립트가 없습니다.")
    elif working_directory:
        script_path = Path(item["working_directory"]) / script
        if not script_path.is_file():
            errors.append(f"실행 스크립트가 존재하지 않습니다: {script_path}")

    arguments = item.get("arguments", [])
    if not isinstance(arguments, list) or not all(isinstance(v, (str, int, float)) for v in arguments):
        errors.append("arguments는 문자열 또는 숫자의 배열이어야 합니다.")
        arguments = []
    item["arguments"] = [str(v) for v in arguments]
    item["auto_start"] = bool(item.get("auto_start", False))
    item.setdefault("schedule_type", "none")
    errors.extend(_validate_schedule(item, service=True))

    item["_enabled"] = not errors
    item["_errors"] = errors
    return item


def _validate_telnet_job(raw: Any) -> dict[str, Any]:
    item = dict(raw) if isinstance(raw, dict) else {}
    errors: list[str] = []

    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append("작업명이 없습니다.")
        name = "이름 없는 Telnet 작업"
    item["name"] = str(name).strip()

    host = item.get("host")
    if not isinstance(host, str) or not host.strip():
        errors.append("서버 주소가 없습니다.")

    port = item.get("port", 23)
    if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
        errors.append("포트 번호는 1~65535의 정수여야 합니다.")

    username = item.get("username")
    if not isinstance(username, str) or not username:
        errors.append("사용자 ID가 없습니다.")

    if "password" not in item or not isinstance(item.get("password"), str):
        errors.append("password 문자열이 없습니다.")

    for key in ("login_prompt", "password_prompt", "shell_prompt"):
        if not isinstance(item.get(key), str) or not item.get(key):
            errors.append(f"{key}가 없습니다.")

    commands = item.get("commands")
    if not isinstance(commands, list) or not commands or not all(isinstance(v, str) and v for v in commands):
        errors.append("commands에는 한 개 이상의 문자열 명령어가 필요합니다.")
        item["commands"] = []

    for key, default in (("connect_timeout_seconds", 10), ("command_timeout_seconds", 60)):
        item.setdefault(key, default)
        if not _positive_number(item.get(key)):
            errors.append(f"{key}는 0보다 큰 숫자여야 합니다.")

    item["auto_run"] = bool(item.get("auto_run", False))
    item.setdefault("schedule_type", "none")
    errors.extend(_validate_schedule(item, service=False))

    item["_enabled"] = not errors
    item["_errors"] = errors
    return item


def load_config(path: str | Path) -> ConfigLoadResult:
    config_path = Path(path)
    global_errors: list[str] = []

    if not config_path.is_file():
        return ConfigLoadResult([], [], [f"config.json이 존재하지 않습니다: {config_path}"])

    try:
        with config_path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except json.JSONDecodeError as exc:
        return ConfigLoadResult([], [], [f"JSON 문법 오류: {exc}"])
    except OSError as exc:
        return ConfigLoadResult([], [], [f"설정 파일 읽기 실패: {exc}"])

    if not isinstance(data, dict):
        return ConfigLoadResult([], [], ["config.json의 최상위 값은 객체여야 합니다."])

    raw_services = data.get("services", [])
    raw_jobs = data.get("telnet_jobs", [])
    if not isinstance(raw_services, list):
        global_errors.append("services는 배열이어야 합니다.")
        raw_services = []
    if not isinstance(raw_jobs, list):
        global_errors.append("telnet_jobs는 배열이어야 합니다.")
        raw_jobs = []

    base_dir = config_path.resolve().parent
    services = [_validate_service(raw, base_dir) for raw in raw_services]
    jobs = [_validate_telnet_job(raw) for raw in raw_jobs]

    for collection, label in ((services, "서비스"), (jobs, "Telnet 작업")):
        seen: set[str] = set()
        for item in collection:
            name = item["name"]
            if name in seen:
                item["_enabled"] = False
                item["_errors"].append(f"중복된 {label}명입니다: {name}")
            seen.add(name)

    return ConfigLoadResult(services, jobs, global_errors)
