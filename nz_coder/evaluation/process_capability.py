"""Provider-free persistent process capability contracts."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import shlex
import sys
import time

from nz_coder.runtime.process.process_service import ProcessService


@dataclass(frozen=True)
class ProcessCapabilityCase:
    case_id: str
    name: str
    command: str
    needs_write: bool
    needs_reconnect: bool


def process_capability_manifest() -> tuple[ProcessCapabilityCase, ...]:
    python = shlex.quote(sys.executable)
    return (
        ProcessCapabilityCase(
            "P1", "dev-server",
            f"{python} -c \"import time; print('READY', flush=True); time.sleep(30)\"",
            False, True,
        ),
        ProcessCapabilityCase(
            "P2", "watch-mode",
            f"{python} -c \"import time; print('WATCHING', flush=True); time.sleep(30)\"",
            False, True,
        ),
        ProcessCapabilityCase(
            "P3", "repl",
            f"{python} -i -c \"print('REPL_READY', flush=True)\"",
            True, True,
        ),
        ProcessCapabilityCase(
            "P4", "log-monitor",
            f"{python} -c \"import time; print('LINE_1', flush=True); time.sleep(30)\"",
            False, True,
        ),
        ProcessCapabilityCase(
            "P5", "process-crash",
            f"{python} -c \"import sys; print('CRASHING', flush=True); sys.exit(7)\"",
            False, True,
        ),
        ProcessCapabilityCase(
            "P6", "multiple-processes",
            f"{python} -c \"import time; print('SERVICE', flush=True); time.sleep(30)\"",
            False, True,
        ),
    )


def run_persistent_process_capability_benchmark(output_dir: Path) -> dict:
    """Exercise durable IDs, later I/O, crash status, and zero-orphan cleanup."""
    workspace = Path(output_dir).resolve() / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    runs = []
    service = ProcessService(workspace, kill_grace_seconds=0.05)
    for case in process_capability_manifest():
        handles = []
        count = 2 if case.case_id == "P6" else 1
        for _index in range(count):
            handles.append(service.start(
                case.command,
                cwd=workspace,
                owner_session_id="capability-benchmark",
                tty=case.case_id == "P3",
            ))
        primary = handles[0]
        first = service.read(
            primary.process_id,
            owner_session_id="capability-benchmark",
            cursor=0,
            wait_seconds=2,
        )
        can_write = False
        if case.case_id == "P3" and primary.status == "running":
            try:
                service.write(
                    primary.process_id,
                    "1 + 1\n",
                    owner_session_id="capability-benchmark",
                )
                can_write = True
            except Exception:
                can_write = False
        if case.case_id == "P5":
            deadline = time.monotonic() + 2
            while service.get(
                primary.process_id, owner_session_id="capability-benchmark",
            ).status == "running" and time.monotonic() < deadline:
                time.sleep(0.01)
        status = service.get(
            primary.process_id, owner_session_id="capability-benchmark",
        )
        runs.append({
            **asdict(case),
            "process_handle_returned": bool(primary.process_id),
            "can_write_after_return": can_write,
            "can_read_after_return": bool(first.output),
            "can_reconnect": service.get(
                primary.process_id, owner_session_id="capability-benchmark",
            ).process_id == primary.process_id,
            "status": status.status,
            "exit_code": status.exit_code,
            "process_count": len(handles),
        })
        for handle in handles:
            service.kill(
                handle.process_id, owner_session_id="capability-benchmark",
            )
    service.close()
    orphan_process_count = len(service.list(active_only=True))
    structural_failures = sum(
        not run["process_handle_returned"]
        or not run["can_read_after_return"]
        or not run["can_reconnect"]
        for run in runs
    )
    result = {
        "benchmark_version": 2,
        "suite_type": "persistent-process-capability-contract",
        "runs": runs,
        "structural_failures": structural_failures,
        "orphan_process_count": orphan_process_count,
        "decision": "persistent process capability complete" if structural_failures == 0 and orphan_process_count == 0 else "persistent process capability incomplete",
        "note": (
            "The workspace ProcessService returns durable IDs, bounded cursor reads, "
            "later stdin writes, status, process-group kill, and deterministic cleanup."
        ),
    }
    target = Path(output_dir).resolve() / "persistent-process-capability.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    return result


__all__ = [
    "ProcessCapabilityCase", "process_capability_manifest",
    "run_persistent_process_capability_benchmark",
]
