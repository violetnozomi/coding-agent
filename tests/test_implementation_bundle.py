"""First-turn implementation workset coverage."""
from __future__ import annotations


def _contract(tmp_path):
    from nz_coder.runtime.agent.task_contract import TaskContract

    return TaskContract.from_dict({
        "objective": "Add named cron fields",
        "requirements": [
            {
                "id": "R1",
                "description": "Support JAN-DEC names",
                "kind": "behavior",
                "expected_artifacts": ["cron_engine/parser.py"],
            },
            {
                "id": "R2",
                "description": "Add parser tests",
                "kind": "test",
                "expected_artifacts": ["cron_engine/tests/test_parser.py"],
            },
            {
                "id": "R3",
                "description": "Update cron syntax docs",
                "kind": "docs",
                "expected_artifacts": ["README.md"],
            },
        ],
        "constraints": ["Preserve numeric fields"],
    }, workspace=tmp_path)


def test_multi_artifact_contract_activates_implementation_bundle(tmp_path):
    from nz_coder.intelligence.implementation_bundle import (
        should_build_implementation_bundle,
    )

    assert should_build_implementation_bundle(
        _contract(tmp_path),
        text_complexity="moderate",
        task_mode="feature",
    ) is True


def test_requirement_rich_contract_activates_bundle_when_text_heuristic_is_simple(
    tmp_path,
):
    from nz_coder.intelligence.implementation_bundle import (
        should_build_implementation_bundle,
    )

    assert should_build_implementation_bundle(
        _contract(tmp_path),
        text_complexity="simple",
        task_mode="test",
    ) is True


def test_simple_contract_does_not_activate_implementation_bundle(tmp_path):
    from nz_coder.intelligence.implementation_bundle import (
        should_build_implementation_bundle,
    )
    from nz_coder.runtime.agent.task_contract import TaskContract

    contract = TaskContract.from_dict({
        "objective": "Edit one file",
        "requirements": [{
            "id": "R1",
            "description": "Change value",
            "kind": "artifact",
            "expected_artifacts": ["app.py"],
        }],
    }, workspace=tmp_path)

    assert should_build_implementation_bundle(
        contract,
        text_complexity="simple",
        task_mode="feature",
    ) is False


def test_bundle_contains_contract_execution_facts_and_bounded_snippets(tmp_path):
    from nz_coder.intelligence.implementation_bundle import build_implementation_bundle

    parser = tmp_path / "cron_engine" / "parser.py"
    parser.parent.mkdir(parents=True)
    parser.write_text(
        "class CronField:\n"
        "    def parse(self, value):\n"
        "        return int(value)\n",
        encoding="utf-8",
    )
    test_file = tmp_path / "cron_engine" / "tests" / "test_parser.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_numeric():\n    assert True\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Cron syntax\nNumeric fields.\n", encoding="utf-8")
    facts = {
        "workspace_root": str(tmp_path),
        "project_root": str(tmp_path),
        "source_roots": ["cron_engine"],
        "test_roots": ["cron_engine/tests"],
        "python_packages": [{
            "module_name": "cron_engine",
            "package_path": "cron_engine",
            "module_cwd": str(tmp_path),
        }],
        "test_commands": ["pytest"],
        "entrypoints": [],
    }

    bundle = build_implementation_bundle(
        query="Support JAN-DEC and add parser tests and docs",
        contract=_contract(tmp_path),
        execution_facts=facts,
        workspace=tmp_path,
        candidate_files=("cron_engine/parser.py",),
        token_budget=1500,
    )

    assert bundle.startswith("<implementation-bundle>")
    assert "R1 [behavior] Support JAN-DEC names" in bundle
    assert f"module_cwd={tmp_path}" in bundle
    assert "cron_engine/parser.py" in bundle
    assert "class CronField" in bundle
    assert len(bundle) <= 6000


def test_bundle_rejects_candidate_outside_workspace(tmp_path):
    from nz_coder.intelligence.implementation_bundle import build_implementation_bundle

    bundle = build_implementation_bundle(
        query="change code",
        contract=_contract(tmp_path),
        execution_facts={"workspace_root": str(tmp_path)},
        workspace=tmp_path,
        candidate_files=("../secret.txt",),
        token_budget=1500,
    )

    assert "../secret.txt" not in bundle
