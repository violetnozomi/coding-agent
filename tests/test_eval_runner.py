"""Tests for lightweight eval runner."""
import json


def test_eval_runner_dry_run_writes_results(tmp_path, monkeypatch):
    from nz_coder import eval_runner

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "task.json").write_text(json.dumps({
        "id": "demo",
        "repo": str(repo.relative_to(tmp_path)),
        "prompt": "change app",
        "verification": ["python -m py_compile app.py"],
        "max_turns": 3,
        "tags": ["python"],
    }), encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    tasks = eval_runner.load_tasks("tasks", limit=1)
    result = eval_runner.run_task(tasks[0], mode="dry-run")
    assert result["id"] == "demo"
    out_json, out_md = eval_runner.write_results([result], "eval/results")
    assert out_json.exists()
    assert out_md.exists()
    assert "| demo |" in out_md.read_text(encoding="utf-8")
