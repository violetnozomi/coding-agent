"""Typed configuration provenance and user-owned workspace trust records.

Workspace files are parsed as data and never merged into ``os.environ``.  A
security-sensitive workspace value is accepted only when a user-owned trust
record matches the exact workspace identity and current value fingerprint.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import tempfile
import threading
from typing import Mapping

from dotenv import dotenv_values

from nz_coder.foundation.private_paths import harden_private_path


_SCHEMA_VERSION = 1
_MAX_CONFIG_BYTES = 1024 * 1024
_STORE_LOCK = threading.RLock()

DEFAULT_CONFIG_VALUES: dict[str, str] = {
    "API_KEY": "",
    "OPENAI_API_KEY": "",
    "ANTHROPIC_API_KEY": "",
    "GEMINI_API_KEY": "",
    "MODEL_PROVIDER": "openai-compatible",
    "MODEL_ID": "deepseek-v4-flash",
    "MODEL_VARIANT": "",
    "API_BASE_URL": "https://api.deepseek.com",
    "OPENAI_API_BASE_URL": "https://api.openai.com/v1",
    "ANTHROPIC_API_BASE_URL": "https://api.anthropic.com",
    "GEMINI_API_BASE_URL": "https://generativelanguage.googleapis.com/v1beta",
    "PERMISSION_MODE": "default",
    "NZ_MCP_ENABLED": "0",
    "NZ_MCP_SERVERS_JSON": "",
    "NZ_LSP_PYTHON_COMMAND": "",
    "LOG_LEVEL": "INFO",
}

_SENSITIVE_EXACT = frozenset({
    "API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "DEEPSEEK_API_KEY",
    "NZ_IMAGE_DESCRIBE_API_KEY",
    "API_BASE_URL",
    "OPENAI_API_BASE_URL",
    "ANTHROPIC_API_BASE_URL",
    "GEMINI_API_BASE_URL",
    "NZ_IMAGE_DESCRIBE_BASE_URL",
    "MODEL_PROVIDER",
    "PERMISSION_MODE",
    "NZ_MCP_ENABLED",
    "NZ_MCP_SERVERS_JSON",
    "NZ_MCP_USER_CONFIG",
    "NZ_MCP_PROJECT_CONFIG",
    "NZ_MCP_TRUST_STORE",
    "NZ_MCP_AUTH_STORE",
    "ALLOW_BASH_PACKAGE_INSTALLS",
    "NZ_CONTINUE_LOOP_ON_DENY",
})
_SECRET_MARKERS = ("API_KEY", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL", "COOKIE")
_EXECUTION_MARKERS = (
    "AUTO_EXEC",
    "AUTO_RUN",
    "ALLOW_NETWORK",
    "ALLOW_SHELL",
    "DISABLE_SECURITY",
    "SKIP_PERMISSION",
)


class ConfigSource(str, Enum):
    """Origin of one effective configuration value."""

    DEFAULT = "default"
    ENVIRONMENT = "environment"
    USER = "user"
    WORKSPACE = "workspace"
    TRUSTED_WORKSPACE = "trusted-workspace"


@dataclass(frozen=True)
class ConfigValue:
    """One effective value and the security metadata used to select it."""

    key: str
    value: str
    source: ConfigSource
    ignored: bool = False
    requires_trust: bool = False
    used_default: bool = False
    secret: bool = False

    def public(self) -> dict[str, object]:
        return {
            "value": "<configured>" if self.secret and bool(self.value) else (
                "<not-configured>" if self.secret else self.value
            ),
            "source": self.source.value,
            "ignored": self.ignored,
            "requires_trust": self.requires_trust,
            "used_default": self.used_default,
        }


@dataclass(frozen=True)
class ConfigIssue:
    """A bounded validation issue that is safe for operator-facing output."""

    key: str
    message: str
    source: ConfigSource


class ConfigValidationError(ValueError):
    """Raised when a configuration or trust document is structurally unsafe."""


@dataclass
class ConfigSnapshot:
    """Effective configuration values with provenance and accumulated issues."""

    workspace: Path
    workspace_fingerprint: str
    workspace_trusted: bool
    values: dict[str, ConfigValue]
    issues: list[ConfigIssue] = field(default_factory=list)

    def get(self, key: str, default: str | None = None) -> str:
        record = self.values.get(key)
        if record is not None:
            return record.value
        fallback = "" if default is None else str(default)
        record = ConfigValue(
            key,
            fallback,
            ConfigSource.DEFAULT,
            used_default=True,
            secret=is_secret_config_key(key),
        )
        self.values[key] = record
        return fallback

    def value(self, key: str) -> ConfigValue:
        self.get(key, DEFAULT_CONFIG_VALUES.get(key, ""))
        return self.values[key]

    def get_int(
        self,
        key: str,
        default: int,
        *,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int:
        raw = self.get(key, str(default))
        try:
            value = int(raw)
            if minimum is not None and value < minimum:
                raise ValueError(f"must be >= {minimum}")
            if maximum is not None and value > maximum:
                raise ValueError(f"must be <= {maximum}")
            return value
        except (TypeError, ValueError) as exc:
            self._record_issue(key, f"invalid integer; using default ({exc})")
            return default

    def get_float(
        self,
        key: str,
        default: float,
        *,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> float:
        raw = self.get(key, str(default))
        try:
            value = float(raw)
            if not math.isfinite(value):
                raise ValueError("must be finite")
            if minimum is not None and value < minimum:
                raise ValueError(f"must be >= {minimum}")
            if maximum is not None and value > maximum:
                raise ValueError(f"must be <= {maximum}")
            return value
        except (TypeError, ValueError) as exc:
            self._record_issue(key, f"invalid number; using default ({exc})")
            return default

    def get_bool(self, key: str, default: bool) -> bool:
        raw = self.get(key, "1" if default else "0").strip().lower()
        if raw in {"1", "true", "yes", "on"}:
            return True
        if raw in {"0", "false", "no", "off"}:
            return False
        self._record_issue(key, "invalid boolean; using default")
        return default

    def public(self) -> dict[str, dict[str, object]]:
        return {key: value.public() for key, value in sorted(self.values.items())}

    def public_json(self) -> str:
        return json.dumps(self.public(), ensure_ascii=False, sort_keys=True)

    def _record_issue(self, key: str, message: str) -> None:
        if any(item.key == key and item.message == message for item in self.issues):
            return
        self.issues.append(ConfigIssue(key, message[:300], self.value(key).source))


def default_user_config_path(environ: Mapping[str, str] | None = None) -> Path:
    """Return the user-owned provider/configuration file outside workspaces."""
    environment = os.environ if environ is None else environ
    configured = str(environment.get("NZ_CODER_USER_CONFIG", "")).strip()
    if configured:
        return Path(configured).expanduser().absolute()
    if os.name == "nt" and environment.get("APPDATA"):
        return Path(environment["APPDATA"]) / "nz-coder" / "config.env"
    base = Path(environment.get("XDG_CONFIG_HOME", "")).expanduser() if environment.get("XDG_CONFIG_HOME") else Path.home() / ".config"
    return base / "nz-coder" / "config.env"


def default_trust_store_path(environ: Mapping[str, str] | None = None) -> Path:
    environment = os.environ if environ is None else environ
    configured = str(environment.get("NZ_CODER_WORKSPACE_TRUST_STORE", "")).strip()
    if configured:
        return Path(configured).expanduser().absolute()
    return default_user_config_path(environment).with_name("workspace-trust.json")


def is_secret_config_key(key: str) -> bool:
    upper = str(key).upper()
    return any(marker in upper for marker in _SECRET_MARKERS)


def is_sensitive_config_key(key: str) -> bool:
    upper = str(key).upper()
    if upper in _SENSITIVE_EXACT or is_secret_config_key(upper):
        return True
    if upper.startswith("NZ_LSP_") and upper.endswith("_COMMAND"):
        return True
    return any(marker in upper for marker in _EXECUTION_MARKERS)


def workspace_config_fingerprint(values: Mapping[str, str]) -> str:
    """Fingerprint only security-relevant workspace assignments."""
    sensitive = {
        str(key): str(value)
        for key, value in values.items()
        if is_sensitive_config_key(str(key))
    }
    payload = json.dumps(sensitive, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class WorkspaceTrustStore:
    """Private, schema-versioned trust records bound to exact workspaces."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path or default_trust_store_path()).expanduser().absolute()

    def is_trusted(
        self,
        workspace: Path,
        config_type: str,
        fingerprint: str,
        *,
        executable: str = "",
    ) -> bool:
        if not fingerprint:
            return False
        identity = _workspace_identity(workspace)
        key = _trust_key(identity, config_type, executable)
        record = self._read().get(key)
        return bool(
            isinstance(record, dict)
            and record.get("workspace") == identity
            and record.get("config_type") == config_type
            and record.get("fingerprint") == fingerprint
            and record.get("executable") == executable
        )

    def trust(
        self,
        workspace: Path,
        config_type: str,
        fingerprint: str,
        *,
        executable: str = "",
    ) -> None:
        if not fingerprint:
            raise ValueError("Trust fingerprint cannot be empty")
        _ensure_store_outside_workspace(self.path, workspace)
        identity = _workspace_identity(workspace)
        key = _trust_key(identity, config_type, executable)
        with _STORE_LOCK:
            entries = self._read_unlocked()
            entries[key] = {
                "workspace": identity,
                "config_type": str(config_type),
                "fingerprint": str(fingerprint),
                "executable": str(executable),
            }
            self._write_unlocked(entries)

    def remove(self, workspace: Path, config_type: str, *, executable: str = "") -> bool:
        identity = _workspace_identity(workspace)
        key = _trust_key(identity, config_type, executable)
        with _STORE_LOCK:
            entries = self._read_unlocked()
            removed = entries.pop(key, None) is not None
            if removed:
                self._write_unlocked(entries)
            return removed

    def _read(self) -> dict[str, dict[str, object]]:
        with _STORE_LOCK:
            return self._read_unlocked()

    def _read_unlocked(self) -> dict[str, dict[str, object]]:
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return {}
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ConfigValidationError("Workspace trust store must be a regular file")
        if info.st_size > _MAX_CONFIG_BYTES:
            raise ConfigValidationError("Workspace trust store exceeds 1 MiB")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ConfigValidationError("Workspace trust store is invalid") from exc
        if not isinstance(payload, dict) or payload.get("version") != _SCHEMA_VERSION:
            raise ConfigValidationError("Workspace trust store schema is invalid")
        entries = payload.get("entries")
        if not isinstance(entries, dict):
            raise ConfigValidationError("Workspace trust store entries are invalid")
        return {str(key): value for key, value in entries.items() if isinstance(value, dict)}

    def _write_unlocked(self, entries: Mapping[str, object]) -> None:
        parent = self.path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        harden_private_path(parent)
        descriptor, name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=parent)
        temporary = Path(name)
        try:
            payload = json.dumps(
                {"version": _SCHEMA_VERSION, "entries": dict(sorted(entries.items()))},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, self.path)
            harden_private_path(self.path)
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass
            temporary.unlink(missing_ok=True)


