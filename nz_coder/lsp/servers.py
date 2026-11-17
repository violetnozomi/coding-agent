"""Discover installed language servers without downloading dependencies."""

from __future__ import annotations

import os
import hashlib
import json
import re
import shlex
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path

from nz_coder.foundation.languages import LSP_LANGUAGES, lsp_command_config_key
from nz_coder.foundation.execution_identity import (
    ExecutionIdentity,
    resolve_execution_identity,
)
from nz_coder.foundation.secret_values import secret_str
from nz_coder.foundation.subprocess_env import build_sanitized_subprocess_env
from nz_coder.foundation.user_paths import prepare_user_storage
from nz_coder.foundation.workspace_trust import (
    ConfigSnapshot,
    ConfigSource,
    WorkspaceTrustStore,
    current_config_snapshot,
    default_trust_store_path,
    ConfigValidationError,
    normalize_lsp_servers_json,
    validate_lsp_environment,
)
from nz_coder.runtime.process.execution_environment import current_execution_environment


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
    config_source: str = "system-path"
    trusted: bool = True
    fingerprint: str = ""
    execution_identity: ExecutionIdentity | None = None
    environment: tuple[tuple[str, str], ...] = field(default=(), repr=False)
    environment_fingerprint: str = ""
    initialization_json: str = field(default="{}", repr=False)
    settings_json: str = field(default="{}", repr=False)
    analysis_environment: str = "unknown"
    analysis_configuration_source: str = "unconfigured"
    origin_workspaces: tuple[tuple[str, int, int], ...] = ()
    trust_record_key: str = field(default="", repr=False)
    trust_store_path: Path | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "environment", tuple((k, secret_str(v)) for k, v in self.environment)
        )
        object.__setattr__(
            self, "initialization_json", secret_str(self.initialization_json)
        )
        object.__setattr__(self, "settings_json", secret_str(self.settings_json))


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


