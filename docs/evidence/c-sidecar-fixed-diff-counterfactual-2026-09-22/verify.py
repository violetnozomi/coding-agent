"""Read-only offline integrity checks; never sends requests or imports experiment."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
BASE = "ac47114affa6556ab8b56e1bb9e1a02171514fc0"


def read(name):
    return json.loads((OUT / name).read_text())


def sha(value):
    data = value.encode() if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(data).hexdigest()


def main():
    old = read("packets/packet-old.json")
    fixed = read("packets/packet-fixed.json")
    request = read("provider/request.json")
    assert request == fixed["request"]
    assert sha(request) == read("preflight/READY.json")["request_sha256"]
    assert hashlib.sha256((OUT / "experiment.py").read_bytes()).hexdigest() == read("preflight/READY.json")["builder_sha256"]
    for key in old["context"]:
        if key != "file_edit_summary":
            assert old["context"][key] == fixed["context"][key], key
    for key in old["request"]:
        if key != "messages":
            assert old["request"][key] == request[key], key
    assert old["request"]["messages"][0] == request["messages"][0]
    before = old["request"]["messages"][1]["content"]
    after = request["messages"][1]["content"]
    start = "=== FILE EDITS PERFORMED THIS TURN ==="
    end = "=== MAIN AGENT FINAL TEXT (the answer the agent is delivering) ==="
    assert before.split(start)[0] == after.split(start)[0]
    assert before.split(end)[1] == after.split(end)[1]
    assert (OUT / "packets/request-visible-system.txt").read_text() == request["messages"][0]["content"]
    assert (OUT / "packets/request-visible-user.txt").read_text() == after
    assert "def to_dict(config):" in after.split("- configkit/config/writer.py: ")[1].split("\n- ")[0]
    # Neither evaluator result nor known-defect hint was added to any section.
    assert "invalid_write_preserves_destination" not in after
    assert "11/12" not in after
    assert read("preflight/project-tests.json")["exit"] == 0
    acceptance = read("preflight/acceptance.json")
    assert acceptance["passed"] == 11 and acceptance["total"] == 12
    assert len(read("provider/attempts.json")) == 1
    response = read("provider/response.json")
    assert response["physical_requests"] == 1 and response["retries"] == 0
    assert response["http_status"] == 200 and response["parsed_verdict"]["verdict"] == "accept"
    assert read("preflight/provider-config.json")["physical_cap"] == 1
    source = read("preflight/source-hashes.json")
    for name, value in source.items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == value, name
    # Every old tracked evidence file and production file must remain byte-identical.
    tree = subprocess.check_output(["git", "ls-tree", "-r", BASE, "nz_coder", "docs/evidence"], cwd=ROOT, text=True)
    checked = 0
    for line in tree.splitlines():
        metadata, name = line.split("\t", 1)
        data = (ROOT / name).read_bytes()
        actual = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        assert actual == metadata.split()[2], name
        checked += 1
    forbidden = {"reasoning_content", "private_reasoning", "_nz_provider_reasoning_content",
                 "authorization", "cookie", "api_key", "access_token"}

    def check_fields(value):
        if isinstance(value, dict):
            assert not set(str(k).lower() for k in value) & forbidden
            for item in value.values():
                check_fields(item)
        elif isinstance(value, list):
            for item in value:
                check_fields(item)

    secrets = []
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        if any(marker in key.upper() for marker in ("API_KEY", "TOKEN", "SECRET", "PASSWORD")):
            value = value.strip().strip("\"'")
            if len(value) >= 12:
                secrets.append(value)
    for path in OUT.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        text = path.read_text()
        assert not any(secret in text for secret in secrets), "credential match"
        assert str(Path.home()) not in text, "local personal path"
        if path.suffix == ".json":
            check_fields(json.loads(text))
    audit = {"passed": True, "historical_files_and_source_verified": checked,
             "production_source_changed": False, "historical_evidence_changed": False,
             "packet_delta_only_file_edits": True, "old_byte_equivalent": True,
             "physical_requests": 1, "main_requests": 0, "verifier_requests": 1, "retries": 0,
             "credentials_scan": "pass", "private_reasoning_field_scan": "pass",
             "personal_path_scan": "pass", "cost": "unknown"}
    (OUT / "integrity-audit.json").write_text(json.dumps(audit, indent=2) + "\n")
    sums = {str(p.relative_to(OUT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(OUT.rglob("*")) if p.is_file() and p.name != "SHA256SUMS.json" and "__pycache__" not in p.parts}
    (OUT / "SHA256SUMS.json").write_text(json.dumps(sums, indent=2) + "\n")
    print(f"PASS: packet isolation, one physical request, immutable source/history ({checked} files), privacy and hashes")


if __name__ == "__main__":
    main()
