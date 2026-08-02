from __future__ import annotations

import base64
import os
import secrets
import threading
from multiprocessing.connection import Client, Listener
from pathlib import Path
from typing import Any, Callable

from credentials import protect_secret, unprotect_secret
from storage import default_data_directory

PIPE_ADDRESS = r"\\.\pipe\PythonServiceManager-v1" if os.name == "nt" else (str(default_data_directory() / "engine.sock"))


def _secure_file(path: Path) -> None:
    if os.name != "nt":
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return
    try:
        import ntsecuritycon
        import win32api
        import win32con
        import win32security

        descriptor = win32security.SECURITY_DESCRIPTOR()
        dacl = win32security.ACL()
        system_sid = win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None)
        admins_sid = win32security.CreateWellKnownSid(win32security.WinBuiltinAdministratorsSid, None)
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        current_sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        for sid in (system_sid, admins_sid, current_sid):
            dacl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, sid)
        descriptor.SetSecurityDescriptorDacl(1, dacl, 0)
        win32security.SetFileSecurity(str(path), win32security.DACL_SECURITY_INFORMATION, descriptor)
    except Exception:
        # The HMAC key is still DPAPI protected; the installer applies the final directory ACL.
        pass


def load_or_create_authkey(path: str | Path | None = None) -> bytes:
    target = Path(path or default_data_directory() / "ipc.key")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        envelope = target.read_text(encoding="ascii").strip()
        return base64.b64decode(unprotect_secret(envelope), validate=True)
    key = secrets.token_bytes(32)
    envelope = protect_secret(base64.b64encode(key).decode("ascii"))
    temporary = target.with_suffix(".tmp")
    temporary.write_text(envelope, encoding="ascii")
    os.replace(temporary, target)
    _secure_file(target)
    return key


class EngineIpcServer:
    def __init__(self, dispatcher: Callable[[dict[str, Any]], dict[str, Any]], *, address: Any = PIPE_ADDRESS, authkey: bytes | None = None):
        self.dispatcher = dispatcher
        self.address = address
        self.authkey = authkey or load_or_create_authkey()
        self._shutdown = threading.Event()
        self._listener: Listener | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if os.name != "nt" and isinstance(self.address, str):
            path = Path(self.address)
            if path.exists():
                path.unlink()
        family = "AF_PIPE" if os.name == "nt" else "AF_UNIX"
        self._listener = Listener(self.address, family=family, authkey=self.authkey)
        self._thread = threading.Thread(target=self._accept_loop, name="engine-ipc", daemon=True)
        self._thread.start()

    def _accept_loop(self) -> None:
        assert self._listener is not None
        while not self._shutdown.is_set():
            try:
                connection = self._listener.accept()
            except (OSError, EOFError):
                break
            threading.Thread(target=self._serve_connection, args=(connection,), daemon=True).start()

    def _serve_connection(self, connection) -> None:
        try:
            request = connection.recv()
            if not isinstance(request, dict):
                connection.send({"ok": False, "error": "요청은 객체여야 합니다."})
            else:
                connection.send(self.dispatcher(request))
        except (OSError, EOFError) as exc:
            try:
                connection.send({"ok": False, "error": str(exc)})
            except (OSError, EOFError):
                pass
        finally:
            connection.close()

    def stop(self) -> None:
        self._shutdown.set()
        if self._listener:
            self._listener.close()
        if os.name != "nt" and isinstance(self.address, str):
            try:
                Path(self.address).unlink()
            except OSError:
                pass


class EngineClient:
    def __init__(self, *, address: Any = PIPE_ADDRESS, authkey: bytes | None = None):
        self.address = address
        self.authkey = authkey

    def request(self, command: str, **payload: Any) -> dict[str, Any]:
        authkey = self.authkey or load_or_create_authkey()
        family = "AF_PIPE" if os.name == "nt" else "AF_UNIX"
        connection = Client(self.address, family=family, authkey=authkey)
        try:
            connection.send({"command": command, **payload})
            response = connection.recv()
            if not isinstance(response, dict):
                raise RuntimeError("엔진 응답 형식이 올바르지 않습니다.")
            return response
        finally:
            connection.close()
