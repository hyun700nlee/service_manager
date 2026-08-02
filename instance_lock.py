from __future__ import annotations

import ctypes
import os

from storage import default_data_directory


class AlreadyRunningError(RuntimeError):
    pass


class EngineInstanceLock:
    def __init__(self):
        self._handle = None
        self._file = None

    def acquire(self) -> None:
        if os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            handle = kernel32.CreateMutexW(None, False, "Global\\PythonServiceManagerEngine-v1")
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            if ctypes.get_last_error() == 183:
                kernel32.CloseHandle(handle)
                raise AlreadyRunningError("서비스 관리자 엔진이 이미 실행 중입니다.")
            self._handle = handle
            return
        import fcntl

        path = default_data_directory() / "engine.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("a+")
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._file.close()
            self._file = None
            raise AlreadyRunningError("서비스 관리자 엔진이 이미 실행 중입니다.") from exc

    def release(self) -> None:
        if self._handle is not None:
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(self._handle)
            self._handle = None
        if self._file is not None:
            self._file.close()
            self._file = None

    def __enter__(self) -> "EngineInstanceLock":
        self.acquire()
        return self

    def __exit__(self, *_args) -> None:
        self.release()
