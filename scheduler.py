from __future__ import annotations

import json
import threading
from dataclasses import asdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

try:
    from croniter import croniter
except ImportError:
    croniter = None  # type: ignore

from event_logging import EventLogger
from models import Schedule, ScheduleType
from remote_jobs import RemoteJobManager
from storage import Repository
from supervisor import ServiceSupervisor


class EngineScheduler:
    def __init__(self, repository: Repository, supervisor: ServiceSupervisor, jobs: RemoteJobManager, logger: EventLogger):
        self.repository = repository
        self.supervisor = supervisor
        self.jobs = jobs
        self.logger = logger
        self._shutdown = threading.Event()
        self._next_due: dict[str, datetime] = {}
        self._schedule_signatures: dict[str, str] = {}
        self.reload()
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()

    def reload(self) -> None:
        current_ids: set[str] = set()
        persisted = self.repository.get_runtime_states()
        for item in self.repository.list_services():
            current_ids.add(item.id)
            signature = json.dumps(asdict(item.schedule), sort_keys=True)
            if item.schedule.type == ScheduleType.NONE.value:
                self._next_due.pop(item.id, None)
                self._schedule_signatures[item.id] = signature
                continue
            if item.schedule.type != ScheduleType.NONE.value and (item.id not in self._next_due or self._schedule_signatures.get(item.id) != signature):
                saved = persisted.get(f"schedule:{item.id}", {}).get("next_due")
                if self._schedule_signatures.get(item.id) != signature:
                    saved = None
                due = datetime.fromisoformat(saved) if saved else self._initial_due(item.schedule)
                if due and due <= datetime.now(due.tzinfo) and item.schedule.misfire_policy == "skip":
                    due = self._advance(item.schedule, due, datetime.now(due.tzinfo))
                if due:
                    self._next_due[item.id] = due
                    self.repository.save_runtime_state(f"schedule:{item.id}", {"next_due": due.isoformat()})
            self._schedule_signatures[item.id] = signature
        for item in self.repository.list_remote_jobs():
            current_ids.add(item.id)
            signature = json.dumps(asdict(item.schedule), sort_keys=True)
            if not item.auto_run or item.schedule.type == ScheduleType.NONE.value:
                self._next_due.pop(item.id, None)
                self._schedule_signatures[item.id] = signature
                continue
            if item.auto_run and item.schedule.type != ScheduleType.NONE.value and (item.id not in self._next_due or self._schedule_signatures.get(item.id) != signature):
                saved = persisted.get(f"schedule:{item.id}", {}).get("next_due")
                if self._schedule_signatures.get(item.id) != signature:
                    saved = None
                due = datetime.fromisoformat(saved) if saved else self._initial_due(item.schedule)
                if due and due <= datetime.now(due.tzinfo) and item.schedule.misfire_policy == "skip":
                    due = self._advance(item.schedule, due, datetime.now(due.tzinfo))
                if due:
                    self._next_due[item.id] = due
                    self.repository.save_runtime_state(f"schedule:{item.id}", {"next_due": due.isoformat()})
            self._schedule_signatures[item.id] = signature
        for resource_id in set(self._next_due) - current_ids:
            del self._next_due[resource_id]
            self._schedule_signatures.pop(resource_id, None)

    @staticmethod
    def _initial_due(schedule: Schedule) -> datetime | None:
        zone = ZoneInfo(schedule.timezone)
        now = datetime.now(zone)
        if schedule.type == ScheduleType.INTERVAL.value:
            return now + timedelta(minutes=float(schedule.interval_minutes or 0))
        if schedule.type == ScheduleType.DAILY.value:
            hour, minute = map(int, str(schedule.daily_time).split(":"))
            due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            return due if due > now else due + timedelta(days=1)
        if schedule.type == ScheduleType.ONCE.value:
            due = datetime.fromisoformat(str(schedule.once_at))
            return due.replace(tzinfo=zone) if due.tzinfo is None else due.astimezone(zone)
        if schedule.type == ScheduleType.CRON.value:
            if croniter is None:
                raise RuntimeError("Cron 예약을 사용하려면 croniter가 필요합니다.")
            return croniter(str(schedule.cron), now).get_next(datetime)
        return None

    @staticmethod
    def _advance(schedule: Schedule, previous: datetime, now: datetime) -> datetime | None:
        if schedule.type == ScheduleType.ONCE.value:
            return None
        if schedule.type == ScheduleType.INTERVAL.value:
            step = timedelta(minutes=float(schedule.interval_minutes or 0))
            candidate = previous + step
            while candidate <= now:
                candidate += step
            return candidate
        if schedule.type == ScheduleType.DAILY.value:
            hour, minute = map(int, str(schedule.daily_time).split(":"))
            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            return candidate if candidate > now else candidate + timedelta(days=1)
        if schedule.type == ScheduleType.CRON.value:
            if croniter is None:
                return None
            return croniter(str(schedule.cron), now).get_next(datetime)
        return None

    def _loop(self) -> None:
        while not self._shutdown.wait(1):
            services = {item.id: item for item in self.repository.list_services()}
            jobs = {item.id: item for item in self.repository.list_remote_jobs()}
            for resource_id, due in list(self._next_due.items()):
                now = datetime.now(due.tzinfo)
                if now < due:
                    continue
                if resource_id in services:
                    item = services[resource_id]
                    if item.enabled:
                        self.logger.emit("INFO", "service", item.name, "schedule_due", "예약 재시작 시각이 되었습니다.", source_id=item.id)
                        self.supervisor.restart(item.id)
                    schedule = item.schedule
                elif resource_id in jobs:
                    item = jobs[resource_id]
                    if item.enabled and item.auto_run:
                        self.jobs.run(item.id, manual=False)
                    schedule = item.schedule
                else:
                    del self._next_due[resource_id]
                    continue
                next_due = self._advance(schedule, due, now)
                if next_due is None:
                    del self._next_due[resource_id]
                else:
                    self._next_due[resource_id] = next_due
                self.repository.save_runtime_state(f"schedule:{resource_id}", {"next_due": next_due.isoformat() if next_due else None})

    def next_due(self, resource_id: str) -> str | None:
        due = self._next_due.get(resource_id)
        return due.isoformat() if due else None

    def shutdown(self) -> None:
        self._shutdown.set()
