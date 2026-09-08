"""One-shot T04 allocation from the sealed P1 predecessor; no new billing ledger."""
from __future__ import annotations

from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path

from . import runner
from .billing import BillingStopped, Policy, summarize
from .catalog import digest

PREDECESSOR_ID = "p1-live-20260908-073600"
RUNS = runner.ROOT / ".nz-coder-runs"
# Sealed local evidence, not caller-supplied amounts or arbitrary evidence paths.
ANCHORS = {
    f"{PREDECESSOR_ID}.config.json": "22806e70391f9304c4aa01cdafa0e455549bbc5d3365685f1541abfe224c7b93",
    f"{PREDECESSOR_ID}/frozen.json": "43ffb167a0f9f7db3d4b666bbcaa0c8ba12b06a4dfef9532cdfa84cb93dd63f8",
    f"{PREDECESSOR_ID}/T01/billing.jsonl": "c90dad4b6c3503d1aba3a08d4b883c48ced382ca4e22ba8985cdcc425f4c626a",
    f"{PREDECESSOR_ID}/T01/final.patch": "e4619db21366552daca85d888128a50de788e49d5d963d426655a65086eae0f3",
    f"{PREDECESSOR_ID}/T01/result.json": "0d3c6436feb0e31568a19cf265d64159a417e5f1951729f7cb9a47df0548e5e8",
    f"{PREDECESSOR_ID}/summary.json": "b67a7a99540c681197cc7efe5c85b119ebb2464385527ebd018acc11ea4c60a1",
}


def carryover(config: dict) -> dict:
    """Read-only evidence/money check, also usable before a new grant exists."""
    if config.get("continuation") != {"predecessor_experiment_id": PREDECESSOR_ID}:
        raise BillingStopped("T04 requires the sealed predecessor")
    output = Path(config["output_directory"])
    if (output.resolve().parent != RUNS.resolve() or output.name != config["experiment_id"]
            or output.name == PREDECESSOR_ID or output.is_symlink()):
        raise BillingStopped("Continuation output must be a new named private experiment")
    for relative, expected in ANCHORS.items():
        path = RUNS / relative
        if path.resolve().is_relative_to(RUNS.resolve()) is False or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise BillingStopped("Predecessor evidence missing or changed")
    def read(relative):
        return json.loads((RUNS / relative).read_text())
    old = read(f"{PREDECESSOR_ID}.config.json")
    if (any(config.get(key) != old.get(key) for key in
            ("endpoint", "model", "effort", "token_budget", "hit_rate", "miss_rate", "output_rate"))
            or config["authorization_reference"] == old["authorization_reference"]):
        raise BillingStopped("Continuation needs unchanged model/limits/rates and a new authorization reference")
    frozen = read(f"{PREDECESSOR_ID}/frozen.json")
    summary = read(f"{PREDECESSOR_ID}/summary.json")
    result = read(f"{PREDECESSOR_ID}/T01/result.json")
    if (summary.get("stopped") is not True or summary.get("experiment_id") != PREDECESSOR_ID
            or summary["tasks"].get("T01") != result or result.get("starts") != 1
            or any(t.get("starts") != 0 or t.get("final_status") != "not_run"
                   for name, t in summary["tasks"].items() if name != "T01")
            or "T04" not in summary["tasks"] or (RUNS / PREDECESSOR_ID / "T04").exists()
            or old.get("authorized") is not True or old.get("budget_currency") != "CNY"
            or any(old.get(k) != frozen.get(k) for k in
                   ("experiment_id", "authorization_reference", "harness_revision", "total_budget"))):
        raise BillingStopped("Predecessor identity, stop or unused T04 evidence differs")
    # Reconstruct original accounting, without reinterpreting legacy mode grants
    # or inventing diagnostics absent from the historical journal.
    policy = Policy(total_budget=Decimal(old["total_budget"]), task_budget=Decimal(old["per_task_budget"]),
                    token_budget=old["token_budget"], hit_rate=Decimal(old["hit_rate"]),
                    miss_rate=Decimal(old["miss_rate"]), output_rate=Decimal(old["output_rate"]))
    rows = [json.loads(line) for line in (RUNS / PREDECESSOR_ID / "T01/billing.jsonl").read_text().splitlines()]
    billing = summarize(rows, policy)
    for key in ("usage_derived_cost", "unresolved_reservation", "remaining_total_budget", "usage",
                "requests_sent", "unknown_requests", "usage_complete", "blocked"):
        if billing[key] != result["billing"][key]:
            raise BillingStopped("Predecessor journal and saved result disagree")
    remaining = Decimal(billing["remaining_total_budget"])
    allocation = Decimal(config["total_budget"])
    if (Decimal(summary["remaining_total_budget"]) != remaining or not allocation.is_finite()
            or not Decimal(0) < allocation <= min(remaining, Decimal(5))):
        raise BillingStopped("Continuation exceeds the original balance or 5 CNY allocation ceiling")
    return dict(predecessor_experiment_id=PREDECESSOR_ID,
        predecessor_authorization_reference=old["authorization_reference"], source_hashes=ANCHORS.copy(),
        original_total_budget=str(policy.total_budget), old_known_cost=billing["usage_derived_cost"],
        old_unknown_reservation=billing["unresolved_reservation"], balance_before_allocation=str(remaining),
        allocation=str(allocation), unallocated_balance=str(remaining - allocation),
        new_experiment_id=config["experiment_id"], new_authorization_reference=config["authorization_reference"],
        currency="CNY", balance_basis="ledger balance, not verified account balance")


