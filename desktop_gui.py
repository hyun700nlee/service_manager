from __future__ import annotations

import argparse
import queue
import shlex
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from engine import ServiceManagerEngine
from instance_lock import EngineInstanceLock
from ipc import EngineClient
from models import RemoteJobDefinition, ServiceDefinition

APP_TITLE = "Python 서비스 관리자"


class EmbeddedClient:
    def __init__(self, engine: ServiceManagerEngine):
        self.engine = engine

    def request(self, command: str, **payload: Any) -> dict[str, Any]:
        return self.engine.dispatch({"command": command, **payload})


class ServiceEditor(tk.Toplevel):
    def __init__(self, parent, item: dict[str, Any] | None, on_save: Callable[[dict[str, Any]], None]):
        super().__init__(parent)
        self.title("서비스 설정")
        self.transient(parent)
        self.grab_set()
        self.resizable(True, True)
        self.item = ServiceDefinition.from_dict(item or {}).to_dict()
        self.on_save = on_save
        self.vars: dict[str, tk.Variable] = {}
        body = ttk.Frame(self, padding=12)
        body.pack(fill=tk.BOTH, expand=True)
        fields = [
            ("name", "서비스명", self.item.get("name", "")),
            ("executable", "실행 파일", self.item.get("executable", "")),
            ("working_directory", "작업 디렉터리", self.item.get("working_directory", "")),
            ("arguments", "인수", " ".join(shlex.quote(str(v)) for v in self.item.get("arguments", []))),
        ]
        for row, (key, label, value) in enumerate(fields):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
            var = tk.StringVar(value=value)
            self.vars[key] = var
            ttk.Entry(body, textvariable=var, width=64).grid(row=row, column=1, sticky="ew", pady=4)
            if key == "executable":
                ttk.Button(body, text="찾기", command=lambda v=var: self._browse_file(v)).grid(row=row, column=2, padx=(6, 0))
            elif key == "working_directory":
                ttk.Button(body, text="찾기", command=lambda v=var: self._browse_dir(v)).grid(row=row, column=2, padx=(6, 0))
        self.vars["enabled"] = tk.BooleanVar(value=self.item.get("enabled", True))
        self.vars["auto_start"] = tk.BooleanVar(value=self.item.get("auto_start", False))
        ttk.Checkbutton(body, text="활성화", variable=self.vars["enabled"]).grid(row=4, column=1, sticky="w")
        ttk.Checkbutton(body, text="엔진 시작 시 자동 시작", variable=self.vars["auto_start"]).grid(row=5, column=1, sticky="w")

        policy = self.item.get("restart_policy", {})
        ttk.Label(body, text="재시작 정책").grid(row=6, column=0, sticky="w", pady=4)
        self.vars["restart_mode"] = tk.StringVar(value=policy.get("mode", "on_failure"))
        ttk.Combobox(body, textvariable=self.vars["restart_mode"], values=("never", "on_failure", "always"), state="readonly").grid(row=6, column=1, sticky="ew")

        health = self.item.get("health_check", {})
        ttk.Label(body, text="상태 확인").grid(row=7, column=0, sticky="w", pady=4)
        self.vars["health_type"] = tk.StringVar(value=health.get("type", "process"))
        ttk.Combobox(body, textvariable=self.vars["health_type"], values=("process", "tcp", "http", "command"), state="readonly").grid(row=7, column=1, sticky="ew")
        target = health.get("url") or (f"{health.get('host')}:{health.get('port')}" if health.get("host") else "") or " ".join(health.get("command", []))
        self.vars["health_target"] = tk.StringVar(value=target)
        ttk.Label(body, text="상태 확인 대상").grid(row=8, column=0, sticky="w", pady=4)
        ttk.Entry(body, textvariable=self.vars["health_target"]).grid(row=8, column=1, sticky="ew")
        self.vars["health_enabled"] = tk.BooleanVar(value=health.get("enabled", False))
        ttk.Checkbutton(body, text="상태 확인 활성화", variable=self.vars["health_enabled"]).grid(row=9, column=1, sticky="w")

        schedule = self.item.get("schedule", {})
        ttk.Label(body, text="예약 유형").grid(row=10, column=0, sticky="w", pady=4)
        self.vars["schedule_type"] = tk.StringVar(value=schedule.get("type", "none"))
        ttk.Combobox(body, textvariable=self.vars["schedule_type"], values=("none", "interval", "daily", "cron", "once"), state="readonly").grid(row=10, column=1, sticky="ew")
        self.vars["schedule_value"] = tk.StringVar(value=str(schedule.get("interval_minutes") or schedule.get("daily_time") or schedule.get("cron") or schedule.get("once_at") or ""))
        ttk.Label(body, text="예약 값").grid(row=11, column=0, sticky="w", pady=4)
        ttk.Entry(body, textvariable=self.vars["schedule_value"]).grid(row=11, column=1, sticky="ew")
        ttk.Label(body, text="주기=분, 일일=HH:MM, Cron=표현식, 1회=ISO 시각", foreground="#555").grid(row=12, column=1, sticky="w")

        ttk.Label(body, text="환경변수").grid(row=13, column=0, sticky="nw", pady=4)
        self.environment = tk.Text(body, height=5, width=60)
        self.environment.grid(row=13, column=1, columnspan=2, sticky="nsew")
        self.environment.insert("1.0", "\n".join(f"{k}={v}" for k, v in self.item.get("environment", {}).items()))
        self.error_var = tk.StringVar()
        ttk.Label(body, textvariable=self.error_var, foreground="#a00000", wraplength=600).grid(row=14, column=0, columnspan=3, sticky="w", pady=(8, 0))
        buttons = ttk.Frame(body)
        buttons.grid(row=15, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="검증 및 저장", command=self._save).pack(side=tk.LEFT, padx=4)
        ttk.Button(buttons, text="취소", command=self.destroy).pack(side=tk.LEFT, padx=4)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(13, weight=1)
        self.geometry("760x650")

    @staticmethod
    def _browse_file(var: tk.StringVar) -> None:
        value = filedialog.askopenfilename(filetypes=[("실행 파일", "*.exe *.bat *.cmd *.py"), ("모든 파일", "*.*")])
        if value:
            var.set(value)

    @staticmethod
    def _browse_dir(var: tk.StringVar) -> None:
        value = filedialog.askdirectory()
        if value:
            var.set(value)

    def _save(self) -> None:
        raw = dict(self.item)
        raw.update(
            name=str(self.vars["name"].get()).strip(), executable=str(self.vars["executable"].get()).strip(),
            working_directory=str(self.vars["working_directory"].get()).strip(), enabled=bool(self.vars["enabled"].get()),
            auto_start=bool(self.vars["auto_start"].get()),
        )
        try:
            raw["arguments"] = shlex.split(str(self.vars["arguments"].get()), posix=False)
            environment: dict[str, str] = {}
            for line in self.environment.get("1.0", tk.END).splitlines():
                if line.strip():
                    key, value = line.split("=", 1)
                    environment[key.strip()] = value
            raw["environment"] = environment
            raw["restart_policy"] = {**raw.get("restart_policy", {}), "mode": self.vars["restart_mode"].get()}
            health_type = str(self.vars["health_type"].get())
            target = str(self.vars["health_target"].get()).strip()
            health = {**raw.get("health_check", {}), "type": health_type, "enabled": bool(self.vars["health_enabled"].get()), "host": None, "port": None, "url": None, "command": []}
            if health_type == "tcp" and target:
                health["host"], port = target.rsplit(":", 1)
                health["port"] = int(port)
            elif health_type == "http":
                health["url"] = target
            elif health_type == "command":
                health["command"] = shlex.split(target, posix=False)
            raw["health_check"] = health
            schedule_type = str(self.vars["schedule_type"].get())
            value = str(self.vars["schedule_value"].get()).strip()
            schedule = {**raw.get("schedule", {}), "type": schedule_type, "interval_minutes": None, "daily_time": None, "cron": None, "once_at": None}
            if schedule_type == "interval":
                schedule["interval_minutes"] = float(value)
            elif schedule_type == "daily":
                schedule["daily_time"] = value
            elif schedule_type == "cron":
                schedule["cron"] = value
            elif schedule_type == "once":
                schedule["once_at"] = value
            raw["schedule"] = schedule
            item = ServiceDefinition.from_dict(raw)
            errors = item.validate()
            if errors:
                raise ValueError("\n".join(errors))
            self.on_save(item.to_dict())
            self.destroy()
        except (ValueError, OSError) as exc:
            self.error_var.set(str(exc))


