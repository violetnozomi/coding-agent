"""Project profile detection for repository-level coding tasks.

The profile is intentionally lightweight: it scans common project metadata files
and returns a compact summary that can be shown to the model without bloating the
prompt. It is not a full build-system detector.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from nz_coder.runtime.process.workdir import current_workdir
from nz_coder.tools import register

_EXCLUDED_DIRS = {
    ".git", ".hg", ".svn", ".nz-coder", ".nz-coder-runs", "node_modules",
    "dist", "build", "target", ".tox", ".nox", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "htmlcov", "coverage",
}
_GENERATED_CANDIDATES = (
    "dist", "build", "node_modules", "target", ".tox", ".nox", "coverage",
    "htmlcov", ".next", "out",
)
_LANG_BY_EXT = {
    ".py": "python", ".pyi": "python", ".js": "javascript", ".jsx": "javascript",
    ".mjs": "javascript", ".cjs": "javascript", ".ts": "typescript",
    ".tsx": "typescript", ".go": "go", ".rs": "rust", ".java": "java",
    ".rb": "ruby", ".php": "php", ".c": "c", ".cc": "cpp", ".cpp": "cpp",
    ".h": "c", ".hpp": "cpp", ".cs": "csharp",
}


def _is_excluded_directory(name: str) -> bool:
    return name in _EXCLUDED_DIRS


def _safe_workdir(path: str | Path | None = None) -> Path:
    """Return a workspace-contained directory path."""
    root = current_workdir().resolve()
    target = root if path is None else (root / Path(path)).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path escapes workspace: {path}") from exc
    if not target.exists() or not target.is_dir():
        raise ValueError(f"not a directory: {path or '.'}")
    return target


def _read_text(path: Path, max_chars: int = 200_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:max_chars]
    except OSError:
        return ""


def _load_package_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _add_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def _script_command(pm: str, script: str) -> str:
    if pm in {"pnpm", "yarn"}:
        return f"{pm} {script}"
    if script == "test":
        return "npm test"
    return f"npm run {script}"


def _node_package_manager(root: Path) -> str:
    if (root / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (root / "yarn.lock").exists():
        return "yarn"
    return "npm"


def _looks_like_source_root(path: Path) -> bool:
    if not path.is_dir() or _is_excluded_directory(path.name):
        return False
    if (path / "__init__.py").exists():
        return True
    return any(child.suffix in _LANG_BY_EXT for child in path.glob("*"))


def _sample_languages(root: Path, limit: int = 5000) -> list[str]:
    found: list[str] = []
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if not _is_excluded_directory(name)]
        for filename in filenames:
            if count >= limit:
                return found
            fp = Path(dirpath) / filename
            lang = _LANG_BY_EXT.get(fp.suffix.lower())
            if lang:
                _add_unique(found, lang)
            count += 1
    return found


def _detect_python(root: Path, profile: dict) -> None:
    pyproject = _read_text(root / "pyproject.toml").lower() if (root / "pyproject.toml").exists() else ""
    setup_cfg = _read_text(root / "setup.cfg").lower() if (root / "setup.cfg").exists() else ""
    tox_ini = root / "tox.ini"
    noxfile = root / "noxfile.py"

    if any((root / name).exists() for name in ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "pytest.ini", "tox.ini", "noxfile.py")):
        _add_unique(profile["languages"], "python")
        if "poetry" in pyproject:
            _add_unique(profile["package_managers"], "poetry")
        elif "uv" in pyproject:
            _add_unique(profile["package_managers"], "uv")
        else:
            _add_unique(profile["package_managers"], "pip")

    native_runner = root / "tests" / "runtests.py"
    if native_runner.is_file():
        _add_unique(profile["test_commands"], "python tests/runtests.py")
    if (
        (root / "pytest.ini").exists()
        or "pytest" in pyproject
        or ((root / "tests").exists() and not native_runner.is_file())
    ):
        _add_unique(profile["test_commands"], "pytest")
    if tox_ini.exists():
        _add_unique(profile["test_commands"], "tox")
    if noxfile.exists():
        _add_unique(profile["test_commands"], "nox")

    combined = pyproject + "\n" + setup_cfg
    if "mypy" in combined:
        _add_unique(profile["typecheck_commands"], "mypy")
    if "pyright" in combined:
        _add_unique(profile["typecheck_commands"], "pyright")
    if "ruff" in combined:
        _add_unique(profile["lint_commands"], "ruff check")
    if "flake8" in combined:
        _add_unique(profile["lint_commands"], "flake8")


def _detect_node(root: Path, profile: dict) -> None:
    package_json = root / "package.json"
    if not package_json.exists():
        return
    data = _load_package_json(package_json)
    pm = _node_package_manager(root)
    _add_unique(profile["package_managers"], pm)
    deps_blob = json.dumps({
        "dependencies": data.get("dependencies", {}),
        "devDependencies": data.get("devDependencies", {}),
    }).lower()
    if "typescript" in deps_blob or (root / "tsconfig.json").exists():
        _add_unique(profile["languages"], "typescript")
    else:
        _add_unique(profile["languages"], "javascript")

    scripts = data.get("scripts", {}) if isinstance(data.get("scripts"), dict) else {}
    for key in ("test", "unit", "test:unit"):
        if key in scripts:
            _add_unique(profile["test_commands"], _script_command(pm, key))
            break
    for key in ("typecheck", "check-types", "tsc"):
        if key in scripts:
            _add_unique(profile["typecheck_commands"], _script_command(pm, key))
            break
    for key in ("lint", "eslint"):
        if key in scripts:
            _add_unique(profile["lint_commands"], _script_command(pm, key))
            break
    if "build" in scripts:
        _add_unique(profile["build_commands"], _script_command(pm, "build"))


def _detect_go_rust(root: Path, profile: dict) -> None:
    if (root / "go.mod").exists():
        _add_unique(profile["languages"], "go")
        _add_unique(profile["package_managers"], "go")
        _add_unique(profile["test_commands"], "go test ./...")
        _add_unique(profile["build_commands"], "go build ./...")
    if (root / "Cargo.toml").exists():
        _add_unique(profile["languages"], "rust")
        _add_unique(profile["package_managers"], "cargo")
        _add_unique(profile["typecheck_commands"], "cargo check")
        _add_unique(profile["test_commands"], "cargo test")
        _add_unique(profile["build_commands"], "cargo build")


def _detect_roots(root: Path, profile: dict) -> None:
    for name in ("src", "lib", "app", "pkg", "cmd"):
        if (root / name).is_dir():
            _add_unique(profile["source_roots"], name)
    for package_src in root.glob("packages/*/src"):
        if package_src.is_dir():
            rel = package_src.relative_to(root).as_posix()
            _add_unique(profile["source_roots"], rel)
    for child in root.iterdir() if root.exists() else []:
        if child.is_dir() and child.name not in {"tests", "test", "examples"} and _looks_like_source_root(child):
            _add_unique(profile["source_roots"], child.name)
    for name in ("tests", "test", "__tests__", "spec", "specs"):
        if (root / name).is_dir():
            _add_unique(profile["test_roots"], name)
    for gen in _GENERATED_CANDIDATES:
        if (root / gen).exists():
            _add_unique(profile["generated_dirs"], gen)


def _detect_workflows(root: Path, profile: dict) -> None:
    workflows = root / ".github" / "workflows"
    if not workflows.is_dir():
        return
    for fp in list(workflows.glob("*.yml")) + list(workflows.glob("*.yaml")):
        text = _read_text(fp, max_chars=40_000).lower()
        for marker, bucket, command in (
            ("pytest", "test_commands", "pytest"),
            ("pnpm test", "test_commands", "pnpm test"),
            ("npm test", "test_commands", "npm test"),
            ("yarn test", "test_commands", "yarn test"),
            ("mypy", "typecheck_commands", "mypy"),
            ("ruff", "lint_commands", "ruff check"),
            ("cargo check", "typecheck_commands", "cargo check"),
            ("go test", "test_commands", "go test ./..."),
        ):
            if marker in text:
                _add_unique(profile[bucket], command)


def build_project_profile(path: str | Path | None = None, save: bool = False) -> dict:
    """Build a lightweight project profile for the current workspace."""
    root = _safe_workdir(path)
    profile = {
        "workspace": str(root),
        "languages": [],
        "package_managers": [],
        "source_roots": [],
        "test_roots": [],
        "test_commands": [],
        "typecheck_commands": [],
        "lint_commands": [],
        "build_commands": [],
        "generated_dirs": [],
        "known_env_noise": [],
    }

    for lang in _sample_languages(root):
        _add_unique(profile["languages"], lang)
    _detect_python(root, profile)
    _detect_node(root, profile)
    _detect_go_rust(root, profile)
    _detect_roots(root, profile)
    _detect_workflows(root, profile)

    if "python" in profile["languages"]:
        _add_unique(profile["known_env_noise"], "missing optional deps")
        _add_unique(profile["known_env_noise"], "DISPLAY")
    if "typescript" in profile["languages"] or "javascript" in profile["languages"]:
        _add_unique(profile["known_env_noise"], "node_modules missing")

    if save:
        out = root / ".nz-coder" / "project_profile.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    return profile


def build_project_execution_facts(path: str | Path | None = None) -> dict:
    """Return deterministic roots and launch facts the model must not infer.

    The paths are derived from the active workspace and lightweight project
    profile.  They are data for prompt construction, not executable commands.
    """
    root = _safe_workdir(path)
    profile = build_project_profile(root.relative_to(current_workdir()), save=False)
    project_root = _single_nested_project_root(root) or root
    if project_root != root:
        nested_relative = project_root.relative_to(root).as_posix()
        nested_profile = build_project_profile(
            project_root.relative_to(current_workdir()),
            save=False,
        )
        for test_root in nested_profile.get("test_roots", []):
            _add_unique(
                profile["test_roots"],
                f"{nested_relative}/{test_root}",
            )
        for command in nested_profile.get("test_commands", []):
            if command == "pytest" and nested_profile.get("test_roots"):
                for test_root in nested_profile["test_roots"]:
                    _add_unique(
                        profile["test_commands"],
                        f"python -m pytest -q {nested_relative}/{test_root}",
                    )
            else:
                _add_unique(profile["test_commands"], command)
    python_packages: list[dict] = []
    seen_packages: set[str] = set()

    package_roots = [root]
    package_roots.extend(
        root / item
        for item in profile.get("source_roots", [])
        if (root / item).is_dir()
    )
    for candidate_root in package_roots:
        candidates = []
        if (candidate_root / "__init__.py").is_file() and candidate_root != root:
            candidates.append(candidate_root)
        candidates.extend(
            child for child in candidate_root.iterdir()
            if child.is_dir()
            and not _is_excluded_directory(child.name)
            and (child / "__init__.py").is_file()
        )
        for package in candidates:
            relative = package.relative_to(root).as_posix()
            if relative in seen_packages:
                continue
            seen_packages.add(relative)
            python_packages.append({
                "module_name": package.name,
                "package_path": relative,
                "module_cwd": str(package.parent.resolve()),
            })

    entrypoints: list[str] = []
    for package in python_packages:
        main_path = root / package["package_path"] / "__main__.py"
        if main_path.is_file():
            entrypoints.append(main_path.relative_to(root).as_posix())
    for candidate in ("main.py", "app.py", "manage.py"):
        if (root / candidate).is_file():
            _add_unique(entrypoints, candidate)

    return {
        "workspace_root": str(root.resolve()),
        "project_root": str(project_root.resolve()),
        "source_roots": list(profile.get("source_roots", [])),
        "test_roots": list(profile.get("test_roots", [])),
        "python_packages": python_packages,
        "node_packages": _node_execution_facts(root),
        "test_commands": list(profile.get("test_commands", [])),
        "typecheck_commands": list(profile.get("typecheck_commands", [])),
        "lint_commands": list(profile.get("lint_commands", [])),
        "build_commands": list(profile.get("build_commands", [])),
        "entrypoints": entrypoints,
    }


def _single_nested_project_root(root: Path) -> Path | None:
    """Return one unambiguous one-level project root inside a workspace."""
    manifests = (
        "pyproject.toml", "setup.py", "setup.cfg", "package.json",
        "go.mod", "Cargo.toml",
    )
    if any((root / name).is_file() for name in manifests):
        return None
    candidates = [
        child
        for child in root.iterdir()
        if child.is_dir()
        and not _is_excluded_directory(child.name)
        and any((child / name).is_file() for name in manifests)
    ]
    return candidates[0] if len(candidates) == 1 else None


def _node_execution_facts(root: Path) -> list[dict]:
    """Return root and one-level workspace package facts without globbing deeply."""
    result: list[dict] = []
    candidates = [root]
    packages = root / "packages"
    if packages.is_dir():
        candidates.extend(child for child in packages.iterdir() if child.is_dir())
    for candidate in candidates:
        if not (candidate / "package.json").is_file():
            continue
        result.append({
            "package_root": candidate.relative_to(root).as_posix(),
            "package_manager": _node_package_manager(candidate),
        })
    return result


def load_project_profile(path: str | Path | None = None, rebuild: bool = False) -> dict:
    """Load a saved project profile or rebuild it if missing/stale is acceptable."""
    root = _safe_workdir(path)
    fp = root / ".nz-coder" / "project_profile.json"
    if fp.exists() and not rebuild:
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            pass
    return build_project_profile(root.relative_to(current_workdir()), save=False)


def compact_profile_summary(profile: dict, max_items: int = 3) -> str:
    """Return a one-line prompt-friendly profile summary."""
    def pick(key: str) -> str:
        values = [str(v) for v in profile.get(key, []) if v]
        return ",".join(values[:max_items]) if values else "none"

    langs = pick("languages")
    tests = pick("test_commands")
    typecheck = pick("typecheck_commands")
    sources = pick("source_roots")
    test_roots = pick("test_roots")
    pms = pick("package_managers")
    return (
        f"ProjectProfile: languages={langs}; package_managers={pms}; "
        f"tests={tests}; typecheck={typecheck}; source_roots={sources}; "
        f"test_roots={test_roots}"
    )


def format_project_profile(profile: dict, saved: bool = False) -> str:
    """Format a profile for tool output, keeping it short and stable."""
    lines = [compact_profile_summary(profile)]
    for label, key in (
        ("lint", "lint_commands"),
        ("build", "build_commands"),
        ("generated_dirs", "generated_dirs"),
        ("known_env_noise", "known_env_noise"),
    ):
        values = profile.get(key, [])
        if values:
            lines.append(f"{label}: {', '.join(values[:5])}")
    if saved:
        lines.append("saved: .nz-coder/project_profile.json")
    return "\n".join(lines)


def project_profile(save: bool = True, rebuild: bool = True) -> str:
    """Tool handler: scan and summarize the current project profile."""
    try:
        profile = build_project_profile(save=save) if rebuild else load_project_profile()
        return format_project_profile(profile, saved=save)
    except Exception as exc:
        return f"Error: {exc}"


register(
    name="project_profile",
    description=(
        "Detect the current repository profile: languages, package managers, "
        "source/test roots, and likely test/typecheck/lint commands. Returns a concise summary."
    ),
    parameters={
        "type": "object",
        "properties": {
            "save": {"type": "boolean", "description": "Save to .nz-coder/project_profile.json. Default: true."},
            "rebuild": {"type": "boolean", "description": "Re-scan instead of loading cached profile. Default: true."},
        },
    },
    handler=project_profile,
    plan_mode_allowed=True,
)
