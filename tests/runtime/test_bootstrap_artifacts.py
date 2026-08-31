"""Tests for deterministic pre-index bootstrap artifact resolution."""
from __future__ import annotations


def _cron_fixture(tmp_path):
    package = tmp_path / "cron_engine"
    tests = package / "tests"
    tests.mkdir(parents=True)
    for relative in (
        "__init__.py",
        "parser.py",
        "scheduler.py",
        "cli.py",
        "__main__.py",
        "README.md",
        "tests/__init__.py",
        "tests/test_parser.py",
        "tests/test_scheduler.py",
        "tests/test_cli.py",
    ):
        target = package / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# fixture\n", encoding="utf-8")


def test_cron_prompt_resolves_required_and_candidate_artifacts(tmp_path):
    """G6: bootstrap works before semantic repository intelligence is ready."""
    from nz_coder.intelligence.bootstrap_artifacts import (
        resolve_bootstrap_artifacts,
    )

    _cron_fixture(tmp_path)
    result = resolve_bootstrap_artifacts(
        (
            "完善 cron_engine：parser 支持 JAN-DEC 月份名称；保持现有数字 API "
            "兼容；补充 parser、scheduler、CLI 测试并更新 README。"
        ),
        workspace=tmp_path,
    )

    required = set(result.required_paths)
    assert "cron_engine/parser.py" in required
    assert "cron_engine/tests/test_parser.py" in required
    assert "cron_engine/tests/test_scheduler.py" in required
    assert "cron_engine/tests/test_cli.py" in required
    assert "cron_engine/README.md" in required
    assert result.artifact_count >= 5
    assert result.candidate_count > 0
    assert "cron_engine/scheduler.py" in result.candidate_paths
    assert "cron_engine/cli.py" in result.candidate_paths
    assert "cron_engine/__main__.py" in result.candidate_paths


def test_real_cron_prompt_ignores_numeric_and_directory_slash_tokens(tmp_path):
    """Slash-shaped prose/acceptance targets are not source artifacts."""
    from nz_coder.intelligence.bootstrap_artifacts import (
        resolve_bootstrap_artifacts,
    )

    _cron_fixture(tmp_path)
    result = resolve_bootstrap_artifacts(
        (
            "完善 cron_engine：让 month 字段支持 JAN-DEC 月份名称，让 "
            "day_of_week 字段支持 SUN-SAT 星期名称，名称大小写不敏感，并支持"
            "名称的单值、列表、范围和步长组合；保持现有数字 API 与 0/7 周日兼容。"
            "补充 parser、scheduler、CLI 测试并更新 README。完成后运行 "
            "python -m pytest -q cron_engine/tests。"
        ),
        workspace=tmp_path,
    )

    required = set(result.required_paths)
    assert "0/7" not in required
    assert "cron_engine/tests" not in required
    assert "cron_engine/parser.py" in required
    assert "cron_engine/tests/test_parser.py" in required
    assert "cron_engine/tests/test_scheduler.py" in required
    assert "cron_engine/tests/test_cli.py" in required
    assert "cron_engine/README.md" in required


def test_contract_splits_requested_test_files_into_distinct_requirements(tmp_path):
    """Each determinable requested test file has independent mutation evidence."""
    from nz_coder.runtime.agent.task_contract import derive_task_contract

    _cron_fixture(tmp_path)
    contract = derive_task_contract(
        (
            "完善 cron_engine：parser 支持 JAN-DEC 月份名称；保持现有数字 API "
            "兼容；补充 parser、scheduler、CLI 测试并更新 README。"
        ),
        acceptance_command="python -m pytest -q cron_engine/tests",
        workspace=tmp_path,
    )

    test_requirements = [
        item for item in contract.requirements if item.kind == "test"
    ]
    assert {item.expected_artifacts for item in test_requirements} == {
        ("cron_engine/tests/test_parser.py",),
        ("cron_engine/tests/test_scheduler.py",),
        ("cron_engine/tests/test_cli.py",),
    }


def test_real_cron_contract_binds_behavior_to_parser_not_slash_prose(tmp_path):
    from nz_coder.runtime.agent.task_contract import derive_task_contract

    _cron_fixture(tmp_path)
    contract = derive_task_contract(
        (
            "完善 cron_engine：让 month 字段支持 JAN-DEC，让 day_of_week 字段"
            "支持 SUN-SAT；保持现有数字 API 与 0/7 周日兼容。补充 parser、"
            "scheduler、CLI 测试并更新 README。"
        ),
        acceptance_command="python -m pytest -q cron_engine/tests",
        workspace=tmp_path,
    )

    behavior = next(item for item in contract.requirements if item.kind == "behavior")
    assert behavior.expected_artifacts == ("cron_engine/parser.py",)


def test_bootstrap_explicit_allowlist_rejects_traceback_source_path(tmp_path):
    """Existing traceback files are evidence, not hard mutation artifacts."""
    from nz_coder.intelligence.bootstrap_artifacts import (
        resolve_bootstrap_artifacts,
    )

    source = tmp_path / "src" / "_pytest" / "runner.py"
    source.parent.mkdir(parents=True)
    source.write_text("# traceback frame\n", encoding="utf-8")
    (tmp_path / "collector.c").write_text("/* extension frame */\n", encoding="utf-8")

    result = resolve_bootstrap_artifacts(
        (
            "Fix initialization behavior. Traceback (most recent call last): "
            "File src/_pytest/runner.py, line 42; collector.c was loaded."
        ),
        workspace=tmp_path,
        explicit_path_allowlist=(),
    )

    assert "src/_pytest/runner.py" not in result.required_paths
    assert "collector.c" not in result.required_paths


def test_bootstrap_explicit_allowlist_keeps_positive_write_target(tmp_path):
    """The Runtime-approved mutation path remains a deterministic artifact."""
    from nz_coder.intelligence.bootstrap_artifacts import (
        resolve_bootstrap_artifacts,
    )

    source = tmp_path / "src" / "parser.py"
    source.parent.mkdir(parents=True)
    source.write_text("# parser\n", encoding="utf-8")

    result = resolve_bootstrap_artifacts(
        "Fix src/parser.py and preserve existing behavior.",
        workspace=tmp_path,
        explicit_path_allowlist=("src/parser.py",),
    )

    assert "src/parser.py" in result.required_for("behavior")


def test_bootstrap_explicit_allowlist_supports_positive_basename_target(tmp_path):
    """A basename can be hard evidence when Runtime coupled it to a write verb."""
    from nz_coder.intelligence.bootstrap_artifacts import (
        resolve_bootstrap_artifacts,
    )

    (tmp_path / "parser.py").write_text("# parser\n", encoding="utf-8")
    result = resolve_bootstrap_artifacts(
        "Fix parser.py and preserve existing behavior.",
        workspace=tmp_path,
        explicit_path_allowlist=("parser.py",),
    )

    assert "parser.py" in result.required_for("behavior")


def test_bootstrap_allowlist_does_not_disable_semantic_surface_resolution(tmp_path):
    """Filtering literal paths must preserve project-creation surface inference."""
    from nz_coder.intelligence.bootstrap_artifacts import (
        resolve_bootstrap_artifacts,
    )

    _cron_fixture(tmp_path)
    result = resolve_bootstrap_artifacts(
        "完善 cron_engine：parser 支持月份名称；补充 parser 测试并更新 README。",
        workspace=tmp_path,
        explicit_path_allowlist=(),
    )

    assert "cron_engine/parser.py" in result.required_for("behavior")
    assert "cron_engine/tests/test_parser.py" in result.required_for("test")