class RemoteJobEditor(tk.Toplevel):
    def __init__(self, parent, client, item: dict[str, Any] | None, on_save: Callable[[dict[str, Any], str | None], None]):
        super().__init__(parent)
        self.title("원격 작업 설정")
        self.transient(parent)
        self.grab_set()
        self.client = client
        self.item = RemoteJobDefinition.from_dict(item or {}).to_dict()
        self.on_save = on_save
        self.vars: dict[str, tk.Variable] = {}
        body = ttk.Frame(self, padding=12)
        body.pack(fill=tk.BOTH, expand=True)
        fields = [
            ("name", "작업명", self.item.get("name", "")), ("host", "호스트", self.item.get("host", "")),
            ("port", "포트", str(self.item.get("port", 22))), ("username", "사용자", self.item.get("username", "")),
            ("private_key_path", "개인 키", self.item.get("private_key_path") or ""),
            ("host_key_fingerprint", "호스트 키 지문", self.item.get("host_key_fingerprint") or ""),
            ("success_pattern", "성공 정규식", self.item.get("success_pattern") or ""),
            ("failure_pattern", "실패 정규식", self.item.get("failure_pattern") or ""),
        ]
        for row, (key, label, value) in enumerate(fields):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
            var = tk.StringVar(value=value)
            self.vars[key] = var
            ttk.Entry(body, textvariable=var, width=60).grid(row=row, column=1, sticky="ew", pady=3)
        ttk.Button(body, text="SSH 지문 조회", command=self._fingerprint).grid(row=5, column=2, padx=(6, 0))
        self.vars["protocol"] = tk.StringVar(value=self.item.get("protocol", "ssh"))
        ttk.Label(body, text="프로토콜").grid(row=8, column=0, sticky="w", pady=3)
        ttk.Combobox(body, textvariable=self.vars["protocol"], values=("ssh", "telnet"), state="readonly").grid(row=8, column=1, sticky="ew")
        self.vars["auth_method"] = tk.StringVar(value=self.item.get("auth_method", "password"))
        ttk.Label(body, text="인증 방식").grid(row=9, column=0, sticky="w", pady=3)
        ttk.Combobox(body, textvariable=self.vars["auth_method"], values=("password", "key", "agent"), state="readonly").grid(row=9, column=1, sticky="ew")
        self.vars["secret"] = tk.StringVar()
        ttk.Label(body, text="비밀번호/키 암호").grid(row=10, column=0, sticky="w", pady=3)
        ttk.Entry(body, textvariable=self.vars["secret"], show="●").grid(row=10, column=1, sticky="ew")
        ttk.Label(body, text="기존 비밀값을 유지하려면 비워두십시오.", foreground="#555").grid(row=10, column=2, sticky="w")
        ttk.Label(body, text="명령(한 줄에 하나)").grid(row=11, column=0, sticky="nw", pady=3)
        self.commands = tk.Text(body, height=8)
        self.commands.grid(row=11, column=1, columnspan=2, sticky="nsew")
        self.commands.insert("1.0", "\n".join(self.item.get("commands", [])))
        self.vars["enabled"] = tk.BooleanVar(value=self.item.get("enabled", True))
        self.vars["auto_run"] = tk.BooleanVar(value=self.item.get("auto_run", False))
        self.vars["legacy_confirmed"] = tk.BooleanVar(value=self.item.get("legacy_telnet_confirmed", False))
        ttk.Checkbutton(body, text="활성화", variable=self.vars["enabled"]).grid(row=12, column=1, sticky="w")
        ttk.Checkbutton(body, text="예약 자동 실행", variable=self.vars["auto_run"]).grid(row=13, column=1, sticky="w")
        ttk.Checkbutton(body, text="Telnet이 암호화되지 않음을 이해하고 사용", variable=self.vars["legacy_confirmed"]).grid(row=14, column=1, columnspan=2, sticky="w")
        self.error_var = tk.StringVar()
        ttk.Label(body, textvariable=self.error_var, foreground="#a00000", wraplength=650).grid(row=15, column=0, columnspan=3, sticky="w")
        buttons = ttk.Frame(body)
        buttons.grid(row=16, column=0, columnspan=3, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="검증 및 저장", command=self._save).pack(side=tk.LEFT, padx=4)
        ttk.Button(buttons, text="취소", command=self.destroy).pack(side=tk.LEFT, padx=4)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(11, weight=1)
        self.geometry("850x680")

    def _fingerprint(self) -> None:
        try:
            response = self.client.request("fetch_ssh_fingerprint", host=self.vars["host"].get(), port=int(self.vars["port"].get()))
            if not response.get("ok"):
                raise RuntimeError(response.get("error"))
            fingerprint = str(response["fingerprint"])
            if messagebox.askyesno("SSH 호스트 키", f"서버 지문을 신뢰하시겠습니까?\n\n{fingerprint}", parent=self):
                self.vars["host_key_fingerprint"].set(fingerprint)
        except Exception as exc:
            self.error_var.set(str(exc))


