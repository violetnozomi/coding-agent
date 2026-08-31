import json
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace


class _SleepingAgent:
    def __init__(self, *args, **kwargs):
        pass

    async def run(self, *args, **kwargs):
        time.sleep(5)
        return {"status": "completed"}


class _NoopAgent:
    def __init__(self, *args, **kwargs):
        pass

    async def run(self, messages, on_tool=None, stream=False):
        if on_tool:
            on_tool("bash", "ok")
        return {"status": "completed"}


class _LargeToolOutputAgent:
    """Exercise the real multiprocessing result channel above pipe capacity."""

    def __init__(self, *args, **kwargs):
        pass

    async def run(self, messages, on_tool=None, stream=False):
        if on_tool:
            for index in range(24):
                on_tool("read_file", f"event-{index}:" + ("x" * 3990))
        return {"status": "completed", "events": 24}


class _RaisingAgent:
    def __init__(self, *args, **kwargs):
        pass

    async def run(self, *args, **kwargs):
        raise ValueError("child boom")


class _AbruptExitAgent:
    def __init__(self, *args, **kwargs):
        pass

    async def run(self, *args, **kwargs):
        import os

        os._exit(7)


class _LingeringThreadAgent:
    """Return a result while a non-daemon dependency thread stays alive."""

    def __init__(self, *args, **kwargs):
        pass

    async def run(self, messages, on_tool=None, stream=False):
        threading.Thread(target=time.sleep, args=(30,), daemon=False).start()
        if on_tool:
            on_tool("lsp", "analysis complete")
        return {"status": "completed"}


# ── FailureFeedback / adapter / feedback formatting ───────────────────────────

def test_official_failure_feedback_summarizes_report_and_output(tmp_path):
    from nz_coder.swebench.adapter import SWEBenchAdapter

    instance_id = "astropy__astropy-14182"
    instance_dir = tmp_path / "logs" / instance_id
    instance_dir.mkdir(parents=True)
    (instance_dir / "report.json").write_text(json.dumps({
        instance_id: {
            "resolved": False,
            "patch_successfully_applied": True,
            "tests_status": {
                "FAIL_TO_PASS": {
                    "success": [],
                    "failure": ["astropy/io/ascii/tests/test_rst.py::test_rst_with_header_rows"],
                },
                "PASS_TO_PASS": {
                    "success": ["astropy/io/ascii/tests/test_rst.py::test_read_normal"],
                    "failure": ["astropy/io/ascii/tests/test_rst.py::test_write_normal"],
                },
            },
        }
    }), encoding="utf-8")
    (instance_dir / "test_output.txt").write_text(
        "\x1b[31mFAILURES\x1b[0m\n"
        "FAILED astropy/io/ascii/tests/test_rst.py::test_rst_with_header_rows "
        "- ValueError: Column wave failed to convert: could not convert string to float: 'float64'\n",
        encoding="utf-8",
    )

    adapter = SWEBenchAdapter()
    fb = adapter.load_feedback(instance_id, tmp_path / "logs")
    feedback = fb.to_agent_prompt()

    assert "<official-swebench-feedback>" in feedback
    assert "Official resolved: False" in feedback
    assert "FAIL_TO_PASS: astropy/io/ascii/tests/test_rst.py::test_rst_with_header_rows" in feedback
    assert "PASS_TO_PASS: astropy/io/ascii/tests/test_rst.py::test_write_normal" in feedback
    assert "Regression failures:" in feedback
    assert "<regression-guard>" in feedback
    assert "First restore every listed PASS_TO_PASS regression" in feedback
    assert "Do not replace existing reader/writer paths wholesale" in feedback
    assert "Do not delete an existing override method" in feedback
    assert "Do not delete existing classes" in feedback
    assert "Do not add new read/write/process_lines methods" in feedback
    assert "Prefer tiny index/default/parameter-forwarding changes" in feedback
    assert "derive indexes from" in feedback
    assert "`len(header_rows)`" in feedback
    assert "Preserve public API signatures" in feedback
    assert "warning text, stdout/stderr text, and error messages" in feedback
    assert "set_script_prefix()" in feedback
    assert "avoid top-level imports from `django.urls`" in feedback
    assert "Preserve every passing PASS_TO_PASS test" in feedback
    assert "ValueError: Column wave failed to convert" in feedback
    assert "\x1b[" not in feedback

    assert fb.has_regressions is True
    assert fb.pass_to_pass == [
        "PASS_TO_PASS: astropy/io/ascii/tests/test_rst.py::test_write_normal"
    ]
    assert len(fb.passing_tests) == 1


def test_official_failure_feedback_without_regression_uses_retry_constraints(tmp_path):
    from nz_coder.swebench.adapter import SWEBenchAdapter

    instance_id = "django__django-10914"
    instance_dir = tmp_path / "logs" / instance_id
    instance_dir.mkdir(parents=True)
    (instance_dir / "report.json").write_text(json.dumps({
        instance_id: {
            "resolved": False,
            "patch_successfully_applied": True,
            "tests_status": {
                "FAIL_TO_PASS": {
                    "success": [],
                    "failure": ["tests.test_target"],
                },
                "PASS_TO_PASS": {
                    "success": ["tests.test_existing"],
                    "failure": [],
                },
            },
        }
    }), encoding="utf-8")
    (instance_dir / "test_output.txt").write_text("FAILED tests.test_target - AssertionError\n", encoding="utf-8")

    adapter = SWEBenchAdapter()
    fb = adapter.load_feedback(instance_id, tmp_path / "logs")
    feedback = fb.to_agent_prompt()

    assert "<retry-constraints>" in feedback
    assert "<regression-guard>" not in feedback
    assert "preserve the 1 official passing tests" in feedback
    assert "public APIs backward compatible" in feedback
    assert "asserted warning/stdout/error text exactly" in feedback
    assert "set_script_prefix()" in feedback

    assert fb.has_regressions is False
    assert fb.pass_to_pass == []


