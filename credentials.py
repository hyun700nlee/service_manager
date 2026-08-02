from __future__ import annotations

import base64
import ctypes
import os
import re
from ctypes import wintypes


class CredentialProtectionError(RuntimeError):
    pass


if os.name == "nt":
    class _DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


    def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
        buffer = ctypes.create_string_buffer(data)
        return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def protect_secret(secret: str, *, machine_scope: bool = True) -> str:
    """Protect a secret with Windows DPAPI and return a portable base64 envelope."""
    if os.name != "nt":
        raise CredentialProtectionError("DPAPI는 Windows에서만 사용할 수 있습니다.")
    raw = secret.encode("utf-8")
    input_blob, input_buffer = _blob(raw)
    output_blob = _DataBlob()
    flags = 0x4 if machine_scope else 0
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptProtectData(
        ctypes.byref(input_blob), "ServiceManager", None, None, None, flags, ctypes.byref(output_blob)
    ):
        raise CredentialProtectionError(f"DPAPI 암호화 실패: {ctypes.get_last_error()}")
    try:
        protected = ctypes.string_at(output_blob.pbData, output_blob.cbData)
        return "dpapi-machine:" + base64.b64encode(protected).decode("ascii")
    finally:
        kernel32.LocalFree(output_blob.pbData)
        del input_buffer


def unprotect_secret(envelope: str) -> str:
    if os.name != "nt":
        raise CredentialProtectionError("DPAPI는 Windows에서만 사용할 수 있습니다.")
    if not envelope.startswith("dpapi-machine:"):
        raise CredentialProtectionError("지원하지 않는 자격증명 형식입니다.")
    try:
        protected = base64.b64decode(envelope.split(":", 1)[1], validate=True)
    except (ValueError, TypeError) as exc:
        raise CredentialProtectionError("손상된 자격증명입니다.") from exc
    input_blob, input_buffer = _blob(protected)
    output_blob = _DataBlob()
    description = wintypes.LPWSTR()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptUnprotectData(
        ctypes.byref(input_blob), ctypes.byref(description), None, None, None, 0, ctypes.byref(output_blob)
    ):
        raise CredentialProtectionError(f"DPAPI 복호화 실패: {ctypes.get_last_error()}")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData).decode("utf-8")
    finally:
        kernel32.LocalFree(output_blob.pbData)
        if description:
            kernel32.LocalFree(description)
        del input_buffer


class SecretRedactor:
    def __init__(self) -> None:
        self._secrets: set[str] = set()

    def register(self, value: str | None) -> None:
        if value and len(value) >= 3:
            self._secrets.add(value)

    def redact(self, message: str) -> str:
        result = str(message)
        for secret in sorted(self._secrets, key=len, reverse=True):
            result = result.replace(secret, "***")
        result = re.sub(
            r"(?i)\b(password|passwd|token|api[_-]?key|secret)(\s*[:=]\s*)([^\s,;]+)",
            lambda match: f"{match.group(1)}{match.group(2)}***",
            result,
        )
        result = re.sub(r"(?i)\b(authorization\s*:\s*bearer\s+)[^\s]+", r"\1***", result)
        return result
