"""项目级子工作区准备：借用依赖、私有资源和明确的可用性回执。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path

from nz_coder.foundation.subprocess_env import (
    build_sanitized_subprocess_env,
    UnsafeSubprocessEnvironment,
)
from nz_coder.protocol.public_error import PublicError
from nz_coder.runtime.process.execution_environment import ExecutionEnvironment
from nz_coder.runtime.process.preparation import (
    PreparationFailure,
    PreparationInterrupted,
)
from nz_coder.runtime.worktree.setup import _create_symlinks

_FIELDS = {
    "copy_files",
    "symlink_directories",
    "owned_directories",
    "environment",
    "setup",
    "checks",
    "inputs",
    "private",
    "read_only_mode",
    "submodules",
}
_PROFILE = {
    "environment",
    "setup",
    "checks",
    "symlink_directories",
    "owned_directories",
}
_INTERNAL = {".git", ".nz-coder", ".nz-coder-runs", "__pycache__"}


def _fail(code, message):
    return PreparationFailure(PublicError(code, message))


def _relative(name):
    path = Path(name)
    if (
        not isinstance(name, str)
        or not name
        or path.is_absolute()
        or any(p in {".git", ".."} for p in path.parts)
        or path == Path(".")
    ):
        raise _fail(
            "preparation_config_invalid",
            "Resource paths must be relative workspace paths.",
        )
    return path


def _inside(root, name):
    path = root / _relative(name)
    if not path.resolve().is_relative_to(root.resolve()):
        raise _fail(
            "preparation_path_invalid", "Resource path escapes its workspace: " + name
        )
    return path


def source_manifest(root: Path, excluded=(), *, paths=None) -> dict:
    """内容清单不跟随链接；生成物只能按显式归属排除。"""
    root = Path(root)
    excludes = tuple(Path(p).as_posix().rstrip("/") for p in excluded)
    result = {}

    def omitted(name):
        return any(name == p or name.startswith(p + "/") for p in excludes)

    def candidates():
        if paths is not None:
            yield from (root / _relative(name) for name in sorted(paths))
            return
        for current, dirs, files in os.walk(root, followlinks=False):
            current = Path(current)
            dirs[:] = sorted(
                name
                for name in dirs
                if name not in _INTERNAL
                and not omitted((current / name).relative_to(root).as_posix())
            )
            yield from (current / name for name in dirs[:] + sorted(files))
            dirs[:] = [name for name in dirs if not (current / name).is_symlink()]

    for path in candidates():
        rel = path.relative_to(root).as_posix()
        if any(part in _INTERNAL for part in Path(rel).parts) or omitted(rel):
            continue
        if path.is_symlink():
            result[rel] = {
                "kind": "symlink",
                "sha256": hashlib.sha256(os.fsencode(os.readlink(path))).hexdigest(),
            }
        elif path.is_file():
            if not path.resolve().is_relative_to(root.resolve()):
                raise _fail(
                    "preparation_path_invalid", "Source path escapes its workspace."
                )
            result[rel] = {
                "kind": "file",
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
    return result


@dataclass(frozen=True)
class WorkspacePreparationSpec:
    value: dict

    @classmethod
    def from_snapshot(cls, snapshot):
        raw = (
            snapshot.get("NZ_SUBAGENT_WORKSPACE_JSON", "{}")
            if snapshot is not None
            else "{}"
        )
        try:
            value = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise _fail(
                "preparation_config_invalid", "Invalid NZ_SUBAGENT_WORKSPACE_JSON."
            ) from exc
        if not isinstance(value, dict) or set(value) - _FIELDS:
            raise _fail(
                "preparation_config_invalid", "Unknown workspace preparation fields."
            )
        if "private" in value and not isinstance(value["private"], dict):
            raise _fail(
                "preparation_config_invalid",
                "private must be a complete environment profile.",
            )
        for profile in (value, value.get("private")):
            if profile is None:
                continue
            if not isinstance(profile, dict) or (
                profile is not value and set(profile) - _PROFILE
            ):
                raise _fail(
                    "preparation_config_invalid", "Invalid private environment profile."
                )
            for key in (
                "copy_files",
                "symlink_directories",
                "owned_directories",
                "inputs",
                "submodules",
            ):
                paths = profile.get(key, [])
                if not isinstance(paths, list) or any(
                    not isinstance(p, str) for p in paths
                ):
                    raise _fail(
                        "preparation_config_invalid",
                        key + " must contain relative paths.",
                    )
                for path in paths:
                    _relative(path)
            env = profile.get("environment", {})
            if not isinstance(env, dict) or any(
                not isinstance(k, str) or not isinstance(v, str) for k, v in env.items()
            ):
                raise _fail(
                    "preparation_config_invalid",
                    "environment must map names to strings.",
                )
            for key in ("setup", "checks"):
                commands = profile.get(key, [])
                if not isinstance(commands, list) or any(
                    not isinstance(a, list)
                    or not a
                    or any(not isinstance(v, str) or "\x00" in v for v in a)
                    for a in commands
                ):
                    raise _fail(
                        "preparation_config_invalid", key + " must be argv arrays."
                    )
        if value.get("read_only_mode", "worktree") not in {"worktree", "direct"}:
            raise _fail(
                "preparation_config_invalid",
                "read_only_mode must be worktree or direct.",
            )
        return cls(value)

    def profile(self, mode, *, module_paths=()):
        if mode not in {"reuse", "private"}:
            raise _fail(
                "preparation_config_invalid",
                "environment_mode must be reuse or private.",
            )
        if mode == "private":
            if "private" not in self.value:
                raise _fail(
                    "preparation_private_missing",
                    "This project has no private environment profile.",
                )
            profile = self.value["private"]
        else:
            profile = {k: v for k, v in self.value.items() if k in _PROFILE}
        links = profile.get("symlink_directories", [])
        owned = profile.get("owned_directories", [])
        modules = list(
            dict.fromkeys([*self.value.get("submodules", []), *module_paths])
        )
        resources = links + owned + modules

        def overlaps(a, b):
            return a == b or a.startswith(b + "/") or b.startswith(a + "/")

        if any(
            overlaps(a, b) for i, a in enumerate(resources) for b in resources[i + 1 :]
        ) or any(
            overlaps(name, directory)
            for name in self.value.get("copy_files", [])
            for directory in resources
        ):
            raise _fail(
                "preparation_resource_conflict",
                "Copy/source paths overlap owned or shared resources.",
            )
        return profile


@dataclass(frozen=True)
class EnvironmentReceipt:
    environment: ExecutionEnvironment
    excluded: tuple[str, ...]


def prepare_environment(
    spec,
    *,
    parent,
    child,
    state,
    preparation,
    mode="reuse",
    read_only=False,
    persist=lambda: None,
    log_directory: Path | None = None,
    module_paths=(),
):
    parent, child = Path(parent).resolve(), Path(child).resolve()
    origins = tuple((str(root), root.stat().st_dev, root.stat().st_ino)
                    for root in dict.fromkeys((parent, child)))
    profile = spec.profile(mode, module_paths=module_paths)
    previous = state.get("environment_preparation") or {}
    base = build_sanitized_subprocess_env()

    def expand(text):
        def replace(match):
            key = match.group(1)
            if key == "parent":
                return str(parent)
            if key == "worktree":
                return str(child)
            if key.startswith("env:") and key[4:] in base:
                return base[key[4:]]
            raise _fail(
                "preparation_config_invalid",
                "Unknown or unavailable environment placeholder.",
            )

        return re.sub(r"\$\{([^}]+)\}", replace, text)

    overrides = {
        key: expand(value) for key, value in profile.get("environment", {}).items()
    }
    if read_only:
        environment = ExecutionEnvironment.capture(overrides, source=base, origin_workspaces=origins)
        state["environment_preparation"] = {
            "status": "ready",
            "level": "workspace",
            "phase": "read_only",
            "mode": mode,
            "owned": [],
            "borrowed": [],
        }
        persist()
        return EnvironmentReceipt(environment, ())
    owned = tuple(profile.get("owned_directories", []))
    borrowed = tuple(profile.get("symlink_directories", []))
    required_modules = tuple(spec.value.get("submodules", []))
    module_paths = tuple(dict.fromkeys([*required_modules, *module_paths]))
    excluded = owned + borrowed + module_paths
    if previous.get("mode") != mode:
        for name in previous.get("borrowed", []):
            link = child / name
            if link.is_symlink() and link.resolve() == (parent / name).resolve():
                link.unlink()
    for name in owned:
        path = _inside(child, name)
        if (
            path.is_symlink()
            or path.is_file()
            or any(
                name == p or name.startswith(p + "/") or p.startswith(name + "/")
                for p in borrowed
            )
        ):
            raise _fail(
                "preparation_resource_conflict",
                "Owned directory overlaps source or shared resources.",
            )
    new_owned = set(owned) - set(previous.get("owned", []))
    if new_owned:
        initial = source_manifest(child)
        if any(
            any(key == name or key.startswith(name + "/") for key in initial)
            for name in new_owned
        ):
            raise _fail(
                "preparation_resource_conflict",
                "Owned directory contains source files.",
            )
    if owned and (child / ".git").exists():
        tracked = preparation.run_git(
            ("ls-files", "-z"), cwd=child, timeout=preparation.remaining_seconds
        )
        if tracked.returncode or any(
            path == name or path.startswith(name + "/")
            for path in tracked.stdout.split("\0")
            if path
            for name in owned
        ):
            raise _fail(
                "preparation_resource_conflict",
                "Owned directory overlaps tracked source.",
            )
    resources = []
    for name in borrowed:
        source = _inside(parent, name)
        if not source.is_dir():
            raise _fail(
                "preparation_resource_missing",
                "Shared dependency directory is unavailable: " + name,
            )
        info = source.stat()
        resources.append({"path": str(source), "identity": [info.st_dev, info.st_ino]})
    runtime = overrides.get("VIRTUAL_ENV")
    if runtime and not Path(runtime).resolve().is_relative_to(child):
        path = Path(runtime).resolve()
        if not path.is_dir():
            raise _fail(
                "preparation_resource_missing",
                "Borrowed virtual environment is unavailable.",
            )
        info = path.stat()
        resources.append({"path": str(path), "identity": [info.st_dev, info.st_ino]})
    inputs = []
    for name in spec.value.get("inputs", []):
        path = _inside(child, name)
        if not path.is_file():
            raise _fail(
                "preparation_input_missing",
                "Required environment input is unavailable: " + name,
            )
        inputs.append((str(path), hashlib.sha256(path.read_bytes()).hexdigest()))
        if mode == "reuse" and child != parent:
            parent_path = _inside(parent, name)
            if (
                not parent_path.is_file()
                or hashlib.sha256(parent_path.read_bytes()).hexdigest() != inputs[-1][1]
            ):
                raise _fail(
                    "preparation_dependency_changed",
                    "Child dependency inputs differ from parent; select a private environment.",
                )
            inputs.append((str(parent_path), inputs[-1][1]))
    commands = {
        key: [tuple(expand(v) for v in argv) for argv in profile.get(key, [])]
        for key in ("setup", "checks")
    }
    for argv in commands["setup"] + commands["checks"]:
        for value in argv:
            candidate = Path(value) if Path(value).is_absolute() else child / value
            if candidate.is_file() and candidate.resolve().is_relative_to(child):
                inputs.append(
                    (str(candidate), hashlib.sha256(candidate.read_bytes()).hexdigest())
                )
    try:
        environment = ExecutionEnvironment.capture(
            overrides,
            source=base,
            inputs=inputs,
            resources=tuple((r["path"], *r["identity"]) for r in resources),
            excluded=tuple(str(child / name) for name in excluded),
            origin_workspaces=origins,
        )
    except UnsafeSubprocessEnvironment as exc:
        raise _fail("preparation_environment_invalid", str(exc)) from exc
    fingerprint = hashlib.sha256(
        json.dumps(
            [spec.value, mode, str(child), environment.fingerprint, inputs, resources],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    record = {
        "status": "preparing",
        "phase": "files",
        "mode": mode,
        "fingerprint": fingerprint,
        "owned": list(owned),
        "borrowed": list(borrowed),
        "resources": resources,
        "module_paths": list(module_paths),
        "level": "runtime" if commands["checks"] else "workspace",
    }
    state["environment_preparation"] = record
    persist()
    before = source_manifest(child, excluded)
    owned_intact = previous.get("owned_resources") is not None
    for resource in previous.get("owned_resources", []):
        path = _inside(child, resource["path"])
        if (
            not path.is_dir()
            or [path.stat().st_dev, path.stat().st_ino] != resource["identity"]
        ):
            owned_intact = False
    try:
        for name in spec.value.get("copy_files", []):
            preparation.checkpoint()
            source, target = _inside(parent, name), _inside(child, name)
            if not source.is_file() or source.is_symlink() or target.is_symlink():
                raise _fail(
                    "preparation_file_missing",
                    "Required local configuration is unavailable: " + name,
                )
            if source != target:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
        _create_symlinks(parent, child, list(borrowed), preparation=preparation)
        record["submodules"] = _prepare_submodules(
            required_modules, parent=parent, child=child, preparation=preparation
        )
        before = source_manifest(child, excluded)
        for phase in ("setup", "checks"):
            record["phase"] = phase
            persist()
            if (
                phase == "setup"
                and previous.get("status") == "ready"
                and previous.get("fingerprint") == fingerprint
                and owned_intact
            ):
                continue
            for argv in commands[phase]:
                preparation.checkpoint()
                outcome = preparation.run(
                    argv,
                    cwd=child,
                    timeout=preparation.remaining_seconds,
                    environment=environment.as_dict(),
                )
                if log_directory is not None:
                    log_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                    log_path = log_directory / (
                        phase + "-" + str(time.time_ns()) + ".log"
                    )
                    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(
                        fd, "w", encoding="utf-8", errors="backslashreplace"
                    ) as log:
                        log.write(outcome.stdout + "\n[stderr]\n" + outcome.stderr)
                    record["log_path"] = str(log_path)
                record["exit_code"] = outcome.returncode
                # 诊断保存在当前子任务，不将输出拼到框架工作记忆。
                record["diagnostic"] = (outcome.stdout + outcome.stderr)[-4000:]
                if outcome.returncode:
                    raise _fail(
                        "preparation_" + phase + "_failed",
                        f"Environment {phase} failed with exit code {outcome.returncode}.",
                    )
        preparation.checkpoint()
        if source_manifest(child, excluded) != before:
            raise _fail(
                "preparation_source_changed",
                "Environment setup changed project source outside owned directories.",
            )
        record["owned_resources"] = [
            {
                "path": name,
                "identity": [
                    (child / name).stat().st_dev,
                    (child / name).stat().st_ino,
                ],
            }
            for name in owned
            if (child / name).is_dir()
        ]
        environment = replace(
            environment,
            source_paths=tuple(before),
            resources=environment.resources
            + tuple(
                (str(child / r["path"]), *r["identity"])
                for r in record["owned_resources"]
            ),
        )
        record.update(status="ready", phase="ready")
        persist()
        return EnvironmentReceipt(environment, excluded)
    except BaseException as exc:
        record["status"] = (
            "cancelled"
            if isinstance(exc, PreparationInterrupted) and exc.reason == "cancelled"
            else "failed"
        )
        persist()
        raise


def _prepare_submodules(paths, *, parent, child, preparation):
    """只接纳干净固定依赖；每层验证 SHA 和子方 Git 元数据归属。"""
    if not paths:
        return []
    if not (child / ".git").exists():
        raise _fail(
            "preparation_submodule_unsupported",
            "Required submodules need a Git worktree.",
        )

    def checked(args, cwd=child):
        result = preparation.run_git(
            tuple(args), cwd=cwd, timeout=preparation.remaining_seconds
        )
        if result.returncode:
            raise _fail(
                "preparation_submodule_failed",
                "Submodule Git inspection/preparation failed: " + result.stderr[-1000:],
            )
        return result.stdout

    def links(root):
        result = {}
        for row in checked(("-C", str(root), "ls-files", "--stage", "-z")).split("\0"):
            if not row:
                continue
            fields, name = row.split("\t", 1)
            mode, sha, stage = fields.split()
            if mode == "160000":
                if stage != "0":
                    raise _fail(
                        "preparation_submodule_unsupported",
                        "Conflicted submodule index is unsupported.",
                    )
                result[name] = sha
        return result

    parent_links, child_links = links(parent), links(child)
    for name in paths:
        _inside(parent, name)
        if name not in parent_links or child_links.get(name) != parent_links[name]:
            raise _fail(
                "preparation_submodule_unsupported",
                "Submodule gitlink differs from the selected base.",
            )
        source = _inside(parent, name)
        head = checked(("-C", str(source), "rev-parse", "HEAD"), parent).strip()
        dirty = checked(
            ("-C", str(source), "status", "--porcelain", "-z", "--untracked-files=all"),
            parent,
        )
        if head != parent_links[name] or dirty:
            raise _fail(
                "preparation_submodule_unsupported",
                "Parent submodule must be clean and match its gitlink.",
            )
    gitdir = Path(checked(("rev-parse", "--absolute-git-dir")).strip()).resolve()
    common = checked(
        ("rev-parse", "--path-format=absolute", "--git-common-dir")
    ).strip()
    # 锁只保护共享 Git 元数据准备；不覆盖后续编译和测试。
    import fcntl
    from nz_coder.foundation.user_paths import prepare_user_storage

    lockdir = prepare_user_storage(parent).workspace_cache / "preparation-locks"
    lockdir.mkdir(parents=True, exist_ok=True)
    with (lockdir / (hashlib.sha256(common.encode()).hexdigest() + ".lock")).open(
        "a+b"
    ) as lock:
        while True:
            preparation.checkpoint()
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(0.01)
        try:
            checked(
                (
                    "submodule",
                    "update",
                    "--init",
                    "--recursive",
                    "--checkout",
                    "--",
                    *paths,
                )
            )
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    result = []

    def verify(name, expected):
        root = _inside(child, name)
        actual = checked(("-C", str(root), "rev-parse", "HEAD")).strip()
        top = Path(
            checked(("-C", str(root), "rev-parse", "--show-toplevel")).strip()
        ).resolve()
        metadata = Path(
            checked(("-C", str(root), "rev-parse", "--absolute-git-dir")).strip()
        ).resolve()
        dirty = checked(
            ("-C", str(root), "status", "--porcelain", "-z", "--untracked-files=all")
        )
        if (
            actual != expected
            or top != root
            or dirty
            or not (metadata.is_relative_to(gitdir) or metadata.is_relative_to(child))
        ):
            raise _fail(
                "preparation_submodule_invalid",
                "Submodule source/commit/metadata ownership is unconfirmed.",
            )
        result.append({"path": name, "sha": expected, "gitdir": str(metadata)})
        for nested, sha in links(root).items():
            verify(name + "/" + nested, sha)

    for name in paths:
        verify(name, parent_links[name])
    return result
