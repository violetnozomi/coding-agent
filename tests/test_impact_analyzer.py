"""Tests for ImpactAnalyzer Lite."""
import subprocess



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


def test_impact_analyzer_includes_untracked_files(tmp_path):
    from nz_coder import config
    from nz_coder.impact_analyzer import _git_changed_files

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
