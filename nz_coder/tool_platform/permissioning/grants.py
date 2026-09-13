"""User-private, workspace-scoped persistent permission grants."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile

from nz_coder.foundation.file_lock import exclusive_file_lock
from nz_coder.foundation.private_paths import harden_private_path
from nz_coder.foundation.workspace_trust import (
    default_user_config_path,
    workspace_identity,
    workspace_identity_key,
)


_VERSION = 1
_MAX_STORE_BYTES = 1024 * 1024
_MAX_WORKSPACES = 4096
_MAX_RULES_PER_WORKSPACE = 256
_MAX_RULE_BYTES = 4096


def default_user_grant_store_path() -> Path:
    """Return the platform-aware private grant store outside repositories."""
    return default_user_config_path().with_name("workspace-grants.json")


class UserGrantStore:
    """Atomically load and update exact scoped allow rules for one workspace."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path or default_user_grant_store_path()).expanduser().absolute()

    def load(self, workspace: Path | str) -> list[str]:
        self._validate_location(workspace)
        with exclusive_file_lock(self.path.with_suffix(self.path.suffix + ".lock")):
            payload = self._read()
        record = payload["workspaces"].get(workspace_identity_key(workspace), {})
        if not isinstance(record, dict) or record.get("workspace") != workspace_identity(workspace):
            return []
        rules = record.get("allow", [])
        if not isinstance(rules, list) or any(not isinstance(item, str) for item in rules):
            raise ValueError("Invalid workspace grant store")
        return list(rules)

    def add(self, workspace: Path | str, serialized_rule: str) -> None:
        rule = str(serialized_rule)
        if not rule or len(rule.encode("utf-8")) > _MAX_RULE_BYTES:
            raise ValueError("Permission grant is invalid")
        self._validate_location(workspace)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with exclusive_file_lock(lock_path):
            payload = self._read()
            workspaces = payload["workspaces"]
            key = workspace_identity_key(workspace)
            record = workspaces.setdefault(key, {
                "workspace": workspace_identity(workspace),
                "allow": [],
            })
            if not isinstance(record, dict) or record.get("workspace") != workspace_identity(workspace):
                raise ValueError("Invalid workspace grant store")
            allow = record.get("allow")
            if not isinstance(allow, list) or any(not isinstance(item, str) for item in allow):
                raise ValueError("Invalid workspace grant store")
            if rule not in allow:
                if len(allow) >= _MAX_RULES_PER_WORKSPACE:
                    raise ValueError("Workspace grant rule limit exceeded")
                allow.append(rule)
            if len(workspaces) > _MAX_WORKSPACES:
                raise ValueError("Workspace grant store limit exceeded")
            self._write(payload)

    def load_disabled_skills(self, workspace: Path | str) -> set[str]:
        """Load user-owned disabled Skill names for exactly one workspace."""
        self._validate_location(workspace)
        with exclusive_file_lock(self.path.with_suffix(self.path.suffix + ".lock")):
            payload = self._read()
        record = payload["workspaces"].get(workspace_identity_key(workspace), {})
        if not isinstance(record, dict) or record.get("workspace") != workspace_identity(workspace):
            return set()
        values = record.get("disabled_skills", [])
        if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
            raise ValueError("Invalid workspace grant store")
        return {item for item in values if item}

    def set_skill_enabled(
        self,
        workspace: Path | str,
        name: str,
        enabled: bool,
    ) -> None:
        """Persist a user preference without mutating project authority."""
        selected = str(name).strip()
        if not selected or len(selected.encode("utf-8")) > _MAX_RULE_BYTES:
            raise ValueError("Skill preference is invalid")
        self._validate_location(workspace)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with exclusive_file_lock(lock_path):
            payload = self._read()
            workspaces = payload["workspaces"]
            key = workspace_identity_key(workspace)
            record = workspaces.setdefault(key, {
                "workspace": workspace_identity(workspace),
                "allow": [],
            })
            if not isinstance(record, dict) or record.get("workspace") != workspace_identity(workspace):
                raise ValueError("Invalid workspace grant store")
            values = record.setdefault("disabled_skills", [])
            if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
                raise ValueError("Invalid workspace grant store")
            disabled = set(values)
            if enabled:
                disabled.discard(selected)
            else:
                if len(disabled) >= _MAX_RULES_PER_WORKSPACE:
                    raise ValueError("Workspace Skill preference limit exceeded")
                disabled.add(selected)
            record["disabled_skills"] = sorted(disabled)
            if len(workspaces) > _MAX_WORKSPACES:
                raise ValueError("Workspace grant store limit exceeded")
            self._write(payload)

    def _validate_location(self, workspace: Path | str) -> None:
        root = Path(workspace).expanduser().resolve(strict=True)
        resolved = self.path.resolve(strict=False)
        try:
            resolved.relative_to(root)
        except ValueError:
            pass
        else:
            raise ValueError("Workspace grant store must remain outside the workspace")
        ancestors: list[Path] = []
        cursor = self.path.parent
        while True:
            ancestors.append(cursor)
            if cursor == cursor.parent:
                break
            cursor = cursor.parent
        for candidate in (*reversed(ancestors), self.path):
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                continue
            attributes = int(getattr(info, "st_file_attributes", 0))
            reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
            if stat.S_ISLNK(info.st_mode) or attributes & reparse:
                raise ValueError("Workspace grant store path is unsafe")
            if candidate == self.path and not stat.S_ISREG(info.st_mode):
                raise ValueError("Workspace grant store must be a regular file")
            if candidate != self.path and not stat.S_ISDIR(info.st_mode):
                raise ValueError("Workspace grant store directory is unsafe")

    def _read(self) -> dict:
        try:
            descriptor = os.open(
                self.path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
        except FileNotFoundError:
            return {"version": _VERSION, "workspaces": {}}
        try:
            opened = os.fstat(descriptor)
            current = self.path.lstat()
            attributes = int(getattr(current, "st_file_attributes", 0))
            reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
            if (
                not stat.S_ISREG(opened.st_mode)
                or stat.S_ISLNK(current.st_mode)
                or attributes & reparse
                or (int(opened.st_dev), int(opened.st_ino))
                != (int(current.st_dev), int(current.st_ino))
            ):
                raise ValueError("Workspace grant store path is unsafe")
            if opened.st_size > _MAX_STORE_BYTES:
                raise ValueError("Workspace grant store exceeds size limit")
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                descriptor = -1
                payload = json.load(stream)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid workspace grant store") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if (
            not isinstance(payload, dict)
            or payload.get("version") != _VERSION
            or not isinstance(payload.get("workspaces"), dict)
            or len(payload["workspaces"]) > _MAX_WORKSPACES
        ):
            raise ValueError("Invalid workspace grant store")
        return payload

    def _write(self, payload: dict) -> None:
        encoded = (json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ) + "\n").encode("utf-8")
        if len(encoded) > _MAX_STORE_BYTES:
            raise ValueError("Workspace grant store exceeds size limit")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        result = harden_private_path(self.path.parent)
        if not result.hardened:
            raise PermissionError(result.detail)
        descriptor, name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, self.path)
            harden_private_path(self.path)
            if os.name != "nt":
                directory = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)


__all__ = ["UserGrantStore", "default_user_grant_store_path"]
