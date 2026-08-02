from __future__ import annotations

import argparse
import signal
import threading

from engine import ServiceManagerEngine
from instance_lock import EngineInstanceLock
from ipc import EngineIpcServer


def run_console(database: str | None = None, legacy_config: str | None = None) -> int:
    instance_lock = EngineInstanceLock()
    instance_lock.acquire()
    engine = ServiceManagerEngine(database_path=database, legacy_config=legacy_config)
    server = EngineIpcServer(engine.dispatch)
    server.start()
    stopping = threading.Event()

    def stop_handler(*_args) -> None:
        if stopping.is_set():
            return
        stopping.set()
        server.stop()
        engine.shutdown()
        instance_lock.release()

    signal.signal(signal.SIGINT, stop_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop_handler)
    try:
        engine.wait()
    finally:
        stop_handler()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Python Service Manager background engine")
    parser.add_argument("--database")
    parser.add_argument("--legacy-config", help="기존 config.json을 최초 1회 가져올 때만 지정합니다.")
    args = parser.parse_args()
    raise SystemExit(run_console(args.database, args.legacy_config))


if __name__ == "__main__":
    main()
