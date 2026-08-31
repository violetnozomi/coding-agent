"""Repository intelligence tools for SWE-bench: diff_status, verify_changed_files,
read_symbol, find_symbol_callers.

These tools reduce exploration round-trips and verification noise — the two
biggest sources of agent timeout in SWE-bench Lite.
"""

from __future__ import annotations

import ast
import json
import math
import re
import subprocess
from collections import defaultdict
from pathlib import Path

from nz_coder.runtime.process.workdir import current_workdir
from nz_coder.runtime.agent.task_policy import (
    is_source_file,
    is_test_file as _policy_is_test_file,
    language_for_path,
)
from nz_coder.tools import register

# ── path safety ────────────────────────────────────────────────────────────────

EXCLUDED_DIRS = {
    ".git",
    ".nz-coder",
    ".nz-coder-runs",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".tox",
    ".eggs",
    "build",
    "dist",
    "node_modules",
}

STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "this", "that", "should", "when",
    "where", "which", "there", "their", "into", "true", "false", "none",
    "error", "failed", "failure", "test", "tests", "class", "function",
    "also", "does", "have", "not", "are", "has", "its", "can", "will",
})


def _safe_path(p: str = ".") -> Path:
    wd = Path(current_workdir())
    path = (wd / (p or ".")).resolve()
    try:
        path.relative_to(wd.resolve())
    except ValueError:
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def _run_git(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(current_workdir()),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


# ── helpers ────────────────────────────────────────────────────────────────────

def _is_test_file(path: str) -> bool:
    return _policy_is_test_file(path)


def _is_excluded(path: str) -> bool:
    parts = Path(path).parts
    return any(part in EXCLUDED_DIRS for part in parts)


def _line_numbered(lines: list[str], start_line: int) -> str:
    return "\n".join(
        f"{start_line + i:4d} | {line.rstrip()}"
        for i, line in enumerate(lines)
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Tool 1: diff_status
# ═══════════════════════════════════════════════════════════════════════════════

def diff_status() -> str:
    """Show current git diff: changed files, diff size, test modifications,
    and a recommendation for the next step.
    """
    try:
        repository = _run_git(["rev-parse", "--is-inside-work-tree"])
        if repository.returncode != 0 or repository.stdout.strip() != "true":
            from nz_coder.state.changes import (
                current_changed_files,
                render_current_change_diff,
            )

            changed_files = [
                path for path in current_changed_files() if not _is_excluded(path)
            ]
            diff_text = render_current_change_diff() if changed_files else ""
            lines = [
                "workspace_mode: non_git",
                "diff_source: change_tracker",
                f"has_non_empty_diff: {str(bool(changed_files)).lower()}",
                f"diff_chars: {len(diff_text)}",
                f"changed_files_count: {len(changed_files)}",
                "",
                "Changed files:",
            ]
            lines.extend(f"  {path}" for path in changed_files)
            if not changed_files:
                lines.append("  (none tracked in this run)")
            lines.extend([
                "",
                "Recommendation: Git is not required; use the tracked workspace diff and verification evidence.",
            ])
            return "\n".join(lines)
        # Tracked changes
        name_result = _run_git([
            "diff", "--name-only", "--",
            ".", ":!.nz-coder", ":!.nz-coder-runs",
        ])
        if name_result.returncode not in (0, 1):
            return f"Error: git diff failed (returncode={name_result.returncode}): {name_result.stderr.strip()}"
        name_only = name_result.stdout.splitlines()

        diff_text = _run_git([
            "diff", "--", ".", ":!.nz-coder", ":!.nz-coder-runs",
        ]).stdout

        # Untracked (new) files — stage them so they show up, then unstage
        # after collecting the diff, so the worktree stays clean.
        untracked_result = _run_git([
            "ls-files", "--others", "--exclude-standard",
        ])
        untracked_files: list[str] = []
        if untracked_result.returncode in (0, 1):
            untracked_files = [
                f for f in untracked_result.stdout.splitlines()
                if f and not _is_excluded(f)
            ]

        changed_files = [f for f in name_only if f and not _is_excluded(f)]
        # Merge untracked new files into the changed list
        for uf in untracked_files:
            if uf not in changed_files:
                changed_files.append(uf)
        py_files = [f for f in changed_files if f.endswith(".py")]
        language_counts: dict[str, int] = {}
        for rel in changed_files:
            lang = language_for_path(rel)
            if lang != "other":
                language_counts[lang] = language_counts.get(lang, 0) + 1
        tests_modified = any(_is_test_file(f) for f in changed_files)
        code_files = [f for f in changed_files if is_source_file(f)]
        source_files = [f for f in code_files if not _is_test_file(f)]
        untracked_chars = 0
        for rel in untracked_files:
            try:
                untracked_chars += (current_workdir() / rel).stat().st_size
            except OSError:
                pass

        has_diff = bool(diff_text.strip()) or bool(untracked_files)
        diff_chars = len(diff_text) + untracked_chars

        if has_diff and tests_modified:
            recommendation = (
                "Diff includes test files. If the task asks for tests, keep them and "
                "verify coverage; otherwise inspect whether the test edit was accidental."
            )
        elif has_diff and source_files:
            recommendation = (
                "Code diff exists. Run verify_changed_files or the narrowest relevant "
                "project check, then finalize if it satisfies the task."
            )
        elif has_diff:
            recommendation = "Non-code diff exists. Verify it matches the user request."
        else:
            recommendation = "No diff yet. Continue investigating or make the requested change."

        lines = [
            f"has_non_empty_diff: {str(has_diff).lower()}",
            f"diff_chars: {diff_chars}",
            f"changed_files_count: {len(changed_files)}",
            f"python_files_changed: {len(py_files)}",
            "languages_changed: " + (", ".join(f"{k}={v}" for k, v in sorted(language_counts.items())) or "none"),
            f"tests_modified: {str(tests_modified).lower()}",
            f"source_only: {str(bool(source_files) and not tests_modified).lower()}",
            "",
            "Changed files:",
        ]
        if changed_files:
            lines.extend(f"  {f}" for f in changed_files)
        else:
            lines.append("  (none)")
        lines.extend(["", f"Recommendation: {recommendation}"])
        return "\n".join(lines)
    except subprocess.TimeoutExpired:
        return "Error: git diff/status timed out"
    except Exception as exc:
        return f"Error: {exc}"


# ═══════════════════════════════════════════════════════════════════════════════
# Tool 2: verify_changed_files
# ═══════════════════════════════════════════════════════════════════════════════

def _changed_files_for_verification(include_tests: bool) -> list[str]:
    """返回需要低噪音验证的 changed source files。"""
    repository = _run_git(["rev-parse", "--is-inside-work-tree"])
    if repository.returncode != 0 or repository.stdout.strip() != "true":
        from nz_coder.state.changes import current_changed_files

        changed = [
            path for path in current_changed_files() if not _is_excluded(path)
        ]
    else:
        diff_result = _run_git([
            "diff", "--name-only", "--",
            ".", ":!.nz-coder", ":!.nz-coder-runs",
        ])
        if diff_result.returncode not in (0, 1):
            raise RuntimeError(
                f"git diff failed (returncode={diff_result.returncode})"
            )
        changed = [
            f for f in diff_result.stdout.splitlines()
            if f and not _is_excluded(f)
        ]

        untracked = _run_git(["ls-files", "--others", "--exclude-standard"])
        if untracked.returncode in (0, 1):
            for rel in untracked.stdout.splitlines():
                if rel and not _is_excluded(rel) and rel not in changed:
                    changed.append(rel)

    return [
        f for f in changed
        if is_source_file(f) and (include_tests or not _is_test_file(f))
    ]


def _package_json_scripts() -> dict:
    try:
        data = json.loads((current_workdir() / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    scripts = data.get("scripts", {}) if isinstance(data, dict) else {}
    return scripts if isinstance(scripts, dict) else {}


def _node_package_manager() -> str:
    if (current_workdir() / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (current_workdir() / "yarn.lock").exists():
        return "yarn"
    return "npm"


def _node_typecheck_command() -> list[str] | None:
    scripts = _package_json_scripts()
    if "typecheck" in scripts:
        manager = _node_package_manager()
        if manager == "pnpm":
            return ["pnpm", "run", "typecheck"]
        if manager == "yarn":
            return ["yarn", "typecheck"]
        return ["npm", "run", "-s", "typecheck"]
    local_tsc = current_workdir() / "node_modules" / ".bin" / "tsc"
    if local_tsc.exists() and (current_workdir() / "tsconfig.json").exists():
        return [str(local_tsc), "--noEmit"]
    return None


def _run_verifier(label: str, cmd: list[str], timeout: int) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=current_workdir(),
        )
    except FileNotFoundError:
        return False, f"FAIL {label}\ncommand not found: {cmd[0]}"
    if result.returncode == 0:
        return True, f"OK  {label}"
    detail = (result.stderr or result.stdout or "").strip()
    return False, f"FAIL {label}\n{detail}"


def verify_changed_files(include_tests: bool = False, timeout: int = 30) -> str:
    """Run low-noise language-aware checks on changed source files.

    Python uses ``py_compile`` per file. TypeScript/JavaScript uses an existing
    typecheck script or local ``tsc`` when available. Go compiles changed package
    directories with ``go test -run \'^$\'`` so tests are not executed. Rust uses
    ``cargo check`` when Cargo.toml is present.
    """
    try:
        timeout = int(timeout or 30)
        timeout = max(1, min(timeout, 120))
        files = _changed_files_for_verification(include_tests)
        if not files:
            return "OK: no changed source files requiring verification."

        rows: list[str] = []
        warnings: list[str] = []
        ok = True

        py_files = [f for f in files if language_for_path(f) == "python"]
        for rel in py_files:
            fp = current_workdir() / rel
            if not fp.exists():
                rows.append(f"SKIP {rel} (deleted file)")
                continue
            passed, row = _run_verifier(rel, ["python3", "-m", "py_compile", str(fp)], timeout)
            ok = ok and passed
            rows.append(row)

        node_files = [f for f in files if language_for_path(f) in {"javascript", "typescript"}]
        if node_files:
            cmd = _node_typecheck_command()
            if cmd:
                passed, row = _run_verifier("node typecheck", cmd, timeout)
                ok = ok and passed
                rows.append(row)
            else:
                warnings.append(
                    "WARN node: changed JS/TS files but no package typecheck script "
                    "or local node_modules/.bin/tsc was found."
                )

        go_dirs = sorted({str(Path(f).parent) or "." for f in files if language_for_path(f) == "go"})
        has_go_metadata = any(
            (current_workdir() / name).exists() for name in ("go.mod", "go.work")
        )
        if go_dirs and not has_go_metadata:
            warnings.append(
                "WARN go: changed Go files but no root go.mod or go.work was found."
            )
        else:
            for rel_dir in go_dirs:
                pkg = "." if rel_dir in {"", "."} else "./" + rel_dir.replace("\\", "/")
                passed, row = _run_verifier(
                    f"go compile {pkg}", ["go", "test", pkg, "-run", "^$"], timeout
                )
                ok = ok and passed
                rows.append(row)

        rust_files = [f for f in files if language_for_path(f) == "rust"]
        if rust_files:
            if (current_workdir() / "Cargo.toml").exists():
                passed, row = _run_verifier("cargo check", ["cargo", "check"], timeout)
                ok = ok and passed
                rows.append(row)
            else:
                warnings.append("WARN rust: changed Rust files but no Cargo.toml was found.")

        unsupported = [
            f for f in files
            if language_for_path(f) not in {"python", "javascript", "typescript", "go", "rust"}
        ]
        if unsupported:
            warnings.append(
                "WARN unsupported: no built-in verifier for " + ", ".join(unsupported[:8])
            )

        rows.extend(warnings)
        if not rows:
            return "OK: no changed source files requiring verification."
        if not ok:
            return "FAIL: changed files verification\n" + "\n".join(rows)
        if warnings:
            return "WARN: changed files verification incomplete\n" + "\n".join(rows)
        if py_files and len(py_files) == len(files):
            return "OK: py_compile changed files\n" + "\n".join(rows)
        return "OK: changed files verification\n" + "\n".join(rows)
    except subprocess.TimeoutExpired:
        return f"Error: changed-file verification timed out after {timeout}s"
    except Exception as exc:
        return f"Error: {exc}"


# ═══════════════════════════════════════════════════════════════════════════════
# Tool 3: read_symbol
# ═══════════════════════════════════════════════════════════════════════════════

def _collect_symbols(tree: ast.Module, max_depth: int = 40) -> dict[str, ast.AST]:
    """Collect functions/classes with qualified nested names, capped by depth."""
    symbols: dict[str, ast.AST] = {}
    max_depth = max(0, int(max_depth or 0))

    def visit_node(node: ast.AST, prefix: str, depth: int) -> None:
        if depth > max_depth:
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = f"{prefix}.{node.name}" if prefix else node.name
            symbols.setdefault(name, node)
            if depth < max_depth:
                visit_body(node.body, name, depth + 1)
            return

        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                visit_node(child, prefix, depth)

    def visit_body(body: list[ast.stmt], prefix: str = "", depth: int = 0) -> None:
        for node in body:
            visit_node(node, prefix, depth)

    visit_body(tree.body)
    return symbols


def _symbol_type(node: ast.AST) -> str:
    if isinstance(node, ast.ClassDef):
        return "class"
    if isinstance(node, ast.AsyncFunctionDef):
        return "async function/method"
    if isinstance(node, ast.FunctionDef):
        return "function/method"
    return type(node).__name__


def read_symbol(
    path: str,
    symbol: str = "",
    context_lines: int = 8,
    mode: str = "read",
    max_depth: int = 40,
) -> str:
    """Read/locate Python symbols via AST.

    Modes:
      - ``"read"`` (default): return line-numbered source for a specific
        function, class, method, or nested qualified symbol.  Falls back to
        listing available symbols when the requested one is not found.
      - ``"list"``: return all functions, classes, methods, and nested
        symbols in the file.  ``symbol`` is ignored in this mode.
    """
    try:
        fp = _safe_path(path)
        source = fp.read_text(encoding="utf-8", errors="replace")
        lines = source.splitlines()
        tree = ast.parse(source, filename=str(fp))
        symbols = _collect_symbols(tree, max_depth=max_depth)

        # ── List mode ────────────────────────────────────────────────────────
        if mode == "list":
            if not symbols:
                return f"{path}: (no functions, classes, or methods found)"
            out = [f"{path}: {len(symbols)} symbols"]
            for name, node in symbols.items():
                st = _symbol_type(node)
                sl = getattr(node, "lineno", "?")
                el = getattr(node, "end_lineno", "?")
                # show decorators for functions
                decorators = ""
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    dec_names = []
                    for d in (getattr(node, "decorator_list", []) or []):
                        if isinstance(d, ast.Name):
                            dec_names.append(d.id)
                        elif isinstance(d, ast.Attribute):
                            dec_names.append(f"{d.value.id}.{d.attr}" if isinstance(d.value, ast.Name) else d.attr)
                    if dec_names:
                        decorators = f"  @{', @'.join(dec_names)}"
                out.append(f"  {st:20s} lines {sl:>4}-{el:<4}  {name}{decorators}")
            return "\n".join(out)

        # ── Read mode ────────────────────────────────────────────────────────
        if symbol not in symbols:
            candidates = sorted(symbols.keys())
            shown = candidates[:60]
            header = f"symbol '{symbol}' not found in {path}\n"
            if shown:
                header += "\nAvailable symbols:\n"
                header += "\n".join(f"  - {s}" for s in shown)
                if len(candidates) > 60:
                    header += f"\n  ... ({len(candidates) - 60} more)"
            else:
                header += "\n(no functions, classes, or methods found)"
            return header

        node = symbols[symbol]
        ctx = max(0, int(context_lines or 8))
        start = max(1, getattr(node, "lineno", 1) - ctx)
        end_lineno = getattr(node, "end_lineno", getattr(node, "lineno", 1))
        end = min(len(lines), end_lineno + ctx)

        selected = lines[start - 1:end]
        return (
            f"{path}:{symbol}\n"
            f"type: {_symbol_type(node)}\n"
            f"lines: {getattr(node, 'lineno', '?')}-{end_lineno}\n\n"
            f"{_line_numbered(selected, start)}"
        )
    except SyntaxError as exc:
        return f"Error: Python syntax error in {path}: {exc}"
    except FileNotFoundError:
        return f"Error: file not found: {path}"
    except Exception as exc:
        return f"Error: {exc}"


# ═══════════════════════════════════════════════════════════════════════════════
# Legacy helper: smart_search (not registered as a tool)
# ═══════════════════════════════════════════════════════════════════════════════

_MAX_FILES_TO_SCAN = 2000


def _extract_tokens(*texts: str, limit: int = 40) -> list[str]:
    raw = "\n".join(t or "" for t in texts)

    candidates: list[str] = []
    candidates.extend(re.findall(
        r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*",
        raw,
    ))

    # Also pull keywords from quoted strings (error messages, test names)
    quoted = re.findall(r"[`'\"]([^`'\"]{4,120})[`'\"]", raw)
    for q in quoted:
        candidates.extend(re.findall(r"[A-Za-z_][A-Za-z0-9_]+", q))

    seen: set[str] = set()
    tokens: list[str] = []
    for token in candidates:
        low = token.lower()
        if len(token) < 3 or low in STOPWORDS or low in seen:
            continue
        seen.add(low)
        tokens.append(token)
        if len(tokens) >= limit:
            break
    return tokens


def _file_weight(rel: str) -> float:
    p = rel.replace("\\", "/")
    w = 1.0
    if _is_test_file(p):
        w *= 0.45
    if "/docs/" in p or p.startswith("docs/") or "/doc/" in p:
        w *= 0.35
    if "/examples/" in p or p.startswith("examples/"):
        w *= 0.50
    if p.endswith(".py"):
        w *= 1.20
    return w


def _parse_python_source(source: str, fp: Path) -> ast.AST | None:
    if fp.suffix != ".py":
        return None
    try:
        return ast.parse(source, filename=str(fp))
    except (SyntaxError, ValueError):
        return None


def _ast_summary(fp: Path, limit: int = 12, tree: ast.AST | None = None) -> list[str]:
    if tree is None:
        try:
            source = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        tree = _parse_python_source(source, fp)
    if tree is None:
        return []

    items: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            methods = [
                c.name for c in node.body
                if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            suffix = f"  methods={methods[:8]}" if methods else ""
            items.append(f"class {node.name}  line {node.lineno}{suffix}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            items.append(f"def {node.name}  line {node.lineno}")
        if len(items) >= limit:
            break
    return items


def _git_grep_pathspecs(base: Path, include: str) -> list[str]:
    """Return git-grep pathspecs for a safe workspace path and include glob."""
    include = include or "*.py"
    try:
        rel = str(base.relative_to(current_workdir())).replace("\\", "/")
    except ValueError:
        rel = "."
    if base.is_file():
        return [rel]
    if rel in ("", "."):
        return [include]
    return [f"{rel}/{include}", f"{rel}/**/{include}"]


def smart_search(
    query: str,
    failing_tests: list[str] | None = None,
    traceback: str = "",
    path: str = ".",
    include: str = "*.py",
    max_files: int = 8,
) -> str:
    """High-signal repository search for initial code exploration.

    Extracts tokens from issue text, failing tests, and traceback; greps the
    repo; ranks candidate files by relevance; and returns Python AST symbol
    summaries for top hits when parsing is available.
    """
    try:
        failing_tests = failing_tests or []
        base = _safe_path(path)
        max_files = max(1, min(int(max_files or 8), 20))

        # ── token extraction ──────────────────────────────────────────────────
        tokens = _extract_tokens(
            query or "",
            "\n".join(failing_tests),
            traceback or "",
        )
        if not tokens:
            return "Error: no useful search tokens extracted from query/tests/traceback"

        # ── grep-first strategy: use fast grep to find candidate files ────────
        # Instead of scanning every .py file, grep with the top 5 tokens
        # (files_with_matches mode) to build a short candidate list.
        # Then only read + score those candidates. This matches Claude Code's
        # GrepTool-first approach.
        candidate_paths: set[str] = set()
        pathspecs = _git_grep_pathspecs(base, include or "*.py")
        search_tokens = tokens[:5]  # use top 5 tokens for initial grep

        for token in search_tokens:
            try:
                result = subprocess.run(
                    ["git", "grep", "-l", "-e", token, "--", *pathspecs],
                    cwd=str(current_workdir()),
                    capture_output=True, text=True,
                    encoding="utf-8", errors="replace",
                    timeout=15,
                )
                # git grep returns 1 when no matches; 128/129 means command/pathspec error.
                if result.returncode not in (0, 1):
                    continue
                for line in result.stdout.splitlines():
                    line = line.replace(str(current_workdir()) + "/", "").replace(str(current_workdir()) + "\\", "")
                    if line and not _is_excluded(line):
                        candidate_paths.add(line)
            except Exception:
                pass  # timeout or error → fall through to broader collection

        # If grep found candidates, use them; otherwise fall back to scanning
        if candidate_paths:
            files = [current_workdir() / p for p in candidate_paths if (current_workdir() / p).is_file()]
        else:
            files = []
            if base.is_file():
                if base.name.endswith(".py"):
                    files = [base]
            else:
                for fp in base.rglob(include or "*.py"):
                    if len(files) >= _MAX_FILES_TO_SCAN:
                        break
                    if not fp.is_file():
                        continue
                    if _is_excluded(str(fp.relative_to(current_workdir()))):
                        continue
                    files.append(fp)

        if not files:
            return f"No files matching {include!r} found under {path!r}"

        # ── score candidate files ─────────────────────────────────────────────
        scores: dict[str, float] = defaultdict(float)
        reasons: dict[str, list[str]] = defaultdict(list)
        samples: dict[str, list[str]] = defaultdict(list)
        term_counts: dict[str, dict[str, int]] = defaultdict(dict)
        document_frequency: dict[str, int] = defaultdict(int)
        parsed_trees: dict[str, ast.AST | None] = {}
        token_lowers = [(t, t.lower()) for t in tokens]
        readable_files = 0

        files = sorted(files, key=lambda item: str(item.relative_to(current_workdir())))

        for fp in files:
            rel = str(fp.relative_to(current_workdir()))
            rel_low = rel.lower()

            try:
                file_text = fp.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            readable_files += 1
            file_lines = file_text.splitlines()
            file_low = file_text.lower()
            weight = _file_weight(rel)

            for token, low in token_lowers:
                # filename match
                if low in rel_low:
                    scores[rel] += 3.0 * weight
                    reasons[rel].append(f"filename contains: {token}")

                count = file_low.count(low)
                if count > 0:
                    term_counts[rel][token] = count
                    document_frequency[low] += 1

            # Capture sample lines without making score scale linearly by file size.
            for i, line in enumerate(file_lines, 1):
                low_line = line.lower()
                matched = False
                for token, low in token_lowers:
                    if low in low_line:
                        matched = True
                        break
                if matched and len(samples[rel]) < 6:
                    samples[rel].append(f"{i}: {line.strip()[:160]}")

            # failing test token bonus (whole file scan)
            for test_name in failing_tests:
                tn = str(test_name).lower()
                if tn and tn in file_low:
                    scores[rel] += 5.0 * weight
                    reasons[rel].append(f"contains failing test: {test_name}")

            # AST symbol name bonus
            tree = _parse_python_source(file_text, fp)
            parsed_trees[rel] = tree
            if tree is not None:
                symbols = _collect_symbols(tree)
                for sym_name in symbols:
                    sym_low = sym_name.lower()
                    if any(low in sym_low or sym_low in low for _, low in token_lowers):
                        scores[rel] += 4.0 * weight
                        reasons[rel].append(f"symbol match: {sym_name}")

        for rel, counts in term_counts.items():
            weight = _file_weight(rel)
            top_terms = sorted(counts.items(), key=lambda item: item[1], reverse=True)[:4]
            for token, count in counts.items():
                low = token.lower()
                idf = math.log((readable_files + 1) / (document_frequency[low] + 1)) + 1.0
                scores[rel] += math.log1p(count) * idf * weight
            for token, count in top_terms:
                reasons[rel].append(f"content match: {token} x{count}")

        # ── rank and format ───────────────────────────────────────────────────
        ranked = [(rel, score) for rel, score in scores.items() if score > 0]
        ranked.sort(key=lambda item: item[1], reverse=True)
        ranked = ranked[:max_files]

        if not ranked:
            return (
                "No strong candidates found.\n"
                f"Tokens extracted: {', '.join(tokens[:30])}\n"
                "Try broadening the search with a different path or include pattern."
            )

        out = [
            f"Tokens: {', '.join(tokens[:30])}",
            f"Search considered {len(files)} candidate file(s), returning top {len(ranked)}:",
            "",
        ]

        for idx, (rel, score) in enumerate(ranked, 1):
            fp = current_workdir() / rel
            ast_items = _ast_summary(fp, tree=parsed_trees.get(rel))
            reason_lines: list[str] = []
            seen: set[str] = set()
            for r in reasons.get(rel, []):
                if r not in seen:
                    reason_lines.append(r)
                    seen.add(r)
                if len(reason_lines) >= 4:
                    break

            out.append(f"{idx}. {rel}  (score: {score:.1f})")
            if reason_lines:
                out.append(f"   reasons: {'; '.join(reason_lines)}")
            if samples.get(rel):
                out.append("   matching lines:")
                out.extend(f"     {s}" for s in samples[rel][:3])
            if ast_items:
                out.append("   symbols:")
                out.extend(f"     {s}" for s in ast_items[:8])
            out.append("")

        out.append(
            "Next: use read_symbol on the top source candidate symbol, "
            "or read_file around the most relevant matching line."
        )
        return "\n".join(out).rstrip()
    except ValueError as exc:
        return f"Error: {exc}"
    except OSError as exc:
        return f"Error: filesystem error during smart_search: {exc}"
    except Exception as exc:
        return f"Error: unexpected smart_search failure ({type(exc).__name__}): {exc}"


# ═══════════════════════════════════════════════════════════════════════════════
# Tool: find_symbol_callers — AST-based cross-file reference search
# ═══════════════════════════════════════════════════════════════════════════════

def _find_callers_ast(tree: ast.AST, symbol: str, filepath: str) -> list[dict]:
    """Walk an AST and find all references to *symbol*.

    Returns list of {file, line, context} dicts.
    """
    results: list[dict] = []
    symbol_lower = symbol.lower()

    def callable_name(node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        if isinstance(node, ast.Call):
            return callable_name(node.func)
        return None

    class ReferenceVisitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            func_name = callable_name(node.func)
            if func_name and func_name.lower() == symbol_lower:
                results.append({
                    "file": filepath,
                    "line": getattr(node, "lineno", 0),
                    "context": f"call: {func_name}(...)",
                })
            for arg in node.args:
                self.visit(arg)
            for keyword in node.keywords:
                self.visit(keyword.value)

        def visit_Attribute(self, node: ast.Attribute) -> None:
            if node.attr.lower() == symbol_lower:
                results.append({
                    "file": filepath,
                    "line": getattr(node, "lineno", 0),
                    "context": f"attr: .{node.attr}",
                })
            self.visit(node.value)

        def visit_Name(self, node: ast.Name) -> None:
            if node.id.lower() == symbol_lower:
                results.append({
                    "file": filepath,
                    "line": getattr(node, "lineno", 0),
                    "context": f"name: {node.id}",
                })

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_definition(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_definition(node)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self._visit_definition(node)
            for base in node.bases:
                self.visit(base)
            for keyword in node.keywords:
                self.visit(keyword.value)

        def _visit_definition(
            self,
            node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
        ) -> None:
            for dec in (getattr(node, "decorator_list", []) or []):
                dec_name = callable_name(dec)
                if dec_name and dec_name.lower() == symbol_lower:
                    results.append({
                        "file": filepath,
                        "line": getattr(dec, "lineno", getattr(node, "lineno", 0)),
                        "context": f"decorator: @{dec_name} on {getattr(node, 'name', '?')}",
                    })
                    break
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.visit(node.args)
                if node.returns is not None:
                    self.visit(node.returns)
            for stmt in node.body:
                self.visit(stmt)

    ReferenceVisitor().visit(tree)

    return results


def find_symbol_callers(
    path: str,
    symbol: str,
    include: str = "*.py",
    max_results: int = 40,
    source_only: bool = True,
) -> str:
    """Find all references to *symbol* across Python files via AST.

    Walks every .py file under *path*, parses the AST, and reports every
    call site, attribute reference, decorator usage, and bare name reference
    to *symbol*.  Much faster than grep because it only matches actual Python
    identifiers (not comments, strings, or partial matches).

    Args:
        path: Directory to search.
        symbol: Symbol name to find callers of (e.g. ``'parse_http_date'``).
        include: Glob for files. Default: ``'*.py'``.
        max_results: Max references to return. Default: 40.
        source_only: Exclude test files. Default: True.
    """
    try:
        base = _safe_path(path)
        max_results = max(1, min(int(max_results or 40), 100))

        # Collect candidate .py files
        files: list[Path] = []
        if base.is_file():
            files = [base]
        else:
            for fp in base.rglob(include or "*.py"):
                if len(files) >= 500:  # cap total files to scan
                    break
                if not fp.is_file():
                    continue
                rel = str(fp.relative_to(current_workdir()))
                if _is_excluded(rel):
                    continue
                if source_only and _is_test_file(rel):
                    continue
                files.append(fp)

        if not files:
            return f"No Python files found under {path!r}"

        all_refs: list[dict] = []
        skipped_files: list[str] = []
        for fp in files:
            rel = str(fp.relative_to(current_workdir()))
            try:
                source = fp.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(source, filename=str(fp))
            except (SyntaxError, ValueError) as exc:
                skipped_files.append(f"{rel}: parse error: {exc}")
                continue
            except OSError as exc:
                skipped_files.append(f"{rel}: read error: {exc}")
                continue

            refs = _find_callers_ast(tree, symbol, rel)
            all_refs.extend(refs)
            if len(all_refs) >= max_results:
                break

        if not all_refs:
            suffix = f"; skipped {len(skipped_files)} file(s)" if skipped_files else ""
            return f"No references to '{symbol}' found in {path}{suffix}"

        sliced = all_refs[:max_results]
        out = [f"Found {len(all_refs)} reference(s) to '{symbol}':", ""]
        for ref in sliced:
            out.append(f"  {ref['file']}:{ref['line']}  {ref['context']}")

        if len(all_refs) > max_results:
            out.append(f"  ... ({len(all_refs) - max_results} more)")
        if skipped_files:
            out.append(f"  skipped {len(skipped_files)} unparsable/unreadable file(s)")
        return "\n".join(out)
    except ValueError as exc:
        return f"Error: {exc}"
    except OSError as exc:
        return f"Error: filesystem error during find_symbol_callers: {exc}"
    except Exception as exc:
        return f"Error: unexpected find_symbol_callers failure ({type(exc).__name__}): {exc}"


# ═══════════════════════════════════════════════════════════════════════════════
# Register
# ═══════════════════════════════════════════════════════════════════════════════

register(
    name="diff_status",
    description=(
        "Show current git diff status: which files changed, diff size, whether "
        "tests were modified, and whether to verify/finalize or keep searching."
    ),
    parameters={"type": "object", "properties": {}},
    handler=diff_status,
    execution="read",
)

register(
    name="verify_changed_files",
    description=(
        "Run low-noise language-aware checks on changed source files: Python "
        "py_compile, JS/TS typecheck when configured, Go package check, and "
        "Rust cargo check. Excludes test files by default."
    ),
    parameters={
        "type": "object",
        "properties": {
            "include_tests": {
                "type": "boolean",
                "description": "Include changed test files. Default: false.",
            },
            "timeout": {
                "type": "integer",
                "description": "Per-file timeout in seconds. Default: 30, max: 120.",
            },
        },
    },
    handler=verify_changed_files,
    side_effect="mutates-shell",
)

register(
    name="read_symbol",
    description=(
        "Read or list Python symbols via AST. Mode 'read' (default) returns "
        "line-numbered source for a function/class/method or nested symbol. "
        "Mode 'list' returns all Python symbols in the file, including nested "
        "symbols. Use this instead of grep_search + read_file when you know "
        "the target symbol name or want a file overview."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative Python file path.",
            },
            "symbol": {
                "type": "string",
                "description": "Symbol name (for mode='read'): 'func', 'ClassName', 'ClassName.method', or nested qualified name. Ignored in mode='list'.",
            },
            "context_lines": {
                "type": "integer",
                "description": "Lines before/after the symbol (mode='read' only). Default: 8.",
            },
            "mode": {
                "type": "string",
                "enum": ["read", "list"],
                "description": "Mode: 'read' returns source for a symbol (default), 'list' returns all symbols.",
            },
            "max_depth": {
                "type": "integer",
                "description": "Max nested symbol depth to collect. Default: 40.",
            },
        },
        "required": ["path"],
    },
    handler=read_symbol,
    execution="read",
)

register(
    name="find_symbol_callers",
    description=(
        "Find all references to a Python symbol across the repository via AST. "
        "Reports call sites, attribute references, decorator usages, and bare "
        "name references. Much faster than grep for Python code because it only "
        "matches real identifiers (not comments, strings, or partial matches)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Directory or file to search in.",
            },
            "symbol": {
                "type": "string",
                "description": "Symbol name to find callers of, e.g. 'parse_http_date'.",
            },
            "include": {
                "type": "string",
                "description": "Glob for Python files. Default: '*.py'.",
            },
            "max_results": {
                "type": "integer",
                "description": "Max references to return. Default: 40, max: 100.",
            },
            "source_only": {
                "type": "boolean",
                "description": "Exclude test files. Default: true.",
            },
        },
        "required": ["path", "symbol"],
    },
    handler=find_symbol_callers,
    execution="read",
)
