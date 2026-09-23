"""Offline privacy, immutability, and one-request audit."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
BASE = "ce93e9c4f97dd6492e5f6fcdf9d54ecd1b8bc5ec"


def sha(value):
    raw = value.encode() if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip() == BASE
    assert subprocess.check_output(["git", "rev-parse", "origin/main"], cwd=ROOT, text=True).strip() == BASE
    source = json.loads((OUT / "baseline/source-hashes.json").read_text())["files"]
    for path, expected in source.items():
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == expected, path
    assert not subprocess.check_output(["git", "diff", "--name-only", "--", "nz_coder"], cwd=ROOT)
    result = json.loads((OUT / "result/raw-response.json").read_text())
    accounting = json.loads((OUT / "result/provider-accounting.json").read_text())
    assert result["physical_requests"] == accounting["attempts"] == 1
    assert result["retries"] == accounting["retries"] == 0
    assert result["http_status"] == 200
    assert result["parsed_verdict"]["verdict"] == "accept"
    assert accounting["cost"] is None
    request = json.loads((OUT / "counterfactual/request.json").read_text())
    user = request["messages"][1]["content"]
    assert "invalid_write_preserves_destination" not in user
    assert "11/12" not in user
    assert "oracle/" not in user
    assert "=== RELATED UNCHANGED IMPLEMENTATION EVIDENCE ===" in user
    # Avoid publishing provider credentials or private reasoning fields.
    forbidden_keys = {"authorization", "cookie", "api_key", "access_token", "reasoning_content", "private_reasoning"}
    def audit(value):
        if isinstance(value, dict):
            assert not set(str(k).lower() for k in value) & forbidden_keys
            for item in value.values():
                audit(item)
        elif isinstance(value, list):
            for item in value:
                audit(item)
    for path in (OUT / "result").glob("*.json"):
        audit(json.loads(path.read_text()))
    secrets = []
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            key, value = line.split("=", 1)
            if any(marker in key.upper() for marker in ("API_KEY", "TOKEN", "SECRET", "PASSWORD")) and len(value.strip()) >= 12:
                secrets.append(value.strip().strip("\"'"))
    for path in OUT.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        text = path.read_text()
        assert not any(secret in text for secret in secrets)
        assert str(Path.home()) not in text
    # Old tracked evidence and source must remain byte-identical.
    checked = 0
    tree = subprocess.check_output(["git", "ls-tree", "-r", BASE, "nz_coder", "docs/evidence"], cwd=ROOT, text=True)
    for line in tree.splitlines():
        metadata, path = line.split("\t", 1)
        data = (ROOT / path).read_bytes()
        actual = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        assert actual == metadata.split()[2], path
        checked += 1
    audit_result = {
        "passed": True, "classification": "CF-C2: ACCEPT", "historical_files_checked": checked,
        "production_source_changed": False, "frozen_c_workspace_unchanged": True,
        "historical_evidence_unchanged": True, "hidden_evaluator_leakage": False,
        "physical_requests": 1, "main_requests": 0, "planner_requests": 0,
        "embedding_requests": 0, "infcodex_requests": 0, "retries": 0,
        "cost": "unknown", "private_reasoning_fields": "absent",
    }
    (OUT / "integrity-audit.json").write_text(json.dumps(audit_result, indent=2) + "\n")
    sums = {str(path.relative_to(OUT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(OUT.rglob("*")) if path.is_file() and path.name != "SHA256SUMS.json" and "__pycache__" not in path.parts}
    (OUT / "SHA256SUMS.json").write_text(json.dumps(sums, indent=2) + "\n")
    print(f"PASS: {checked} historical files unchanged; one request, privacy, source and packet checks passed")


if __name__ == "__main__":
    main()
