from __future__ import annotations

import select
import socket
import time

IAC = 255
DONT = 254
DO = 253
WONT = 252
WILL = 251
SB = 250
SE = 240


class Telnet:
    """Small synchronous Telnet client used when stdlib telnetlib is unavailable.

    It supports the subset required by this application: connect, write,
    read_until, and close. Telnet option requests are declined so ordinary
    login shells remain usable without terminal emulation.
    """

    def __init__(self, host: str | None = None, port: int = 0, timeout: float | None = None):
        self.sock: socket.socket | None = None
        self.timeout = timeout
        self._cooked = bytearray()
        self._iac_pending = False
        self._command_pending: int | None = None
        self._subnegotiation = False
        self._sub_iac = False
        if host is not None:
            self.open(host, port, timeout)

    def open(self, host: str, port: int = 0, timeout: float | None = None) -> None:
        self.close()
        self.timeout = timeout
        self.sock = socket.create_connection((host, port or 23), timeout=timeout)

    def close(self) -> None:
        sock, self.sock = self.sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def write(self, buffer: bytes) -> None:
        if self.sock is None:
            raise EOFError("Telnet 연결이 닫혀 있습니다.")
        # Literal IAC bytes must be escaped as IAC IAC.
        escaped = buffer.replace(bytes((IAC,)), bytes((IAC, IAC)))
        self.sock.sendall(escaped)

    def read_until(self, expected: bytes, timeout: float | None = None) -> bytes:
        if not expected:
            raise ValueError("expected는 비어 있을 수 없습니다.")
        deadline = None if timeout is None else time.monotonic() + timeout

        while True:
            index = self._cooked.find(expected)
            if index >= 0:
                end = index + len(expected)
                result = bytes(self._cooked[:end])
                del self._cooked[:end]
                return result

            if self.sock is None:
                if self._cooked:
                    result = bytes(self._cooked)
                    self._cooked.clear()
                    return result
                raise EOFError("Telnet 연결이 닫혔습니다.")

            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            if remaining == 0.0:
                result = bytes(self._cooked)
                self._cooked.clear()
                return result

            readable, _, _ = select.select([self.sock], [], [], remaining)
            if not readable:
                result = bytes(self._cooked)
                self._cooked.clear()
                return result

            chunk = self.sock.recv(4096)
            if not chunk:
                self.close()
                if self._cooked:
                    result = bytes(self._cooked)
                    self._cooked.clear()
                    return result
                raise EOFError("서버가 Telnet 연결을 종료했습니다.")
            self._process_received(chunk)

    def _process_received(self, data: bytes) -> None:
        for value in data:
            if self._subnegotiation:
                if self._sub_iac:
                    self._sub_iac = False
                    if value == SE:
                        self._subnegotiation = False
                    elif value == IAC:
                        continue
                elif value == IAC:
                    self._sub_iac = True
                continue

            if self._command_pending is not None:
                command = self._command_pending
                self._command_pending = None
                self._decline_option(command, value)
                continue

            if self._iac_pending:
                self._iac_pending = False
                if value == IAC:
                    self._cooked.append(IAC)
                elif value in (DO, DONT, WILL, WONT):
                    self._command_pending = value
                elif value == SB:
                    self._subnegotiation = True
                # Other two-byte Telnet commands are ignored.
                continue

            if value == IAC:
                self._iac_pending = True
            else:
                self._cooked.append(value)

    def _decline_option(self, command: int, option: int) -> None:
        if self.sock is None:
            return
        if command in (DO, DONT):
            response = bytes((IAC, WONT, option))
        else:
            response = bytes((IAC, DONT, option))
        try:
            self.sock.sendall(response)
        except OSError:
            self.close()