def test_parse_deleted_methods_finds_removed_defs():
    from nz_coder.swebench.guardrail import PatchGuardrail

    patch = (
        "diff --git a/astropy/io/ascii/rst.py b/astropy/io/ascii/rst.py\n"
        "index abc..def 100644\n"
        "--- a/astropy/io/ascii/rst.py\n"
        "+++ b/astropy/io/ascii/rst.py\n"
        "@@ -40,10 +40,4 @@\n"
        " class RST:\n"
        "-    def write(self, lines):\n"
        "-        pass\n"
        "-    def _write_header(self):\n"
        "-        return ''\n"
        "+    pass\n"
    )
    result = PatchGuardrail()._parse_deleted_methods_raw(patch)
    assert result == {"astropy/io/ascii/rst.py": ["write", "_write_header"]}


def test_parse_deleted_methods_ignores_added_and_context_lines():
    from nz_coder.swebench.guardrail import PatchGuardrail

    patch = (
        "+++ b/pkg/module.py\n"
        "+    def new_method(self):\n"     # added line — should be ignored
        "     def context_method(self):\n"  # context line — should be ignored
        "-    def removed_method(self):\n"
    )
    result = PatchGuardrail()._parse_deleted_methods_raw(patch)
    assert result == {"pkg/module.py": ["removed_method"]}


def test_parse_deleted_methods_returns_empty_when_no_deletions():
    from nz_coder.swebench.guardrail import PatchGuardrail

    patch = (
        "+++ b/pkg/module.py\n"
        "+    def new_method(self):\n"
        "+        pass\n"
    )
    assert PatchGuardrail()._parse_deleted_methods_raw(patch) == {}


def test_regression_guard_includes_deleted_method_names(tmp_path):
    from nz_coder.swebench.adapter import SWEBenchAdapter

    instance_id = "astropy__astropy-14182"
    instance_dir = tmp_path / "logs" / instance_id
    instance_dir.mkdir(parents=True)
    (instance_dir / "report.json").write_text(json.dumps({
        instance_id: {
            "resolved": False,
            "patch_successfully_applied": True,
            "tests_status": {
                "FAIL_TO_PASS": {
                    "success": [],
                    "failure": ["astropy/io/ascii/tests/test_rst.py::test_rst_with_header_rows"],
                },
                "PASS_TO_PASS": {
                    "success": [],
                    "failure": ["astropy/io/ascii/tests/test_rst.py::test_write_normal"],
                },
            },
        }
    }), encoding="utf-8")
    (instance_dir / "test_output.txt").write_text("FAILED test_write_normal\n", encoding="utf-8")

    previous_patch = (
        "diff --git a/astropy/io/ascii/rst.py b/astropy/io/ascii/rst.py\n"
        "index abc..def 100644\n"
        "--- a/astropy/io/ascii/rst.py\n"
        "+++ b/astropy/io/ascii/rst.py\n"
        "@@ -50,5 +50,2 @@\n"
        "-    def write(self, lines):\n"
        "-        self._separator()\n"
        "+    pass\n"
    )
    fb = SWEBenchAdapter().load_feedback(instance_id, tmp_path / "logs")
    feedback = fb.to_agent_prompt(previous_patch=previous_patch)
    assert "astropy/io/ascii/rst.py: write" in feedback
    assert "Likely culprits" in feedback
    assert "<regression-guard>" in feedback


def test_regression_guard_includes_risky_added_methods_and_broad_except(tmp_path):
    from nz_coder.swebench.adapter import SWEBenchAdapter

    instance_id = "astropy__astropy-14182"
    instance_dir = tmp_path / "logs" / instance_id
    instance_dir.mkdir(parents=True)
    (instance_dir / "report.json").write_text(json.dumps({
        instance_id: {
            "resolved": False,
            "patch_successfully_applied": True,
            "tests_status": {
                "FAIL_TO_PASS": {
                    "success": [],
                    "failure": ["astropy/io/ascii/tests/test_rst.py::test_rst_with_header_rows"],
                },
                "PASS_TO_PASS": {
                    "success": [],
                    "failure": ["astropy/io/ascii/tests/test_rst.py::test_write_normal"],
                },
            },
        }
    }), encoding="utf-8")
    (instance_dir / "test_output.txt").write_text("FAILED test_write_normal\n", encoding="utf-8")

    previous_patch = (
        "diff --git a/astropy/io/ascii/rst.py b/astropy/io/ascii/rst.py\n"
        "--- a/astropy/io/ascii/rst.py\n"
        "+++ b/astropy/io/ascii/rst.py\n"
        "@@ -20,2 +20,12 @@\n"
        " class SimpleRSTHeader(FixedWidthHeader):\n"
        "+    def write(self, lines):\n"
        "+        try:\n"
        "+            return lines\n"
        "+        except:\n"
        "+            return []\n"
    )

    fb = SWEBenchAdapter().load_feedback(instance_id, tmp_path / "logs")
    feedback = fb.to_agent_prompt(previous_patch=previous_patch)

    assert "new reader/writer methods added by the previous patch" in feedback
    assert "astropy/io/ascii/rst.py: SimpleRSTHeader.write" in feedback
    assert "previous patch added a broad except fallback" in feedback


def test_should_not_apply_structurally_risky_previous_patch_under_regression():
    from nz_coder.swebench.adapter import SWEBenchAdapter
    from nz_coder.swebench.guardrail import PatchGuardrail
    from nz_coder.swebench.models import FailureFeedback
    from nz_coder.swebench.orchestrator import RetryOrchestrator

    previous_patch = (
        "+++ b/astropy/io/ascii/rst.py\n"
        " class SimpleRSTHeader(FixedWidthHeader):\n"
        "+    def write(self, lines):\n"
        "+        try:\n"
        "+            return lines\n"
        "+        except:\n"
        "+            return []\n"
    )

    fb = FailureFeedback(
        instance_id="x", resolved=False, patch_applied=True,
        fail_to_pass=[], pass_to_pass=["PASS_TO_PASS: test_x"],
        passing_tests=[], output_excerpt="",
    )
    g = PatchGuardrail()
    risk = g.analyze(previous_patch, regression_context=True)
    orch = RetryOrchestrator(SWEBenchAdapter(), g)
    assert orch._should_apply_previous_patch(previous_patch, fb, risk) is False


