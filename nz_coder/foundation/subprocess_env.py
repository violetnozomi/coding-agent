"""Fail-closed, credential-safe environment construction for child processes."""
from __future__ import annotations

from collections.abc import Mapping
import os
import re


class UnsafeSubprocessEnvironment(ValueError):
    """A requested child environment assignment may carry credentials."""


_SAFE_EXACT = frozenset({
    "ALLUSERSPROFILE",
    "APPDATA",
    "CARGO_HOME",
    "COLORTERM",
    "COMSPEC",
    "CONDA_DEFAULT_ENV",
    "CONDA_PREFIX",
    "DOTNET_ROOT",
    "GEM_HOME",
    "GEM_PATH",
    "GOENV",
    "GOMODCACHE",
    "GOPATH",
    "GOROOT",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "JAVA_HOME",
    "LANG",
    "LOCALAPPDATA",
    "LOGNAME",
    "NODE_PATH",
    "NUMBER_OF_PROCESSORS",
    "OS",
    "PATH",
    "PATHEXT",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PROGRAMW6432",
    "PROMPT",
    "PSMODULEPATH",
    "PYTHONHOME",
    "PYTHONPATH",
    "RUSTUP_HOME",
    "SHELL",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TERM",
    "TMP",
    "TMPDIR",
    "USER",
    "USERDOMAIN",
    "USERNAME",
    "USERPROFILE",
    "VIRTUAL_ENV",
    "WINDIR",
})
_SAFE_PREFIXES = ("LC_",)
_SECRET_MARKERS = (
    "API_KEY",
    "APIKEY",
    "AUTHORIZATION",
    "BEARER",
    "COOKIE",
    "CREDENTIAL",
    "OAUTH",
    "PASSWORD",
    "PRIVATE_KEY",
    "SECRET",
    "SESSION_TOKEN",
    "TOKEN",
)
_SECRET_EXACT = frozenset({
    "AWS_ACCESS_KEY_ID",
    "AWS_PROFILE",
    "AWS_SHARED_CREDENTIALS_FILE",
    "AZURE_CLIENT_ID",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "SSH_AGENT_PID",
    "SSH_ASKPASS",
    "SSH_AUTH_SOCK",
})
_VALID_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_()]*$")


def is_credential_environment_name(name: str) -> bool:
    """Return whether an environment name is likely to carry authority."""
    upper = str(name or "").strip().upper()
    if not upper or upper in _SECRET_EXACT:
        return bool(upper in _SECRET_EXACT)
    if any(marker in upper for marker in _SECRET_MARKERS):
        return True
    return (
        upper.startswith(("AWS_", "AZURE_", "GOOGLE_", "GCP_", "GITHUB_", "GITLAB_"))
        and upper not in {"GITHUB_ACTIONS"}
    ) or upper.startswith(("NPM_", "PIP_INDEX_", "TWINE_"))


def build_sanitized_subprocess_env(
    *,
    source: Mapping[str, str] | None = None,
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the minimum useful child environment without ambient authority.

    Explicit overrides support low-risk process configuration such as
    ``MCP_MODE``. Credential-like names fail closed; callers must use a
    dedicated user-owned credential channel instead of ambient inheritance.
    """
    inherited = os.environ if source is None else source
    result: dict[str, str] = {}
    for raw_name, raw_value in inherited.items():
        name = str(raw_name)
        upper = name.upper()
        if is_credential_environment_name(upper):
            continue
        if upper in _SAFE_EXACT or upper.startswith(_SAFE_PREFIXES):
            result[name] = str(raw_value)
    for raw_name, raw_value in (overrides or {}).items():
        name = str(raw_name)
        if not _VALID_NAME.fullmatch(name):
            raise UnsafeSubprocessEnvironment("Child environment name is invalid")
        if is_credential_environment_name(name):
            raise UnsafeSubprocessEnvironment(
                f"Credential-like child environment variable is blocked: {name}"
            )
        value = str(raw_value)
        if "\x00" in value:
            raise UnsafeSubprocessEnvironment(
                f"Child environment variable contains a null byte: {name}"
            )
        result[name] = value
    return result


__all__ = [
    "UnsafeSubprocessEnvironment",
    "build_sanitized_subprocess_env",
    "is_credential_environment_name",
]
