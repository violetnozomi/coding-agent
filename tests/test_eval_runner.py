"""Tests for lightweight eval runner."""
import json
import subprocess


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
    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert "summary" in data
    assert data["summary"]["tasks"] == 1
    md_text = out_md.read_text(encoding="utf-8")
    assert "## Summary" in md_text
    assert "| demo |" in md_text


def test_eval_runner_reports_dirty_after_revert(tmp_path, monkeypatch):
    from nz_coder import eval_runner

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (repo / "generated.py").write_text("y = 2\n", encoding="utf-8")

    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "task.json").write_text(json.dumps({
        "id": "dirty",
        "repo": str(repo.relative_to(tmp_path)),
        "prompt": "noop",
        "verification": [],
        "max_turns": 1,
        "tags": ["python"],
    }), encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    task = eval_runner.load_tasks("tasks", limit=1)[0]
    result = eval_runner.run_task(task, mode="dry-run")
    assert result["dirty_after_revert"] is True
    assert "generated.py" in result["dirty_files_after_revert"]
    assert "repo still dirty after tracked revert" in result["notes"]


def test_eval_runner_project_creation_checks_expected_files_with_project_prefix(tmp_path, monkeypatch):
    from nz_coder import eval_runner

    repo = tmp_path / "repo"
    repo.mkdir()
    project = repo / "word_counter"
    (project / "tests").mkdir(parents=True)
    (project / "app.py").write_text("def main():\n    return 0\n", encoding="utf-8")
    (project / "tests" / "test_smoke.py").write_text(
        "def test_smoke():\n    assert True\n",
        encoding="utf-8",
    )

    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "task.json").write_text(json.dumps({
        "id": "greenfield",
        "task_type": "project_creation",
        "repo": str(repo.relative_to(tmp_path)),
        "project_root": "word_counter",
        "prompt": "create a new project",
        "expected_files": ["word_counter/app.py", "word_counter/tests/test_smoke.py"],
        "verification": ["python -m py_compile app.py", "python -m pytest tests/test_smoke.py"],
        "max_turns": 1,
        "tags": ["python", "project_creation"],
    }), encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    task = eval_runner.load_tasks("tasks", limit=1)[0]
    result = eval_runner.run_task(task, mode="dry-run")
    assert result["task_type"] == "project_creation"
    assert result["expected_files_ok"] is True
    assert result["status"] == "success"
