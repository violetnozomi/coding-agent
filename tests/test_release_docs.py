"""Reader-facing regression checks for the documented release boundary."""
from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
ARCHITECTURE = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
BASELINE_PATH = ROOT / "docs" / "release-baseline.md"
BASELINE = BASELINE_PATH.read_text(encoding="utf-8")
LEARNING_LOG = (ROOT / "docs" / "infcode-alignment-learning-log.md").read_text(
    encoding="utf-8"
)
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
RELEASE_CHECKLIST = (ROOT / "docs" / "release-checklist.md").read_text(
    encoding="utf-8"
)
FINAL_REPORT_PATH = ROOT / "docs" / "terminal-product-parity-final-report-2026-08-13.md"


def test_release_baseline_answers_new_reader_boundary_questions():
    """A new reader can distinguish supported, deferred, and product work."""
    required = (
        "local terminal coding Agent",
        "loopback Session service",
        "## Supported Core Baseline",
        "## Frozen By Default",
        "## Deferred Evidence",
        "## Consumer-Driven Only",
        "## Known Limitations",
        "SWE-bench",
        "public interoperability",
        "Dodo/PySide parallel product was removed",
    )
    for phrase in required:
        assert phrase in BASELINE, f"release boundary no longer answers: {phrase}"


def test_supported_entry_points_are_visible_without_network_actions():
    """The baseline names the local interfaces and separates network actions."""
    for command in (
        "nz-coder                         terminal REPL",
        "nz-coder init",
        "nz-coder doctor",
        "nz-coder serve",
        "nz-coder mcp ...",
        "nz-coder models ...",
        "nz-coder extensions ...",
        "python -m nz_coder --help",
        "nz-coder doctor --json",
        "pytest -q",
    ):
        assert command in BASELINE
    assert "Commands that perform network or process actions remain explicit" in BASELINE


def test_current_docs_do_not_restore_superseded_architecture_claims():
    """Current overview documents must not describe removed or completed gaps."""
    combined = README + "\n" + ARCHITECTURE
    stale_claims = (
        "limited to local stdio servers",
        "does not provide remote HTTP/SSE",
        "Unchanged files reuse an in-process cache",
        "nz_coder/dodo/     #",
        "existing PySide client",
    )
    for claim in stale_claims:
        assert claim not in combined, f"superseded architecture claim returned: {claim}"
    assert "docs/release-baseline.md" in README
    assert "docs/release-checklist.md" in README
    assert "Removed Dodo Parallel Architecture" in ARCHITECTURE


def test_historical_gap_matrix_points_to_a036_and_baseline():
    """The chronological audit cannot be mistaken for the current backlog."""
    assert "历史差距再审计（A028 时点，已由 A036 取代）" in LEARNING_LOG
    assert "## 39. A036：当前差距再审计与 release baseline" in LEARNING_LOG
    assert "不应再把本节表格当作当前 backlog" in LEARNING_LOG
    assert "A046：真实 Ctrl+C 故障修复与全能力证据再审计" in LEARNING_LOG
    assert "应以 A046 的分级矩阵为准" in LEARNING_LOG


def test_release_baseline_relative_links_exist():
    """Local links in the baseline resolve from the document directory."""
    links = re.findall(r"\[[^]]+\]\(([^)]+)\)", BASELINE)
    assert links
    for target in links:
        if "://" in target or target.startswith("#"):
            continue
        path_text = target.split("#", 1)[0]
        assert (BASELINE_PATH.parent / path_text).exists(), f"broken link: {target}"


def test_release_linter_version_is_reproducible():
    """Release linting must not silently change when Ruff changes defaults."""
    assert 'ruff==0.15.10' in PYPROJECT
    assert 'python -m pip install -e ".[dev]"' in RELEASE_CHECKLIST


def test_bundled_prompt_commands_are_included_in_distribution_data():
    assert '"bundled_commands/*.md"' in PYPROJECT


def test_final_product_report_has_required_surfaces_and_eighty_capabilities():
    report = FINAL_REPORT_PATH.read_text(encoding="utf-8")
    rows = [line for line in report.splitlines() if re.match(r"^\| C\d{3} ", line)]

    assert len(rows) >= 80
    assert "| ID | Capability | nzcoder | InfCodeX | OpenCode | Verdict | Priority |" in report
    assert all(re.search(r"\| P[012] \|$", row) for row in rows)
    for phrase in (
        "Embedded | Headless | SDK | HTTP | Remote",
        "### vs InfCodeX",
        "### vs OpenCode",
        "内部/个人日常使用",
        "公开 GitHub",
        "Demo 感",
        "20/20",
        "LOCAL_UI_ONLY",
        "REMOTE_RUNTIME",
        "SHARED",
        "orphan_process_count",
    ):
        assert phrase in report


def test_reader_documentation_set_is_linked_from_readme():
    for target in (
        "docs/quick-start.md",
        "docs/cli-reference.md",
        "docs/remote-daemon.md",
        "docs/process.md",
        "docs/mcp.md",
        "docs/skills-and-commands.md",
        "docs/memory.md",
        "docs/troubleshooting.md",
    ):
        assert target in README
