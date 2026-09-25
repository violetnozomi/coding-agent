"""Offline immutable-input, scope and packet integrity verification."""

import hashlib
import json
from pathlib import Path
import subprocess

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
BASE = "8bc90584eedbb696b497e885a8989ac1885c357b"
ALLOWED = {
    "nz_coder/intelligence/analyzers.py",
    "nz_coder/intelligence/code_index.py",
    "nz_coder/runtime/verification/sidecar_verifier.py",
    "nz_coder/runtime/verification/constructor_evidence.py",
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    frozen = json.loads((HERE / "baseline/source-hashes.json").read_text())
    for path, expected in frozen.items():
        if path not in ALLOWED:
            assert digest(ROOT / path) == expected, path
    changed = subprocess.check_output(
        ["git", "diff", BASE, "--name-only"], cwd=ROOT, text=True
    ).splitlines()
    assert all(p in ALLOWED for p in changed if p.startswith("nz_coder/"))
    old = subprocess.check_output(
        ["git", "show", BASE + ":nz_coder/runtime/verification/sidecar_verifier.py"],
        cwd=ROOT,
        text=True,
    )
    new = (ROOT / "nz_coder/runtime/verification/sidecar_verifier.py").read_text()
    # All prompt constants and verdict definitions before VerifierContext unchanged.
    assert (
        old[old.index("SEMANTIC_CONTRACT_CERTIFICATION") : old.index("@dataclass")]
        == new[new.index("SEMANTIC_CONTRACT_CERTIFICATION") : new.index("@dataclass")]
    )
    before = json.loads((HERE / "c/before-packet.json").read_text())["context"]
    after = json.loads((HERE / "c/after-packet.json").read_text())["context"]
    assert {k for k in before if before[k] != after[k]} == {
        "supporting_repository_evidence",
        "supporting_repository_digest",
    }
    assert after["supporting_repository_evidence"].startswith(
        before["supporting_repository_evidence"] + "\n"
    )
    section = after["supporting_repository_evidence"][
        len(before["supporting_repository_evidence"]) + 1 :
    ]
    assert "@dataclass(frozen=True)\nclass Config:" in section and len(section) <= 2400
    for forbidden in [
        "11/12",
        "invalid_write_preserves_destination",
        "oracle/",
        "acceptance/",
    ]:
        assert (
            forbidden
            not in json.loads((HERE / "c/after-packet.json").read_text())["packet"]
        )
    manifest = json.loads((HERE / "SHA256SUMS.json").read_text())
    actual = {
        p.relative_to(HERE).as_posix()
        for p in HERE.rglob("*")
        if p.is_file() and p.name != "SHA256SUMS.json" and "__pycache__" not in p.parts
    }
    assert actual == set(manifest)
    for path, expected in manifest.items():
        assert digest(HERE / path) == expected, path
    print(
        f"PASS: {len(manifest)} evidence hashes; historical/frozen/source boundaries; prompt and packet isolation"
    )


if __name__ == "__main__":
    main()
