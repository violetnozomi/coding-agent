"""Shared InfCode-style ripgrep search producers and process lifecycle."""
from __future__ import annotations

import fnmatch
import json
import math
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from nz_coder.foundation.subprocess_env import build_sanitized_subprocess_env
from nz_coder.protocol.public_error import PublicInputError


class RipgrepCancelled(Exception):
    """Raised after a ripgrep producer has settled cooperative cancellation."""


# Compatibility name retained for A082 callers. Both producers use one cancel type.
RipgrepFilesCancelled = RipgrepCancelled


@dataclass(frozen=True)
class RipgrepFilesResult:
    """One consumer-bounded file window and whether an additional row existed."""

    files: tuple[str, ...]
    truncated: bool
    used_ripgrep: bool


@dataclass(frozen=True)
class RipgrepSearchMatch:
    """Validated data from one ripgrep JSON match event."""

    path: str
    text: str
    line: int
    absolute_offset: int
    submatches: tuple[dict, ...]


@dataclass(frozen=True)
class RipgrepSearchResult:
    """Collected match events and ripgrep's code-2 partial marker."""

    items: tuple[RipgrepSearchMatch, ...]
    partial: bool


@dataclass(frozen=True)
class _RipgrepProcessResult:
    """Settled process outcome shared by files and JSON search consumers."""

    code: int
    stderr: str
    stopped_early: bool


def _stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except OSError:
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        process.wait()


def _cancelled(cancel_event: threading.Event | None) -> bool:
    return cancel_event is not None and cancel_event.is_set()


def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
    if _cancelled(cancel_event):
        raise RipgrepCancelled


def _non_negative_integer(value, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"invalid ripgrep {field}")
    return value


def _path_text(value, field: str) -> str:
    if not isinstance(value, dict) or not isinstance(value.get("text"), str):
        raise ValueError(f"invalid ripgrep {field}")
    return value["text"]


def _validate_time_stats(value, field: str) -> None:
    if not isinstance(value, dict) or not isinstance(value.get("human"), str):
        raise ValueError(f"invalid ripgrep {field}")
    _non_negative_integer(value.get("secs"), f"{field} secs")
    _non_negative_integer(value.get("nanos"), f"{field} nanos")


def _validate_stats(value, field: str = "stats") -> None:
    if not isinstance(value, dict):
        raise ValueError(f"invalid ripgrep {field}")
    _validate_time_stats(value.get("elapsed"), f"{field} elapsed")
    for name in (
        "searches",
        "searches_with_match",
        "bytes_searched",
        "bytes_printed",
        "matched_lines",
        "matches",
    ):
        _non_negative_integer(value.get(name), f"{field} {name}")


def decode_ripgrep_event(line: str) -> RipgrepSearchMatch | None:
    """Decode the full begin/match/end/summary JSON union used by InfCode."""
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as error:
        raise PublicInputError("invalid ripgrep JSON output") from error
    if not isinstance(payload, dict):
        raise PublicInputError("invalid ripgrep JSON event")
    event_type = payload.get("type")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise PublicInputError("invalid ripgrep JSON event data")
    if event_type == "begin":
        _path_text(data.get("path"), "begin path")
        return None
    if event_type == "end":
        _path_text(data.get("path"), "end path")
        binary_offset = data.get("binary_offset")
        if binary_offset is not None:
            _non_negative_integer(binary_offset, "binary offset")
        _validate_stats(data.get("stats"), "end stats")
        return None
    if event_type == "summary":
        _validate_time_stats(data.get("elapsed_total"), "summary elapsed_total")
        _validate_stats(data.get("stats"), "summary stats")
        return None
    if event_type != "match":
        raise PublicInputError("invalid ripgrep JSON event type")
    path = _path_text(data.get("path"), "match path")
    text = _path_text(data.get("lines"), "match lines")
    raw_submatches = data.get("submatches")
    if not isinstance(raw_submatches, list):
        raise ValueError("invalid ripgrep submatches")
    submatches: list[dict] = []
    for item in raw_submatches:
        if not isinstance(item, dict):
            raise ValueError("invalid ripgrep submatch")
        match = item.get("match")
        submatches.append({
            "text": _path_text(match, "submatch text"),
            "start": _non_negative_integer(item.get("start"), "submatch start"),
            "end": _non_negative_integer(item.get("end"), "submatch end"),
        })
    return RipgrepSearchMatch(
        path=re.sub(r"^\.[\\/]", "", path),
        text=text,
        line=_non_negative_integer(data.get("line_number"), "line number"),
        absolute_offset=_non_negative_integer(
            data.get("absolute_offset"),
            "absolute offset",
        ),
        submatches=tuple(submatches),
    )


