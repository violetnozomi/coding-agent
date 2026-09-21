"""Offline publication audit. Never invokes a Provider or changes frozen runs."""
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[2]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def sanitize(value):
    if isinstance(value, dict):
        return {k: sanitize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if isinstance(value, str):
        return value.replace(str(ROOT), "<REPO>").replace(str(Path.home()), "<USER_HOME>")
    return value


def audit():
    for path in OUT.rglob("__pycache__"):
        shutil.rmtree(path)
    # Only newly generated analysis has not already been scrubbed and frozen.
    for path in (OUT / "analysis").rglob("*.json"):
        write(path, sanitize(json.loads(path.read_text())))

    failures = []
    invariants = {}
    for label in ("source", "historical"):
        expected = json.loads((OUT / "preflight" / f"{label}-hashes.json").read_text())
        changed = [p for p, h in expected.items() if not (ROOT / p).is_file() or digest(ROOT / p) != h]
        invariants[label] = {"files": len(expected), "changed": changed}
        failures.extend(f"{label}: {p}" for p in changed)
    for case in ("M", "Q"):
        base = OUT / case / "nzcoder"
        frozen = json.loads((base / "FROZEN.json").read_text())["sha256"]
        changed = [p for p, h in frozen.items() if not (base / p).is_file() or digest(base / p) != h]
        actual = {str(p.relative_to(base)) for p in base.rglob("*") if p.is_file() and p.name != "FROZEN.json"}
        unexpected = sorted(actual - frozen.keys())
        invariants[case] = {"frozen_files": len(frozen), "changed": changed, "unexpected": unexpected}
        failures.extend(f"{case}: {p}" for p in changed + unexpected)

    # Read secret values only for exact-value comparison; never print them.
    secrets = set()
    for k, v in os.environ.items():
        if re.search(r"API_KEY|TOKEN|PASSWORD|SECRET", k, re.I) and len(v) >= 16:
            secrets.add(v.encode())
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            k, sep, v = line.partition("=")
            v = v.strip().strip("\"'")
            if sep and re.search(r"API_KEY|TOKEN|PASSWORD|SECRET", k, re.I) and len(v) >= 16:
                secrets.add(v.encode())

    forbidden = {"reasoning_content", "reasoning", "authorization", "proxy-authorization", "cookie", "set-cookie", "api_key", "api-key", "password", "access_token", "refresh_token"}

    def inspect(value, label):
        if isinstance(value, dict):
            for key, item in value.items():
                token_count = key.lower() == "reasoning" and type(item) is int and item >= 0
                if key.lower() in forbidden and item and not token_count:
                    failures.append(f"private data key: {label}:{key}")
                inspect(item, label)
        elif isinstance(value, list):
            for item in value:
                inspect(item, label)

    files = [p for p in sorted(OUT.rglob("*")) if p.is_file() and p.name not in {"integrity-audit.json", "SHA256SUMS.json"}]
    for path in files:
        label = str(path.relative_to(OUT))
        data = path.read_bytes()
        if any(secret in data for secret in secrets):
            failures.append(f"credential value: {label}")
        if str(Path.home()).encode() in data or re.search(rb"(?:^|[\s\"':=])/(?:home|Users)/[A-Za-z0-9_.-]+/", data):
            failures.append(f"host user path: {label}")
        if re.search(rb"\bsk-[A-Za-z0-9_-]{20,}\b|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", data):
            failures.append(f"credential pattern: {label}")
        if path.suffix == ".json":
            inspect(json.loads(data), label)
        elif path.suffix == ".jsonl":
            for line in data.splitlines():
                if line.strip():
                    inspect(json.loads(line), label)

    production_diff = subprocess.check_output(["git", "diff", "45db8e38a8de58d404b6601a47b2fe5b518f248a", "--", "nz_coder"], cwd=ROOT)
    if production_diff:
        failures.append("production source diff")
    report = {
        "passed": not failures,
        "baseline": "45db8e38a8de58d404b6601a47b2fe5b518f248a",
        "invariants": invariants,
        "scanned_files": len(files),
        "checks": ["exact configured secret values (never emitted)", "credential patterns", "structured private reasoning/header keys", "host user paths", "frozen evidence hashes", "historical and production hashes"],
        "failures": failures,
        "real_main_requests": 24,
        "real_auxiliary_requests": 1,
        "infcodex_requests": 0,
        "cost": "unknown",
        "limitation": "Pattern and exact-value scans do not prove absence of every possible unknown secret.",
    }
    write(OUT / "integrity-audit.json", report)
    if failures:
        print(json.dumps(report, indent=2))
        raise SystemExit(1)
    manifest = {str(p.relative_to(OUT)): digest(p) for p in sorted(OUT.rglob("*")) if p.is_file() and p.name != "SHA256SUMS.json"}
    write(OUT / "SHA256SUMS.json", manifest)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    audit()