def load_config_snapshot(
    workspace: Path,
    *,
    environ: Mapping[str, str] | None = None,
    user_config_path: Path | None = None,
    trust_store: WorkspaceTrustStore | None = None,
) -> ConfigSnapshot:
    """Load shell, user, and workspace configuration without side effects."""
    root = Path(workspace).expanduser().absolute()
    environment = dict(os.environ if environ is None else environ)
    user_path = Path(user_config_path or default_user_config_path(environment)).expanduser().absolute()
    store = trust_store or WorkspaceTrustStore(default_trust_store_path(environment))
    _ensure_store_outside_workspace(store.path, root)
    user_values, user_issues = _read_env_file(user_path, ConfigSource.USER)
    workspace_values, workspace_issues = _read_env_file(root / ".env", ConfigSource.WORKSPACE)
    fingerprint = workspace_config_fingerprint(workspace_values)
    try:
        trusted = store.is_trusted(root, "workspace-config", fingerprint)
        trust_issue: list[ConfigIssue] = []
    except ConfigValidationError:
        trusted = False
        trust_issue = [ConfigIssue("workspace-trust", "invalid trust store; workspace values ignored", ConfigSource.USER)]
    keys = set(DEFAULT_CONFIG_VALUES) | set(user_values) | set(workspace_values) | set(environment)
    values: dict[str, ConfigValue] = {}
    for key in keys:
        default = DEFAULT_CONFIG_VALUES.get(key, "")
        sensitive = is_sensitive_config_key(key)
        secret = is_secret_config_key(key)
        if key in environment:
            values[key] = ConfigValue(key, str(environment[key]), ConfigSource.ENVIRONMENT, requires_trust=False, secret=secret)
            continue
        if key in user_values:
            values[key] = ConfigValue(key, user_values[key], ConfigSource.USER, requires_trust=False, secret=secret)
            continue
        if key in workspace_values:
            if sensitive and not trusted:
                values[key] = ConfigValue(
                    key,
                    default,
                    ConfigSource.DEFAULT,
                    ignored=True,
                    requires_trust=True,
                    used_default=True,
                    secret=secret,
                )
            else:
                values[key] = ConfigValue(
                    key,
                    workspace_values[key],
                    ConfigSource.TRUSTED_WORKSPACE if sensitive else ConfigSource.WORKSPACE,
                    requires_trust=sensitive,
                    secret=secret,
                )
            continue
        values[key] = ConfigValue(key, default, ConfigSource.DEFAULT, used_default=True, secret=secret)
    snapshot = ConfigSnapshot(
        workspace=root,
        workspace_fingerprint=fingerprint,
        workspace_trusted=trusted,
        values=values,
        issues=[*user_issues, *workspace_issues, *trust_issue],
    )
    # These historically crashed every CLI surface during module import.  Keep
    # validation here as well as in config.py so ``doctor --workspace`` can
    # diagnose a workspace other than the process startup directory.
    snapshot.get_int("MAX_AGENT_TURNS", 500, minimum=1)
    snapshot.get_int("BASH_TIMEOUT_SECONDS", 120, minimum=1)
    snapshot.get_int("NZ_PROCESS_BUFFER_BYTES", 2 * 1024 * 1024, minimum=1)
    snapshot.get_float("NZ_PROVIDER_HARD_TIMEOUT_SECONDS", 600.0, minimum=1.0)
    return snapshot


