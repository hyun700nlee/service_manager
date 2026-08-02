from __future__ import annotations

from datetime import datetime, timedelta


VALID_SCHEDULE_TYPES = {"none", "interval", "daily"}


def parse_hhmm(value: str) -> tuple[int, int]:
    """Parse HH:MM and raise ValueError for invalid values."""
    if not isinstance(value, str):
        raise ValueError("시각은 HH:MM 문자열이어야 합니다.")
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError("시각 형식은 HH:MM이어야 합니다.")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("시각 범위가 올바르지 않습니다.")
    return hour, minute


def initial_next_due(
    schedule_type: str,
    *,
    now: datetime,
    daily_time: str | None = None,
    interval_minutes: int | float | None = None,
) -> datetime | None:
    """Return the first future execution time. Missed work is never backfilled."""
    if schedule_type == "none":
        return None
    if schedule_type == "interval":
        if interval_minutes is None or float(interval_minutes) <= 0:
            raise ValueError("interval_minutes는 0보다 커야 합니다.")
        return now + timedelta(minutes=float(interval_minutes))
    if schedule_type == "daily":
        hour, minute = parse_hhmm(daily_time or "")
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate
    raise ValueError(f"지원하지 않는 schedule_type: {schedule_type}")


def advance_due(
    current_due: datetime,
    schedule_type: str,
    *,
    now: datetime,
    daily_time: str | None = None,
    interval_minutes: int | float | None = None,
) -> datetime | None:
    """Advance a due time to the next future slot without catch-up execution."""
    if schedule_type == "none":
        return None
    if schedule_type == "interval":
        if interval_minutes is None or float(interval_minutes) <= 0:
            raise ValueError("interval_minutes는 0보다 커야 합니다.")
        step = timedelta(minutes=float(interval_minutes))
        candidate = current_due + step
        while candidate <= now:
            candidate += step
        return candidate
    if schedule_type == "daily":
        hour, minute = parse_hhmm(daily_time or "")
        candidate = current_due + timedelta(days=1)
        candidate = candidate.replace(hour=hour, minute=minute, second=0, microsecond=0)
        while candidate <= now:
            candidate += timedelta(days=1)
        return candidate
    raise ValueError(f"지원하지 않는 schedule_type: {schedule_type}")


def format_datetime(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else "-"


def describe_schedule(config: dict, *, service: bool) -> str:
    schedule_type = config.get("schedule_type", "none")
    if schedule_type == "none":
        return "없음"
    if schedule_type == "interval":
        key = "restart_interval_minutes" if service else "interval_minutes"
        minutes = config.get(key)
        return f"{minutes}분마다"
    key = "restart_time" if service else "run_time"
    return f"매일 {config.get(key)}"
