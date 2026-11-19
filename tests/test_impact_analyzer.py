"""Tests for ImpactAnalyzer Lite."""
import subprocess



def test_impact_analyzer_low_for_small_single_file():
    from nz_coder.intelligence.impact_analyzer import analyze_patch_impact

    report = analyze_patch_impact(
        changed_files=["src/foo.py"],
        diff_text="+def helper():\n+    return 1\n",
        project_profile={"test_roots": ["tests"], "test_commands": ["pytest"]},
        diff_chars=80,
    )
    assert report["risk"] in {"low", "medium"}
    assert report["suggested_verification"]


def test_impact_analyzer_high_for_config_and_large_diff():
    from nz_coder.intelligence.impact_analyzer import analyze_patch_impact

    report = analyze_patch_impact(
        changed_files=["config/settings.py", "migrations/001.py", "src/auth.py", "src/db.py", "src/api.py"],
        diff_text="+" * 13000,
        project_profile={"test_roots": ["tests"], "test_commands": ["pytest"]},
        diff_chars=13000,
    )
    assert report["risk"] == "high"
    assert any("sensitive" in reason for reason in report["reasons"])


def test_impact_analyzer_includes_untracked_files(tmp_path):
    from nz_coder.foundation import config
    from nz_coder.intelligence.impact_analyzer import _git_changed_files

    old = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "new_file.py").write_text("x = 1\n", encoding="utf-8")
        (tmp_path / ".nz-coder").mkdir()
        (tmp_path / ".nz-coder" / "runtime_state.json").write_text("{}", encoding="utf-8")

        changed = _git_changed_files()
        assert "src/new_file.py" in changed
        assert ".nz-coder/runtime_state.json" not in changed
    finally:
        config.WORKDIR = old


def test_deleted_public_symbols_require_conservative_replan():
    from nz_coder.intelligence.impact_analyzer import analyze_patch_impact

    diff = """--- a/pkg/api.py
+++ b/pkg/api.py
@@ -1,8 +1,2 @@
-def public_api(value):
-    return value
-
-def _private_helper(value):
-    return value
-
-class PublicClient:
-    pass
+VALUE = 1
"""

    report = analyze_patch_impact(
        changed_files=["pkg/api.py"],
        diff_text=diff,
        project_profile={"test_roots": ["tests"], "test_commands": ["pytest"]},
    )

    signals = {item["category"]: item for item in report["risk_signals"]}
    assert report["requires_replan"] is True
    assert "deleted_public_symbols" in signals
    assert "public_api" in signals["deleted_public_symbols"]["detail"]
    assert "PublicClient" in signals["deleted_public_symbols"]["detail"]
    assert "_private_helper" not in signals["deleted_public_symbols"]["detail"]


def test_public_signature_change_is_not_misreported_as_deletion():
    from nz_coder.intelligence.impact_analyzer import analyze_patch_impact

    diff = """--- a/pkg/api.py
+++ b/pkg/api.py
@@ -1,2 +1,2 @@
-def public_api(value):
+def public_api(value, strict=False):
     return value
"""

    report = analyze_patch_impact(
        changed_files=["pkg/api.py"],
        diff_text=diff,
        project_profile={"test_roots": ["tests"], "test_commands": ["pytest"]},
    )

    categories = {item["category"] for item in report["risk_signals"]}
    assert "public_signature_change" in categories
    assert "deleted_public_symbols" not in categories


def test_body_only_edit_does_not_trigger_public_api_replan():
    from nz_coder.intelligence.impact_analyzer import analyze_patch_impact

    diff = """--- a/pkg/api.py
+++ b/pkg/api.py
@@ -1,2 +1,2 @@
 def public_api(value):
-    return value
+    return value + 1
"""

    report = analyze_patch_impact(
        changed_files=["pkg/api.py"],
        diff_text=diff,
        project_profile={"test_roots": ["tests"], "test_commands": ["pytest"]},
    )

    categories = {item["category"] for item in report["risk_signals"]}
    assert "public_signature_change" not in categories
    assert "deleted_public_symbols" not in categories
    assert report["requires_replan"] is False


def test_sensitive_persistent_delete_requires_semantic_review():
    """A data-deleting migration must not be treated as a trivial patch."""
    from nz_coder.intelligence.impact_analyzer import analyze_patch_impact

    diff = """--- a/django/contrib/auth/migrations/0011_permissions.py
+++ b/django/contrib/auth/migrations/0011_permissions.py
@@ -10,3 +10,4 @@ def migrate_permissions():
     permissions = Permission.objects.filter(content_type=target)
+    permissions.delete()
"""
    report = analyze_patch_impact(
        changed_files=[
            "django/contrib/auth/migrations/0011_permissions.py",
        ],
        diff_text=diff,
        project_profile={"test_roots": ["tests"], "test_commands": ["pytest"]},
    )

    signal = next(
        item for item in report["risk_signals"]
        if item["category"] == "persistent_data_deletion"
    )
    assert signal["severity"] == "review"
    assert report["requires_replan"] is False


def test_non_sensitive_delete_call_does_not_claim_persistent_data_risk():
    """A generic method named delete is insufficient evidence of data loss."""
    from nz_coder.intelligence.impact_analyzer import analyze_patch_impact

    report = analyze_patch_impact(
        changed_files=["src/cache_cleanup.py"],
        diff_text="+    temporary_entry.delete()\n",
        project_profile={"test_roots": ["tests"], "test_commands": ["pytest"]},
    )

    assert not any(
        item["category"] == "persistent_data_deletion"
        for item in report["risk_signals"]
    )


