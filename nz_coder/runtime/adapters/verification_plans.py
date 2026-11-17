"""把外部规划建议转换成域检查定义；规划不保存执行进度。"""
from __future__ import annotations

import hashlib
import json
import shlex
from pathlib import Path, PurePosixPath

from nz_coder.runtime.verification.domain.identity import command_identity
from nz_coder.runtime.verification.domain.models import CheckSpec
from .verification_facts import command_capabilities


def check_id(command, capability: str) -> str:
    identity = (command.argv, command.cwd, command.environment, command.opaque, capability)
    return "check-" + hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:20]


def check_spec(
    command: str, cwd: str, *, capability: str, required: bool = False,
    targets: tuple[str, ...] = (), stage: str = "targeted", source: str = "model_execution",
    requirement_ids: tuple[str, ...] = (), native_runner_kind: str = "", covers: tuple[str, ...] = (),
    coverage_mode: str = "identity",
) -> CheckSpec:
    identity = command_identity(command, cwd)
    id = check_id(identity, capability)
    if coverage_mode == "capability_targets":
        id += "-" + hashlib.sha256(json.dumps(targets).encode()).hexdigest()[:12]
    return CheckSpec(id, identity, capability, targets, required, stage,
                     source, requirement_ids, native_runner_kind, covers, coverage_mode)


def register_verification_plan(domain, plan: dict) -> None:
    from nz_coder.foundation.file_kinds import language_for_path
    from nz_coder.runtime.agent.task_policy import test_command_targets

    specs = []
    workspace = domain.scope.task.workspace
    for stage in plan.get("stages", ()):
        name = stage.get("name")
        if name not in {"static", "targeted", "regression"}:
            continue
        for item in stage.get("commands", ()):
            command = str(item.get("command") or "").strip()
            if not command:
                continue
            if command == "verify_changed_files":
                for path in plan.get("changed_files", ()):
                    capability = {"python": "syntax", "javascript": "type", "typescript": "type",
                                  "go": "build", "rust": "build"}.get(language_for_path(path))
                    if capability is not None:
                        specs.append(check_spec(command, workspace, capability=capability,
                            targets=(path,), required=bool(item.get("required", stage.get("required", False))),
                            stage=name, source="planner", coverage_mode="capability_targets"))
                continue
            capabilities = command_capabilities(command) or ("custom_assert",)
            for capability in capabilities:
                cwd = str(item.get("cwd") or workspace)
                targets = tuple(test_command_targets(command))
                words = command_identity(command, cwd).argv
                if capability == "syntax" and len(words) > 3 and words[1:3] == ("-m", "py_compile"):
                    targets = tuple(words[3:])
                specs.append(check_spec(command, cwd, capability=capability,
                    targets=targets, required=bool(item.get("required", stage.get("required", False))),
                    coverage_mode="capability_targets" if capability == "syntax" and targets else "identity",
                    stage=name, source="planner",
                    native_runner_kind=str(plan.get("native_runner_kind") or "")))
    domain.replace_plan(tuple(specs))


def _go_test_covers_artifact(artifact: PurePosixPath, command: str) -> bool:
    from nz_coder.intelligence.verification_planner import verification_success_is_reliable

    # 只接受单条直接 Go 测试，Shell 包装、重定向和复合命令均不推断包身份。
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    tokens = list(lexer)
    if (
        len(tokens) < 2 or Path(tokens[0]).name != "go" or tokens[1] != "test"
        or not verification_success_is_reliable(command)
        or any(token and all(char in "();<>|&" for char in token) for token in tokens)
    ):
        return False
    value_options = {
        "-run", "-count", "-timeout", "-parallel", "-cpu", "-bench", "-benchtime", "-list", "-tags",
    }
    flag_options = {"-v", "-race", "-cover", "-short", "-failfast", "-json", "-benchmem"}
    targets = []
    index = 2
    while index < len(tokens):
        token = tokens[index]
        option, separator, _value = token.partition("=")
        if option == "-args":
            break
        if option in value_options:
            index += 1 if separator else 2
            continue
        if token.startswith("-"):
            if option not in flag_options:
                return False
        else:
            targets.append(token)
        index += 1
    # 包路径是 Go 测试的覆盖范围；函数名和测试参数不能替代它。
    return any(
        (target == "." or target.startswith("./"))
        and PurePosixPath(target) in {artifact.parent, artifact}
        for target in targets
    )
