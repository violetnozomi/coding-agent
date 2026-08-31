"""Deterministic artifact hints available before repository indexing is ready."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


_IGNORED_PARTS = frozenset({
    ".git", ".hg", ".mypy_cache", ".nz-coder", ".pytest_cache",
    ".ruff_cache", ".tox", ".venv", "__pycache__", "build", "dist",
    "node_modules", "venv",
})
_PATH_RE = re.compile(
    r"(?<![\w.-])(?:\.?[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+(?:\.[A-Za-z0-9]+)?"
)
_BASENAME_FILE_RE = re.compile(
    r"(?<![\w/.-])([A-Za-z0-9_.-]+\."
    r"(?:py|pyi|js|jsx|mjs|cjs|ts|tsx|go|rs|java|rb|php|c|cc|cpp|h|hpp|"
    r"md|rst|txt|json|ya?ml))\b",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_TEST_MARKERS = ("test", "tests", "pytest", "测试", "覆盖")
_DOC_MARKERS = ("readme", "documentation", "docs", "document", "文档", "说明")
_BEHAVIOR_MARKERS = (
    "add", "fix", "implement", "support", "update", "change", "refactor",
    "新增", "修复", "实现", "支持", "完善", "修改", "更新", "重构",
)


@dataclass(frozen=True)
class BootstrapArtifact:
    """One safe workspace-relative artifact hypothesis."""

    path: str
    confidence: float
    role: str
    required: bool
    reason: str


@dataclass(frozen=True)
class BootstrapArtifactResolution:
    """Required artifacts and softer implementation-bundle candidates."""

    artifacts: tuple[BootstrapArtifact, ...] = ()

    @property
    def required_paths(self) -> tuple[str, ...]:
        return tuple(item.path for item in self.artifacts if item.required)

    @property
    def candidate_paths(self) -> tuple[str, ...]:
        return tuple(item.path for item in self.artifacts if not item.required)

    @property
    def artifact_count(self) -> int:
        return len(self.required_paths)

    @property
    def candidate_count(self) -> int:
        return len(self.candidate_paths)

    def required_for(self, role: str) -> tuple[str, ...]:
        return tuple(
            item.path
            for item in self.artifacts
            if item.required and item.role == role
        )


def resolve_bootstrap_artifacts(
    task_text: str,
    *,
    workspace: str | Path,
    max_files: int = 5000,
    explicit_path_allowlist: tuple[str, ...] | None = None,
) -> BootstrapArtifactResolution:
    """Resolve high-confidence artifacts using only bounded filesystem facts."""
    root = Path(workspace).resolve()
    text = " ".join(str(task_text or "").split())
    lowered = text.casefold()
    if not text or not root.is_dir():
        return BootstrapArtifactResolution()

    paths = _workspace_files(root, max_files=max_files)
    allowed_explicit_paths = (
        None
        if explicit_path_allowlist is None
        else {
            normalized
            for raw in explicit_path_allowlist
            if (normalized := _safe_relative(raw))
        }
    )
    by_name: dict[str, list[str]] = {}
    by_stem: dict[str, list[str]] = {}
    for path in paths:
        pure = PurePosixPath(path)
        by_name.setdefault(pure.name.casefold(), []).append(path)
        by_stem.setdefault(pure.stem.casefold(), []).append(path)

    resolved: dict[str, BootstrapArtifact] = {}

    def add(
        path: str,
        confidence: float,
        role: str,
        required: bool,
        reason: str,
        *,
        allow_missing: bool = False,
    ) -> None:
        if path not in paths and not allow_missing:
            return
        current = resolved.get(path)
        candidate = BootstrapArtifact(path, confidence, role, required, reason)
        if current is None or (required, confidence) > (current.required, current.confidence):
            resolved[path] = candidate

    normalized_text = text.replace("\\", "/")
    explicit_mentions = list(_PATH_RE.findall(normalized_text))
    if allowed_explicit_paths is not None:
        explicit_mentions.extend(_BASENAME_FILE_RE.findall(normalized_text))
    for match in dict.fromkeys(explicit_mentions):
        normalized = _safe_relative(match)
        if (
            normalized
            and (
                allowed_explicit_paths is None
                or normalized in allowed_explicit_paths
            )
            and (
            normalized in paths or _looks_like_explicit_file(normalized)
            )
        ):
            add(
                normalized,
                1.0,
                _role_for_path(normalized),
                True,
                "explicit path",
                allow_missing=True,
            )

    # Path components are handled only by the explicit-path authority above.
    # Do not feed traceback or verification-command paths back into semantic
    # surface inference, where a distant verb such as "fix" could otherwise
    # promote `src/_pytest/runner.py` to a required mutation artifact.
    semantic_text = _PATH_RE.sub(" ", normalized_text)
    if allowed_explicit_paths is not None:
        semantic_text = _BASENAME_FILE_RE.sub(" ", semantic_text)
    words = tuple(dict.fromkeys(
        word.casefold() for word in _WORD_RE.findall(semantic_text)
    ))
    mentions_tests = any(marker in lowered for marker in _TEST_MARKERS)
    mentions_docs = any(marker in lowered for marker in _DOC_MARKERS)

    if mentions_docs:
        for name in ("readme.md", "readme.rst", "readme.txt", "readme"):
            selected = _select_nearest(by_name.get(name, []), words)
            if selected:
                add(selected, 0.95, "docs", True, "explicit documentation request")
                break

    mentioned_surfaces = [
        word for word in words
        if word not in {"api", "readme", "test", "tests", "pytest"}
        and (
            word in by_stem
            or f"test_{word}.py" in by_name
            or f"{word}_test.py" in by_name
        )
    ]
    mentioned_surfaces = list(dict.fromkeys(mentioned_surfaces))

    for surface in mentioned_surfaces:
        source_matches = [
            path for path in by_stem.get(surface, [])
            if "/tests/" not in f"/{path}"
            and not PurePosixPath(path).name.startswith("test_")
        ]
        if len(source_matches) == 1:
            source = source_matches[0]
            if _surface_has_behavior_action(lowered, surface):
                add(source, 0.95, "behavior", True, "unique action surface")
            else:
                add(source, 0.75, "candidate", False, "mentioned source surface")

        if mentions_tests:
            names = (f"test_{surface}.py", f"{surface}_test.py")
            matches = [path for name in names for path in by_name.get(name, [])]
            selected = _select_nearest(matches, words)
            if selected:
                add(selected, 0.90, "test", True, "requested surface test")

    # A request may describe the behavior first and name its implementation
    # surface only in the later test clause (for example, "support month names;
    # add parser/scheduler/CLI tests").  If no stronger action-local match was
    # found, the first uniquely resolvable requested surface is the conservative
    # primary implementation artifact.  Remaining surfaces stay soft candidates.
    has_required_behavior = any(
        item.required and item.role == "behavior" for item in resolved.values()
    )
    if not has_required_behavior and any(marker in lowered for marker in _BEHAVIOR_MARKERS):
        for surface in mentioned_surfaces:
            source_matches = [
                path for path in by_stem.get(surface, [])
                if "/tests/" not in f"/{path}"
                and not PurePosixPath(path).name.startswith("test_")
            ]
            if len(source_matches) == 1:
                add(
                    source_matches[0],
                    0.90,
                    "behavior",
                    True,
                    "primary requested test surface",
                )
                break

    if "cli" in words:
        for path in by_stem.get("cli", []):
            if "/tests/" not in f"/{path}":
                add(path, 0.75, "candidate", False, "mentioned CLI surface")

    for name in ("__main__.py", "main.py"):
        selected = _select_nearest(by_name.get(name, []), words)
        if selected:
            add(selected, 0.60, "candidate", False, "project entry point")

    ordered = sorted(
        resolved.values(),
        key=lambda item: (
            not item.required,
            {"behavior": 0, "test": 1, "docs": 2}.get(item.role, 3),
            -item.confidence,
            item.path,
        ),
    )
    return BootstrapArtifactResolution(tuple(ordered))


def _workspace_files(root: Path, *, max_files: int) -> set[str]:
    result: set[str] = set()
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(
            name for name in dirs
            if name not in _IGNORED_PARTS and not name.startswith(".nz-coder")
        )
        current_path = Path(current)
        for name in sorted(files):
            try:
                relative = (current_path / name).relative_to(root).as_posix()
            except ValueError:
                continue
            result.add(relative)
            if len(result) >= max(1, int(max_files)):
                return result
    return result


def _safe_relative(value: str) -> str:
    path = PurePosixPath(str(value or "").strip().replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        return ""
    normalized = path.as_posix()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _looks_like_explicit_file(path: str) -> bool:
    """Allow a missing explicit file, but reject prose ratios and directories."""
    name = PurePosixPath(path).name
    return bool(
        name.startswith(".")
        or ("." in name and not name.endswith("."))
    )


def _role_for_path(path: str) -> str:
    name = PurePosixPath(path).name.casefold()
    if name.startswith("readme") or "/docs/" in f"/{path.casefold()}/":
        return "docs"
    if name.startswith("test_") or name.endswith("_test.py") or "/tests/" in f"/{path}":
        return "test"
    return "behavior"


def _select_nearest(paths: list[str], words: tuple[str, ...]) -> str:
    if not paths:
        return ""
    package_words = set(words)
    return sorted(
        paths,
        key=lambda path: (
            -sum(part.casefold() in package_words for part in PurePosixPath(path).parts),
            len(PurePosixPath(path).parts),
            path,
        ),
    )[0]


def _surface_has_behavior_action(text: str, surface: str) -> bool:
    index = text.find(surface)
    if index < 0:
        return False
    start = max(text.rfind(";", 0, index), text.rfind("；", 0, index), text.rfind("。", 0, index))
    endings = [
        position for delimiter in (";", "；", "。")
        if (position := text.find(delimiter, index)) >= 0
    ]
    end = min(endings) if endings else len(text)
    window = text[start + 1:end]
    if any(marker in window for marker in _TEST_MARKERS):
        return False
    return any(marker in window for marker in _BEHAVIOR_MARKERS)


__all__ = [
    "BootstrapArtifact",
    "BootstrapArtifactResolution",
    "resolve_bootstrap_artifacts",
]