def test_sensitive_cache_delete_does_not_claim_persistent_data_risk():
    """A sensitive directory alone does not turn cache invalidation into data loss."""
    from nz_coder.intelligence.impact_analyzer import analyze_patch_impact

    diff = """--- a/auth/cache_cleanup.py
+++ b/auth/cache_cleanup.py
@@ -2,3 +2,4 @@ def invalidate(queryset_key):
     permissions = Permission.objects.all()
+    cache.delete(queryset_key)
"""
    report = analyze_patch_impact(
        changed_files=["auth/cache_cleanup.py"],
        diff_text=diff,
        project_profile={"test_roots": ["tests"], "test_commands": ["pytest"]},
    )

    assert not any(
        item["category"] == "persistent_data_deletion"
        for item in report["risk_signals"]
    )


def test_unscoped_multifile_delete_is_not_assigned_to_sensitive_path():
    """A headerless diff cannot identify which of several files owns a delete."""
    from nz_coder.intelligence.impact_analyzer import analyze_patch_impact

    report = analyze_patch_impact(
        changed_files=[
            "django/contrib/auth/migrations/0011_permissions.py",
            "src/cache_cleanup.py",
        ],
        diff_text="+    cache.delete(session_key)\n",
        project_profile={"test_roots": ["tests"], "test_commands": ["pytest"]},
    )

    assert not any(
        item["category"] == "persistent_data_deletion"
        for item in report["risk_signals"]
    )


def test_source_change_outside_user_named_path_requires_replan():
    from nz_coder.intelligence.impact_analyzer import analyze_patch_impact

    report = analyze_patch_impact(
        changed_files=["pkg/target.py", "pkg/unrelated.py", "tests/test_target.py"],
        diff_text="",
        project_profile={"test_roots": ["tests"], "test_commands": ["pytest"]},
        requested_paths=["pkg/target.py"],
    )

    signal = next(
        item for item in report["risk_signals"]
        if item["category"] == "requested_scope_expansion"
    )
    assert report["requires_replan"] is True
    assert signal["detail"] == "pkg/unrelated.py"
    assert "tests/test_target.py" not in signal["detail"]


def test_broad_source_changes_preserve_scope_risk_signals():
    from nz_coder.intelligence.impact_analyzer import analyze_patch_impact

    files = [f"src/module_{index}.py" for index in range(6)]
    report = analyze_patch_impact(
        changed_files=files,
        diff_text="",
        project_profile={"test_roots": ["tests"], "test_commands": ["pytest"]},
        requested_paths=["src/main.py"],
    )

    assert report["risk"] == "high"
    assert report["requires_replan"] is True
    assert {item["category"] for item in report["risk_signals"]} == {
        "broad_scope_expansion", "requested_scope_expansion",
    }


def test_impact_report_exposes_fingerprint_and_replan_signal():
    from nz_coder.intelligence.impact_analyzer import analyze_patch_impact, format_impact_report

    report = analyze_patch_impact(
        changed_files=["pkg/other.py"],
        diff_text="",
        project_profile={"test_roots": ["tests"], "test_commands": ["pytest"]},
        requested_paths=["pkg/target.py"],
    )
    output = format_impact_report(report)

    assert "Risk fingerprint:" in output
    assert "Requires replan: true" in output
    assert "[replan] requested_scope_expansion" in output


def test_explicit_empty_change_list_does_not_fall_back_to_git(monkeypatch):
    import nz_coder.intelligence.impact_analyzer as analyzer

    monkeypatch.setattr(
        analyzer,
        "_git_changed_files",
        lambda: ["unrelated/from-parent-repo.py"],
    )
    report = analyzer.analyze_patch_impact(
        changed_files=[],
        diff_text="",
        project_profile={"test_roots": ["tests"], "test_commands": ["pytest"]},
    )

    assert report["affected_files"] == []
    assert report["requires_replan"] is False


def test_positional_requested_paths_still_define_scope():
    from nz_coder.intelligence.impact_analyzer import analyze_patch_impact

    report = analyze_patch_impact(
        ["pkg/other.py"],
        "",
        {"test_roots": ["tests"], "test_commands": ["pytest"]},
        False,
        0,
        ["pkg/target.py"],
    )

    assert any(
        item["category"] == "requested_scope_expansion"
        for item in report["risk_signals"]
    )


def test_structural_changed_scope_enriches_impact_and_verification_risk():
    from nz_coder.intelligence.impact_analyzer import analyze_patch_impact

    report = analyze_patch_impact(
        changed_files=["helpers.py"],
        diff_text="",
        project_profile={"test_roots": ["tests"], "test_commands": ["pytest"]},
        structural_scope={
            "changed_symbols": ["normalize"],
            "impacted_callers": ["service.py:handle", "cli.py:main"],
            "related": ["tests/test_service.py"],
        },
    )

    assert report["structural_impact"]["changed_symbols"] == ["normalize"]
    assert report["structural_impact"]["impacted_callers"] == ["service.py:handle", "cli.py:main"]
    assert "tests/test_service.py" in report["likely_tests"]
    assert any("2 structural callers" in reason for reason in report["reasons"])


def test_source_count_alone_is_informational_in_new_and_existing_projects():
    from nz_coder.intelligence.impact_analyzer import analyze_patch_impact
    paths = [f'pkg/module_{n}.py' for n in range(11)]
    for diff in ('', 'diff --git a/pkg/module_0.py b/pkg/module_0.py\n@@ -1 +1 @@\n-value = 1\n+value = 2'):
        report = analyze_patch_impact(changed_files=paths, diff_text=diff)
        assert report['requires_replan'] is False
        signal, = report['risk_signals']
        assert signal['severity'] == 'info' and '11 source files' in signal['detail']
