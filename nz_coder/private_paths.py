"""Cross-platform owner-private filesystem hardening infrastructure.

POSIX mode bits and Windows DACLs are different security mechanisms.  This
module gives persistence boundaries one small contract without making the rest
of the product import Windows-only modules.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any


@dataclass(frozen=True)
class PrivatePathSecurity:
    """Observed security strength for one local path."""

    path: str
    hardened: bool
    tier: str
    detail: str


def harden_private_path(
    path: str | os.PathLike[str],
    *,
    os_name: str | None = None,
    windows_api: Any | None = None,
) -> PrivatePathSecurity:
    """Apply owner-private permissions and report the verified result.

    Windows failures intentionally return Tier B instead of treating ``chmod``
    as a DACL. Callers with an authenticated fallback can remain available
    while Doctor reports the weaker host state.
    """
    target = Path(path)
    if not target.exists() or target.is_symlink():
        return _result(target, False, "B", "path is missing or is a symbolic link")
    selected_os = os.name if os_name is None else os_name
    if selected_os != "nt":
        try:
            target.chmod(0o700 if target.is_dir() else 0o600)
        except OSError as exc:
            return _result(target, False, "B", _bounded_error(exc))
        return _result(target, True, "A", "owner-only POSIX mode applied")

    try:
        api = windows_api or _WindowsPrivateACL()
        if not api.is_available():
            return _result(target, False, "B", "Windows DACL APIs are unavailable")
        api.apply(target, is_directory=target.is_dir())
        if not api.inspect(target):
            return _result(target, False, "B", "Windows DACL verification failed")
    except (AttributeError, OSError, RuntimeError, ValueError) as exc:
        return _result(target, False, "B", _bounded_error(exc))
    return _result(
        target,
        True,
        "A",
        "protected Windows DACL grants only current user and Local System",
    )


def inspect_private_path(
    path: str | os.PathLike[str],
    *,
    os_name: str | None = None,
    windows_api: Any | None = None,
) -> PrivatePathSecurity:
    """Inspect the platform security contract without changing the path."""
    target = Path(path)
    if not target.exists() or target.is_symlink():
        return _result(target, False, "B", "path is missing or is a symbolic link")
    selected_os = os.name if os_name is None else os_name
    if selected_os != "nt":
        try:
            mode = target.stat().st_mode & 0o777
        except OSError as exc:
            return _result(target, False, "B", _bounded_error(exc))
        expected = 0o700 if target.is_dir() else 0o600
        private = mode & 0o077 == 0
        detail = (
            f"owner-only POSIX mode {mode:04o}"
            if private
            else f"POSIX mode {mode:04o} permits group or other access; expected {expected:04o}"
        )
        return _result(target, private, "A" if private else "B", detail)
    try:
        api = windows_api or _WindowsPrivateACL()
        if not api.is_available():
            return _result(target, False, "B", "Windows DACL APIs are unavailable")
        private = bool(api.inspect(target))
    except (AttributeError, OSError, RuntimeError, ValueError) as exc:
        return _result(target, False, "B", _bounded_error(exc))
    return _result(
        target,
        private,
        "A" if private else "B",
        "protected current-user-and-SYSTEM DACL"
        if private
        else "path does not have the protected current-user-and-SYSTEM DACL",
    )


def windows_private_acl_available(windows_api: Any | None = None) -> bool:
    """Return whether the required Windows security APIs can be loaded."""
    try:
        return bool((windows_api or _WindowsPrivateACL()).is_available())
    except (AttributeError, OSError, RuntimeError, ValueError):
        return False


def _result(
    path: Path,
    hardened: bool,
    tier: str,
    detail: str,
) -> PrivatePathSecurity:
    return PrivatePathSecurity(str(path), hardened, tier, str(detail)[:300])


def _bounded_error(error: BaseException) -> str:
    value = str(error).strip() or error.__class__.__name__
    return value[:240]


class _WindowsPrivateACL:
    """Lazy ctypes adapter for protected Windows file DACLs."""

    _DACL_SECURITY_INFORMATION = 0x00000004
    _PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
    _SE_FILE_OBJECT = 1
    _SE_DACL_PROTECTED = 0x1000

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.wintypes = wintypes
        self.advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure()

    def is_available(self) -> bool:
        required = (
            "OpenProcessToken",
            "GetTokenInformation",
            "ConvertSidToStringSidW",
            "ConvertStringSecurityDescriptorToSecurityDescriptorW",
            "GetSecurityDescriptorDacl",
            "SetNamedSecurityInfoW",
            "GetNamedSecurityInfoW",
            "GetSecurityDescriptorControl",
            "ConvertSecurityDescriptorToStringSecurityDescriptorW",
        )
        return all(hasattr(self.advapi32, name) for name in required)

    def apply(self, path: Path, *, is_directory: bool) -> None:
        ctypes = self.ctypes
        flags = "OICI" if is_directory else ""
        current_sid = self._current_user_sid()
        sddl = (
            f"D:P(A;{flags};FA;;;SY)"
            f"(A;{flags};FA;;;{current_sid})"
        )
        descriptor = ctypes.c_void_p()
        if not self.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl,
            1,
            ctypes.byref(descriptor),
            None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            present = self.wintypes.BOOL()
            defaulted = self.wintypes.BOOL()
            dacl = ctypes.c_void_p()
            if not self.advapi32.GetSecurityDescriptorDacl(
                descriptor,
                ctypes.byref(present),
                ctypes.byref(dacl),
                ctypes.byref(defaulted),
            ) or not present.value:
                raise ctypes.WinError(ctypes.get_last_error())
            mutable_path = ctypes.create_unicode_buffer(str(path))
            status = self.advapi32.SetNamedSecurityInfoW(
                mutable_path,
                self._SE_FILE_OBJECT,
                self._DACL_SECURITY_INFORMATION
                | self._PROTECTED_DACL_SECURITY_INFORMATION,
                None,
                None,
                dacl,
                None,
            )
            if status:
                raise ctypes.WinError(int(status))
        finally:
            self.kernel32.LocalFree(descriptor)

    def inspect(self, path: Path) -> bool:
        ctypes = self.ctypes
        descriptor = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        mutable_path = ctypes.create_unicode_buffer(str(path))
        status = self.advapi32.GetNamedSecurityInfoW(
            mutable_path,
            self._SE_FILE_OBJECT,
            self._DACL_SECURITY_INFORMATION,
            None,
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),
        )
        if status:
            raise ctypes.WinError(int(status))
        try:
            control = self.wintypes.WORD()
            revision = self.wintypes.DWORD()
            if not self.advapi32.GetSecurityDescriptorControl(
                descriptor,
                ctypes.byref(control),
                ctypes.byref(revision),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if not control.value & self._SE_DACL_PROTECTED:
                return False
            rendered = self.wintypes.LPWSTR()
            if not self.advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                descriptor,
                1,
                self._DACL_SECURITY_INFORMATION,
                ctypes.byref(rendered),
                None,
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                sddl = str(rendered.value or "")
            finally:
                self.kernel32.LocalFree(ctypes.cast(rendered, ctypes.c_void_p))
        finally:
            self.kernel32.LocalFree(descriptor)
        current = self._current_user_sid().upper()
        return _private_dacl_sddl(sddl, current)

    def _current_user_sid(self) -> str:
        ctypes = self.ctypes
        wintypes = self.wintypes

        class _SidAndAttributes(ctypes.Structure):
            _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

        class _TokenUser(ctypes.Structure):
            _fields_ = [("User", _SidAndAttributes)]

        token = wintypes.HANDLE()
        if not self.advapi32.OpenProcessToken(
            self.kernel32.GetCurrentProcess(),
            0x0008,
            ctypes.byref(token),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            size = wintypes.DWORD()
            self.advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
            if not size.value:
                raise ctypes.WinError(ctypes.get_last_error())
            buffer = ctypes.create_string_buffer(size.value)
            if not self.advapi32.GetTokenInformation(
                token,
                1,
                buffer,
                size.value,
                ctypes.byref(size),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            user = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents
            rendered = wintypes.LPWSTR()
            if not self.advapi32.ConvertSidToStringSidW(
                user.User.Sid,
                ctypes.byref(rendered),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                return str(rendered.value)
            finally:
                self.kernel32.LocalFree(ctypes.cast(rendered, ctypes.c_void_p))
        finally:
            self.kernel32.CloseHandle(token)

    def _configure(self) -> None:
        ctypes = self.ctypes
        wintypes = self.wintypes
        advapi32 = self.advapi32
        kernel32 = self.kernel32

        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.ConvertSidToStringSidW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.LPWSTR),
        ]
        advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
        advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
        advapi32.GetSecurityDescriptorDacl.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.BOOL),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.BOOL),
        ]
        advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
        advapi32.SetNamedSecurityInfoW.argtypes = [
            wintypes.LPWSTR,
            ctypes.c_int,
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
        advapi32.GetNamedSecurityInfoW.argtypes = [
            wintypes.LPWSTR,
            ctypes.c_int,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
        advapi32.GetSecurityDescriptorControl.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.WORD),
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
        advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL


def _private_dacl_sddl(sddl: str, current_sid: str) -> bool:
    """Validate the narrow DACL shape emitted by this module."""
    normalized = str(sddl or "").upper()
    current = str(current_sid or "").upper()
    if not current or not normalized.startswith("D:P"):
        return False
    entries: list[tuple[str, str, str]] = []
    for raw in re.findall(r"\(([^)]*)\)", normalized):
        fields = raw.split(";")
        if len(fields) < 6:
            return False
        entries.append((fields[0], fields[2], fields[5]))
    if not entries:
        return False
    system = {"SY", "S-1-5-18"}
    allowed = {current} | system
    if any(trustee not in allowed for _kind, _rights, trustee in entries):
        return False
    full_allow = {
        trustee
        for kind, rights, trustee in entries
        if kind == "A" and rights in {"FA", "GA"}
    }
    return current in full_allow and bool(full_allow & system)


__all__ = [
    "PrivatePathSecurity",
    "harden_private_path",
    "inspect_private_path",
    "windows_private_acl_available",
]
