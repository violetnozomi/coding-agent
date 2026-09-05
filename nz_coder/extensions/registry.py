"""Projection registry for skills, hooks, optional packs, and MCP servers."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from nz_coder.foundation.project_control import ProjectControlSnapshot
from nz_coder.foundation.workspace_trust import current_config_snapshot
from nz_coder.mcp.config import load_mcp_server_configs
from nz_coder.runtime.verification.hooks import load_configured_hooks_from_settings
from nz_coder.runtime.process.workdir import current_workdir
from nz_coder.state.skills import SkillLoader, current_skill_loader
from nz_coder.tools import list_optional_packs

_KINDS = frozenset({"skill", "hook", "tool_pack", "mcp_server", "error"})
_SCOPES = frozenset({"global", "user", "workspace", "process", "session"})
_LIFECYCLES = frozenset({"static", "lazy", "reloadable", "live"})
_EFFECTS = frozenset({"read", "serial", "write"})
_CORE_HOOK_CAPABILITIES = (
    "no_tool_response:verification_gate",
    "no_tool_response:reflection_gate",
    "post_tool_use:failure_diagnostic",
    "post_tool_batch:writes_denied",
    "post_tool_batch:todo_reminder",
    "post_tool_batch:manual_compact",
)


@dataclass(frozen=True)
class ExtensionDescriptor:
    """Secret-free immutable metadata for one extension boundary."""

    extension_id: str
    kind: str
    name: str
    source: str
    scope: str
    status: str
    lifecycle: str
    trusted: bool
    enabled: bool = True
    health: str = "healthy"
    description: str = ""
    capabilities: tuple[str, ...] = ()
    effects: tuple[tuple[str, str], ...] = ()
    permissions: tuple[str, ...] = ()
    error: str = ""
    contract_version: int = 1

    def __post_init__(self) -> None:
        if not self.extension_id or ":" not in self.extension_id:
            raise ValueError("Extension id must use KIND:NAME")
        if self.kind not in _KINDS:
            raise ValueError(f"Unknown extension kind: {self.kind}")
        if self.scope not in _SCOPES:
            raise ValueError(f"Unknown extension scope: {self.scope}")
        if self.lifecycle not in _LIFECYCLES:
            raise ValueError(f"Unknown extension lifecycle: {self.lifecycle}")
        invalid = sorted({effect for _name, effect in self.effects} - _EFFECTS)
        if invalid:
            raise ValueError(f"Unknown extension effect(s): {', '.join(invalid)}")

    def to_dict(self) -> dict[str, Any]:
        """Return a stable JSON-compatible representation."""
        data = asdict(self)
        data["capabilities"] = list(self.capabilities)
        data["effects"] = [
            {"capability": name, "effect": effect} for name, effect in self.effects
        ]
        data["permissions"] = list(self.permissions)
        return data


class ExtensionRegistry:
    """Build one isolated snapshot from the existing extension owners."""

    def __init__(
        self,
        *,
        workspace: Path | None = None,
        skill_loader: SkillLoader | None = None,
        mcp_runtime: Any = None,
        project_control_snapshot: ProjectControlSnapshot | None = None,
        hook_loader: Callable[..., list] | None = None,
        mcp_config_loader: Callable[..., list] | None = None,
    ):
        self.workspace = (workspace or current_workdir()).resolve()
        self.config_snapshot = current_config_snapshot(self.workspace)
        self.skill_loader = skill_loader
        self.mcp_runtime = mcp_runtime
        self.project_control_snapshot = (
            project_control_snapshot
            or getattr(skill_loader, "_project_control_snapshot", None)
            or self.config_snapshot.project_control
        )
        self._hook_loader = hook_loader
        self._mcp_config_loader = mcp_config_loader or load_mcp_server_configs

    def snapshot(self) -> list[ExtensionDescriptor]:
        """Return all projections; one broken source becomes an error row."""
        items: list[ExtensionDescriptor] = []
        for name, collector in (
            ("skills", self._skills),
            ("hooks", self._hooks),
            ("tool_packs", self._tool_packs),
            ("mcp", self._mcp_servers),
        ):
            try:
                items.extend(collector())
            except Exception as exc:
                items.append(_error_descriptor(name, exc))
        return sorted(items, key=lambda item: (item.kind, item.extension_id))

    def get(self, extension_id: str) -> ExtensionDescriptor | None:
        """Read one descriptor from the current isolated snapshot."""
        return next(
            (item for item in self.snapshot() if item.extension_id == extension_id),
            None,
        )

    def reload(self) -> list[dict[str, Any]]:
        """Delegate reload to real owners and report restart-only kinds honestly."""
        results: list[dict[str, Any]] = []
        loader = self.skill_loader or current_skill_loader()
        loader.reload()
        results.append({"kind": "skill", "status": "reloaded"})
        self._hooks()
        results.append({"kind": "hook", "status": "reloaded"})
        results.append({"kind": "tool_pack", "status": "restart_required"})
        if self.mcp_runtime is None:
            results.append({"kind": "mcp_server", "status": "restart_required"})
        else:
            changed = bool(self.mcp_runtime.reload_config())
            results.append({
                "kind": "mcp_server",
                "status": "reloaded" if changed else "unchanged",
            })
        return results

    def set_enabled(self, extension_id: str, enabled: bool) -> dict[str, Any]:
        """Delegate lifecycle mutation instead of storing registry-owned state."""
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be bool")
        kind, separator, name = str(extension_id).partition(":")
        if not separator or not name:
            raise ValueError("Extension id must use KIND:NAME")
        if kind == "skill":
            loader = self.skill_loader or current_skill_loader()
            status = loader.set_enabled(name, enabled)
            return {
                "extension_id": extension_id,
                "enabled": enabled,
                "status": status,
                "restart_required": False,
            }
        item = self.get(extension_id)
        if item is None:
            raise ValueError(f"Unknown extension '{extension_id}'")
        return {
            "extension_id": extension_id,
            "enabled": item.enabled,
            "status": "restart_required",
            "restart_required": True,
        }

    def _skills(self) -> list[ExtensionDescriptor]:
        loader = self.skill_loader or current_skill_loader()
        result = []
        for item in loader.list_skills():
            source = str(item.get("source") or "bundled")
            result.append(
                ExtensionDescriptor(
                    extension_id=f"skill:{item['name']}",
                    kind="skill",
                    name=item["name"],
                    source=source,
                    scope=_scope_for_source(source),
                    status=str(item.get("status") or "available"),
                    lifecycle="reloadable",
                    trusted=source != "project",
                    enabled=str(item.get("status") or "available") != "disabled",
                    description=str(item.get("description") or ""),
                    capabilities=("prompt_instructions",),
                    permissions=tuple(sorted(set(item.get("allowed_tools") or []))),
                )
            )
        return result

    def _hooks(self) -> list[ExtensionDescriptor]:
        result = [
            ExtensionDescriptor(
                extension_id="hook:core",
                kind="hook",
                name="core",
                source="bundled",
                scope="global",
                status="loaded",
                lifecycle="static",
                trusted=True,
                description="Built-in Agent lifecycle policy hooks.",
                capabilities=_CORE_HOOK_CAPABILITIES,
                effects=tuple((name, "serial") for name in _CORE_HOOK_CAPABILITIES),
            )
        ]
        hooks = (
            self._hook_loader(self.workspace / ".nz-coder" / "settings.json")
            if self._hook_loader is not None
            else load_configured_hooks_from_settings(
                strict=True,
                project_control_snapshot=self.project_control_snapshot,
            )
        )
        for hook in hooks:
            capabilities = [f"event:{hook.event}", f"action:{hook.action.type}"]
            if hook.reject:
                capabilities.append("decision:reject")
            if hook.continue_run:
                capabilities.append("decision:continue")
            result.append(
                ExtensionDescriptor(
                    extension_id=f"hook:{hook.id}",
                    kind="hook",
                    name=hook.id,
                    source="project",
                    scope="workspace",
                    status="loaded",
                    lifecycle="reloadable",
                    trusted=False,
                    description=f"Schema-limited configured hook for {hook.event}.",
                    capabilities=tuple(capabilities),
                    effects=((hook.event, "serial"),),
                    permissions=("schema_limited_prompt", f"on_error:{hook.on_error}"),
                )
            )
        return result

    def _tool_packs(self) -> list[ExtensionDescriptor]:
        return [
            ExtensionDescriptor(
                extension_id=f"tool_pack:{item['name']}",
                kind="tool_pack",
                name=item["name"],
                source="bundled",
                scope="process",
                status="loaded" if item["loaded"] else "available",
                lifecycle="lazy",
                trusted=True,
                description=str(item.get("description") or ""),
                capabilities=tuple(item.get("tool_names") or []),
                effects=tuple(
                    (name, item.get("tool_effects", {}).get(name, "serial"))
                    for name in item.get("tool_names") or []
                ),
            )
            for item in list_optional_packs()
        ]

    def _mcp_servers(self) -> list[ExtensionDescriptor]:
        records = (
            self.mcp_runtime.extension_snapshot()
            if self.mcp_runtime is not None
            else [
                _mcp_config_record(item)
                for item in (
                    self._mcp_config_loader(workspace=self.workspace)
                    if self._mcp_config_loader is not load_mcp_server_configs
                    else self._mcp_config_loader(
                        workspace=self.workspace,
                        project_control_snapshot=self.project_control_snapshot,
                        config_snapshot=self.config_snapshot,
                    )
                )
            ]
        )
        result = []
        for item in records:
            source = str(item.get("source") or "explicit")
            capabilities = list(item.get("tools") or [])
            prompt_count = int(item.get("prompt_count") or 0)
            resource_count = int(item.get("resource_count") or 0)
            if prompt_count:
                capabilities.append(f"prompts:{prompt_count}")
            if resource_count:
                capabilities.append(f"resources:{resource_count}")
            result.append(
                ExtensionDescriptor(
                    extension_id=f"mcp_server:{item['name']}",
                    kind="mcp_server",
                    name=item["name"],
                    source=source,
                    scope=_scope_for_source(source, default="session"),
                    status=str(item.get("status") or "configured"),
                    lifecycle="live",
                    trusted=bool(item.get("trusted", False)),
                    enabled=str(item.get("status") or "configured") != "disabled",
                    health="failed" if item.get("error") else "healthy",
                    description=f"MCP {item.get('transport') or 'stdio'} server.",
                    capabilities=tuple(capabilities),
                    effects=tuple(item.get("tool_effects") or []),
                    permissions=("external_untrusted_output",),
                    error=str(item.get("error") or ""),
                )
            )
        return result


def extension_snapshot(**kwargs: Any) -> list[ExtensionDescriptor]:
    """Convenience API for one current-context extension snapshot."""
    return ExtensionRegistry(**kwargs).snapshot()


def _mcp_config_record(server: Any) -> dict[str, Any]:
    status = "configured"
    if not server.enabled:
        status = "disabled"
    elif not server.trusted:
        status = "untrusted"
    return {
        "name": server.name,
        "source": server.source,
        "trusted": server.trusted,
        "status": status,
        "transport": server.transport,
        "tools": (),
        "tool_effects": (),
    }


def _scope_for_source(source: str, *, default: str = "global") -> str:
    if source == "project":
        return "workspace"
    if source == "user":
        return "user"
    if source in {"environment", "explicit"}:
        return "process"
    if source == "bundled":
        return "global"
    return default


def _error_descriptor(source: str, exc: Exception) -> ExtensionDescriptor:
    return ExtensionDescriptor(
        extension_id=f"error:{source}",
        kind="error",
        name=source,
        source=source,
        scope="workspace",
        status="failed",
        lifecycle="reloadable",
        trusted=False,
        enabled=False,
        health="failed",
        description="Extension source could not be projected.",
        error=f"{type(exc).__name__}: {exc}",
    )