def server_trust_kind(path: Path, profile: ResolvedServer) -> str:
    """为同根多环境保留独立授权槽；载荷内容变化仍由记录指纹拒绝。"""
    language = language_for_path(path)
    if language is None:
        raise ValueError("No configured LSP language for this file")
    scope = (
        os.path.normcase(str(profile.root)),
        profile.environment_fingerprint,
        profile.origin_workspaces,
        profile.command,
    )
    digest = hashlib.sha256(
        json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"lsp:{language}:{digest}"


def analysis_environment_for_file(
    profile: ResolvedServer, path: Path, workspace: Path
) -> str:
    """沿用已捕获启动配置，只重新确认当前 C++ 文件是否有适用编译命令。"""
    if profile.language_id != "cpp":
        return profile.analysis_environment
    names = {profile.server_id.lower().removesuffix(".exe")}
    if profile.execution_identity is not None:
        names.add(
            profile.execution_identity.executable_path.name.lower().removesuffix(".exe")
        )
        if profile.execution_identity.entrypoint_path is not None:
            names.add(
                profile.execution_identity.entrypoint_path.name.lower().removesuffix(
                    ".exe"
                )
            )
    if "clangd" not in names:
        return "unknown"
    return _cpp_analysis_configuration(
        profile.command, path, workspace.resolve(), profile.root
    )[0]


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


def _resolve_executable(
    command: tuple[str, ...], root: Path, environment: dict[str, str]
) -> tuple[str, ...] | None:
    expanded = tuple(part.format(root=str(root)) for part in command)
    executable = expanded[0]
    candidate = Path(executable)
    if candidate.is_absolute() or candidate.parent != Path("."):
        resolved = candidate if candidate.is_absolute() else (root / candidate)
        if not resolved.is_file():
            return None
        return (str(resolved), *expanded[1:])
    found = shutil.which(executable, path=environment.get("PATH", os.defpath))
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


def _expand_configuration(value, workspace: Path, environment: dict[str, str]):
    if isinstance(value, str):

        def replace(match):
            token = match.group(1)
            if token == "worktree":
                return str(workspace)
            if token.startswith("env:") and token[4:] in environment:
                return environment[token[4:]]
            raise ConfigValidationError(
                "Invalid NZ_LSP_SERVERS_JSON: unavailable placeholder"
            )

        return re.sub(r"\$\{([^}]+)\}", replace, value)
    if isinstance(value, dict):
        return {
            k: _expand_configuration(v, workspace, environment)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_expand_configuration(v, workspace, environment) for v in value]
    return value


def _analysis_configuration(
    spec,
    command,
    service_name,
    execution_identity,
    initialization,
    settings,
    trusted_environment,
    prepared,
    path,
    workspace,
    root,
):
    executable_names = {
        Path(part).name.lower().removesuffix(".exe")
        for part in (
            service_name,
            execution_identity.executable_path,
            execution_identity.entrypoint_path or "",
        )
    }
    if spec.language == "python":
        if executable_names & {"pyright-langserver", "basedpyright-langserver"}:
            python_settings = settings.get("python", {})
            explicit_settings = (
                python_settings.get("pythonPath")
                if isinstance(python_settings, dict)
                else None
            )
            explicit_initialization = initialization.get("pythonPath")
            interpreter = explicit_settings or explicit_initialization
            source = (
                "explicit-settings" if explicit_settings else "explicit-initialization"
            )
            if (
                not interpreter
                and prepared is not None
                and trusted_environment.get("VIRTUAL_ENV")
            ):
                venv = Path(trusted_environment["VIRTUAL_ENV"])
                candidate = venv / (
                    "Scripts/python.exe" if os.name == "nt" else "bin/python"
                )
                if candidate.is_file():
                    interpreter = str(candidate)
                    source = "prepared-interpreter"
            if interpreter:
                if not isinstance(interpreter, str):
                    raise ConfigValidationError(
                        "Invalid NZ_LSP_SERVERS_JSON: pythonPath must be a string"
                    )
                if not isinstance(python_settings, dict):
                    raise ConfigValidationError(
                        "Invalid NZ_LSP_SERVERS_JSON: python settings must be an object"
                    )
                # Pyright 实际读取 python 配置节；初始化示例经适配后也必须进入该节。
                settings.setdefault("python", {})["pythonPath"] = interpreter
                return "configured", source
        return "unconfigured", "unconfigured"
    if spec.language == "cpp":
        if "clangd" not in executable_names:
            return "unknown", "unconfigured"
        return _cpp_analysis_configuration(command, path, workspace, root)
    return "not_applicable", "not-applicable"


def _cpp_analysis_configuration(command, path: Path, workspace: Path, root: Path):
    database_dir = None
    for index, argument in enumerate(command[1:], 1):
        if argument.startswith("--compile-commands-dir="):
            database_dir = argument.partition("=")[2]
        elif argument == "--compile-commands-dir" and index + 1 < len(command):
            database_dir = command[index + 1]
    if database_dir is None:
        return "unconfigured", "unconfigured"
    selected = Path(database_dir)
    selected = selected if selected.is_absolute() else root / selected
    try:
        database = selected / "compile_commands.json"
        if database.stat().st_size > 8 * 1024 * 1024:
            return "unknown", "unconfigured"
        entries = json.loads(database.read_text(encoding="utf-8"))
        if not isinstance(entries, list):
            return "unknown", "unconfigured"
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("directory"), str)
                or not isinstance(entry.get("file"), str)
            ):
                continue
            directory = Path(entry["directory"])
            directory = directory if directory.is_absolute() else selected / directory
            directory = directory.resolve()
            source = Path(entry["file"])
            source = (source if source.is_absolute() else directory / source).resolve()
            has_command = bool(entry.get("command")) or (
                isinstance(entry.get("arguments"), list)
                and bool(entry["arguments"])
                and all(isinstance(value, str) for value in entry["arguments"])
            )
            if (
                has_command
                and source == path.resolve()
                and source.is_relative_to(workspace)
                and directory.is_relative_to(workspace)
            ):
                return "configured", "compile-database"
    except (OSError, ValueError, UnicodeError):
        return "unconfigured", "unconfigured"
    return "unconfigured", "unconfigured"