class NotificationEditor(tk.Toplevel):
    def __init__(self, parent, client):
        super().__init__(parent)
        self.title("장애 알림 설정")
        self.transient(parent)
        self.grab_set()
        self.client = client
        response = client.request("get_settings")
        self.settings = dict(response.get("notifications") or {})
        body = ttk.Frame(self, padding=12)
        body.pack(fill=tk.BOTH, expand=True)
        self.enabled = tk.BooleanVar(value=self.settings.get("enabled", False))
        ttk.Checkbutton(body, text="장애·회로 차단·복구 알림 활성화", variable=self.enabled).grid(row=0, column=0, columnspan=2, sticky="w")
        fields = [
            ("webhook_url", "Webhook URL", "", True),
            ("smtp_host", "SMTP 서버", self.settings.get("smtp_host", ""), False),
            ("smtp_port", "SMTP 포트", str(self.settings.get("smtp_port", 587)), False),
            ("smtp_user", "SMTP 사용자", self.settings.get("smtp_user", ""), False),
            ("smtp_password", "SMTP 비밀번호", "", True),
            ("sender", "보내는 주소", self.settings.get("sender", ""), False),
            ("recipients", "받는 주소(쉼표 구분)", ", ".join(self.settings.get("recipients", [])), False),
        ]
        self.vars: dict[str, tk.StringVar] = {}
        for row, (key, label, value, secret) in enumerate(fields, 1):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", pady=4, padx=(0, 8))
            var = tk.StringVar(value=value)
            self.vars[key] = var
            ttk.Entry(body, textvariable=var, width=58, show="●" if secret else "").grid(row=row, column=1, sticky="ew", pady=4)
        ttk.Label(body, text="Webhook URL과 SMTP 비밀번호는 비워두면 기존 값을 유지합니다.", foreground="#555").grid(row=8, column=0, columnspan=2, sticky="w")
        self.error_var = tk.StringVar()
        ttk.Label(body, textvariable=self.error_var, foreground="#a00000").grid(row=9, column=0, columnspan=2, sticky="w")
        buttons = ttk.Frame(body)
        buttons.grid(row=10, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="저장", command=self._save).pack(side=tk.LEFT, padx=4)
        ttk.Button(buttons, text="취소", command=self.destroy).pack(side=tk.LEFT, padx=4)
        body.columnconfigure(1, weight=1)
        self.geometry("700x470")

    def _save(self) -> None:
        try:
            settings = dict(self.settings)
            settings.update(
                enabled=bool(self.enabled.get()), smtp_host=self.vars["smtp_host"].get().strip(),
                smtp_port=int(self.vars["smtp_port"].get()), smtp_user=self.vars["smtp_user"].get().strip(),
                sender=self.vars["sender"].get().strip(),
                recipients=[item.strip() for item in self.vars["recipients"].get().split(",") if item.strip()],
                smtp_starttls=True, dedupe_seconds=300,
            )
            response = self.client.request(
                "update_settings", notifications=settings,
                webhook_url=self.vars["webhook_url"].get().strip() or None,
                smtp_password=self.vars["smtp_password"].get() or None,
            )
            if not response.get("ok"):
                raise RuntimeError(response.get("error"))
            self.destroy()
        except Exception as exc:
            self.error_var.set(str(exc))

    def _save(self) -> None:
        try:
            raw = dict(self.item)
            for key in ("name", "host", "username", "private_key_path", "host_key_fingerprint", "success_pattern", "failure_pattern"):
                raw[key] = str(self.vars[key].get()).strip() or None
            raw.update(
                name=raw["name"] or "", host=raw["host"] or "", port=int(self.vars["port"].get()),
                protocol=self.vars["protocol"].get(), auth_method=self.vars["auth_method"].get(),
                enabled=bool(self.vars["enabled"].get()), auto_run=bool(self.vars["auto_run"].get()),
                legacy_telnet_confirmed=bool(self.vars["legacy_confirmed"].get()),
                commands=[line.strip() for line in self.commands.get("1.0", tk.END).splitlines() if line.strip()],
            )
            item = RemoteJobDefinition.from_dict(raw)
            errors = item.validate()
            if errors:
                raise ValueError("\n".join(errors))
            self.on_save(item.to_dict(), str(self.vars["secret"].get()) or None)
            self.destroy()
        except (ValueError, OSError) as exc:
            self.error_var.set(str(exc))


