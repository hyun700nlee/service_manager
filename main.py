from __future__ import annotations

import queue
import sys
import threading
import time
import tkinter as tk
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

from config_loader import ConfigLoadResult, load_config
from process_manager import ProcessManager
from schedule_utils import advance_due, describe_schedule, format_datetime, initial_next_due
from telnet_worker import TelnetJobManager


APP_TITLE = "Python 서비스 관리자"
QUEUE_POLL_MS = 100
SCHEDULE_POLL_MS = 10_000
MAX_LOG_LINES = 2_000


def application_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


class ServiceManagerApp:
    def __init__(self, root: tk.Tk, config_result: ConfigLoadResult, app_dir: Path):
        self.root = root
        self.app_dir = app_dir
        self.services = config_result.services
        self.telnet_jobs = config_result.telnet_jobs
        self.service_by_name = {item["name"]: item for item in self.services}
        self.job_by_name = {item["name"]: item for item in self.telnet_jobs}

        self.event_queue: queue.Queue = queue.Queue()
        self.process_manager = ProcessManager(self.services, self.event_queue)
        self.telnet_manager = TelnetJobManager(self.telnet_jobs, self.event_queue)

        self.logs: dict[tuple[str, str], deque[str]] = defaultdict(lambda: deque(maxlen=MAX_LOG_LINES))
        self.selected_log_key: tuple[str, str] = ("system", "시스템")
        self.service_values: dict[str, dict[str, Any]] = {}
        self.telnet_values: dict[str, dict[str, Any]] = {}
        self.service_next_due: dict[str, datetime | None] = {}
        self.telnet_next_due: dict[str, datetime | None] = {}

        self._queue_after_id: str | None = None
        self._schedule_after_id: str | None = None
        self._exiting = False
        self._exit_deadline = 0.0
        self.tray_icon = None

        self.auto_scroll_var = tk.BooleanVar(value=True)
        self.current_log_var = tk.StringVar(value="현재 로그: 시스템")

        self._configure_window()
        self._build_gui()
        self._populate_tables()
        self._initialize_schedules()
        self._setup_tray()

        for error in config_result.global_errors:
            self._append_log(("system", "시스템"), f"[ERROR] {error}")

        self.process_manager.initialize_states()
        self.telnet_manager.initialize_states()
        self._queue_after_id = self.root.after(QUEUE_POLL_MS, self._drain_event_queue)
        self._schedule_after_id = self.root.after(SCHEDULE_POLL_MS, self._check_schedules)
        self.root.after(300, self._start_auto_services)

    def _configure_window(self) -> None:
        self.root.title(APP_TITLE)
        self.root.geometry("1080x760")
        self.root.minsize(850, 600)
        icon_path = self.app_dir / "icon.ico"
        if icon_path.is_file():
            try:
                self.root.iconbitmap(default=str(icon_path))
            except tk.TclError:
                pass
        self.root.protocol("WM_DELETE_WINDOW", self._hide_to_tray)

    def _build_gui(self) -> None:
        main = ttk.Frame(self.root, padding=8)
        main.pack(fill=tk.BOTH, expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(0, weight=3)
        main.rowconfigure(1, weight=2)
        main.rowconfigure(2, weight=4)

        service_frame = ttk.LabelFrame(main, text="Python 서비스", padding=6)
        service_frame.grid(row=0, column=0, sticky="nsew", pady=(0, 6))
        service_frame.columnconfigure(0, weight=1)
        service_frame.rowconfigure(0, weight=1)

        service_columns = ("name", "state", "pid", "last_start", "schedule")
        self.service_tree = ttk.Treeview(service_frame, columns=service_columns, show="headings", selectmode="browse")
        headings = {
            "name": "서비스명",
            "state": "상태",
            "pid": "PID",
            "last_start": "마지막 시작",
            "schedule": "예약",
        }
        widths = {"name": 260, "state": 90, "pid": 90, "last_start": 180, "schedule": 230}
        for key in service_columns:
            self.service_tree.heading(key, text=headings[key])
            self.service_tree.column(key, width=widths[key], anchor=tk.CENTER if key != "name" else tk.W)
        service_scroll = ttk.Scrollbar(service_frame, orient=tk.VERTICAL, command=self.service_tree.yview)
        self.service_tree.configure(yscrollcommand=service_scroll.set)
        self.service_tree.grid(row=0, column=0, sticky="nsew")
        service_scroll.grid(row=0, column=1, sticky="ns")
        self.service_tree.bind("<<TreeviewSelect>>", self._on_service_selected)

        service_buttons = ttk.Frame(service_frame)
        service_buttons.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Button(service_buttons, text="시작", command=self._start_selected_service).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(service_buttons, text="종료", command=self._stop_selected_service).pack(side=tk.LEFT, padx=4)
        ttk.Button(service_buttons, text="재시작", command=self._restart_selected_service).pack(side=tk.LEFT, padx=4)
        ttk.Separator(service_buttons, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)
        ttk.Button(service_buttons, text="전체 시작", command=lambda: self.process_manager.start_all(0.0)).pack(side=tk.LEFT, padx=4)
        ttk.Button(service_buttons, text="전체 종료", command=self.process_manager.stop_all_async).pack(side=tk.LEFT, padx=4)

        telnet_frame = ttk.LabelFrame(main, text="Telnet 작업", padding=6)
        telnet_frame.grid(row=1, column=0, sticky="nsew", pady=(0, 6))
        telnet_frame.columnconfigure(0, weight=1)
        telnet_frame.rowconfigure(0, weight=1)

        telnet_columns = ("name", "state", "last_run", "next_run", "result")
        self.telnet_tree = ttk.Treeview(telnet_frame, columns=telnet_columns, show="headings", selectmode="browse")
        telnet_headings = {
            "name": "작업명",
            "state": "상태",
            "last_run": "마지막 실행",
            "next_run": "다음 실행",
            "result": "결과",
        }
        telnet_widths = {"name": 260, "state": 90, "last_run": 180, "next_run": 180, "result": 160}
        for key in telnet_columns:
            self.telnet_tree.heading(key, text=telnet_headings[key])
            self.telnet_tree.column(key, width=telnet_widths[key], anchor=tk.CENTER if key != "name" else tk.W)
        telnet_scroll = ttk.Scrollbar(telnet_frame, orient=tk.VERTICAL, command=self.telnet_tree.yview)
        self.telnet_tree.configure(yscrollcommand=telnet_scroll.set)
        self.telnet_tree.grid(row=0, column=0, sticky="nsew")
        telnet_scroll.grid(row=0, column=1, sticky="ns")
        self.telnet_tree.bind("<<TreeviewSelect>>", self._on_telnet_selected)

        telnet_buttons = ttk.Frame(telnet_frame)
        telnet_buttons.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Button(telnet_buttons, text="지금 실행", command=self._run_selected_telnet).pack(side=tk.LEFT)

        log_frame = ttk.LabelFrame(main, text="로그", padding=6)
        log_frame.grid(row=2, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(1, weight=1)

        log_toolbar = ttk.Frame(log_frame)
        log_toolbar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        ttk.Label(log_toolbar, textvariable=self.current_log_var).pack(side=tk.LEFT)
        ttk.Checkbutton(log_toolbar, text="자동 스크롤", variable=self.auto_scroll_var).pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(log_toolbar, text="화면 지우기", command=self._clear_current_log).pack(side=tk.RIGHT)

        self.log_text = tk.Text(log_frame, wrap="none", state=tk.DISABLED, font=("Consolas", 10))
        log_y_scroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        log_x_scroll = ttk.Scrollbar(log_frame, orient=tk.HORIZONTAL, command=self.log_text.xview)
        self.log_text.configure(yscrollcommand=log_y_scroll.set, xscrollcommand=log_x_scroll.set)
        self.log_text.grid(row=1, column=0, sticky="nsew")
        log_y_scroll.grid(row=1, column=1, sticky="ns")
        log_x_scroll.grid(row=2, column=0, sticky="ew")

    def _populate_tables(self) -> None:
        for config in self.services:
            name = config["name"]
            state = "중지" if config.get("_enabled") else "오류"
            schedule = describe_schedule(config, service=True)
            if config.get("auto_start"):
                schedule += " / 자동 시작"
            values = {
                "name": name,
                "state": state,
                "pid": "-",
                "last_start": "-",
                "schedule": schedule,
            }
            self.service_values[name] = values
            self.service_tree.insert("", tk.END, iid=name, values=tuple(values[column] for column in ("name", "state", "pid", "last_start", "schedule")))

        for config in self.telnet_jobs:
            name = config["name"]
            state = "대기" if config.get("_enabled") else "실패"
            result = "-" if config.get("_enabled") else "설정 오류"
            values = {
                "name": name,
                "state": state,
                "last_run": "-",
                "next_run": "-",
                "result": result,
            }
            self.telnet_values[name] = values
            self.telnet_tree.insert("", tk.END, iid=name, values=tuple(values[column] for column in ("name", "state", "last_run", "next_run", "result")))

    def _initialize_schedules(self) -> None:
        now = datetime.now()
        for config in self.services:
            name = config["name"]
            try:
                self.service_next_due[name] = initial_next_due(
                    config.get("schedule_type", "none") if config.get("_enabled") else "none",
                    now=now,
                    daily_time=config.get("restart_time"),
                    interval_minutes=config.get("restart_interval_minutes"),
                )
            except ValueError as exc:
                self.service_next_due[name] = None
                self._append_log(("service", name), f"[ERROR] 예약 초기화 실패: {exc}")

        for config in self.telnet_jobs:
            name = config["name"]
            schedule_type = config.get("schedule_type", "none") if config.get("_enabled") and config.get("auto_run") else "none"
            try:
                due = initial_next_due(
                    schedule_type,
                    now=now,
                    daily_time=config.get("run_time"),
                    interval_minutes=config.get("interval_minutes"),
                )
            except ValueError as exc:
                due = None
                self._append_log(("telnet", name), f"[ERROR] 예약 초기화 실패: {exc}")
            self.telnet_next_due[name] = due
            self._update_telnet_next_run(name)

    def _start_auto_services(self) -> None:
        names = [item["name"] for item in self.services if item.get("_enabled") and item.get("auto_start")]

        def start_sequence() -> None:
            for index, name in enumerate(names):
                if self._exiting:
                    return
                if index > 0 and self._wait_for_exit(2.0):
                    return
                self.process_manager.start_async(name)

        threading.Thread(target=start_sequence, daemon=True).start()

    def _wait_for_exit(self, seconds: float) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self._exiting:
                return True
            time.sleep(min(0.1, end - time.monotonic()))
        return self._exiting

    def _selected_service_name(self) -> str | None:
        selection = self.service_tree.selection()
        return selection[0] if selection else None

    def _selected_telnet_name(self) -> str | None:
        selection = self.telnet_tree.selection()
        return selection[0] if selection else None

    def _start_selected_service(self) -> None:
        name = self._selected_service_name()
        if name:
            self.process_manager.start_async(name)

    def _stop_selected_service(self) -> None:
        name = self._selected_service_name()
        if name:
            self.process_manager.stop_async(name)

    def _restart_selected_service(self) -> None:
        name = self._selected_service_name()
        if name:
            self.process_manager.restart_async(name)

    def _run_selected_telnet(self) -> None:
        name = self._selected_telnet_name()
        if name:
            self.telnet_manager.run_async(name, manual=True)

    def _on_service_selected(self, _event=None) -> None:
        name = self._selected_service_name()
        if not name:
            return
        self.telnet_tree.selection_remove(*self.telnet_tree.selection())
        self._show_log(("service", name), f"Python 서비스 / {name}")

    def _on_telnet_selected(self, _event=None) -> None:
        name = self._selected_telnet_name()
        if not name:
            return
        self.service_tree.selection_remove(*self.service_tree.selection())
        self._show_log(("telnet", name), f"Telnet 작업 / {name}")

    def _show_log(self, key: tuple[str, str], title: str) -> None:
        self.selected_log_key = key
        self.current_log_var.set(f"현재 로그: {title}")
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        content = "\n".join(self.logs[key])
        if content:
            self.log_text.insert(tk.END, content + "\n")
        self.log_text.configure(state=tk.DISABLED)
        if self.auto_scroll_var.get():
            self.log_text.see(tk.END)

    def _clear_current_log(self) -> None:
        self.logs[self.selected_log_key].clear()
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _append_log(self, key: tuple[str, str], line: str) -> None:
        self.logs[key].append(line)
        if key != self.selected_log_key:
            return
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, line + "\n")
        try:
            line_count = int(self.log_text.index("end-1c").split(".")[0])
            if line_count > MAX_LOG_LINES + 1:
                self.log_text.delete("1.0", "2.0")
        except (tk.TclError, ValueError):
            pass
        self.log_text.configure(state=tk.DISABLED)
        if self.auto_scroll_var.get():
            self.log_text.see(tk.END)

    def _drain_event_queue(self) -> None:
        if self._exiting and not self.root.winfo_exists():
            return
        try:
            while True:
                event = self.event_queue.get_nowait()
                event_type = event.get("type")
                if event_type == "log":
                    self._handle_log_event(event)
                elif event_type == "service_state":
                    self._handle_service_state(event)
                elif event_type == "telnet_state":
                    self._handle_telnet_state(event)
        except queue.Empty:
            pass
        if not self._exiting:
            self._queue_after_id = self.root.after(QUEUE_POLL_MS, self._drain_event_queue)

    def _handle_log_event(self, event: dict[str, Any]) -> None:
        timestamp = event.get("timestamp") or datetime.now()
        stream = event.get("stream", "system")
        prefix = "[ERR] " if stream == "stderr" else ""
        line = f"[{timestamp:%Y-%m-%d %H:%M:%S}] {prefix}{event.get('message', '')}"
        self._append_log((event["source_type"], event["name"]), line)

    def _handle_service_state(self, event: dict[str, Any]) -> None:
        name = event["name"]
        if name not in self.service_values:
            return
        values = self.service_values[name]
        values["state"] = event["state"]
        values["pid"] = event.get("pid") or "-"
        values["last_start"] = format_datetime(event.get("last_start"))
        self.service_tree.item(name, values=tuple(values[column] for column in ("name", "state", "pid", "last_start", "schedule")))

        config = self.service_by_name[name]
        if event["state"] == "실행 중" and config.get("schedule_type") == "interval":
            started_at = event.get("last_start") or datetime.now()
            try:
                self.service_next_due[name] = initial_next_due(
                    "interval",
                    now=started_at,
                    interval_minutes=config.get("restart_interval_minutes"),
                )
            except ValueError:
                self.service_next_due[name] = None

    def _handle_telnet_state(self, event: dict[str, Any]) -> None:
        name = event["name"]
        if name not in self.telnet_values:
            return
        values = self.telnet_values[name]
        values["state"] = event["state"]
        values["last_run"] = format_datetime(event.get("last_run"))
        values["result"] = event.get("result", "-")
        values["next_run"] = format_datetime(self.telnet_next_due.get(name))
        self.telnet_tree.item(name, values=tuple(values[column] for column in ("name", "state", "last_run", "next_run", "result")))

    def _check_schedules(self) -> None:
        if self._exiting:
            return
        now = datetime.now()

        for config in self.services:
            name = config["name"]
            due = self.service_next_due.get(name)
            if not config.get("_enabled") or due is None or now < due:
                continue
            self._append_log(("service", name), f"[{now:%Y-%m-%d %H:%M:%S}] 예약 재시작 시각 도래")
            self.process_manager.restart_async(name)
            try:
                self.service_next_due[name] = advance_due(
                    due,
                    config.get("schedule_type", "none"),
                    now=now,
                    daily_time=config.get("restart_time"),
                    interval_minutes=config.get("restart_interval_minutes"),
                )
            except ValueError as exc:
                self.service_next_due[name] = None
                self._append_log(("service", name), f"[{now:%Y-%m-%d %H:%M:%S}] [ERROR] 예약 갱신 실패: {exc}")

        for config in self.telnet_jobs:
            name = config["name"]
            due = self.telnet_next_due.get(name)
            if not config.get("_enabled") or not config.get("auto_run") or due is None or now < due:
                continue
            self.telnet_manager.run_async(name, manual=False)
            try:
                self.telnet_next_due[name] = advance_due(
                    due,
                    config.get("schedule_type", "none"),
                    now=now,
                    daily_time=config.get("run_time"),
                    interval_minutes=config.get("interval_minutes"),
                )
            except ValueError as exc:
                self.telnet_next_due[name] = None
                self._append_log(("telnet", name), f"[{now:%Y-%m-%d %H:%M:%S}] [ERROR] 예약 갱신 실패: {exc}")
            self._update_telnet_next_run(name)

        self._schedule_after_id = self.root.after(SCHEDULE_POLL_MS, self._check_schedules)

    def _update_telnet_next_run(self, name: str) -> None:
        if name not in self.telnet_values or not self.telnet_tree.exists(name):
            return
        values = self.telnet_values[name]
        values["next_run"] = format_datetime(self.telnet_next_due.get(name))
        self.telnet_tree.item(name, values=tuple(values[column] for column in ("name", "state", "last_run", "next_run", "result")))

    def _setup_tray(self) -> None:
        try:
            import pystray
            from PIL import Image, ImageDraw
        except Exception as exc:
            self._append_log(("system", "시스템"), f"[ERROR] 트레이 기능을 사용할 수 없습니다: {exc}")
            return

        icon_path = self.app_dir / "icon.ico"
        try:
            image = Image.open(icon_path) if icon_path.is_file() else self._create_tray_image(Image, ImageDraw)
        except Exception:
            image = self._create_tray_image(Image, ImageDraw)

        def tk_call(callback):
            return lambda _icon=None, _item=None: self.root.after(0, callback)

        menu = pystray.Menu(
            pystray.MenuItem("창 열기", tk_call(self._restore_window), default=True),
            pystray.MenuItem("전체 서비스 시작", tk_call(lambda: self.process_manager.start_all(0.0))),
            pystray.MenuItem("전체 서비스 종료", tk_call(self.process_manager.stop_all_async)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("프로그램 종료", tk_call(self._request_exit)),
        )
        self.tray_icon = pystray.Icon("python_service_manager", image, APP_TITLE, menu)
        try:
            self.tray_icon.run_detached()
        except Exception as exc:
            self.tray_icon = None
            self._append_log(("system", "시스템"), f"[ERROR] 트레이 아이콘 시작 실패: {exc}")

    @staticmethod
    def _create_tray_image(Image, ImageDraw):
        image = Image.new("RGBA", (64, 64), (35, 45, 55, 255))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((10, 8, 54, 56), radius=8, fill=(240, 240, 240, 255))
        draw.rectangle((18, 18, 46, 24), fill=(35, 45, 55, 255))
        draw.rectangle((18, 30, 46, 36), fill=(35, 45, 55, 255))
        draw.rectangle((18, 42, 38, 48), fill=(35, 45, 55, 255))
        return image

    def _hide_to_tray(self) -> None:
        if self.tray_icon is None:
            self._request_exit()
            return
        self.root.withdraw()
        try:
            self.tray_icon.notify("프로그램이 알림 영역에서 계속 실행 중입니다.", APP_TITLE)
        except Exception:
            pass

    def _restore_window(self) -> None:
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.focus_force()

    def _request_exit(self) -> None:
        if self._exiting:
            return
        self._restore_window()
        confirmed = messagebox.askyesno(
            "프로그램 종료",
            "실행 중인 Python 서비스를 모두 종료하고 프로그램을 종료하시겠습니까?",
            parent=self.root,
        )
        if not confirmed:
            return

        self._exiting = True
        if self._queue_after_id is not None:
            try:
                self.root.after_cancel(self._queue_after_id)
            except tk.TclError:
                pass
        if self._schedule_after_id is not None:
            try:
                self.root.after_cancel(self._schedule_after_id)
            except tk.TclError:
                pass

        self.telnet_manager.shutdown()
        self.process_manager.shutdown(stop_services=True)
        self._exit_deadline = time.monotonic() + 10.0
        self._poll_exit_completion()

    def _poll_exit_completion(self) -> None:
        self._drain_remaining_events_once()
        if not self.process_manager.any_running() or time.monotonic() >= self._exit_deadline:
            if self.tray_icon is not None:
                try:
                    self.tray_icon.stop()
                except Exception:
                    pass
            self.root.destroy()
            return
        self.root.after(100, self._poll_exit_completion)

    def _drain_remaining_events_once(self) -> None:
        try:
            while True:
                event = self.event_queue.get_nowait()
                if event.get("type") == "log":
                    self._handle_log_event(event)
                elif event.get("type") == "service_state":
                    self._handle_service_state(event)
                elif event.get("type") == "telnet_state":
                    self._handle_telnet_state(event)
        except queue.Empty:
            pass


def main() -> None:
    app_dir = application_directory()
    config_result = load_config(app_dir / "config.json")
    root = tk.Tk()
    ServiceManagerApp(root, config_result, app_dir)
    root.mainloop()


if __name__ == "__main__":
    main()