def test_should_apply_previous_patch_without_structural_regression_risk():
    from nz_coder.swebench.adapter import SWEBenchAdapter
    from nz_coder.swebench.guardrail import PatchGuardrail
    from nz_coder.swebench.models import FailureFeedback
    from nz_coder.swebench.orchestrator import RetryOrchestrator

    previous_patch = (
        "+++ b/pkg/module.py\n"
        "-VALUE = 1\n"
        "+VALUE = 2\n"
    )

    fb = FailureFeedback(
        instance_id="x", resolved=False, patch_applied=True,
        fail_to_pass=[], pass_to_pass=["PASS_TO_PASS: test_x"],
        passing_tests=[], output_excerpt="",
    )
    g = PatchGuardrail()
    risk = g.analyze(previous_patch, regression_context=True)
    orch = RetryOrchestrator(SWEBenchAdapter(), g)
    assert orch._should_apply_previous_patch(previous_patch, fb, risk) is True


def test_previous_attempt_prompt_marks_unapplied_patch_as_anti_example():
    from nz_coder.swebench.adapter import SWEBenchAdapter
    from nz_coder.swebench.guardrail import PatchGuardrail
    from nz_coder.swebench.models import RetryPlan
    from nz_coder.swebench.orchestrator import RetryOrchestrator

    previous_patch = (
        "+++ b/astropy/io/ascii/rst.py\n"
        " class SimpleRSTHeader(FixedWidthHeader):\n"
        "+    def write(self, lines):\n"
        "+        try:\n"
        "+            return lines\n"
        "+        except:\n"
        "+            return []\n"
    )
    g = PatchGuardrail()
    risk = g.analyze(previous_patch, regression_context=True)
    plan = RetryPlan(
        instance_id="x",
        apply_previous_patch=False,
        previous_patch=previous_patch,
        failure_feedback=None,
        risk_report=risk,
        start_from_clean=True,
        empty_patch_retries=1,
    )
    orch = RetryOrchestrator(SWEBenchAdapter(), g)
    prompt = orch._format_previous_attempt_prompt(plan)

    assert "NOT applied to the repository" in prompt
    assert "anti-example" in prompt
    assert "produce a non-empty minimal patch" in prompt
    assert "must edit the implicated source file" in prompt
    assert "Previous patch risk summary" in prompt
    assert "added_methods_under_regression_guard" in prompt
    assert "broad_except_under_regression_guard" in prompt
    assert "risky patch body is intentionally omitted" in prompt


def test_regression_guard_without_previous_patch_omits_culprit_section(tmp_path):
    from nz_coder.swebench.adapter import SWEBenchAdapter

    instance_id = "astropy__astropy-14182"
    instance_dir = tmp_path / "logs" / instance_id
    instance_dir.mkdir(parents=True)
    (instance_dir / "report.json").write_text(json.dumps({
        instance_id: {
            "resolved": False,
            "patch_successfully_applied": True,
            "tests_status": {
                "PASS_TO_PASS": {
                    "success": [],
                    "failure": ["astropy/io/ascii/tests/test_rst.py::test_write_normal"],
                },
            },
        }
    }), encoding="utf-8")
    (instance_dir / "test_output.txt").write_text("FAILED test_write_normal\n", encoding="utf-8")

    fb = SWEBenchAdapter().load_feedback(instance_id, tmp_path / "logs")
    feedback = fb.to_agent_prompt()
    assert "<regression-guard>" in feedback
    assert "Likely culprits" not in feedback


def test_retry_constraints_include_django_warning_and_enum_contracts():
    from nz_coder.swebench.models import FailureFeedback

    fb = FailureFeedback(
        instance_id="x", resolved=False, patch_applied=True,
        fail_to_pass=[], pass_to_pass=[],
        passing_tests=["PASS_TO_PASS: ok"], output_excerpt="",
    )
    feedback = fb._retry_constraints("")

    assert "warning `hint` fields" in feedback
    assert "str(member) == str(member.value)" in feedback


# ── PatchGuardrail / risk labels ──────────────────────────────────────────────

def test_risk_reasons_flags_deleted_methods():
    from nz_coder.swebench.guardrail import PatchGuardrail

    patch = (
        "+++ b/pkg/module.py\n"
        "-    def old_method(self):\n"
        "-        pass\n"
        "+    pass\n"
    )
    report = PatchGuardrail().analyze(patch)
    labels = report.risk_labels()
    assert "patch_quality:deleted_methods:module.py:old_method" in labels


def test_risk_reasons_flags_deleted_classes_under_regression_guard():
    from nz_coder.swebench.guardrail import PatchGuardrail

    patch = (
        "+++ b/astropy/io/ascii/rst.py\n"
        "-class SimpleRSTData(FixedWidthData):\n"
        "-    pass\n"
        "-class RST(FixedWidth):\n"
        "-    pass\n"
    )
    report = PatchGuardrail().analyze(patch, regression_context=True)
    labels = report.risk_labels()
    assert "patch_quality:deleted_classes_under_regression_guard:rst.py:SimpleRSTData,RST" in labels


def test_risk_reasons_does_not_flag_class_replacement_as_deletion():
    from nz_coder.swebench.guardrail import PatchGuardrail

    patch = (
        "+++ b/pkg/module.py\n"
        "-class Parser(Base):\n"
        "-    pass\n"
        "+class Parser(Base):\n"
        "+    updated = True\n"
    )
    report = PatchGuardrail().analyze(patch, regression_context=True)
    assert not any("deleted_classes" in i.category for i in report.items)


def test_risk_reasons_no_flag_when_no_deletions():
    from nz_coder.swebench.guardrail import PatchGuardrail

    patch = (
        "+++ b/pkg/module.py\n"
        "+    def new_method(self):\n"
        "+        pass\n"
    )
    report = PatchGuardrail().analyze(patch)
    assert not any("deleted_methods" in i.category for i in report.items)


