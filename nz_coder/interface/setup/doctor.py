"""Offline installation and workspace diagnostics for the terminal product."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import importlib.util
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

from rich.console import Console
from rich.table import Table

from nz_coder import __version__
from nz_coder.foundation import config
from nz_coder.lsp.servers import language_for_path, resolve_server
from nz_coder.mcp.config import load_mcp_server_configs
from nz_coder.providers import create_provider
from nz_coder.providers.configuration import provider_connection
from nz_coder.providers.models import active_model_selection
from nz_coder.runtime.process.workdir import current_workdir
from nz_coder.foundation.private_paths import (
    inspect_private_path,
    windows_private_acl_available,
)
from nz_coder.foundation.workspace_trust import (
    is_secret_config_key,
    load_config_snapshot,
)
from nz_coder.tool_platform.permissioning.modes import MODES


@dataclass(frozen=True)
class DoctorCheck:
    """One secret-free diagnostic result."""

    name: str
    status: str
    detail: str
    action: str = ""
    category: str = "required"


def collect_doctor_checks(workspace: Path | None = None) -> list[DoctorCheck]:
    """Collect bounded, offline checks without starting providers, LSP, or MCP."""
    root = (workspace or current_workdir()).resolve()
    checks = [
        _check_python(),
        _check_dependencies(),
        _check_workspace(root),
        _check_state_directory(root),
        _check_private_state_security(root),
        _check_credential_file_security(root),
    ]
    checks.extend(_check_configuration(root))
    checks.extend(_check_repo_intelligence(root))
    selection, model_checks = _check_model(root)
    checks.extend(model_checks)
    if selection is not None:
        checks.extend(_check_provider(selection.provider))
    checks.append(_check_permission_mode())
    checks.append(_check_mcp(root))
    checks.append(_check_lsp(root))
    checks.append(_check_web_search())
    checks.append(_check_terminal())
    return checks


def collect_repo_intelligence_checks(
    workspace: Path | None = None,
) -> list[DoctorCheck]:
    """Probe parser, watcher, and LSP tiers without initializing a model provider."""
    root = (workspace or current_workdir()).resolve()
    return _check_repo_intelligence(root)


def doctor_main(
    argv: list[str] | None = None,
    *,
    output_console: Console | None = None,
) -> int:
    """Render diagnostics and return nonzero only for blockers or strict warnings."""
    parser = argparse.ArgumentParser(prog="nz-coder doctor")
    parser.add_argument("--json", action="store_true", help="Emit secret-free JSON")
    parser.add_argument("--strict", action="store_true", help="Treat warnings as failures")
    parser.add_argument(
        "--cwd", "--workspace", dest="workspace", type=Path,
        help="Workspace to diagnose (defaults to the current NZ-Coder workspace)",
    )
    parser.add_argument(
        "--repo-intelligence-only",
        action="store_true",
        help="Probe repository parsers, watcher, and LSP augmentation only",
    )
    args = parser.parse_args(argv)
    workspace = (
        args.workspace.expanduser().resolve()
        if args.workspace is not None else current_workdir().resolve()
    )
    checks = (
        collect_repo_intelligence_checks(workspace)
        if args.repo_intelligence_only
        else collect_doctor_checks(workspace)
    )
    if args.json:
        print(json.dumps({
            "version": __version__,
            "workspace": str(workspace),
            "checks": [asdict(check) for check in checks],
        }, ensure_ascii=False, indent=2))
    else:
        _render_checks(output_console or Console(), checks)
    failed = any(check.status == "fail" for check in checks)
    warned = any(check.status == "warn" for check in checks)
    return 1 if failed or (args.strict and warned) else 0


def _check_python() -> DoctorCheck:
    version = sys.version_info
    supported = version >= (3, 10)
    return DoctorCheck(
        "python",
        "pass" if supported else "fail",
        f"{version.major}.{version.minor}.{version.micro}",
        "Install Python 3.10 or newer." if not supported else "",
    )


def _check_dependencies() -> DoctorCheck:
    modules = ("openai", "dotenv", "rich", "prompt_toolkit", "yaml")
    missing = [name for name in modules if importlib.util.find_spec(name) is None]
    return DoctorCheck(
        "dependencies",
        "fail" if missing else "pass",
        "missing: " + ", ".join(missing) if missing else "required packages importable",
        "Reinstall with: python -m pip install nz-coder" if missing else "",
    )


def _check_repo_intelligence(root: Path) -> list[DoctorCheck]:
    """Report the analyzer tier actually selected in this installation."""
    from nz_coder.intelligence.analyzers import AnalyzerRegistry

    probe = AnalyzerRegistry().capability_probe()

    def parser_check(name: str, languages: tuple[str, ...]) -> DoctorCheck:
        entries = [probe[language] for language in languages]
        available = all(bool(item["available"]) for item in entries)
        detail = ", ".join(
            f"{language}={probe[language]['capability_tier']}"
            for language in languages
        )
        return DoctorCheck(
            name, "pass" if available else "warn", detail,
            "Install the declared tree-sitter grammar wheels; lexical fallback remains available."
            if not available else "",
            "optional",
        )

    python = probe["python"]
    checks = [
        DoctorCheck(
            "repo-parser-python", "pass" if python["available"] else "fail",
            str(python["capability_tier"]),
        ),
        parser_check("repo-parser-ts-js", ("typescript", "javascript")),
        parser_check("repo-parser-go", ("go",)),
    ]
    watchfiles_available = importlib.util.find_spec("watchfiles") is not None
    checks.append(DoctorCheck(
        "repo-watcher", "pass" if watchfiles_available else "warn",
        "watchfiles" if watchfiles_available else "adaptive-polling fallback",
        "Install watchfiles for event-driven incremental indexing."
        if not watchfiles_available else "",
        "optional",
    ))

    semantic_installed = importlib.util.find_spec("sentence_transformers") is not None
    checks.append(DoctorCheck(
        "repo-semantic-retrieval", "warn",
        "experimental backend installed; provider is unconfigured"
        if semantic_installed else "unavailable; sentence-transformers is not installed",
        "Install the optional semantic extra to run embedding experiments."
        if not semantic_installed else "Configure semantic_model per benchmark/run to enable it.",
        "experimental",
    ))

    lsp_available = False
    if config.LSP_ENABLED:
        visited = 0
        ignored = {".git", ".nz-coder", ".venv", "node_modules", "venv"}
        for directory, directories, files in os.walk(root, followlinks=False):
            directories[:] = [name for name in directories if name not in ignored]
            for name in files:
                path = Path(directory) / name
                if language_for_path(path) and resolve_server(path, root):
                    lsp_available = True
                    break
                visited += 1
                if visited >= 500:
                    break
            if lsp_available or visited >= 500:
                break
    checks.append(DoctorCheck(
        "repo-lsp-augmentation", "pass" if lsp_available else "warn",
        "available" if lsp_available else (
            "disabled" if not config.LSP_ENABLED else "no installed server detected"
        ),
        "Enable LSP and install a workspace language server for bounded definition augmentation."
        if not lsp_available else "",
        "optional",
    ))
    return checks


def _check_workspace(root: Path) -> DoctorCheck:
    if not root.exists() or not root.is_dir():
        return DoctorCheck("workspace", "fail", str(root), "Choose an existing directory.")
    writable = os.access(root, os.R_OK | os.W_OK | os.X_OK)
    return DoctorCheck(
        "workspace",
        "pass" if writable else "fail",
        f"{root} ({'read/write' if writable else 'not writable'})",
        "Grant this user read/write access or choose another workspace." if not writable else "",
    )


def _check_state_directory(root: Path) -> DoctorCheck:
    target = root / ".nz-coder"
    parent = target if target.exists() else root
    safe = not target.is_symlink() and os.access(parent, os.R_OK | os.W_OK | os.X_OK)
    return DoctorCheck(
        "state-directory",
        "pass" if safe else "fail",
        str(target),
        "Remove the symlink or repair workspace permissions." if not safe else "",
    )


def _check_private_state_security(
    root: Path,
    *,
    os_name: str | None = None,
    windows_api=None,
) -> DoctorCheck:
    """Report real private-state protection without mutating the workspace."""
    selected_os = os.name if os_name is None else os_name
    target = root / ".nz-coder"
    if selected_os != "nt":
        if not target.exists():
            return DoctorCheck(
                "state-security",
                "pass",
                "owner-only POSIX modes are applied when state is created",
            )
        result = inspect_private_path(target, os_name=selected_os)
        return DoctorCheck(
            "state-security",
            "pass" if result.hardened else "warn",
            result.detail,
            "Restrict the state directory to the current user."
            if not result.hardened else "",
        )

    if not target.exists():
        available = windows_private_acl_available(windows_api)
        return DoctorCheck(
            "state-security",
            "pass" if available else "warn",
            "Windows protected DACL adapter available; applied when state is created"
            if available
            else "Tier B: Windows DACL APIs unavailable; authentication remains required",
            "Run in a normal user profile with Windows security APIs available."
            if not available else "",
        )
    result = inspect_private_path(
        target,
        os_name=selected_os,
        windows_api=windows_api,
    )
    return DoctorCheck(
        "state-security",
        "pass" if result.hardened else "warn",
        result.detail if result.hardened else f"Tier B: {result.detail}",
        "Recreate or update state with NZ-Coder to apply a protected DACL."
        if not result.hardened else "",
    )


def _check_credential_file_security(
    root: Path,
    *,
    os_name: str | None = None,
    windows_api=None,
) -> DoctorCheck:
    """Inspect a persisted workspace credential file without reading secrets."""
    target = root / ".env"
    if not target.exists():
        return DoctorCheck(
            "credential-file-security",
            "pass",
            "workspace .env is absent; shell credentials are not persisted here",
        )
    legacy_credentials = _workspace_credential_names(target)
    selected_os = os.name if os_name is None else os_name
    result = inspect_private_path(
        target,
        os_name=selected_os,
        windows_api=windows_api,
    )
    legacy = (
        "Legacy workspace credential configuration detected; migrate with /connect. "
        if legacy_credentials else ""
    )
    return DoctorCheck(
        "credential-file-security",
        "warn" if legacy_credentials or not result.hardened else "pass",
        legacy + (result.detail if result.hardened else f"Tier B: {result.detail}"),
        "Run /connect to move credentials to the user-private configuration; "
        "then remove credentials from workspace .env."
        if legacy_credentials else (
            "Restrict .env to the current user."
            if not result.hardened else ""
        ),
    )


def _workspace_credential_names(path: Path) -> tuple[str, ...]:
    """Return only configured credential names, never their values."""
    names: list[str] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            name, value = stripped.split("=", 1)
            name = name.removeprefix("export ").strip()
            if value.strip() and is_secret_config_key(name):
                names.append(name)
    except (OSError, UnicodeError):
        return ()
    return tuple(sorted(set(names)))


def _check_configuration(root: Path) -> list[DoctorCheck]:
    """Report all typed configuration issues and ignored workspace controls."""
    snapshot = load_config_snapshot(root)
    checks = [
        DoctorCheck(
            f"config-{issue.key}",
            "warn",
            issue.message,
            "Correct the value in its reported source; the safe default is active.",
        )
        for issue in snapshot.issues
    ]
    ignored = sorted(
        key for key, value in snapshot.values.items()
        if value.ignored and value.requires_trust
    )
    checks.append(DoctorCheck(
        "workspace-config-trust",
        "warn" if ignored else "pass",
        (
            f"untrusted sensitive workspace settings ignored: {', '.join(ignored)}"
            if ignored else (
                "trusted workspace configuration fingerprint"
                if snapshot.workspace_trusted else "no untrusted sensitive overrides"
            )
        ),
        "Review and explicitly trust the exact workspace configuration fingerprint."
        if ignored else "",
    ))
    return checks


def _check_model(root: Path):
    try:
        selection = active_model_selection(root)
        if not selection.model_id.strip():
            raise ValueError("model id is empty")
        create_provider(selection.provider)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return None, [DoctorCheck(
            "model",
            "fail",
            str(exc),
            "Set MODEL_PROVIDER/MODEL_ID or run nz-coder models select PROVIDER/MODEL.",
        )]
    variant = f" variant={selection.variant}" if selection.variant else ""
    return selection, [DoctorCheck(
        "model",
        "pass",
        f"{selection.provider}/{selection.model_id}{variant} ({selection.source})",
    )]


def _check_provider(provider: str) -> list[DoctorCheck]:
    connection = provider_connection(provider)
    credential = DoctorCheck(
        "credential",
        "pass" if connection.configured else "fail",
        f"{connection.credential_name}: {'configured' if connection.configured else 'missing'}",
        f"Set {connection.credential_name} in the shell or use /connect."
        if not connection.configured else "",
    )
    parsed = urlsplit(connection.base_url)
    loopback = (parsed.hostname or "").lower() in {"127.0.0.1", "localhost", "::1"}
    valid_url = bool(parsed.netloc and (parsed.scheme == "https" or (parsed.scheme == "http" and loopback)))
    endpoint = DoctorCheck(
        "provider-endpoint",
        "pass" if valid_url else "fail",
        connection.base_url or "missing",
        "Use HTTPS, or loopback HTTP for a local provider." if not valid_url else "",
    )
    return [credential, endpoint]


def _check_permission_mode() -> DoctorCheck:
    valid = config.PERMISSION_MODE in MODES
    return DoctorCheck(
        "permission-mode",
        "pass" if valid else "fail",
        str(config.PERMISSION_MODE),
        f"Choose one of: {', '.join(MODES)}." if not valid else "",
    )


def _check_mcp(root: Path) -> DoctorCheck:
    try:
        servers = load_mcp_server_configs(workspace=root)
    except (OSError, ValueError) as exc:
        status = "fail" if config.MCP_ENABLED else "warn"
        return DoctorCheck("mcp-config", status, str(exc), "Repair or remove the invalid MCP config.", "optional")
    enabled = [server for server in servers if server.enabled]
    untrusted = [server.name for server in enabled if not server.trusted]
    if untrusted and config.MCP_ENABLED:
        return DoctorCheck(
            "mcp-config",
            "warn",
            f"{len(enabled)} enabled; untrusted: {', '.join(untrusted)}",
            "Review with nz-coder mcp list, then trust the intended project command.",
            "optional",
        )
    state = "enabled" if config.MCP_ENABLED else "disabled"
    return DoctorCheck("mcp-config", "pass", f"{state}; {len(enabled)} configured server(s)", category="optional")


def _check_lsp(root: Path) -> DoctorCheck:
    if not config.LSP_ENABLED:
        return DoctorCheck("lsp", "pass", "disabled", category="optional")
    samples: dict[str, Path] = {}
    ignored = {".git", ".nz-coder", ".venv", "venv", "node_modules", "__pycache__"}
    visited = 0
    for directory, directories, files in os.walk(root, followlinks=False):
        directories[:] = [name for name in directories if name not in ignored]
        for name in files:
            path = Path(directory) / name
            language = language_for_path(path)
            if language and language not in samples:
                samples[language] = path
            visited += 1
            if visited >= 2000:
                break
        if visited >= 2000:
            break
    if not samples:
        return DoctorCheck("lsp", "pass", "enabled; no supported source files detected", category="optional")
    installed = []
    missing = []
    trust_required = []
    for language, path in sorted(samples.items()):
        server = resolve_server(path, root)
        if server is None:
            missing.append(language)
        elif not server.trusted:
            trust_required.append(f"{language}:{server.server_id}")
        else:
            installed.append(f"{language}:{server.server_id}")
    detail = "installed=" + (", ".join(installed) or "none")
    if trust_required:
        detail += "; trust-required=" + ", ".join(trust_required)
    if missing:
        detail += "; optional missing=" + ", ".join(missing)
    return DoctorCheck(
        "lsp",
        "warn" if missing or trust_required else "pass",
        detail,
        "Install only the language servers you need; NZ-Coder still has structural search."
        if missing else (
            "Review and trust a workspace LSP with nz-coder lsp trust <source-file>."
            if trust_required else ""
        ),
        "optional",
    )


def _check_terminal() -> DoctorCheck:
    interactive = bool(
        getattr(sys.stdin, "isatty", lambda: False)()
        and getattr(sys.stdout, "isatty", lambda: False)()
    )
    return DoctorCheck(
        "terminal",
        "pass" if interactive else "warn",
        "interactive TTY" if interactive else "non-interactive; plain fallback only",
        "Run nz-coder from an interactive terminal for fuzzy dialogs." if not interactive else "",
        "optional",
    )


def _check_web_search() -> DoctorCheck:
    """Validate web discovery configuration without making a network request."""
    selected = os.environ.get("NZ_CODER_WEB_SEARCH_PROVIDER", "auto").strip().lower()
    disabled = {"", "off", "none", "disabled"}
    supported = {
        "auto", "default", "bing", "bing-rss", "duckduckgo", "ddg",
        "duckduckgo-html", "github", "github-issues",
    }
    if selected in disabled:
        return DoctorCheck(
            "web-search", "pass", "disabled; webfetch remains available",
            category="optional",
        )
    if selected not in supported:
        return DoctorCheck(
            "web-search", "fail", f"unsupported provider: {selected}",
            "Use auto, bing-rss, duckduckgo-html, github-issues, or off.",
            "optional",
        )
    provider = "default-web" if selected in {"auto", "default"} else selected
    return DoctorCheck(
        "web-search", "pass", f"configured provider={provider}; live network not probed",
        category="optional",
    )


def _render_checks(console: Console, checks: list[DoctorCheck]) -> None:
    table = Table(title=f"NZ-Coder doctor v{__version__}", show_lines=True, expand=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Class", no_wrap=True, width=4)
    table.add_column("Check", style="cyan", no_wrap=True)
    table.add_column("Detail")
    table.add_column("Action")
    styles = {"pass": "green", "warn": "yellow", "fail": "bold red"}
    labels = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}
    categories = {"required": "REQ", "optional": "OPT", "experimental": "EXP"}
    for check in checks:
        table.add_row(
            f"[{styles[check.status]}]{labels[check.status]}[/]",
            categories[check.category],
            check.name,
            check.detail,
            check.action or "—",
        )
    console.print(table)


if __name__ == "__main__":
    raise SystemExit(doctor_main())
