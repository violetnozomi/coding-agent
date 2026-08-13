"""Minimal non-invasive adapters for reference coding-agent comparisons."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import time
from typing import Any, Protocol


@dataclass(frozen=True)
class ReferenceRunRequest:
    workspace: Path
    prompt: str
    model: str
    provider: str
    reasoning: str = "medium"
    max_turns: int = 30
    timeout_s: float = 900.0


@dataclass(frozen=True)
class ReferenceCapability:
    name: str
    available: bool
    reason: str | None
    command: tuple[str, ...] = ()
    runtime: str | None = None


@dataclass(frozen=True)
class ReferenceRunResult:
    reference: str
    status: str
    final_text: str
    changed_files: tuple[str, ...]
    wall_time_ms: float
    exit_code: int | None
    trajectory: tuple[dict[str, Any], ...] = ()
    tokens: dict[str, int] | None = None
    error: str | None = None
    capability: ReferenceCapability | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        if self.capability is not None:
            value["capability"] = asdict(self.capability)
        return value


class ReferenceAdapter(Protocol):
    name: str

    def probe(self) -> ReferenceCapability: ...

    def run(self, request: ReferenceRunRequest) -> ReferenceRunResult: ...


def _version_tuple(value: str) -> tuple[int, ...]:
    match = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", value)
    if not match:
        return ()
    return tuple(int(part or 0) for part in match.groups())


def _workspace_hashes(root: Path) -> dict[str, str]:
    ignored = {".git", ".agent", ".nz-coder", "node_modules", "__pycache__"}
    result: dict[str, str] = {}
    for path in root.rglob("*"):
        if not path.is_file() or any(part in ignored for part in path.parts):
            continue
        relative = path.relative_to(root).as_posix()
        try:
            result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return result


def _changed_files(before: dict[str, str], workspace: Path) -> tuple[str, ...]:
    after = _workspace_hashes(workspace)
    return tuple(sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path)))


def _json_events(output: str) -> tuple[dict[str, Any], ...]:
    events: list[dict[str, Any]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            events.append(item)
    return tuple(events)


def _token_totals(events: tuple[dict[str, Any], ...]) -> dict[str, int] | None:
    totals = {"input": 0, "output": 0, "reasoning": 0, "cache": 0}
    found = False

    def visit(value: Any) -> None:
        nonlocal found
        if isinstance(value, dict):
            for key, item in value.items():
                lowered = str(key).lower()
                if isinstance(item, (int, float)):
                    target = next((name for name in totals if name in lowered and "token" in lowered), None)
                    if target:
                        totals[target] += int(item)
                        found = True
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(list(events))
    return totals if found else None


def _final_text(events: tuple[dict[str, Any], ...], output: str) -> str:
    text_deltas = [
        str(event.get("text") or "") for event in events
        if event.get("type") in {"text.delta", "assistant.delta"}
    ]
    if any(text_deltas):
        return "".join(text_deltas).strip()
    for event in reversed(events):
        for key in ("text", "content", "message", "output"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                nested = value.get("text") or value.get("content")
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
    # Text-mode reference CLIs may include terminal colors/spinners.
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output).strip()[-20_000:]


def _execute(
    name: str,
    capability: ReferenceCapability,
    command: list[str],
    request: ReferenceRunRequest,
    *,
    env_overrides: dict[str, str] | None = None,
) -> ReferenceRunResult:
    if not capability.available:
        return ReferenceRunResult(
            reference=name, status="unavailable", final_text="", changed_files=(),
            wall_time_ms=0.0, exit_code=None, error=capability.reason,
            capability=capability,
        )
    workspace = request.workspace.resolve()
    before = _workspace_hashes(workspace)
    started = time.monotonic()
    environment = os.environ.copy()
    environment.update(env_overrides or {})
    process = subprocess.Popen(
        command, cwd=str(workspace), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=environment, start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=max(1.0, request.timeout_s))
        status = "completed" if process.returncode == 0 else "error"
        error = stderr.strip()[-20_000:] or None if process.returncode else None
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        status = "timeout"
        error = f"reference run exceeded {request.timeout_s:g}s"
    events = _json_events(stdout)
    terminal = next((event for event in reversed(events) if event.get("type") == "run.result"), None)
    if isinstance(terminal, dict):
        if terminal.get("success") is True:
            status = "completed"
            error = None
        elif terminal.get("interrupted") is True:
            status = "interrupted"
            error = str(terminal.get("signalReason") or "reference run interrupted")
        else:
            status = "error"
            error = str(terminal.get("signalReason") or error or "reference run reported failure")
    elif name == "InfCodeX" and events:
        status = "incomplete_protocol"
        error = "InfCodeX exited without the documented run.result terminal event"
    return ReferenceRunResult(
        reference=name, status=status, final_text=_final_text(events, stdout),
        changed_files=_changed_files(before, workspace),
        wall_time_ms=round((time.monotonic() - started) * 1000, 3),
        exit_code=process.returncode, trajectory=events, tokens=_token_totals(events),
        error=error, capability=capability,
    )


class InfCodeXReferenceAdapter:
    name = "InfCodeX"

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def probe(self) -> ReferenceCapability:
        node = shutil.which("node")
        entry = self.root / "src" / "kodax_cli.ts"
        tsx = self.root / "node_modules" / "tsx" / "dist" / "esm" / "index.mjs"
        coding = self.root / "packages" / "coding" / "dist" / "index.js"
        runtime = ""
        node_command: tuple[str, ...] = ()
        if node:
            runtime = subprocess.run(
                [node, "--version"], capture_output=True, text=True, timeout=5, check=False,
            ).stdout.strip()
            if _version_tuple(runtime) >= (20, 0, 0):
                node_command = (node,)
        if not node_command and shutil.which("npx"):
            shim = subprocess.run(
                ["npx", "-y", "node@20", "--version"],
                capture_output=True, text=True, timeout=30, check=False,
            )
            shim_runtime = shim.stdout.strip()
            if shim.returncode == 0 and _version_tuple(shim_runtime) >= (20, 0, 0):
                runtime = shim_runtime
                node_command = ("npx", "-y", "node@20")
        if not node_command:
            reason = "node runtime >=20 is not installed and the Node 20 npx shim is unavailable"
            if runtime:
                reason += f"; found {runtime}"
            return ReferenceCapability(self.name, False, "runtime_version_mismatch: " + reason, runtime=runtime or None)
        missing = [str(path) for path in (entry, tsx, coding) if not path.exists()]
        if missing:
            return ReferenceCapability(
                self.name, False,
                "reference dependencies/build artifacts unavailable: " + ", ".join(missing),
                runtime=runtime,
            )
        prefix = (*node_command,
            "--max-old-space-size=4096", "--require",
            str(self.root / "scripts" / "production-env.cjs"), "--import", str(tsx), str(entry),
        )
        return ReferenceCapability(self.name, True, None, prefix, runtime)

    def run(self, request: ReferenceRunRequest) -> ReferenceRunResult:
        capability = self.probe()
        selected_provider = request.provider
        env_overrides: dict[str, str] = {}
        if selected_provider in {"openai-compatible", "deepseek"} and request.model.startswith("deepseek"):
            selected_provider = "deepseek"
            key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("API_KEY")
            if key:
                env_overrides["DEEPSEEK_API_KEY"] = key
        command = list(capability.command) + [
            "--print", request.prompt, "--provider", selected_provider,
            "--model", request.model, "--max-iter", str(request.max_turns), "--no-session",
            "--agent-mode", "sa", "--auto",
        ]
        reasoning = str(request.reasoning or "").strip().lower()
        if reasoning in {"off", "auto", "quick", "balanced", "deep"}:
            command.extend(("--reasoning", reasoning))
        with tempfile.TemporaryDirectory(prefix="nzcoder-infcodex-home-") as config_home:
            env_overrides["KODAX_HOME"] = config_home
            return _execute(
                self.name, capability, command, request, env_overrides=env_overrides,
            )


class OpenCodeReferenceAdapter:
    name = "OpenCode/infcode-dev"

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def probe(self) -> ReferenceCapability:
        bun = shutil.which("bun")
        entry = self.root / "packages" / "opencode" / "src" / "index.ts"
        lockfile = self.root / "bun.lock"
        if not bun:
            return ReferenceCapability(
                self.name, False,
                "runtime_unavailable: repository requires Bun 1.3.13; bun is not installed",
            )
        runtime = subprocess.run(
            [bun, "--version"], capture_output=True, text=True, timeout=5, check=False,
        ).stdout.strip()
        if _version_tuple(runtime) < (1, 3, 13):
            return ReferenceCapability(
                self.name, False,
                f"runtime_version_mismatch: repository requires Bun 1.3.13; found {runtime}",
                runtime=runtime,
            )
        missing = [str(path) for path in (entry, lockfile) if not path.exists()]
        if missing or not (self.root / "node_modules").exists():
            detail = missing or [str(self.root / "node_modules")]
            return ReferenceCapability(
                self.name, False,
                "reference dependencies unavailable: " + ", ".join(detail), runtime=runtime,
            )
        prefix = (bun, "run", "--conditions=browser", str(entry), "run")
        return ReferenceCapability(self.name, True, None, prefix, runtime)

    def run(self, request: ReferenceRunRequest) -> ReferenceRunResult:
        capability = self.probe()
        command = list(capability.command) + [
            request.prompt, "--format", "json", "--auto", "--dir", str(request.workspace.resolve()),
            "--model", f"{request.provider}/{request.model}", "--variant", request.reasoning,
        ]
        return _execute(self.name, capability, command, request)


def reference_capability_report(
    *, infcodex_root: Path, opencode_root: Path,
) -> dict[str, Any]:
    adapters: tuple[ReferenceAdapter, ...] = (
        InfCodeXReferenceAdapter(infcodex_root), OpenCodeReferenceAdapter(opencode_root),
    )
    return {adapter.name: asdict(adapter.probe()) for adapter in adapters}


class ReferenceBehaviorDriver:
    """Expose a reference adapter through the common behavioral driver contract."""

    evidence_kind = "reference-production"

    def __init__(self, adapter: ReferenceAdapter) -> None:
        self.adapter = adapter

    @staticmethod
    def _normalize(events: tuple[dict[str, Any], ...]) -> tuple[dict[str, Any], ...]:
        normalized: list[dict[str, Any]] = []
        pending_tools: dict[str, dict[str, Any]] = {}
        for event in events:
            kind = str(event.get("type") or event.get("event") or "")
            if kind in {"iteration.start", "turn.started"}:
                continue
            elif kind in {"iteration.end", "turn.completed"}:
                usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
                normalized.append({
                    "event": "llm_response",
                    "input_tokens": int(usage.get("input_tokens") or usage.get("inputTokens") or 0),
                    "output_tokens": int(usage.get("output_tokens") or usage.get("outputTokens") or 0),
                })
            elif kind in {"tool.start", "tool.started"}:
                tool = event.get("tool") if isinstance(event.get("tool"), dict) else {}
                tool_id = str(event.get("id") or tool.get("id") or "")
                pending_tools[tool_id] = {
                    "event": "tool_call", "tool_call_id": tool_id,
                    "name": str(event.get("name") or tool.get("name") or ""),
                    "input": event.get("input") or tool.get("input") or {},
                }
            elif kind in {"tool.result", "tool.completed"}:
                tool_id = str(event.get("id") or "")
                output = str(event.get("content") or event.get("output") or "")
                lowered = output.casefold()
                failed = any(marker in lowered for marker in (
                    "command exited with code", "exit code 1", "exit code 2",
                    "error:", "failed", "traceback (most recent call last)",
                ))
                call = pending_tools.pop(tool_id, {
                    "event": "tool_call", "tool_call_id": tool_id,
                    "name": str(event.get("name") or ""), "input": {},
                })
                call.update({
                    "output": output, "output_len": len(output),
                    "status": "nonzero" if failed else "ok", "command_failed": failed,
                })
                normalized.append(call)
            elif kind in {"compact.finish", "compaction"}:
                normalized.append({"event": "compaction"})
        return tuple(normalized)

    def run(self, task, workspace: Path, config):  # noqa: ANN001
        from nz_coder.evaluation.behavioral import BehaviorObservation

        result = self.adapter.run(ReferenceRunRequest(
            workspace=workspace, prompt=task.prompt, model=config.model,
            provider=config.provider, reasoning=config.reasoning,
            max_turns=config.max_turns,
        ))
        payload = result.to_dict()
        payload["metadata"] = {"raw_status": result.status}
        payload["reference_trajectory_available"] = bool(result.trajectory)
        error = "" if result.status == "completed" else str(result.error or result.status)
        return BehaviorObservation(
            final_response=result.final_text,
            events=self._normalize(result.trajectory),
            run_result=payload,
            changed_files=result.changed_files,
            error=error,
        )


def run_reference_behavior_matrix(
    output_dir: Path,
    *,
    adapter: ReferenceAdapter,
    provider: str,
    model: str,
    reasoning: str = "provider-default",
    max_turns: int = 24,
    repetitions: int = 3,
    case_ids: tuple[str, ...] = ("A", "B", "E", "F", "I"),
) -> dict[str, Any]:
    """Run the same behavioral fixtures or report a structured blocker."""
    from nz_coder.evaluation.behavioral import AgentBehaviorBenchmark, BehaviorBenchmarkConfig

    target_dir = Path(output_dir).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    capability = adapter.probe()
    if not capability.available:
        result = {
            "benchmark_version": 1, "suite_type": "reference-agent-behavior-matrix",
            "reference": adapter.name, "evidence_kind": "unavailable",
            "capability": asdict(capability), "runs": [], "success_rate": None,
            "behavioral_effectiveness": "unavailable",
        }
    else:
        benchmark = AgentBehaviorBenchmark(target_dir, ReferenceBehaviorDriver(adapter))
        runs = []
        for repetition in range(1, max(3, int(repetitions)) + 1):
            for case_id in case_ids:
                runs.append(benchmark.run_case(case_id, BehaviorBenchmarkConfig(
                    model=model, provider=provider, reasoning=reasoning,
                    max_turns=max_turns, repo_intelligence="v3", repetition=repetition,
                )))
        result = {
            "benchmark_version": 1, "suite_type": "reference-agent-behavior-matrix",
            "reference": adapter.name, "evidence_kind": "reference-production",
            "capability": asdict(capability), "provider": provider, "model": model,
            "reasoning": reasoning, "repetitions": max(3, int(repetitions)),
            "runs": runs,
            "success_rate": sum(bool(run["score"]["success"]) for run in runs) / len(runs),
            "behavioral_effectiveness": "measured",
        }
    slug = re.sub(r"[^a-z0-9]+", "-", adapter.name.casefold()).strip("-")
    (target_dir / f"{slug}-reference-matrix.json").write_text(
        json.dumps(result, indent=2, sort_keys=True), encoding="utf-8",
    )
    return result


def rescore_reference_matrix(path: Path) -> dict[str, Any]:
    """Recompute completion scores without executing the stored fixtures again."""
    target = Path(path).resolve()
    result = json.loads(target.read_text(encoding="utf-8"))
    runs = result.get("runs") if isinstance(result, dict) else None
    if not isinstance(runs, list):
        raise ValueError("reference matrix has no runs")
    for run in runs:
        task_data = run.get("task") or {}
        score_data = run.get("score") or {}
        run_result = score_data.get("run_result") or {}
        trajectory_available = run_result.get("reference_trajectory_available") is not False
        score_data["turn_requirement_observable"] = trajectory_available
        score_data["long_horizon_exercised"] = (
            bool((score_data.get("metrics") or {}).get("turns", 0) >= int(task_data.get("min_turns", 1)))
            if trajectory_available else None
        )
        expected_files = tuple(task_data.get("expected_files") or ())
        expected_symbols = tuple(task_data.get("expected_symbols") or ())
        expected_path = tuple(task_data.get("expected_call_path") or ())
        localization_complete = (
            len(score_data.get("correct_files") or ()) == len(expected_files)
            and len(score_data.get("correct_symbols") or ()) == len(expected_symbols)
            and (not expected_path or score_data.get("call_path_correct") is True)
        )
        requires_localization = task_data.get("capability") in {
            "unknown-location-localization", "process-understanding",
            "large-repo-navigation", "tool-scale",
        }
        score_data["success"] = bool(
            not score_data.get("error")
            and score_data.get("final_patch_correctness") is not False
            and (score_data.get("verification") or {}).get("passed") is not False
            and score_data.get("recovery_complete") is not False
            and score_data.get("child_execution_complete") is not False
            and score_data.get("no_unneeded_web") is not False
            and (not requires_localization or localization_complete)
        )
        run["score"] = score_data
    result["success_rate"] = (
        sum(bool(run["score"]["success"]) for run in runs) / len(runs) if runs else None
    )
    result["scorer_revision"] = "completion-correctness-v2"
    target.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    return result


__all__ = [
    "InfCodeXReferenceAdapter", "OpenCodeReferenceAdapter", "ReferenceAdapter",
    "ReferenceBehaviorDriver", "ReferenceCapability", "ReferenceRunRequest",
    "ReferenceRunResult", "reference_capability_report",
    "rescore_reference_matrix", "run_reference_behavior_matrix",
]