def _run_ripgrep_lines(
    cwd: Path,
    args: list[str],
    *,
    on_line: Callable[[str], bool],
    cancel_event: threading.Event | None,
    timeout: float,
    reader_name: str,
) -> _RipgrepProcessResult:
    """Run one rg process; a true callback result requests settled early stop."""
    duration = float(timeout)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("ripgrep timeout must be a positive finite number")
    environment = build_sanitized_subprocess_env()
    environment.pop("RIPGREP_CONFIG_PATH", None)
    _raise_if_cancelled(cancel_event)
    lines: queue.Queue[str] = queue.Queue(maxsize=128)
    reader_done = threading.Event()
    reader_stop = threading.Event()
    with tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(
            args,
            cwd=str(cwd),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=stderr,
        )
        assert process.stdout is not None

        def read_stdout() -> None:
            try:
                for raw_line in iter(process.stdout.readline, b""):
                    value = raw_line.decode("utf-8", errors="replace").strip()
                    if not value:
                        continue
                    while not reader_stop.is_set():
                        try:
                            lines.put(value, timeout=0.02)
                            break
                        except queue.Full:
                            continue
            finally:
                reader_done.set()

        reader = threading.Thread(
            target=read_stdout,
            name=reader_name,
            daemon=True,
        )
        reader.start()
        deadline = time.monotonic() + duration
        stopped_early = False
        try:
            while True:
                _raise_if_cancelled(cancel_event)
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(args, timeout)
                try:
                    if on_line(lines.get(timeout=0.02)):
                        stopped_early = True
                        break
                except queue.Empty:
                    if reader_done.is_set() and lines.empty():
                        break
            if stopped_early:
                reader_stop.set()
                _stop_process(process)
            else:
                process.wait(timeout=max(0.001, deadline - time.monotonic()))
        except BaseException:
            reader_stop.set()
            _stop_process(process)
            raise
        finally:
            reader_stop.set()
            try:
                process.stdout.close()
            except OSError:
                pass
            reader.join()
        stderr.seek(0)
        error = stderr.read().decode("utf-8", errors="replace").strip()
        code = process.returncode
        if code is None:
            raise RuntimeError("ripgrep process did not settle")
    _raise_if_cancelled(cancel_event)
    return _RipgrepProcessResult(code, error, stopped_early)


def _expand_braces(pattern: str, limit: int = 64) -> tuple[str, ...]:
    values = [pattern]
    while len(values) < limit:
        expanded = False
        next_values: list[str] = []
        for value in values:
            left = value.find("{")
            right = value.find("}", left + 1) if left >= 0 else -1
            if left < 0 or right < 0 or "," not in value[left + 1:right]:
                next_values.append(value)
                continue
            for choice in value[left + 1:right].split(","):
                next_values.append(value[:left] + choice + value[right + 1:])
                if len(next_values) >= limit:
                    break
            expanded = True
            if len(next_values) >= limit:
                break
        values = next_values
        if not expanded:
            break
    return tuple(values[:limit])


def _glob_matches(path: Path, pattern: str) -> bool:
    normalized = pattern.replace("\\", "/")
    value = path.as_posix()
    for expanded in _expand_braces(normalized):
        if "/" not in expanded:
            if fnmatch.fnmatchcase(path.name, expanded):
                return True
            continue
        candidates = {expanded}
        current = expanded
        while "**/" in current:
            current = current.replace("**/", "", 1)
            candidates.add(current)
        if any(fnmatch.fnmatchcase(value, item) for item in candidates):
            return True
    return False


def _selected_by_patterns(path: Path, patterns: tuple[str, ...]) -> bool:
    # Ripgrep glob rules are ordered and the last matching rule wins. A list
    # containing a positive glob behaves as an allow-list for unmatched paths.
    has_positive = any(not item.startswith("!") for item in patterns)
    selected = not has_positive
    for item in patterns:
        excluded = item.startswith("!")
        pattern = item[1:] if excluded else item
        if _glob_matches(path, pattern):
            selected = not excluded
    return selected


def _iter_fallback_paths(
    cwd: Path,
    *,
    hidden: bool,
    follow: bool,
    max_depth: int | None,
    cancel_event: threading.Event | None,
):
    for root, directories, filenames in os.walk(cwd, followlinks=follow):
        _raise_if_cancelled(cancel_event)
        root_path = Path(root)
        depth = len(root_path.relative_to(cwd).parts)
        if max_depth is not None and depth >= max_depth:
            directories[:] = []
        if not hidden:
            directories[:] = [item for item in directories if not item.startswith(".")]
            filenames = [item for item in filenames if not item.startswith(".")]
        for filename in filenames:
            _raise_if_cancelled(cancel_event)
            candidate = root_path / filename
            if not follow and candidate.is_symlink():
                continue
            yield candidate


def _fallback_files(
    cwd: Path,
    *,
    patterns: tuple[str, ...],
    hidden: bool,
    follow: bool,
    max_depth: int | None,
    limit: int,
    exclude: Callable[[str], bool] | None,
    cancel_event: threading.Event | None,
) -> RipgrepFilesResult:
    # This base exclusion precedes user patterns, matching filesArgs(). A
    # later positive user glob can therefore re-include matching .git files.
    ordered_patterns = ("!.git/*", *patterns)
    files: list[str] = []
    for candidate in _iter_fallback_paths(
        cwd,
        hidden=hidden,
        follow=follow,
        max_depth=max_depth,
        cancel_event=cancel_event,
    ):
        _raise_if_cancelled(cancel_event)
        try:
            relative = candidate.relative_to(cwd)
        except ValueError:
            continue
        if not _selected_by_patterns(relative, ordered_patterns):
            continue
        value = relative.as_posix()
        if exclude is not None and exclude(value):
            continue
        files.append(value)
        if len(files) > limit:
            return RipgrepFilesResult(tuple(files[:limit]), True, False)
    _raise_if_cancelled(cancel_event)
    return RipgrepFilesResult(tuple(files), False, False)