def test_risk_reasons_flags_magic_separator_index_with_header_rows():
    from nz_coder.swebench.guardrail import PatchGuardrail

    patch = (
        "+++ b/astropy/io/ascii/rst.py\n"
        "+    def __init__(self, header_rows=None):\n"
        "+        super().__init__(header_rows=header_rows)\n"
        "+    def write(self, lines):\n"
        "+        lines = [lines[0]] + lines + [lines[0]]\n"
    )
    report = PatchGuardrail().analyze(patch, regression_context=True)
    assert any("magic_separator_index_under_header_rows" == i.category for i in report.items)


def test_risk_reasons_allows_dynamic_separator_index_with_header_rows():
    from nz_coder.swebench.guardrail import PatchGuardrail

    patch = (
        "+++ b/astropy/io/ascii/rst.py\n"
        "+    def __init__(self, header_rows=None):\n"
        "+        super().__init__(header_rows=header_rows)\n"
        "+    def write(self, lines):\n"
        "+        idx = len(self.header.header_rows)\n"
        "+        lines = [lines[idx]] + lines + [lines[idx]]\n"
    )
    report = PatchGuardrail().analyze(patch, regression_context=True)
    assert not any("magic_separator_index" in i.category for i in report.items)


def test_risk_reasons_flags_added_writer_methods_under_regression_guard():
    from nz_coder.swebench.guardrail import PatchGuardrail

    patch = (
        "+++ b/astropy/io/ascii/rst.py\n"
        " class SimpleRSTHeader(FixedWidthHeader):\n"
        "+    def write(self, lines):\n"
        "+        return lines\n"
        " class RST(FixedWidth):\n"
        "-    def write(self, lines):\n"
        "-        return super().write(lines)\n"
        "+    def write(self, lines):\n"
        "+        return super().write(lines)\n"
    )
    report = PatchGuardrail().analyze(patch, regression_context=True)
    labels = report.risk_labels()
    assert (
        "patch_quality:added_methods_under_regression_guard:"
        "rst.py:SimpleRSTHeader.write"
    ) in labels


def test_risk_reasons_allows_added_start_line_property_under_regression_guard():
    from nz_coder.swebench.guardrail import PatchGuardrail

    patch = (
        "+++ b/astropy/io/ascii/rst.py\n"
        " class SimpleRSTData(FixedWidthData):\n"
        "-    start_line = 3\n"
        "+    @property\n"
        "+    def start_line(self):\n"
        "+        return self.header.start_line + len(self.header.header_rows) + 1\n"
    )
    report = PatchGuardrail().analyze(patch, regression_context=True)
    assert not any("added_methods" in i.category for i in report.items)


def test_risk_reasons_flags_broad_except():
    from nz_coder.swebench.guardrail import PatchGuardrail

    patch = (
        "+++ b/pkg/module.py\n"
        "+    try:\n"
        "+        value = risky_call()\n"
        "+    except:\n"
        "+        value = None\n"
    )
    report = PatchGuardrail().analyze(patch, regression_context=True)
    assert any("broad_except_under_regression_guard" == i.category for i in report.items)


def test_risk_reasons_flags_case_insensitive_match_without_token_normalization():
    from nz_coder.swebench.guardrail import PatchGuardrail

    patch = (
        "+++ b/astropy/io/ascii/qdp.py\n"
        "+    _line_type_re = re.compile(_type_re, re.IGNORECASE)\n"
    )
    report = PatchGuardrail().analyze(patch, regression_context=False)
    assert any("case_insensitive_match_without_token_normalization" == i.category for i in report.items)


def test_risk_reasons_allows_case_insensitive_match_with_token_normalization():
    from nz_coder.swebench.guardrail import PatchGuardrail

    patch = (
        "+++ b/astropy/io/ascii/qdp.py\n"
        "+    _line_type_re = re.compile(_type_re, re.IGNORECASE)\n"
        '+                    if v.upper() == "NO":\n'
    )
    report = PatchGuardrail().analyze(patch, regression_context=False)
    assert not any("case_insensitive_match_without_token_normalization" in i.category for i in report.items)


def test_risk_reasons_flags_top_level_django_urls_import():
    from nz_coder.swebench.guardrail import PatchGuardrail

    patch = (
        "diff --git a/django/core/files/storage.py b/django/core/files/storage.py\n"
        "index abc..def 100644\n"
        "--- a/django/core/files/storage.py\n"
        "+++ b/django/core/files/storage.py\n"
        "@@ -1,5 +1,6 @@\n"
        " import os\n"
        "+from django.urls import get_script_prefix\n"
        " from django.conf import settings\n"
    )
    report = PatchGuardrail().analyze(patch)
    assert any("top_level_django_urls_import" == i.category for i in report.items)


def test_risk_reasons_allows_delayed_django_urls_import():
    from nz_coder.swebench.guardrail import PatchGuardrail

    patch = (
        "diff --git a/django/conf/__init__.py b/django/conf/__init__.py\n"
        "index abc..def 100644\n"
        "--- a/django/conf/__init__.py\n"
        "+++ b/django/conf/__init__.py\n"
        "@@ -70,6 +70,8 @@ class LazySettings:\n"
        "     def _add_script_prefix(self, value):\n"
        "+        from django.urls import get_script_prefix\n"
        "+\n"
        "         return value\n"
    )
    report = PatchGuardrail().analyze(patch)
    assert not any("top_level_django_urls_import" in i.category for i in report.items)


def test_risk_reasons_flags_test_file_changes():
    from nz_coder.swebench.guardrail import PatchGuardrail

    patch = (
        "diff --git a/tests/migrations/test_writer.py b/tests/migrations/test_writer.py\n"
        "--- a/tests/migrations/test_writer.py\n"
        "+++ b/tests/migrations/test_writer.py\n"
        "@@\n"
        "+assert True\n"
    )
    report = PatchGuardrail().analyze(patch)
    assert any("tests_modified" == i.category for i in report.items)


