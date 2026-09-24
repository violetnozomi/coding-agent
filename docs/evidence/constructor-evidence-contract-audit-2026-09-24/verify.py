"""Verify frozen inputs, audit outcomes and the complete evidence manifest offline."""

import hashlib
import json
from pathlib import Path
import subprocess

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def read(name):
    return json.loads((HERE / name).read_text())


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    frozen = read("baseline/source-hashes.json")
    for name, expected in frozen["sha256"].items():
        assert digest(ROOT / name) == expected, name
    changed = subprocess.check_output(
        ["git", "diff", frozen["baseline"], "--name-only"], cwd=ROOT, text=True
    ).splitlines()
    relative = HERE.relative_to(ROOT).as_posix() + "/"
    assert all(name.startswith(relative) for name in changed), changed
    manifest = read("SHA256SUMS.json")
    actual = {
        p.relative_to(HERE).as_posix()
        for p in HERE.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.name != "SHA256SUMS.json"
    }
    assert actual == set(manifest), actual ^ set(manifest)
    for name, expected in manifest.items():
        assert digest(HERE / name) == expected, name
    assert read("analysis/root-cause-boundary.json")["production_implemented"] is False
    c = read("envelope/frozen-c-config.json")
    assert c["edge"]["usage_role"] == "returned"
    assert c["envelope"]["text"].startswith("@dataclass(frozen=True)\nclass Config:")
    assert not read("envelope/parenthesized-decorator.json")["view"]["included"]
    assert "__post_init__" in read("envelope/post-init.json")["view"]["text"]
    assert "__init__" in read("envelope/explicit-init.json")["view"]["text"]
    assert not read("envelope/large-class.json")["view"]["allocation"]["selected"]
    for case in read("analysis/false-positive-budget.json")["cases"].values():
        assert case["max_two"]["rendered_chars"] <= 2400
        assert len(case["max_two"]["selected"]) <= 2
        assert all(
            x["envelope"]["rendered_chars"] <= 1400 for x in case["max_two"]["selected"]
        )
    lsp = read("provenance/lsp-resolved.json")
    assert lsp["returned"]["source"] == "lsp-definition"
    assert lsp["returned"]["usage_role"] == "returned"
    assert lsp["unknown"]["usage_role"] == "unknown"
    assert read("integrity-audit.json")["paid_requests"] == 0
    print(
        f"PASS: {len(manifest)} evidence hashes; {len(frozen['sha256'])} frozen production/history hashes; contract invariants"
    )


if __name__ == "__main__":
    main()