def list_ripgrep_files(
    cwd: str | Path,
    *,
    patterns: tuple[str, ...] = (),
    hidden: bool = True,
    follow: bool = False,
    max_depth: int | None = None,
    limit: int,
    exclude: Callable[[str], bool] | None = None,
    cancel_event: threading.Event | None = None,
    timeout: float = 30.0,
) -> RipgrepFilesResult:
    """Return the first consumer-visible file window from `rg --files`.

    Filtering through ``exclude`` occurs before the ``limit + 1`` truncation
    probe, matching stream ``filter(...).take(limit)`` consumers such as Skill.
    """
    base = Path(cwd).resolve()
    if not base.is_dir():
        raise FileNotFoundError(f"No such directory: {base}")
    if type(limit) is not int or limit < 1:
        raise ValueError("ripgrep file limit must be a positive integer")
    if max_depth is not None and (type(max_depth) is not int or max_depth < 0):
        raise ValueError("ripgrep max_depth must be a non-negative integer")
    _raise_if_cancelled(cancel_event)
    binary = shutil.which("rg")
    if not binary:
        return _fallback_files(
            base,
            patterns=patterns,
            hidden=hidden,
            follow=follow,
            max_depth=max_depth,
            limit=limit,
            exclude=exclude,
            cancel_event=cancel_event,
        )
    args = [binary, "--no-config", "--files", "--glob=!.git/*"]
    if follow:
        args.append("--follow")
    if hidden:
        args.append("--hidden")
    else:
        args.append("--glob=!.*")
    if max_depth is not None:
        args.append(f"--max-depth={max_depth}")
    args.extend(f"--glob={pattern}" for pattern in patterns)
    args.append(".")
    files: list[str] = []

    def consume(value: str) -> bool:
        value = re.sub(r"^\.[\\/]", "", value)
        if exclude is not None and exclude(value):
            return False
        files.append(value)
        return len(files) > limit

    outcome = _run_ripgrep_lines(
        base,
        args,
        on_line=consume,
        cancel_event=cancel_event,
        timeout=timeout,
        reader_name="nz-rg-files-reader",
    )
    if not outcome.stopped_early and outcome.code not in (0, 1):
        raise RuntimeError(
            outcome.stderr or f"ripgrep failed with code {outcome.code}"
        )
    _raise_if_cancelled(cancel_event)
    return RipgrepFilesResult(
        tuple(files[:limit]),
        outcome.stopped_early,
        True,
    )


def search_ripgrep(
    cwd: str | Path,
    pattern: str,
    *,
    patterns: tuple[str, ...] = (),
    limit: int | None = None,
    follow: bool = False,
    files: tuple[str, ...] | None = None,
    case_insensitive: bool = False,
    cancel_event: threading.Event | None = None,
    timeout: float = 30.0,
) -> RipgrepSearchResult:
    """Run InfCode's strict `rg --json` search producer."""
    base = Path(cwd).resolve()
    if not base.is_dir():
        raise FileNotFoundError(f"No such directory: {base}")
    if not isinstance(pattern, str) or not pattern:
        raise ValueError("ripgrep search pattern is required")
    if limit is not None and (type(limit) is not int or limit < 0):
        raise ValueError("ripgrep search limit must be a non-negative integer")
    _raise_if_cancelled(cancel_event)
    binary = shutil.which("rg")
    if not binary:
        raise FileNotFoundError("ripgrep")
    args = [
        binary,
        "--no-config",
        "--json",
        "--hidden",
        "--glob=!.git/*",
        "--no-messages",
    ]
    if follow:
        args.append("--follow")
    args.extend(f"--glob={item}" for item in patterns)
    if limit:
        args.append(f"--max-count={limit}")
    if case_insensitive:
        args.append("--ignore-case")
    targets = files if files is not None else (".",)
    args.extend(["--", pattern, *targets])
    matches: list[RipgrepSearchMatch] = []

    def consume(value: str) -> bool:
        event = decode_ripgrep_event(value)
        if event is not None:
            matches.append(event)
        return False

    outcome = _run_ripgrep_lines(
        base,
        args,
        on_line=consume,
        cancel_event=cancel_event,
        timeout=timeout,
        reader_name="nz-rg-search-reader",
    )
    if outcome.code not in (0, 1, 2):
        raise RuntimeError(
            outcome.stderr or f"ripgrep failed with code {outcome.code}"
        )
    if outcome.code == 1:
        return RipgrepSearchResult((), False)
    return RipgrepSearchResult(tuple(matches), outcome.code == 2)