def test_risk_reasons_flags_broad_enum_value_coercion():
    from nz_coder.swebench.guardrail import PatchGuardrail

    patch = (
        "diff --git a/django/db/models/fields/__init__.py b/django/db/models/fields/__init__.py\n"
        "--- a/django/db/models/fields/__init__.py\n"
        "+++ b/django/db/models/fields/__init__.py\n"
        "@@\n"
        "+        if isinstance(value, enum.Enum):\n"
        "+            value = value.value\n"
    )
    report = PatchGuardrail().analyze(patch)
    assert any("broad_enum_value_coercion" in i.category for i in report.items)


def test_should_not_apply_broad_enum_value_coercion_patch():
    from nz_coder.swebench.adapter import SWEBenchAdapter
    from nz_coder.swebench.guardrail import PatchGuardrail
    from nz_coder.swebench.models import FailureFeedback
    from nz_coder.swebench.orchestrator import RetryOrchestrator

    patch = (
        "diff --git a/django/db/models/query_utils.py b/django/db/models/query_utils.py\n"
        "--- a/django/db/models/query_utils.py\n"
        "+++ b/django/db/models/query_utils.py\n"
        "@@\n"
        "+    def __set__(self, instance, value):\n"
        "+        instance.__dict__[self.field.attname] = self.field.get_prep_value(value)\n"
    )
    fb = FailureFeedback(
        instance_id="x", resolved=False, patch_applied=True,
        fail_to_pass=[], pass_to_pass=[],
        passing_tests=[], output_excerpt="",
    )
    g = PatchGuardrail()
    risk = g.analyze(patch, regression_context=False)
    orch = RetryOrchestrator(SWEBenchAdapter(), g)
    assert orch._should_apply_previous_patch(patch, fb, risk) is False


# ── adapter: image names, Docker, _run, predictions ──────────────────────────

def test_instance_image_name_matches_official_remote_format():
    from nz_coder.swebench.adapter import _instance_image_name

    assert (
        _instance_image_name("astropy__astropy-14182", arch="x86_64")
        == "swebench/sweb.eval.x86_64.astropy_1776_astropy-14182:latest"
    )


def test_instance_image_name_allows_local_namespace():
    from nz_coder.swebench.adapter import _instance_image_name

    assert (
        _instance_image_name("django__django-11001", namespace="", arch="x86_64", tag="dev")
        == "sweb.eval.x86_64.django__django-11001:dev"
    )


def test_check_docker_requires_daemon_access(monkeypatch):
    from nz_coder.swebench import adapter as adapter_mod

    monkeypatch.setattr(adapter_mod.shutil, "which", lambda name: "/usr/bin/docker")

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 1, "", "permission denied")

    monkeypatch.setattr(adapter_mod.subprocess, "run", fake_run)

    from nz_coder.swebench.adapter import SWEBenchAdapter
    ok, name, detail = SWEBenchAdapter._check_docker()

    assert ok is False
    assert name == "docker"
    assert "daemon unavailable" in detail
    assert "permission denied" in detail


def test_docker_pull_timeout_uses_spawn_context(monkeypatch):
    from nz_coder.swebench import adapter as adapter_mod

    requested = []

    class FakeQueue:
        def empty(self):
            return True

    class FakeProcess:
        exitcode = 0

        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def join(self, timeout=None):
            pass

        def is_alive(self):
            return False

    class FakeContext:
        Queue = FakeQueue
        Process = FakeProcess

    monkeypatch.setattr(adapter_mod.multiprocessing, "Queue", FakeQueue)
    monkeypatch.setattr(adapter_mod.multiprocessing, "Process", FakeProcess)
    monkeypatch.setattr(
        adapter_mod.multiprocessing,
        "get_context",
        lambda name: requested.append(name) or FakeContext(),
    )

    assert adapter_mod._docker_pull_with_timeout("example/image", 1) == (
        True,
        "completed",
    )
    assert requested == ["spawn"]


def test_run_evaluation_stops_before_harness_when_docker_unusable(tmp_path, monkeypatch, capsys):
    from nz_coder.swebench import adapter as adapter_mod

    predictions = tmp_path / "predictions.jsonl"
    predictions.write_text(
        json.dumps({
            "instance_id": "django__django-10924",
            "model_name_or_path": "nz-coder",
            "model_patch": "",
        }) + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(adapter_mod.SWEBenchAdapter, "_check_module", staticmethod(lambda name: (True, name, "installed")))
    monkeypatch.setattr(
        adapter_mod.SWEBenchAdapter,
        "_check_docker",
        staticmethod(lambda: (False, "docker", "present but daemon unavailable: permission denied")),
    )
    monkeypatch.setattr(
        adapter_mod.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("harness should not run")),
    )

    result = adapter_mod.SWEBenchAdapter().run_harness(predictions, SimpleNamespace(
        prepull_timeout=0,
        instance_ids=[],
        max_workers=1,
        run_id="test",
        timeout=60,
        clean=False,
    ))

    assert result == 2
    assert "Docker daemon is not usable" in capsys.readouterr().out


def test_run_harness_passes_explicit_clean_value(tmp_path, monkeypatch):
    """The official harness parses --clean as a boolean value, not a flag."""
    from nz_coder.swebench import adapter as adapter_mod

    predictions = tmp_path / "predictions.jsonl"
    predictions.write_text("{}\n", encoding="utf-8")
    commands: list[list[str]] = []
    monkeypatch.setattr(
        adapter_mod.SWEBenchAdapter,
        "_check_module",
        staticmethod(lambda name: (True, name, "installed")),
    )
    monkeypatch.setattr(
        adapter_mod.SWEBenchAdapter,
        "_check_docker",
        staticmethod(lambda: (True, "docker", "ready")),
    )
    monkeypatch.setattr(
        adapter_mod.subprocess,
        "run",
        lambda command: (
            commands.append(command)
            or subprocess.CompletedProcess(command, 0, "", "")
        ),
    )

    result = adapter_mod.SWEBenchAdapter("lite").run_harness(
        predictions,
        SimpleNamespace(
            profile="lite",
            prepull_timeout=0,
            instance_ids=[],
            max_workers=1,
            run_id="clean-contract",
            timeout=60,
            clean=True,
        ),
    )

    assert result == 0
    clean_index = commands[0].index("--clean")
    assert commands[0][clean_index:clean_index + 2] == ["--clean", "true"]