def active_baseline_processes() -> list[int]:
    """Linux-only bounded process check; never expose command lines or environment."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            args = (entry / "cmdline").read_bytes().split(b"\0")
        except FileNotFoundError:
            continue  # Exited during the read, not an active worker.
        if any(module in args for module in (b"evaluation.linux_baseline.runner",
                                             b"evaluation.linux_baseline.live_worker")):
            found.append(int(entry.name))
    return found


def _registration() -> Path:
    return RUNS / "p1-continuations" / PREDECESSOR_ID / "T04"


def _receipt(config: dict, carry: dict) -> dict:
    return dict(stage="allocation_registered_not_worker_started", task_id="T04",
                config_hash=digest(config), carryover=carry, output_directory=config["output_directory"])


def claim(config: dict, carry: dict) -> None:
    """Exclusive predecessor/task registration; failures never release the claim."""
    if active_baseline_processes():
        raise BillingStopped("Another baseline process is active; allocation stopped")
    # All new continuations are confined here. Unknown later output needs human
    # accounting review, not an assumption of zero spending. No recursive scan.
    for directory in RUNS.iterdir():
        if directory.name != PREDECESSOR_ID and any((directory / name).exists() for name in
                ("frozen.json", "billing.jsonl", "worker-started", "T04")):
            raise BillingStopped("Additional experiment evidence requires accounting review")
    if carry != carryover(config):
        raise BillingStopped("Predecessor changed before allocation")
    path = _registration()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.mkdir(mode=0o700)
    with (path / "receipt.json").open("x", encoding="utf-8") as stream:
        os.chmod(stream.fileno(), 0o600)
        json.dump(_receipt(config, carry), stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    for directory in (path, path.parent, path.parent.parent, RUNS):
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    # Created only after receipt + ancestor fsyncs succeed. No subsequent
    # fallible operation can turn a failed claim into a startable one. Losing
    # this final marker after a crash fails closed and needs human review.
    (path / "ready").mkdir(mode=0o700)


def verify_claim(config: dict, carry: dict) -> None:
    if not (_registration() / "ready").is_dir():
        raise BillingStopped("Continuation allocation is not ready; manual review required")
    if json.loads((_registration() / "receipt.json").read_text()) != _receipt(config, carry):
        raise BillingStopped("Continuation allocation belongs to another grant or experiment")
