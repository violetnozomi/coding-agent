"""First-turn implementation workset coverage."""
from __future__ import annotations


def _contract(tmp_path):
    from nz_coder.runtime.agent.task_contract import parse_task_contract

    return parse_task_contract({
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
    ) is True


def test_requirement_rich_contract_activates_bundle_without_semantic_judgment(
    tmp_path,
):
    from nz_coder.intelligence.implementation_bundle import (
        should_build_implementation_bundle,
    )

    assert should_build_implementation_bundle(
        _contract(tmp_path),
    ) is True


def test_single_target_contract_provides_available_material(tmp_path):
    from nz_coder.intelligence.implementation_bundle import (
        should_build_implementation_bundle,
    )
    from nz_coder.runtime.agent.task_contract import parse_task_contract

    contract = parse_task_contract({
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
    ) is True


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


def test_bundle_selects_named_function_after_boilerplate_and_file_header(tmp_path):
    from nz_coder.intelligence.implementation_bundle import build_implementation_bundle
    from nz_coder.runtime.agent.task_contract import parse_task_contract

    source = tmp_path / "matcher.py"
    source.write_text(
        "# This file implements support for matching versions and package names.\n"
        + "# License and general documentation for the project.\n" * 60
        + "def editions_match(left, right):\n"
        "    return left == right  # regression: unspecified edition\n"
        + "\ndef unrelated():\n    return None\n" * 40,
    )
    contract = parse_task_contract({
        "objective": "Fix edition matching",
        "requirements": [{"id": "R1", "description": "Handle unspecified editions",
                          "kind": "behavior", "expected_artifacts": ["matcher.py"]}],
    })
    bundle = build_implementation_bundle(
        query=("Please carefully inspect the available repository files before implementing "
               "and verifying the requested changes using local tools and installed dependencies. " * 20
               + "Fix `editions_match` in matcher.py so unspecified editions match."),
        contract=contract, execution_facts={}, workspace=tmp_path,
    )
    assert "def editions_match(left, right):" in bundle
    assert "return left == right" in bundle
    assert "# This file implements" not in bundle
    assert len(bundle) <= 9600


def test_empty_contract_does_not_activate_implementation_bundle():
    from nz_coder.intelligence.implementation_bundle import should_build_implementation_bundle
    from nz_coder.runtime.verification.domain.requirements import TaskContract

    assert should_build_implementation_bundle(TaskContract()) is False


def test_contract_requirement_without_artifact_is_available_material():
    from nz_coder.intelligence.implementation_bundle import should_build_implementation_bundle
    from nz_coder.runtime.agent.task_contract import parse_task_contract

    contract = parse_task_contract({
        "objective": "Explain cancellation",
        "requirements": [{"id": "R1", "kind": "behavior", "description": "Trace the cancellation chain"}],
    })
    assert should_build_implementation_bundle(contract) is True
