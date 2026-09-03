"""Discover installed language servers without downloading dependencies."""
from __future__ import annotations

import os
import hashlib
import json
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path

from nz_coder.foundation import config
from nz_coder.foundation.languages import LSP_LANGUAGES, lsp_command_config_key
from nz_coder.foundation.workspace_trust import WorkspaceTrustStore


@dataclass(frozen=True)
class LanguageServerSpec:
    """Static discovery metadata for one source-language family."""

    language: str
    language_id: str
    extensions: tuple[str, ...]
    root_markers: tuple[str, ...]
    commands: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class ResolvedServer:
    """An installed language server ready to be launched."""

    server_id: str
    language_id: str
    command: tuple[str, ...]
    root: Path
    analysis_paths: tuple[Path, ...] = ()
    source: str = "system"
    trusted: bool = True
    fingerprint: str = ""


_SPECS = (
    LanguageServerSpec(
        "python",
        "python",
        (".py", ".pyi"),
        ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", ".git"),
        (
            ("ty", "server"),
            ("basedpyright-langserver", "--stdio"),
            ("pyright-langserver", "--stdio"),
            ("pylsp",),
        ),
    ),
    LanguageServerSpec(
        "typescript",
        "typescript",
        (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts"),
        (
            "tsconfig.json",
            "jsconfig.json",
            "package.json",
            "pnpm-lock.yaml",
            "yarn.lock",
            "package-lock.json",
        ),
        (
            ("{root}/node_modules/.bin/typescript-language-server", "--stdio"),
            ("typescript-language-server", "--stdio"),
        ),
    ),
    LanguageServerSpec(
        "go",
        "go",
        (".go",),
        ("go.work", "go.mod", ".git"),
        (("gopls",),),
    ),
    LanguageServerSpec(
        "rust",
        "rust",
        (".rs",),
        ("Cargo.toml", "rust-project.json", ".git"),
        (("rust-analyzer",),),
    ),
    LanguageServerSpec(
        "cpp",
        "cpp",
        (".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"),
        ("compile_commands.json", "CMakeLists.txt", ".clangd", ".git"),
        (("clangd",),),
    ),
    LanguageServerSpec(
        "java",
        "java",
        (".java",),
        ("pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", ".git"),
        (("jdtls",),),
    ),
    LanguageServerSpec(
        "kotlin",
        "kotlin",
        (".kt", ".kts"),
        ("build.gradle", "build.gradle.kts", "settings.gradle", ".git"),
        (("kotlin-language-server",),),
    ),
    LanguageServerSpec(
        "ruby",
        "ruby",
        (".rb", ".rake", ".gemspec", ".ru"),
        ("Gemfile", ".ruby-version", ".git"),
        (("ruby-lsp",),),
    ),
    LanguageServerSpec(
        "php",
        "php",
        (".php",),
        ("composer.json", ".git"),
        (("intelephense", "--stdio"),),
    ),
    LanguageServerSpec(
        "lua",
        "lua",
        (".lua",),
        (".luarc.json", ".luarc.jsonc", ".git"),
        (("lua-language-server",),),
    ),
    LanguageServerSpec(
        "bash",
        "shellscript",
        (".sh", ".bash", ".zsh", ".ksh"),
        (".git",),
        (("bash-language-server", "start"),),
    ),
    LanguageServerSpec(
        "yaml",
        "yaml",
        (".yaml", ".yml"),
        (".git",),
        (("yaml-language-server", "--stdio"),),
    ),
    LanguageServerSpec(
        "dart",
        "dart",
        (".dart",),
        ("pubspec.yaml", ".git"),
        (("dart", "language-server", "--lsp"),),
    ),
)

if tuple(spec.language for spec in _SPECS) != LSP_LANGUAGES:
    raise RuntimeError("LSP server specifications and config languages diverged")


def language_server_specs() -> tuple[LanguageServerSpec, ...]:
    """Expose immutable server metadata for schema parity verification."""
    return _SPECS


def _spec_for_path(path: Path) -> LanguageServerSpec | None:
    suffix = path.suffix.lower()
    return next((spec for spec in _SPECS if suffix in spec.extensions), None)

def language_for_path(path: Path) -> str | None:
    """Return the configured source-language family for a path."""
    spec = _spec_for_path(path)
    return spec.language if spec is not None else None



def _find_root(path: Path, workspace: Path, markers: tuple[str, ...]) -> Path:
    workspace = workspace.resolve()
    current = path.resolve().parent
    while True:
        if any((current / marker).exists() for marker in markers):
            return current
        if current == workspace:
            return workspace
        if workspace not in current.parents:
            return workspace
        current = current.parent


def _resolve_executable(command: tuple[str, ...], root: Path) -> tuple[str, ...] | None:
    expanded = tuple(part.format(root=str(root)) for part in command)
    executable = expanded[0]
    candidate = Path(executable)
    if candidate.is_absolute() or candidate.parent != Path("."):
        resolved = candidate if candidate.is_absolute() else (root / candidate)
        if not resolved.is_file():
            return None
        return (str(resolved), *expanded[1:])
    found = shutil.which(executable)
    if not found:
        return None
    return (found, *expanded[1:])


def _split_override(value: str, *, os_name: str | None = None) -> tuple[str, ...]:
    """Split a configured command without destroying native Windows paths."""
    selected_os = os.name if os_name is None else os_name
    parts = shlex.split(value, posix=selected_os != "nt")
    if selected_os == "nt":
        parts = [
            part[1:-1]
            if len(part) >= 2 and part[0] == part[-1] and part[0] in {'"', "'"}
            else part
            for part in parts
        ]
    return tuple(parts)


def resolve_server(path: Path, workspace: Path) -> ResolvedServer | None:
    """Resolve the first installed server matching ``path``."""
    spec = _spec_for_path(path)
    if spec is None:
        return None
    root = _find_root(path, workspace, spec.root_markers)
    # A pyproject can live directly inside an import package (for example
    # workspace/cron_engine/{pyproject.toml,__init__.py}).  Pyright rooted at
    # that directory cannot resolve ``import cron_engine`` because the import
    # package's parent is missing.  Promote only this package-root layout;
    # ordinary projects and src layouts retain their nearest marker root.
    if (
        spec.language == "python"
        and (root / "__init__.py").is_file()
        and root != workspace.resolve()
    ):
        root = root.parent.resolve()
    analysis_paths: tuple[Path, ...] = ()
    override = config.get(lsp_command_config_key(spec.language), "").strip()
    commands = (_split_override(override),) if override else spec.commands
    for command in commands:
        if not command:
            continue
        resolved = _resolve_executable(command, root)
        if resolved:
            executable = Path(resolved[0]).resolve(strict=False)
            workspace_local = _inside_workspace(executable, workspace)
            fingerprint = _server_fingerprint(resolved, executable)
            trusted = not workspace_local or WorkspaceTrustStore().is_trusted(
                workspace,
                f"lsp:{spec.language}",
                fingerprint,
                executable=str(executable),
            )
            return ResolvedServer(
                server_id=Path(resolved[0]).name,
                language_id=spec.language_id,
                command=resolved,
                root=root,
                analysis_paths=analysis_paths,
                source="workspace" if workspace_local else "system",
                trusted=trusted,
                fingerprint=fingerprint,
            )
    return None


def trust_server(path: Path, workspace: Path) -> ResolvedServer:
    """Trust the currently resolved workspace executable for one source file."""
    server = resolve_server(path, workspace)
    if server is None:
        raise ValueError("No installed LSP server was found for this file")
    if server.source != "workspace":
        return server
    WorkspaceTrustStore().trust(
        workspace,
        f"lsp:{server.language_id}",
        server.fingerprint,
        executable=str(Path(server.command[0]).resolve(strict=False)),
    )
    refreshed = resolve_server(path, workspace)
    if refreshed is None or not refreshed.trusted:
        raise ValueError("LSP workspace trust could not be established")
    return refreshed


def untrust_server(path: Path, workspace: Path) -> bool:
    """Remove trust for the currently resolved workspace LSP executable."""
    server = resolve_server(path, workspace)
    if server is None or server.source != "workspace":
        return False
    return WorkspaceTrustStore().remove(
        workspace,
        f"lsp:{server.language_id}",
        executable=str(Path(server.command[0]).resolve(strict=False)),
    )


def _inside_workspace(path: Path, workspace: Path) -> bool:
    try:
        path.relative_to(Path(workspace).resolve())
    except ValueError:
        return False
    return True


def _server_fingerprint(command: tuple[str, ...], executable: Path) -> str:
    executable_hash = ""
    if executable.is_file():
        digest = hashlib.sha256()
        with executable.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        executable_hash = digest.hexdigest()
    payload = json.dumps(
        {
            "command": list(command),
            "executable": str(executable),
            "executable_hash": executable_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def available_server_summary(path: Path) -> str:
    """Return installation candidates for an unsupported file."""
    spec = _spec_for_path(path)
    if spec is None:
        return f"No LSP server is configured for extension '{path.suffix or path.name}'."
    candidates = ", ".join(Path(command[0]).name for command in spec.commands)
    override = lsp_command_config_key(spec.language)
    return (
        f"No installed LSP server found for {spec.language}. "
        f"Install one of: {candidates}; or set {override}."
    )