def resolve_server(
    path: Path,
    workspace: Path,
    *,
    config_snapshot: ConfigSnapshot | None = None,
) -> ResolvedServer | None:
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
    snapshot = config_snapshot or current_config_snapshot(workspace)
    workspace = workspace.resolve()
    prepared = current_execution_environment()
    host_environment = dict(os.environ)
    # 先验证资源并捕获一次；发现、身份复核与进程启动共用这份最终环境。
    if prepared is not None:
        trusted_environment = prepared.as_dict()
        origins = prepared.origin_workspaces
    else:
        trusted_environment = build_sanitized_subprocess_env(
            source=host_environment, profile="strict-service"
        )
        origins = ()
    cache = prepare_user_storage(root, environ=host_environment).workspace_cache
    trusted_environment.update({key: str(cache) for key in ("TMPDIR", "TMP", "TEMP")})
    profiles = json.loads(
        normalize_lsp_servers_json(snapshot.get("NZ_LSP_SERVERS_JSON", "{}"))
    )
    profile = _expand_configuration(
        profiles.get(spec.language, {}), workspace, trusted_environment
    )
    overrides = profile.get("env", {})
    validate_lsp_environment(overrides)
    environment = build_sanitized_subprocess_env(
        source=trusted_environment, overrides=overrides, profile="strict-service"
    )
    # which 使用宿主 cwd；在冻结前把空项和相对项固定到实际服务 cwd。
    environment["PATH"] = os.pathsep.join(
        str((Path(part) if Path(part).is_absolute() else root / part).resolve())
        for part in environment.get("PATH", os.defpath).split(os.pathsep)
    )
    frozen_environment = tuple(sorted(environment.items()))
    environment_fingerprint = hashlib.sha256(
        json.dumps(
            (frozen_environment, origins, prepared.fingerprint if prepared else ""),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    initialization = profile.get("initialization", {})
    settings = profile.get("settings", {})
    setting = snapshot.value(lsp_command_config_key(spec.language))
    override = setting.value.strip()
    commands = (_split_override(override),) if override else spec.commands
    for command in commands:
        if not command:
            continue
        resolved = _resolve_executable(command, root, environment)
        if resolved:
            service_name = Path(resolved[0]).name
            executable = Path(resolved[0]).resolve(strict=False)
            if override:
                config_source = {
                    ConfigSource.USER: "user-config",
                    ConfigSource.ENVIRONMENT: "environment-config",
                    ConfigSource.TRUSTED_WORKSPACE: "trusted-workspace-config",
                    ConfigSource.WORKSPACE: "workspace-config",
                    ConfigSource.DEFAULT: "default-config",
                }[setting.source]
            elif "{root}" in command[0]:
                config_source = "workspace-local-default"
            else:
                config_source = "system-path"
            execution_identity = resolve_execution_identity(
                resolved,
                cwd=root,
                workspace=workspace,
                config_source=config_source,
                environment_profile="strict-service",
                environment=environment,
                origin_workspaces=origins,
                prepared_environment=prepared is not None,
            )
            resolved = execution_identity.launch_command
            executable = execution_identity.executable_path
            fingerprint = execution_identity.fingerprint
            workspace_controlled = execution_identity.workspace_controlled
            analysis_environment, analysis_source = _analysis_configuration(
                spec,
                resolved,
                service_name,
                execution_identity,
                initialization,
                settings,
                trusted_environment,
                prepared,
                path,
                workspace,
                root,
            )
            server = ResolvedServer(
                server_id=Path(command[0]).name,
                language_id=spec.language_id,
                command=resolved,
                root=root,
                analysis_paths=analysis_paths,
                source="workspace" if workspace_controlled else "system",
                config_source=config_source,
                trusted=not workspace_controlled,
                fingerprint=fingerprint,
                execution_identity=execution_identity,
                environment=frozen_environment,
                environment_fingerprint=environment_fingerprint,
                initialization_json=json.dumps(
                    initialization,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                settings_json=json.dumps(
                    settings, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ),
                analysis_environment=analysis_environment,
                analysis_configuration_source=analysis_source,
                origin_workspaces=origins,
            )
            if workspace_controlled:
                store = WorkspaceTrustStore(default_trust_store_path(host_environment))
                record_key = store.capture_record_key(
                    workspace,
                    server_trust_kind(path, server),
                    executable=str(executable),
                )
                server = replace(
                    server,
                    trusted=store.is_trusted_record(record_key, fingerprint),
                    trust_record_key=record_key,
                    trust_store_path=store.path,
                )
            return server
    return None


def trust_server(
    path: Path, workspace: Path, *, config_snapshot: ConfigSnapshot | None = None
) -> ResolvedServer:
    """Trust the currently resolved workspace executable for one source file."""
    server = resolve_server(path, workspace, config_snapshot=config_snapshot)
    if server is None:
        raise ValueError("No installed LSP server was found for this file")
    if server.source != "workspace":
        return server
    WorkspaceTrustStore(server.trust_store_path).trust(
        workspace,
        server_trust_kind(path, server),
        server.fingerprint,
        executable=str(server.execution_identity.executable_path),
    )
    refreshed = resolve_server(path, workspace, config_snapshot=config_snapshot)
    if refreshed is None or not refreshed.trusted:
        raise ValueError("LSP workspace trust could not be established")
    return refreshed


def untrust_server(
    path: Path, workspace: Path, *, config_snapshot: ConfigSnapshot | None = None
) -> bool:
    """Remove trust for the currently resolved workspace LSP executable."""
    server = resolve_server(path, workspace, config_snapshot=config_snapshot)
    if server is None or server.source != "workspace":
        return False
    return WorkspaceTrustStore(server.trust_store_path).remove_record(
        server.trust_record_key,
    )


def _inside_workspace(path: Path, workspace: Path) -> bool:
    try:
        path.relative_to(Path(workspace).resolve())
    except ValueError:
        return False
    return True


def available_server_summary(path: Path) -> str:
    """Return installation candidates for an unsupported file."""
    spec = _spec_for_path(path)
    if spec is None:
        return (
            f"No LSP server is configured for extension '{path.suffix or path.name}'."
        )
    candidates = ", ".join(Path(command[0]).name for command in spec.commands)
    override = lsp_command_config_key(spec.language)
    return (
        f"No installed LSP server found for {spec.language}. "
        f"Install one of: {candidates}; or set {override}."
    )