class CommercialApp:
    def __init__(self, root: tk.Tk, client):
        self.root = root
        self.client = client
        self.data: dict[str, Any] = {"services": [], "remote_jobs": []}
        self._polling = False
        self._response_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.tray_icon = None
        root.title(APP_TITLE)
        root.geometry("1180x780")
        root.minsize(920, 620)
        root.protocol("WM_DELETE_WINDOW", self._hide_window)
        self.status_var = tk.StringVar(value="엔진 연결 중…")
        self._build()
        self._setup_tray()
        self.root.after(50, self.refresh)
        self.root.after(100, self._drain_responses)

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=8)
        outer.pack(fill=tk.BOTH, expand=True)
        top = ttk.Frame(outer)
        top.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(top, text=APP_TITLE, font=("Segoe UI", 15, "bold")).pack(side=tk.LEFT)
        ttk.Label(top, textvariable=self.status_var).pack(side=tk.RIGHT)
        self.tabs = ttk.Notebook(outer)
        self.tabs.pack(fill=tk.BOTH, expand=True)
        self._build_dashboard()
        self.service_tree = self._resource_tab("서비스", ("name", "state", "pid", "uptime", "restarts", "health", "cpu", "memory", "next"), self._service_buttons)
        self.job_tree = self._resource_tab("원격 작업", ("name", "protocol", "state", "last", "result", "next"), self._job_buttons)
        self._build_events_tab()
        self._build_tools_tab()
        self.root.bind("<F5>", lambda _event: self.refresh())

    def _build_dashboard(self) -> None:
        frame = ttk.Frame(self.tabs, padding=12)
        self.tabs.add(frame, text="대시보드")
        cards = ttk.Frame(frame)
        cards.pack(fill=tk.X)
        self.dashboard_vars = {
            "services": tk.StringVar(value="0"), "running": tk.StringVar(value="0"),
            "failures": tk.StringVar(value="0"), "jobs": tk.StringVar(value="0"),
        }
        for index, (key, label) in enumerate((("services", "전체 서비스"), ("running", "실행 중"), ("failures", "주의 필요"), ("jobs", "원격 작업"))):
            card = ttk.LabelFrame(cards, text=label, padding=14)
            card.grid(row=0, column=index, padx=5, sticky="ew")
            ttk.Label(card, textvariable=self.dashboard_vars[key], font=("Segoe UI", 22, "bold")).pack()
            cards.columnconfigure(index, weight=1)
        ttk.Label(frame, text="주의가 필요한 항목", font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(18, 6))
        self.problem_tree = ttk.Treeview(frame, columns=("type", "name", "state", "reason"), show="headings", height=12)
        for key, label, width in (("type", "유형", 100), ("name", "이름", 220), ("state", "상태", 130), ("reason", "최근 원인", 600)):
            self.problem_tree.heading(key, text=label)
            self.problem_tree.column(key, width=width, anchor=tk.W)
        self.problem_tree.pack(fill=tk.BOTH, expand=True)

    def _resource_tab(self, title: str, columns: tuple[str, ...], button_builder) -> ttk.Treeview:
        frame = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(frame, text=title)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        labels = {"name": "이름", "state": "상태", "pid": "PID", "uptime": "시작 시각", "restarts": "재시작", "health": "상태 확인", "cpu": "CPU %", "memory": "메모리", "next": "다음 예약", "protocol": "프로토콜", "last": "마지막 실행", "result": "결과"}
        for column in columns:
            tree.heading(column, text=labels[column], command=lambda c=column, t=tree: self._sort_tree(t, c, False))
            tree.column(column, width=180 if column in {"name", "uptime", "next", "last"} else 100, anchor=tk.W if column == "name" else tk.CENTER)
        tree.grid(row=0, column=0, sticky="nsew")
        ttk.Scrollbar(frame, command=tree.yview).grid(row=0, column=1, sticky="ns")
        buttons = ttk.Frame(frame)
        buttons.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        button_builder(buttons, tree)
        return tree

    def _service_buttons(self, frame, tree) -> None:
        for text, command in (("추가", self.add_service), ("수정", self.edit_service), ("복제", self.duplicate_service), ("삭제", self.delete_service), ("시작", lambda: self._service_action("start")), ("종료", lambda: self._service_action("stop")), ("재시작", lambda: self._service_action("restart"))):
            ttk.Button(frame, text=text, command=command).pack(side=tk.LEFT, padx=3)

    def _job_buttons(self, frame, tree) -> None:
        for text, command in (("추가", self.add_job), ("수정", self.edit_job), ("복제", self.duplicate_job), ("삭제", self.delete_job), ("지금 실행", lambda: self._job_action("run"))):
            ttk.Button(frame, text=text, command=command).pack(side=tk.LEFT, padx=3)

    def _build_events_tab(self) -> None:
        frame = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(frame, text="이력")
        toolbar = ttk.Frame(frame)
        toolbar.pack(fill=tk.X, pady=(0, 6))
        self.level_var = tk.StringVar()
        self.keyword_var = tk.StringVar()
        ttk.Label(toolbar, text="등급").pack(side=tk.LEFT)
        ttk.Combobox(toolbar, textvariable=self.level_var, values=("", "INFO", "WARNING", "ERROR", "CRITICAL"), width=12, state="readonly").pack(side=tk.LEFT, padx=4)
        ttk.Label(toolbar, text="검색").pack(side=tk.LEFT, padx=(10, 0))
        ttk.Entry(toolbar, textvariable=self.keyword_var, width=30).pack(side=tk.LEFT, padx=4)
        ttk.Button(toolbar, text="조회", command=self.refresh_events).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="CSV 내보내기", command=self.export_events).pack(side=tk.LEFT, padx=4)
        columns = ("time", "level", "source", "type", "message")
        self.event_tree = ttk.Treeview(frame, columns=columns, show="headings")
        for key, label, width in (("time", "시각", 190), ("level", "등급", 80), ("source", "대상", 180), ("type", "이벤트", 150), ("message", "내용", 520)):
            self.event_tree.heading(key, text=label)
            self.event_tree.column(key, width=width, anchor=tk.W)
        self.event_tree.pack(fill=tk.BOTH, expand=True)

    def _build_tools_tab(self) -> None:
        frame = ttk.Frame(self.tabs, padding=20)
        self.tabs.add(frame, text="설정 및 지원")
        ttk.Label(frame, text="설정 관리", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        for text, command in (("JSON 가져오기", self.import_json), ("JSON 내보내기", self.export_json), ("데이터베이스 백업", self.backup), ("지원 진단 ZIP 생성", self.diagnostics)):
            ttk.Button(frame, text=text, command=command, width=28).pack(anchor="w", pady=4)
        ttk.Button(frame, text="장애 알림 설정", command=lambda: NotificationEditor(self.root, self.client), width=28).pack(anchor="w", pady=4)
        ttk.Separator(frame).pack(fill=tk.X, pady=16)
        ttk.Label(frame, text="보안 원칙", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        ttk.Label(frame, text="• 비밀값은 Windows DPAPI로 보호됩니다.\n• JSON 내보내기에는 비밀번호가 포함되지 않습니다.\n• Telnet은 명시적으로 위험을 확인한 작업에만 허용됩니다.", justify=tk.LEFT).pack(anchor="w", pady=8)
        ttk.Button(frame, text="관리 GUI 종료 (엔진은 계속 실행)", command=self._quit_gui).pack(anchor="w", pady=(18, 0))

    def _setup_tray(self) -> None:
        try:
            import pystray
            from PIL import Image, ImageDraw

            base = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
            icon_path = base / "icon.ico"
            if icon_path.is_file():
                image = Image.open(icon_path)
            else:
                image = Image.new("RGBA", (64, 64), (35, 45, 55, 255))
                draw = ImageDraw.Draw(image)
                draw.rounded_rectangle((10, 8, 54, 56), radius=8, fill=(240, 240, 240, 255))
            menu = pystray.Menu(
                pystray.MenuItem("창 열기", lambda *_: self.root.after(0, self._show_window), default=True),
                pystray.MenuItem("GUI 종료", lambda *_: self.root.after(0, self._quit_gui)),
            )
            self.tray_icon = pystray.Icon("python_service_manager", image, APP_TITLE, menu)
            self.tray_icon.run_detached()
        except Exception:
            self.tray_icon = None

    def _hide_window(self) -> None:
        if self.tray_icon is None:
            self.root.iconify()
            return
        self.root.withdraw()
        try:
            self.tray_icon.notify("관리 GUI를 닫아도 백그라운드 엔진은 계속 실행됩니다.", APP_TITLE)
        except Exception:
            pass

    def _show_window(self) -> None:
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()

    def _quit_gui(self) -> None:
        if self.tray_icon is not None:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
        self.root.destroy()

    @staticmethod
    def _sort_tree(tree: ttk.Treeview, column: str, reverse: bool) -> None:
        rows = [(tree.set(item, column), item) for item in tree.get_children("")]
        rows.sort(reverse=reverse)
        for index, (_, item) in enumerate(rows):
            tree.move(item, "", index)
        tree.heading(column, command=lambda: CommercialApp._sort_tree(tree, column, not reverse))

    def _selected(self, tree: ttk.Treeview, collection: str) -> dict[str, Any] | None:
        selection = tree.selection()
        if not selection:
            return None
        selected_id = selection[0]
        return next((item for item in self.data[collection] if item["definition"]["id"] == selected_id), None)

    def refresh(self) -> None:
        if self._polling:
            return
        self._polling = True

        def worker() -> None:
            try:
                response = self.client.request("list")
                self._response_queue.put(("refresh", response))
            except Exception as exc:
                self._response_queue.put(("error", exc))

        threading.Thread(target=worker, daemon=True).start()

    def _drain_responses(self) -> None:
        try:
            while True:
                kind, value = self._response_queue.get_nowait()
                if kind == "refresh":
                    self._apply_refresh(value)
                else:
                    self._connection_error(value)
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self.root.after(100, self._drain_responses)

    def _apply_refresh(self, response: dict[str, Any]) -> None:
        self._polling = False
        if not response.get("ok"):
            self._connection_error(RuntimeError(response.get("error", "알 수 없는 오류")))
            return
        self.data = response
        self.status_var.set(f"엔진 연결됨 · 서비스 {len(response['services'])} · 원격 작업 {len(response['remote_jobs'])}")
        running = sum(1 for item in response["services"] if item["runtime"].get("state") == "running")
        problems = [item for item in response["services"] if item["runtime"].get("state") in {"failed", "circuit_open", "configuration_error", "waiting_dependency"}]
        failed_jobs = [item for item in response["remote_jobs"] if item["runtime"].get("state") in {"failed", "configuration_error"}]
        self.dashboard_vars["services"].set(str(len(response["services"])))
        self.dashboard_vars["running"].set(str(running))
        self.dashboard_vars["failures"].set(str(len(problems) + len(failed_jobs)))
        self.dashboard_vars["jobs"].set(str(len(response["remote_jobs"])))
        self.problem_tree.delete(*self.problem_tree.get_children(""))
        for item in problems:
            d, r = item["definition"], item["runtime"]
            self.problem_tree.insert("", tk.END, values=("서비스", d["name"], r["state"], r.get("failure_reason") or "-"))
        for item in failed_jobs:
            d, r = item["definition"], item["runtime"]
            self.problem_tree.insert("", tk.END, values=("원격 작업", d["name"], r["state"], r.get("failure_reason") or "-"))
        for tree in (self.service_tree, self.job_tree):
            tree.delete(*tree.get_children(""))
        for item in response["services"]:
            d, r = item["definition"], item["runtime"]
            memory = r.get("memory_bytes")
            memory_text = f"{memory / 1024 / 1024:.1f} MB" if memory is not None else "-"
            state_text = f"{r['state']} · 재시작 필요" if r.get("restart_required") else r["state"]
            self.service_tree.insert("", tk.END, iid=d["id"], values=(d["name"], state_text, r.get("pid") or "-", r.get("started_at") or "-", r.get("restart_count", 0), r.get("health", "unknown"), r.get("cpu_percent") if r.get("cpu_percent") is not None else "-", memory_text, r.get("next_due") or "-"))
        for item in response["remote_jobs"]:
            d, r = item["definition"], item["runtime"]
            self.job_tree.insert("", tk.END, iid=d["id"], values=(d["name"], d["protocol"].upper(), r["state"], r.get("last_run") or "-", r.get("result", "-"), r.get("next_due") or "-"))
        self.refresh_events()
        self.root.after(2000, self.refresh)

    def _connection_error(self, exc: Exception) -> None:
        self._polling = False
        self.status_var.set(f"엔진 연결 끊김: {exc}")
        self.root.after(5000, self.refresh)

    def _request(self, command: str, **payload: Any) -> dict[str, Any]:
        try:
            response = self.client.request(command, **payload)
            if not response.get("ok"):
                raise RuntimeError(response.get("error", "요청 실패"))
            self.refresh()
            return response
        except Exception as exc:
            messagebox.showerror("작업 실패", str(exc), parent=self.root)
            return {"ok": False}

    def add_service(self) -> None:
        ServiceEditor(self.root, None, lambda item: self._request("upsert_service", item=item))

    def edit_service(self) -> None:
        item = self._selected(self.service_tree, "services")
        if item:
            ServiceEditor(self.root, item["definition"], lambda value: self._request("upsert_service", item=value))

    def duplicate_service(self) -> None:
        item = self._selected(self.service_tree, "services")
        if item:
            value = dict(item["definition"])
            value.pop("id", None)
            value["name"] += " 복사본"
            ServiceEditor(self.root, value, lambda data: self._request("upsert_service", item=data))

    def delete_service(self) -> None:
        item = self._selected(self.service_tree, "services")
        if item and messagebox.askyesno("서비스 삭제", f"'{item['definition']['name']}' 설정을 삭제하시겠습니까?", parent=self.root):
            self._request("delete_service", id=item["definition"]["id"])

    def _service_action(self, command: str) -> None:
        item = self._selected(self.service_tree, "services")
        if item:
            self._request(command, id=item["definition"]["id"])

    def add_job(self) -> None:
        RemoteJobEditor(self.root, self.client, None, lambda item, secret: self._request("upsert_remote_job", item=item, secret=secret))

    def edit_job(self) -> None:
        item = self._selected(self.job_tree, "remote_jobs")
        if item:
            RemoteJobEditor(self.root, self.client, item["definition"], lambda value, secret: self._request("upsert_remote_job", item=value, secret=secret))

    def duplicate_job(self) -> None:
        item = self._selected(self.job_tree, "remote_jobs")
        if item:
            value = dict(item["definition"])
            value.pop("id", None)
            value["credential_id"] = None
            value["name"] += " 복사본"
            RemoteJobEditor(self.root, self.client, value, lambda data, secret: self._request("upsert_remote_job", item=data, secret=secret))

    def delete_job(self) -> None:
        item = self._selected(self.job_tree, "remote_jobs")
        if item and messagebox.askyesno("작업 삭제", f"'{item['definition']['name']}' 설정을 삭제하시겠습니까?", parent=self.root):
            self._request("delete_remote_job", id=item["definition"]["id"])

    def _job_action(self, command: str) -> None:
        item = self._selected(self.job_tree, "remote_jobs")
        if item:
            self._request(command, id=item["definition"]["id"])

    def refresh_events(self) -> None:
        try:
            response = self.client.request("events", limit=500, level=self.level_var.get() or None, keyword=self.keyword_var.get() or None)
            if not response.get("ok"):
                return
            self.event_tree.delete(*self.event_tree.get_children(""))
            for event in response["events"]:
                self.event_tree.insert("", tk.END, values=(event["timestamp"], event["level"], event["source_name"], event["event_type"], event["message"]))
        except Exception:
            pass

    def import_json(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("JSON", "*.json")])
        if path and messagebox.askyesno("설정 가져오기", "검증된 항목을 현재 설정에 추가하거나 덮어씁니다. 계속하시겠습니까?", parent=self.root):
            self._request("import", path=path)

    def export_events(self) -> None:
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if path:
            self._request("export_events", path=path, limit=5000)

    def export_json(self) -> None:
        path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON", "*.json")])
        if path:
            self._request("export", path=path)

    def backup(self) -> None:
        response = self._request("backup")
        if response.get("ok"):
            messagebox.showinfo("백업 완료", response["path"], parent=self.root)

    def diagnostics(self) -> None:
        path = filedialog.asksaveasfilename(defaultextension=".zip", filetypes=[("ZIP", "*.zip")])
        if path:
            response = self._request("diagnostics", path=path)
            if response.get("ok"):
                messagebox.showinfo("진단 자료 생성", response["path"], parent=self.root)


def run_gui(*, standalone: bool = False, database: str | None = None, legacy_config: str | None = None) -> None:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    embedded_engine = None
    instance_lock = None
    if standalone:
        instance_lock = EngineInstanceLock()
        instance_lock.acquire()
        embedded_engine = ServiceManagerEngine(database_path=database, legacy_config=legacy_config)
        client = EmbeddedClient(embedded_engine)
    else:
        client = EngineClient()
    root = tk.Tk()
    CommercialApp(root, client)
    try:
        root.mainloop()
    finally:
        if embedded_engine:
            embedded_engine.shutdown()
        if instance_lock:
            instance_lock.release()


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--standalone", action="store_true")
    parser.add_argument("--database")
    parser.add_argument("--legacy-config")
    args, _ = parser.parse_known_args()
    run_gui(standalone=args.standalone, database=args.database, legacy_config=args.legacy_config)


if __name__ == "__main__":
    main()
