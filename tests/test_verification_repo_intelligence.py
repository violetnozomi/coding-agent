from __future__ import annotations


def _indirect_pricing_fixture(root) -> None:
    (root / "src" / "domain").mkdir(parents=True)
    (root / "src" / "application").mkdir(parents=True)
    (root / "tests" / "integration").mkdir(parents=True)
    for package in (
        root / "src" / "__init__.py",
        root / "src" / "domain" / "__init__.py",
        root / "src" / "application" / "__init__.py",
        root / "tests" / "__init__.py",
        root / "tests" / "integration" / "__init__.py",
    ):
        package.write_text("", encoding="utf-8")
    (root / "src" / "domain" / "pricing.py").write_text(
        "def calculate_total(subtotal):\n    return subtotal + 5\n",
        encoding="utf-8",
    )
    (root / "src" / "application" / "checkout.py").write_text(
        "from src.domain.pricing import calculate_total\n\n"
        "def checkout(subtotal):\n    return {'total': calculate_total(subtotal)}\n",
        encoding="utf-8",
    )
    (root / "tests" / "integration" / "test_checkout.py").write_text(
        "from src.application.checkout import checkout\n\n"
        "def test_checkout_total():\n    assert checkout(10) == {'total': 15}\n",
        encoding="utf-8",
    )


def test_ri_related_tests_reaches_indirect_checkout_test_beyond_planner_heuristic(
    tmp_path,
) -> None:
    from nz_coder.intelligence.service import RepoIntelligenceService
    from nz_coder.intelligence.verification_planner import plan_verification_commands
    from nz_coder.runtime.workdir import scoped_workdir

    _indirect_pricing_fixture(tmp_path)
    profile = {
        "test_roots": ["tests"],
        "test_commands": ["pytest"],
        "typecheck_commands": [],
    }
    with scoped_workdir(tmp_path):
        current = plan_verification_commands(
            changed_files=["src/domain/pricing.py"],
            project_profile=profile,
            use_repo_intelligence=False,
        )
        current_targets = {
            item["command"]
            for stage in current["stages"]
            for item in stage["commands"]
        }
        service = RepoIntelligenceService(tmp_path)
        try:
            service.prewarm(max_files=50).result(timeout=10)
            scope = service.changed_scope(
                changed_paths=["src/domain/pricing.py"],
                max_depth=4,
                node_limit=100,
                time_budget_ms=1000,
                wait_budget_ms=1000,
            )
        finally:
            service.close()

    assert "pytest tests/integration/test_checkout.py" not in current_targets
    assert "tests/integration/test_checkout.py" in scope["related_tests"]


def test_planner_ri_evidence_catches_regression_missed_by_current_plan(tmp_path) -> None:
    import subprocess

    from nz_coder.intelligence.service import workspace_repo_intelligence
    from nz_coder.intelligence.verification_planner import plan_verification_commands
    from nz_coder.runtime.workdir import scoped_workdir

    _indirect_pricing_fixture(tmp_path)
    profile = {
        "test_roots": ["tests"],
        "test_commands": ["pytest"],
        "typecheck_commands": [],
    }
    # A syntactically valid but behaviorally wrong public change.
    (tmp_path / "src" / "domain" / "pricing.py").write_text(
        "def calculate_total(subtotal):\n    return subtotal + 6\n",
        encoding="utf-8",
    )
    with scoped_workdir(tmp_path):
        current = plan_verification_commands(
            changed_files=["src/domain/pricing.py"],
            project_profile=profile,
            use_repo_intelligence=False,
        )
        service = workspace_repo_intelligence(tmp_path, max_files=50)
        assert service is not None
        service.prewarm(max_files=50).result(timeout=10)
        revised = plan_verification_commands(
            changed_files=["src/domain/pricing.py"],
            project_profile=profile,
            use_repo_intelligence=True,
        )

    current_required = [
        item["command"]
        for stage in current["stages"]
        for item in stage["commands"]
        if item["required"]
    ]
    revised_selected = [
        item["command"]
        for stage in revised["stages"]
        for item in stage["commands"]
    ]
    assert current_required == ["python -m py_compile src/domain/pricing.py"]
    assert "pytest tests/integration/test_checkout.py" in revised_selected
    current_results = [
        subprocess.run(
            command, cwd=tmp_path, shell=True, check=False,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        ).returncode
        for command in current_required
    ]
    revised_runs = [
        subprocess.run(
            command, cwd=tmp_path, shell=True, check=False,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        for command in revised_selected
    ]
    assert current_results == [0]
    assert any(
        completed.returncode == 1 and "test_checkout_total" in completed.stdout
        for completed in revised_runs
    ), [completed.stdout for completed in revised_runs]


def test_verification_gate_surfaces_ri_related_test_without_making_it_required(
    tmp_path,
) -> None:
    from nz_coder.intelligence.service import workspace_repo_intelligence
    from nz_coder.intelligence.verification import VerificationManager
    from nz_coder.recovery import RecoveryState
    from nz_coder.runtime.workdir import scoped_workdir
    from nz_coder.trace import TraceRecorder

    _indirect_pricing_fixture(tmp_path)
    with scoped_workdir(tmp_path):
        service = workspace_repo_intelligence(tmp_path, max_files=50)
        assert service is not None
        service.prewarm(max_files=50).result(timeout=10)
        manager = VerificationManager(RecoveryState(), TraceRecorder())
        manager.mark_write("write_file", {"path": "src/domain/pricing.py"})

        message = manager.make_gate_message()
        pipeline = manager.status()["verification_pipeline"]

    assert "Recommended high-confidence related checks:" in message
    assert "pytest tests/integration/test_checkout.py" in message
    related = [
        item
        for stage in pipeline["stages"]
        for item in stage["commands"]
        if item["command"] == "pytest tests/integration/test_checkout.py"
    ]
    assert len(related) == 1
    assert related[0]["required"] is False
