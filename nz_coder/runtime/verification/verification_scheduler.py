"""Budget-aware staged verification selection for one Agent run."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from nz_coder.runtime.verification.domain.models import CommandIdentity


def _native_completion_entrypoint(argv: tuple[str, ...], capability: str) -> bool:
    """命令入口必须是真实检查器，不能靠自定义脚本的某个参数冒充。"""
    executable = Path(argv[0]).name
    args = argv[1:]
    if re.fullmatch(r"(?:python|pypy)(?:\d+(?:\.\d+)*)?", executable):
        return (capability == "syntax" and args[:2] == ("-m", "py_compile")) or (
            capability == "test" and (args[:2] in {("-m", "pytest"), ("-m", "unittest")}
                                      or (args and args[0].removeprefix("./") == "tests/runtests.py")))
    if executable in {"pytest", "pytest3", "py.test"}:
        return capability == "test"
    if executable == "node":
        return bool(args and ((capability == "test" and args[0] == "--test")
                              or (capability == "syntax" and args[0] in {"--check", "-c"})))
    if executable == "cargo":
        if args[:1] == ("clippy",):
            return capability == "lint"
        return args[:1] == (("check",) if capability == "build" else ("test",)) and capability in {"build", "test"}
    if executable == "go":
        if args[:1] == ("vet",):
            return capability == "lint"
        return args[:1] == ("test",) and capability in {"build", "test"}
    if capability == "type":
        return executable in {"mypy", "pyright", "tsc"}
    return capability == "lint" and (executable in {"eslint", "pylint", "flake8"}
                                     or (executable == "ruff" and args[:1] == ("check",)))


@dataclass(frozen=True)
class VerificationAction:
    """One deterministic scheduler decision at a convergence boundary."""

    kind: str = "none"
    stage: str = ""
    command: str = ""
    cwd: str = ""
    reason: str = ""
    mutation_generation: int = 0
    check_id: str = ""


def _automatic_identity(command: str, cwd: str) -> CommandIdentity | None:
    from nz_coder.intelligence.verification_planner import verification_success_is_reliable
    from nz_coder.runtime.verification.domain.identity import command_identity, verified_check_subject

    identity = command_identity(command, cwd)
    if not identity.opaque:
        return identity
    # 只重放能保留成功证明的有限包装；写文件/隐藏退出状态的原命令交给模型处理。
    if not verification_success_is_reliable(command):
        return None
    return verified_check_subject(identity)


class VerificationScheduler:
    """Select cheap/targeted evidence without repeatedly running acceptance."""

    def completion_actions(self, status: dict, *, mutation_generation: int) -> tuple[VerificationAction, ...]:
        """只执行域内明确待完成的检查；stage 不参与权限判断。"""
        from nz_coder.runtime.adapters.verification_facts import command_capabilities

        if not status.get("strict"):
            from nz_coder.runtime.verification.domain.identity import command_identity

            groups = {}
            for stage in (status.get("verification_pipeline") or {}).get("stages", []):
                for item in stage.get("commands", []):
                    if item.get("required") and item.get("automation_provenance") == "user_declared":
                        command, cwd = item["command"], item["cwd"]
                        groups.setdefault(command_identity(command, cwd), []).append((stage["name"], item))
            actions = []
            for group in groups.values():
                pending = [(stage, item) for stage, item in group if item["status"] not in {"passed", "covered"}]
                if not pending or any(item.get("attempted_current") for _, item in pending):
                    continue
                stage, item = pending[0]
                actions.append(VerificationAction(kind="stage", stage=stage, command=item["command"],
                    cwd=item["cwd"], check_id=item["id"], mutation_generation=mutation_generation,
                    reason="execute explicit user acceptance"))
            return tuple(actions)

        actions = []
        seen = set()
        for stage in (status.get("verification_pipeline") or {}).get("stages", []):
            for item in stage.get("commands", []):
                if item.get("status") in {"passed", "covered"} or item.get("retry_eligible") is not True:
                    continue
                if not item.get("id") or not (item.get("required") or (
                        status.get("targeted_pending") and item.get("capability") == "test")):
                    continue
                command = str(item.get("retry_command") or item.get("command") or "").strip()
                cwd = str(item.get("cwd") or "").strip()
                if not command or not cwd:
                    continue
                identity = _automatic_identity(command, cwd)
                capability = item["capability"]
                if (identity is None or not identity.argv or not _native_completion_entrypoint(identity.argv, capability)
                        or command_capabilities(command) != (capability,)):
                    continue
                key = (command, cwd)
                if key not in seen:
                    seen.add(key)
                    actions.append(VerificationAction(
                        kind="stage", stage=stage["name"], command=command, cwd=cwd,
                        reason="settle known completion evidence", mutation_generation=mutation_generation,
                        check_id=item["id"],
                    ))
        return tuple(actions)

    def action(
        self,
        zone: str,
        *,
        verification_status: dict,
        unresolved_requirements: tuple[str, ...] | list[str],
        has_exact_contract: bool,
        exact_attempts: int = 0,
        mutation_generation: int = 0,
        source_mutation_generation: int | None = None,
        scheduled_generations: dict[str, int] | None = None,
    ) -> VerificationAction:
        normalized_zone = str(zone or "").lower()
        if not verification_status.get("strict") and normalized_zone != "completion":
            return VerificationAction()
        generation = max(0, int(mutation_generation))
        source_generation_supplied = source_mutation_generation is not None
        source_generation = max(
            0,
            int(
                generation
                if source_mutation_generation is None
                else source_mutation_generation
            ),
        )
        scheduled = scheduled_generations or {}

        def stage_action(
            stage: str,
            selected: tuple[str, str],
            reason: str,
        ) -> VerificationAction:
            attempted = int(scheduled.get(stage, -1))
            if (
                (source_generation_supplied and source_generation <= 0)
                or attempted >= source_generation
                or (
                    max(0, int(exact_attempts)) > 0
                    and generation > source_generation
                )
            ):
                return VerificationAction()
            return VerificationAction(
                kind="stage",
                stage=stage,
                command=selected[0],
                cwd=selected[1],
                reason=reason,
                mutation_generation=generation,
                check_id=next((item["id"] for group in (verification_status.get("verification_pipeline") or {}).get("stages", [])
                    for item in group.get("commands", []) if item.get("id") and item.get("command") == selected[0]
                    and item.get("cwd") == selected[1]), ""),
            )

        if normalized_zone == "completion":
            return (
                VerificationAction(
                    kind="acceptance",
                    stage="acceptance",
                    reason="natural completion requires the exact user contract",
                )
                if has_exact_contract else VerificationAction()
            )

        if normalized_zone == "yellow":
            command = self._pending_command(verification_status, "static")
            if command[0]:
                return stage_action(
                    "static", command,
                    "yellow budget uses the cheapest required evidence",
                )
            return VerificationAction()

        if normalized_zone in {"orange", "red"}:
            command = self._pending_command(verification_status, "targeted")
            if command[0]:
                return stage_action(
                    "targeted", command,
                    f"{normalized_zone} budget verifies affected behavior",
                )
            static = self._pending_command(verification_status, "static")
            if static[0]:
                return stage_action(
                    "static", static,
                    f"{normalized_zone} budget settles missing static evidence",
                )
            if (
                normalized_zone == "red"
                and has_exact_contract
                and not tuple(unresolved_requirements)
            ):
                return VerificationAction(
                    kind="acceptance",
                    stage="acceptance",
                    reason="red convergence has no unresolved hard requirement",
                )
        return VerificationAction()

    @staticmethod
    def _pending_command(status: dict, stage_name: str) -> tuple[str, str]:
        pipeline = (
            status.get("verification_pipeline")
            if isinstance(status, dict) else None
        ) or {}
        def selection(item):
            command = str(item.get("retry_command") or item.get("command") or "").strip()
            cwd = str(item.get("cwd") or "").strip()
            if item.get("retry_eligible") is not True or not command or not cwd:
                return "", ""
            return (command, cwd) if _automatic_identity(command, cwd) is not None else ("", "")

        stages = pipeline.get("stages") or []
        definitions = {item.get("id"): (stage.get("name"), item)
                       for stage in stages for item in stage.get("commands") or []}
        for obligation in status.get("test_obligations") or []:
            stage, definition = definitions.get(obligation.get("check_id"), (obligation.get("stage"), {}))
            if stage == stage_name and obligation.get("status") != "passed":
                selected = selection({**definition, **obligation})
                if selected[0]:
                    return selected
        for stage in stages:
            if not isinstance(stage, dict) or stage.get("name") != stage_name:
                continue
            required_pending = [
                item for item in stage.get("commands") or []
                if isinstance(item, dict)
                and item.get("required")
                and item.get("status") not in {"passed", "covered"}
                and selection(item)[0]
            ]
            if required_pending:
                return selection(required_pending[0])
            if stage_name == "targeted" and status.get("next_required_stage") == "targeted":
                related = [
                    item for item in stage.get("commands") or []
                    if isinstance(item, dict)
                    and not item.get("required")
                    and item.get("status") in {"not_run", "failed", "pending"}
                    and selection(item)[0]
                ]
                if related:
                    return selection(related[0])
        return "", ""


__all__ = ["VerificationAction", "VerificationScheduler"]
