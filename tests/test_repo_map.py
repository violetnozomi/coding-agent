"""Tests for the incremental Python AST repository map."""
from __future__ import annotations


def _bind_workdir(monkeypatch, path) -> None:
    from nz_coder.foundation import config

    monkeypatch.setattr(config, "WORKDIR", path)


def test_repo_map_lists_cross_file_definitions_and_methods(tmp_path, monkeypatch):
    from nz_coder.tools.repo_map import repo_map

    _bind_workdir(monkeypatch, tmp_path)
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "service.py").write_text(
        "class Base:\n"
        "    pass\n"
        "\n"
        "class Service(Base):\n"
        "    async def run(self, value: int) -> str:\n"
        "        return str(value)\n"
        "\n"
        "def create_service() -> Service:\n"
        "    def local_helper():\n"
        "        return None\n"
        "    return Service()\n",
        encoding="utf-8",
    )
    (package / "models.py").write_text(
        "class User:\n"
        "    def name(self):\n"
        "        return 'demo'\n",
        encoding="utf-8",
    )

    result = repo_map("pkg")

    assert result.startswith("Python repository map")
    assert "pkg/models.py:" in result
    assert "class User" in result
    assert "method User.name" in result
    assert "pkg/service.py:" in result
    assert "class Service(Base)" in result
    assert "async method Service.run" in result
    assert "function create_service" in result
    assert "local_helper" not in result


def test_repo_map_query_filters_paths_and_symbols(tmp_path, monkeypatch):
    from nz_coder.tools.repo_map import repo_map

    _bind_workdir(monkeypatch, tmp_path)
    (tmp_path / "alpha.py").write_text("def load_alpha():\n    return 1\n", encoding="utf-8")
    (tmp_path / "beta.py").write_text("def load_beta():\n    return 2\n", encoding="utf-8")

    result = repo_map(query="beta load")

    assert "query: beta load" in result
    assert "beta.py:" in result
    assert "load_beta" in result
    assert "alpha.py:" not in result


def test_repo_map_reuses_unchanged_ast_and_refresh_reparses(tmp_path, monkeypatch):
    from nz_coder.intelligence import code_index
    from nz_coder.tools import repo_map as module

    _bind_workdir(monkeypatch, tmp_path)
    (tmp_path / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    real_parse = code_index.ast.parse
    parse_calls = 0

    def counting_parse(*args, **kwargs):
        nonlocal parse_calls
        parse_calls += 1
        return real_parse(*args, **kwargs)

    monkeypatch.setattr(code_index.ast, "parse", counting_parse)

    first = module.repo_map()
    second = module.repo_map()
    refreshed = module.repo_map(refresh=True)

    assert parse_calls == 2
    assert "cache_hits: 0" in first
    assert "cache_hits: 1" in second
    assert "cache_hits: 0" in refreshed


def test_repo_map_blocks_escape_and_non_python_file(tmp_path, monkeypatch):
    from nz_coder.tools.repo_map import repo_map

    _bind_workdir(monkeypatch, tmp_path)
    (tmp_path / "notes.txt").write_text("hello\n", encoding="utf-8")

    escaped = repo_map("../outside")
    unsupported = repo_map("notes.txt")

    assert escaped.startswith("Error: Path escapes workspace:")
    assert unsupported == "Error: repo_map does not support source file: notes.txt"


def test_repo_map_prunes_excluded_dirs_and_reports_limits(tmp_path, monkeypatch):
    from nz_coder.tools.repo_map import repo_map

    _bind_workdir(monkeypatch, tmp_path)
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def beta():\n    return 2\n", encoding="utf-8")
    cache_dir = tmp_path / ".venv"
    cache_dir.mkdir()
    (cache_dir / "hidden.py").write_text("def hidden():\n    return 3\n", encoding="utf-8")
    result = repo_map(max_files=1)

    assert "a.py:" in result
    assert "b.py:" not in result
    assert "hidden.py" not in result
    assert "skipped 1 file(s) beyond max_files=1" in result


def test_repo_map_keeps_unmanaged_hidden_source_directories(tmp_path, monkeypatch):
    """Unknown dot-directories can contain user-owned source and workflows."""
    from nz_coder.tools.repo_map import repo_map

    _bind_workdir(monkeypatch, tmp_path)
    hidden = tmp_path / ".ci-tools"
    hidden.mkdir()
    (hidden / "checks.py").write_text(
        "def run_hidden_checks():\n    return True\n",
        encoding="utf-8",
    )

    result = repo_map()

    assert ".ci-tools/checks.py:" in result
    assert "run_hidden_checks" in result


def test_repo_map_is_registered_as_safe_read_tool():
    import nz_coder.tools.repo_map  # noqa: F401
    from nz_coder.tool_platform.permissioning.tool_groups import READ_TOOLS, SAFE_TOOLS
    from nz_coder.tools import TOOL_HANDLERS

    assert "repo_map" in TOOL_HANDLERS
    assert "repo_map" in READ_TOOLS
    assert "repo_map" in SAFE_TOOLS
    assert "code_references" in TOOL_HANDLERS
    assert "code_references" in READ_TOOLS
    assert "code_references" in SAFE_TOOLS