def _read_env_file(path: Path, source: ConfigSource) -> tuple[dict[str, str], list[ConfigIssue]]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {}, []
    except OSError:
        return {}, [ConfigIssue(path.name, "configuration file cannot be inspected", source)]
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return {}, [ConfigIssue(path.name, "configuration file must be a regular non-symlink file", source)]
    if info.st_size > _MAX_CONFIG_BYTES:
        return {}, [ConfigIssue(path.name, "configuration file exceeds 1 MiB", source)]
    try:
        parsed = dotenv_values(path, interpolate=False)
    except (OSError, UnicodeError, ValueError):
        return {}, [ConfigIssue(path.name, "configuration file cannot be parsed", source)]
    return {str(key): "" if value is None else str(value) for key, value in parsed.items()}, []


def _workspace_identity(workspace: Path) -> dict[str, object]:
    lexical = Path(workspace).expanduser().absolute()
    normalized_lexical = os.path.normcase(os.path.normpath(str(lexical)))
    try:
        resolved = lexical.resolve(strict=True)
        info = resolved.stat()
    except OSError as exc:
        raise ConfigValidationError("Workspace identity cannot be verified") from exc
    return {
        "lexical": normalized_lexical,
        "resolved": os.path.normcase(os.path.normpath(str(resolved))),
        "device": int(info.st_dev),
        "inode": int(info.st_ino),
    }


def _trust_key(identity: Mapping[str, object], config_type: str, executable: str) -> str:
    payload = json.dumps(
        {"workspace": dict(identity), "config_type": str(config_type), "executable": str(executable)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _ensure_store_outside_workspace(path: Path, workspace: Path) -> None:
    store = path.expanduser().absolute()
    root = workspace.expanduser().absolute().resolve(strict=False)
    try:
        store.resolve(strict=False).relative_to(root)
    except ValueError:
        return
    raise ConfigValidationError("Workspace trust store must be outside the workspace")


__all__ = [
    "ConfigIssue",
    "ConfigSnapshot",
    "ConfigSource",
    "ConfigValidationError",
    "ConfigValue",
    "WorkspaceTrustStore",
    "default_trust_store_path",
    "default_user_config_path",
    "is_secret_config_key",
    "is_sensitive_config_key",
    "load_config_snapshot",
    "workspace_config_fingerprint",
]