# ── orchestrator helpers ──────────────────────────────────────────────────────

def test_should_retry_empty_patch_only_for_retry_feedback():
    from nz_coder.swebench.orchestrator import _should_retry_empty_patch

    assert _should_retry_empty_patch(
        "",
        has_feedback=True,
        attempts=0,
        max_retries=1,
    ) is True
    assert _should_retry_empty_patch(
        "diff --git a/x b/x\n",
        has_feedback=True,
        attempts=0,
        max_retries=1,
    ) is False
    assert _should_retry_empty_patch(
        "",
        has_feedback=False,
        attempts=0,
        max_retries=1,
    ) is False
    assert _should_retry_empty_patch(
        "",
        has_feedback=True,
        attempts=1,
        max_retries=1,
    ) is False


def test_empty_patch_retry_feedback_demands_non_empty_diff():
    from nz_coder.swebench.orchestrator import _format_empty_patch_retry_feedback

    feedback = _format_empty_patch_retry_feedback(1, 1)

    assert "<empty-patch-retry>" in feedback
    assert "empty patch" in feedback
    assert "Make one minimal source-code edit" in feedback
    assert "non-empty git diff" in feedback


def test_run_agent_attempt_uses_subprocess_timeout():
    from nz_coder.swebench.orchestrator import AgentRunTimeout, _run_agent_attempt

    try:
        _run_agent_attempt(
            _SleepingAgent,
            "system",
            None,
            [{"role": "user", "content": "work"}],
            lambda name, output: None,
            timeout=1,
        )
    except AgentRunTimeout:
        pass
    else:
        raise AssertionError("expected AgentRunTimeout")


def test_run_agent_attempt_replays_child_tool_events():
    from nz_coder.swebench.orchestrator import _run_agent_attempt

    events = []
    status = _run_agent_attempt(
        _NoopAgent,
        "system",
        None,
        [{"role": "user", "content": "work"}],
        lambda name, output: events.append((name, output)),
        timeout=5,
    )

    assert status == {"status": "completed"}
    assert events == [("bash", "ok")]


def test_run_agent_attempt_uses_spawn_context(monkeypatch):
    from nz_coder.swebench import orchestrator

    requested = []
    real_get_context = orchestrator.multiprocessing.get_context

    def capture_context(name):
        requested.append(name)
        return real_get_context(name)

    monkeypatch.setattr(orchestrator.multiprocessing, "get_context", capture_context)

    status = orchestrator._run_agent_attempt(
        _NoopAgent,
        "system",
        None,
        [{"role": "user", "content": "work"}],
        lambda _name, _output: None,
        timeout=5,
    )

    assert status == {"status": "completed"}
    assert requested == ["spawn"]


def test_run_agent_attempt_keeps_process_timeout_without_os_fork(monkeypatch):
    from nz_coder.swebench import orchestrator

    monkeypatch.setattr(orchestrator, "os", SimpleNamespace(), raising=False)
    observed = []

    def fake_subprocess(*args, **kwargs):
        observed.append((args, kwargs))
        return {"status": "isolated"}

    monkeypatch.setattr(
        orchestrator,
        "_run_agent_attempt_in_subprocess",
        fake_subprocess,
    )

    status = orchestrator._run_agent_attempt(
        _NoopAgent,
        "system",
        None,
        [{"role": "user", "content": "work"}],
        lambda _name, _output: None,
        timeout=5,
    )

    assert status == {"status": "isolated"}
    assert len(observed) == 1


def test_run_agent_attempt_drains_large_child_result_before_joining():
    """A completed child must not be timed out while its Queue feeder is blocked."""
    from nz_coder.swebench.orchestrator import _run_agent_attempt

    events = []
    status = _run_agent_attempt(
        _LargeToolOutputAgent,
        "system",
        None,
        [{"role": "user", "content": "work"}],
        lambda name, output: events.append((name, output)),
        timeout=2,
    )

    assert status == {"status": "completed", "events": 24}
    assert len(events) == 24
    assert events[0][0] == "read_file"
    assert events[-1][1].startswith("event-23:")


def test_run_agent_attempt_keeps_result_when_dependency_thread_lingers():
    """A delivered result survives cleanup of a child with live resources."""
    from nz_coder.swebench.orchestrator import _run_agent_attempt

    events = []
    status = _run_agent_attempt(
        _LingeringThreadAgent,
        "system",
        None,
        [{"role": "user", "content": "work"}],
        lambda name, output: events.append((name, output)),
        timeout=2,
    )

    assert status == {"status": "completed"}
    assert events == [("lsp", "analysis complete")]


def test_run_agent_attempt_surfaces_child_exception():
    from nz_coder.swebench.orchestrator import _run_agent_attempt

    try:
        _run_agent_attempt(
            _RaisingAgent,
            "system",
            None,
            [{"role": "user", "content": "work"}],
            lambda name, output: None,
            timeout=2,
        )
    except RuntimeError as exc:
        assert "ValueError('child boom')" in str(exc)
    else:
        raise AssertionError("expected child exception")


def test_run_agent_attempt_reports_abrupt_child_exit():
    from nz_coder.swebench.orchestrator import _run_agent_attempt

    try:
        _run_agent_attempt(
            _AbruptExitAgent,
            "system",
            None,
            [{"role": "user", "content": "work"}],
            lambda name, output: None,
            timeout=1,
        )
    except RuntimeError as exc:
        assert "exitcode=7" in str(exc)
    else:
        raise AssertionError("expected abrupt child exit")


