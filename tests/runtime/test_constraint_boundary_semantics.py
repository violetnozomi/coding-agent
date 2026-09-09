"""Organizer-authored development oracles; not unseen tasks or model efficacy."""
from __future__ import annotations

import hashlib
import re
import subprocess
import sys
import unicodedata

import pytest


def test_historical_patch_still_fails_known_development_boundary(tmp_path):
    from evaluation.linux_baseline import runner
    from evaluation.linux_baseline.catalog import TASK_SPECS
    patch = runner.ROOT / "evaluation/linux_baseline/results/p2-live-20260909-062500/T07.patch"
    before = hashlib.sha256(patch.read_bytes()).hexdigest()
    spec = next(s for s in TASK_SPECS if s[0] == "T07")
    repo = tmp_path / "repo"
    runner.materialize(spec, repo, tmp_path / "home")
    runner.git(repo, "apply", "--binary", str(patch))
    result = subprocess.run([sys.executable, "-c", "from textkit import slugify; assert slugify('A_B') == 'ab'"],
        cwd=repo, capture_output=True, text=True, env=runner.isolated_environment(tmp_path / "home", repo))
    assert result.returncode == 1 and "AssertionError" in result.stderr
    original_tests = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests"], cwd=repo,
        capture_output=True, text=True, env=runner.isolated_environment(tmp_path / "home", repo))
    assert original_tests.returncode == 0 and "24 passed" in original_tests.stdout
    assert hashlib.sha256(patch.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("exception", ["-", "_"])
def test_category_exclusion_and_reverse_explicit_exception(exception):
    # Oracle: remove Unicode punctuation, except the explicitly retained character.
    def correct(text):
        return "".join(c for c in text if c == exception or not unicodedata.category(c).startswith("P"))
    def wrong(text):
        return re.sub(r"[^\w\s-]", "", text)
    text = "a_b-c!"
    expected = "ab-c" if exception == "-" else "a_bc"
    assert correct(text) == expected
    assert wrong(text) != expected
    assert exception in correct(text)  # No universal 'delete underscore' rule.


def test_single_consumption_order_and_duplicates():
    # Oracle: stable first occurrence order with a once-consumed iterable.
    def correct(items):
        return list(dict.fromkeys(items))

    def wrong(items):
        list(items)
        return list(dict.fromkeys(items))

    assert correct(iter([2, 1, 2])) == [2, 1]
    assert wrong(iter([2, 1, 2])) != [2, 1]


def test_validation_failure_preserves_existing_state():
    # Oracle: validate the entire proposed replacement before committing it.
    def correct(state, values):
        if not all(isinstance(v, int) for v in values):
            raise ValueError
        state[:] = values

    def wrong(state, values):
        state.clear()
        if not all(isinstance(v, int) for v in values):
            raise ValueError
        state[:] = values

    for operation, expected in [(correct, [9]), (wrong, [])]:
        state = [9]
        with pytest.raises(ValueError):
            operation(state, [1, "invalid"])
        assert state == expected
    state = [9]
    correct(state, [1, 2])
    assert state == [1, 2]
