from __future__ import annotations

import subprocess
import sys
from pathlib import Path

try:
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil
except ImportError:
    servicemanager = win32event = win32service = win32serviceutil = None  # type: ignore

from engine import ServiceManagerEngine
from instance_lock import EngineInstanceLock
from ipc import EngineIpcServer

SERVICE_NAME = "PythonServiceManagerEngine"
DISPLAY_NAME = "Python Service Manager Engine"


if win32serviceutil is not None:
    class PythonServiceManagerService(win32serviceutil.ServiceFramework):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = DISPLAY_NAME
        _svc_description_ = "Monitors, schedules and recovers local services and secure remote jobs."

        def __init__(self, args):
            super().__init__(args)
            self.stop_event = win32event.CreateEvent(None, 0, 0, None)
            self.engine: ServiceManagerEngine | None = None
            self.server: EngineIpcServer | None = None
            self.instance_lock = EngineInstanceLock()

        def GetAcceptedControls(self):
            return super().GetAcceptedControls() | win32service.SERVICE_ACCEPT_PRESHUTDOWN

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, waitHint=15000)
            win32event.SetEvent(self.stop_event)

        def SvcShutdown(self):
            self.SvcStop()

        def SvcDoRun(self):
            self.instance_lock.acquire()
            app_dir = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
            self.engine = ServiceManagerEngine(legacy_config=app_dir / "config.json")
            self.server = EngineIpcServer(self.engine.dispatch)
            self.server.start()
            servicemanager.LogInfoMsg(f"{DISPLAY_NAME} started")
            win32event.WaitForSingleObject(self.stop_event, win32event.INFINITE)
            self.server.stop()
            self.engine.shutdown()
            self.instance_lock.release()
            servicemanager.LogInfoMsg(f"{DISPLAY_NAME} stopped")


def configure_service_recovery() -> None:
    subprocess.run(["sc.exe", "config", SERVICE_NAME, "obj=", f"NT SERVICE\\{SERVICE_NAME}", "password=", ""], check=True)
    subprocess.run(["sc.exe", "config", SERVICE_NAME, "start=", "delayed-auto"], check=True)
    subprocess.run(
        ["sc.exe", "failure", SERVICE_NAME, "reset=", "86400", "actions=", "restart/5000/restart/30000/restart/60000"],
        check=True,
    )
    subprocess.run(["sc.exe", "failureflag", SERVICE_NAME, "1"], check=True)


def main() -> None:
    if win32serviceutil is None:
        raise SystemExit("Windows 서비스 기능을 사용하려면 pywin32가 필요합니다.")
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(PythonServiceManagerService)
        servicemanager.StartServiceCtrlDispatcher()
        return
    command = sys.argv[1].lower()
    win32serviceutil.HandleCommandLine(PythonServiceManagerService)
    if command in {"install", "update"}:
        configure_service_recovery()


if __name__ == "__main__":
    main()
