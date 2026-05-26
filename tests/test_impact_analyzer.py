"""Tests for ImpactAnalyzer Lite."""


def test_impact_analyzer_low_for_small_single_file():
    from nz_coder.impact_analyzer import analyze_patch_impact

    report = analyze_patch_impact(
        changed_files=["src/foo.py"],
        diff_text="+def helper():\n+    return 1\n",
        project_profile={"test_roots": ["tests"], "test_commands": ["pytest"]},
        diff_chars=80,
    )
    assert report["risk"] in {"low", "medium"}
    assert report["suggested_verification"]


def test_impact_analyzer_high_for_config_and_large_diff():
    from nz_coder.impact_analyzer import analyze_patch_impact

    report = analyze_patch_impact(
        changed_files=["config/settings.py", "migrations/001.py", "src/auth.py", "src/db.py", "src/api.py"],
        diff_text="+" * 13000,
        project_profile={"test_roots": ["tests"], "test_commands": ["pytest"]},
        diff_chars=13000,
    )
    assert report["risk"] == "high"
    assert any("sensitive" in reason for reason in report["reasons"])