def test_strict_policy_rejection_is_a_process_warning_not_patch_risk():
    from nz_coder.swebench.orchestrator import (
        _agent_status_risk_labels,
        _classify_tool_log_status,
    )

    output = "Error: shell executable 'cd' is not allowed in SWE-bench strict mode"
    status = _classify_tool_log_status(output)
    labels = _agent_status_risk_labels(
        {"status": "completed", "verification_needed": False},
        [{"name": "bash", "status": status, "output": output}],
    )

    assert status == "policy_rejected"
    assert labels == []


def test_closure_reserve_denial_is_policy_rejection_not_patch_risk():
    from nz_coder.swebench.orchestrator import (
        _agent_status_risk_labels,
        _classify_tool_log_status,
    )

    output = (
        "Denied: closure reserve permits only a narrow repair using known files "
        "and focused verification."
    )
    status = _classify_tool_log_status(output)
    labels = _agent_status_risk_labels(
        {"status": "completed", "verification_needed": False},
        [{"name": "bash", "status": status, "output": output}],
    )

    assert status == "policy_rejected"
    assert labels == []


def test_real_bash_dispatch_error_remains_patch_run_risk():
    from nz_coder.swebench.orchestrator import (
        _agent_status_risk_labels,
        _classify_tool_log_status,
    )

    output = "Error: workdir escapes workspace"
    status = _classify_tool_log_status(output)
    labels = _agent_status_risk_labels(
        {"status": "completed", "verification_needed": False},
        [{"name": "bash", "status": status, "output": output}],
    )

    assert status == "error"
    assert labels == ["tool_errors"]


def test_recovered_write_error_from_older_generation_is_not_final_patch_risk():
    from nz_coder.swebench.orchestrator import _agent_status_risk_labels

    labels = _agent_status_risk_labels(
        {
            "status": "completed",
            "verification_needed": False,
            "runtime": {"mutation_generation": 1},
        },
        [
            {"name": "apply_patch", "status": "error", "output": "Error: exact text missing", "generation": 0},
            {"name": "apply_patch", "status": "ok", "output": "Done", "generation": 1},
        ],
    )

    assert labels == []


def test_recovered_write_error_is_preserved_as_bounded_process_warning():
    """Catches hiding recovered execution trouble after removing final risk."""
    from nz_coder.swebench.orchestrator import _agent_status_process_warnings

    warnings = _agent_status_process_warnings(
        {"runtime": {"mutation_generation": 2}},
        [
            {"name": "edit_file", "status": "error", "generation": 0},
            {"name": "apply_patch", "status": "error", "generation": 1},
            {"name": "edit_file", "status": "ok", "generation": 2},
        ],
    )

    assert warnings == ["recovered_tool_errors:2"]


def test_write_error_in_final_generation_remains_patch_run_risk():
    from nz_coder.swebench.orchestrator import _agent_status_risk_labels

    labels = _agent_status_risk_labels(
        {
            "status": "completed",
            "verification_needed": False,
            "runtime": {"mutation_generation": 1},
        },
        [{"name": "edit_file", "status": "error", "output": "Error: write failed", "generation": 1}],
    )

    assert labels == ["tool_errors"]


def test_run_returns_completed_process_on_timeout(tmp_path, monkeypatch):
    from nz_coder.swebench import orchestrator as orch_mod

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"], output=b"partial")

    monkeypatch.setattr(orch_mod.subprocess, "run", fake_run)

    result = orch_mod._run(["git", "clone", "url"], cwd=tmp_path, timeout=3)

    assert result.returncode == 124
    assert result.stdout == "partial"
    assert "Command timed out after 3s" in result.stderr
    assert "git clone url" in result.stderr


def test_prepare_repo_uses_local_cache_when_present(tmp_path, monkeypatch):
    from nz_coder.swebench import orchestrator as orch_mod

    cache_root = tmp_path / "cache"
    cached_repo = cache_root / "django_django.git"
    cached_repo.mkdir(parents=True)
    calls = []

    monkeypatch.setattr(orch_mod, "DEFAULT_REPO_CACHE_DIR", cache_root)

    def fake_run(cmd, *, cwd, timeout):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(orch_mod, "_run", fake_run)

    result = orch_mod._prepare_repo(
        {"repo": "django/django", "base_commit": "abc123"},
        tmp_path / "work" / "django__django-11797",
        timeout=10,
    )

    assert result["returncode"] == 0
    assert calls[0] == ["git", "clone", "--quiet", str(cached_repo), str(tmp_path / "work" / "django__django-11797")]
    assert calls[1] == ["git", "checkout", "--quiet", "abc123"]


def test_prepare_repo_populates_missing_cache_before_checkout(tmp_path, monkeypatch):
    """The first checkout seeds an atomic mirror reused by later instances."""
    from nz_coder.swebench import orchestrator as orch_mod

    cache_root = tmp_path / "cache"
    cached_repo = cache_root / "pylint-dev_pylint.git"
    repo_dir = tmp_path / "work" / "pylint-dev__pylint-5859"
    calls = []

    monkeypatch.setattr(orch_mod, "DEFAULT_REPO_CACHE_DIR", cache_root)

    def fake_run(cmd, *, cwd, timeout):
        calls.append(cmd)
        if cmd[:4] == ["git", "clone", "--mirror", "--quiet"]:
            Path(cmd[-1]).mkdir(parents=True)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(orch_mod, "_run", fake_run)

    result = orch_mod._prepare_repo(
        {"repo": "pylint-dev/pylint", "base_commit": "abc123"},
        repo_dir,
        timeout=10,
    )

    assert result["returncode"] == 0
    assert calls[0][:5] == [
        "git", "clone", "--mirror", "--quiet",
        "https://github.com/pylint-dev/pylint.git",
    ]
    assert Path(calls[0][-1]).name == "repo.git"
    assert cached_repo.is_dir()
    assert calls[1] == ["git", "clone", "--quiet", str(cached_repo), str(repo_dir)]
    assert calls[2] == ["git", "checkout", "--quiet", "abc123"]


def test_prepare_repo_falls_back_when_initial_mirror_clone_fails(
    tmp_path,
    monkeypatch,
):
    """A cache-seeding failure must not prevent a direct checkout retry."""
    from nz_coder.swebench import orchestrator as orch_mod

    cache_root = tmp_path / "cache"
    repo_dir = tmp_path / "work" / "pylint-dev__pylint-5859"
    calls = []

    monkeypatch.setattr(orch_mod, "DEFAULT_REPO_CACHE_DIR", cache_root)

    def fake_run(cmd, *, cwd, timeout):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd,
            128 if cmd[:4] == ["git", "clone", "--mirror", "--quiet"] else 0,
            "",
            "mirror TLS failure",
        )

    monkeypatch.setattr(orch_mod, "_run", fake_run)

    result = orch_mod._prepare_repo(
        {"repo": "pylint-dev/pylint", "base_commit": "abc123"},
        repo_dir,
        timeout=10,
    )

    assert result["returncode"] == 0
    assert calls[0][2:4] == ["--mirror", "--quiet"]
    assert calls[1] == [
        "git", "clone", "--quiet",
        "https://github.com/pylint-dev/pylint.git",
        str(repo_dir),
    ]
    assert calls[2] == ["git", "checkout", "--quiet", "abc123"]
    assert not (cache_root / "pylint-dev_pylint.git").exists()


def test_prepare_repo_falls_back_to_remote_when_cache_clone_fails(tmp_path, monkeypatch):
    from nz_coder.swebench import orchestrator as orch_mod

    cache_root = tmp_path / "cache"
    cached_repo = cache_root / "django_django.git"
    cached_repo.mkdir(parents=True)
    calls = []

    monkeypatch.setattr(orch_mod, "DEFAULT_REPO_CACHE_DIR", cache_root)

    def fake_run(cmd, *, cwd, timeout):
        calls.append(cmd)
        if len(calls) == 1:
            return subprocess.CompletedProcess(cmd, 128, "", "cache corrupt")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(orch_mod, "_run", fake_run)

    result = orch_mod._prepare_repo(
        {"repo": "django/django", "base_commit": "abc123"},
        tmp_path / "work" / "django__django-11797",
        timeout=10,
    )

    assert result["returncode"] == 0
    assert calls[0][3] == str(cached_repo)
    assert calls[1][3] == "https://github.com/django/django.git"
    assert calls[2] == ["git", "checkout", "--quiet", "abc123"]


def test_load_predictions_reads_jsonl(tmp_path):
    from nz_coder.swebench.adapter import SWEBenchAdapter

    predictions = tmp_path / "predictions.jsonl"
    predictions.write_text(
        json.dumps({"instance_id": "one", "model_patch": "diff --git a/x b/x\n"})
        + "\n"
        + json.dumps({"instance_id": "two", "model_patch": ""})
        + "\n",
        encoding="utf-8",
    )

    assert SWEBenchAdapter().load_predictions(predictions) == {
        "one": "diff --git a/x b/x\n",
        "two": "",
    }


def test_write_prediction_discards_agent_failed_patch(tmp_path):
    from nz_coder.swebench.orchestrator import _write_prediction

    path = tmp_path / "predictions.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        _write_prediction(
            fh,
            "django__django-11964",
            "nz-coder",
            {"status": "agent_failed", "model_patch": "diff --git a/x b/x\n"},
        )

    row = json.loads(path.read_text(encoding="utf-8"))

    assert row["model_patch"] == ""


def test_terminal_provider_abort_is_agent_failed_not_empty_patch():
    from nz_coder.swebench.orchestrator import _benchmark_result_status

    status = _benchmark_result_status(
        {"status": "aborted"},
        model_patch="",
        risk_reasons=["agent_status:aborted"],
    )

    assert status == "agent_failed"


def test_max_turns_patch_is_not_publishable():
    from nz_coder.swebench.orchestrator import _benchmark_result_status

    status = _benchmark_result_status(
        {"status": "max_turns"},
        model_patch="diff --git a/x.py b/x.py\n",
        risk_reasons=["agent_status:max_turns"],
    )

    assert status == "agent_failed"


def test_blocking_quality_gate_patch_is_not_publishable():
    from nz_coder.swebench.orchestrator import _benchmark_result_status

    status = _benchmark_result_status(
        {"status": "completed"},
        model_patch="diff --git a/x.py b/x.py\n",
        risk_reasons=["risky_added_methods"],
        blocking_risk=True,
    )

    assert status == "agent_failed"


def test_unverified_patch_is_not_publishable():
    from nz_coder.swebench.orchestrator import _benchmark_result_status

    status = _benchmark_result_status(
        {"status": "stopped_by_hook", "verification_needed": True},
        model_patch="diff --git a/x.py b/x.py\n",
        risk_reasons=["agent_status:stopped_by_hook", "verification_needed"],
    )

    assert status == "agent_failed"


def test_environment_blocked_non_empty_patch_is_risky():
    from nz_coder.swebench.orchestrator import _benchmark_result_status

    status = _benchmark_result_status(
        {
            "status": "completed_unverified",
            "verification_needed": True,
            "verification_state": "blocked_environment",
        },
        model_patch="diff --git a/x.py b/x.py\n",
        risk_reasons=[
            "agent_status:completed_unverified",
            "verification_needed",
        ],
    )

    assert status == "risky"


def test_environment_blocked_empty_patch_stays_empty_patch():
    from nz_coder.swebench.orchestrator import _benchmark_result_status

    status = _benchmark_result_status(
        {
            "status": "completed_unverified",
            "verification_needed": True,
            "verification_state": "blocked_environment",
        },
        model_patch="",
        risk_reasons=["verification_needed"],
    )

    assert status == "empty_patch"


def test_untrusted_environment_blocked_status_is_agent_failed():
    from nz_coder.swebench.orchestrator import _benchmark_result_status

    status = _benchmark_result_status(
        {
            "status": "stopped_by_hook",
            "verification_needed": True,
            "verification_state": "blocked_environment",
        },
        model_patch="diff --git a/x.py b/x.py\n",
        risk_reasons=["verification_needed"],
    )

    assert status == "agent_failed"
