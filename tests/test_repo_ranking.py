"""Tests for InfCode-style layered Repo Map candidate ranking."""
from __future__ import annotations

from nz_coder.tools.repo_ranking import rank_repo_symbol


def _rank(
    *,
    path: str = "src/module.py",
    name: str = "unrelated",
    qualified: str | None = None,
    signature: str = "def unrelated()",
    query: str,
):
    return rank_repo_symbol(
        path=path,
        symbol_name=name,
        qualified_name=qualified or name,
        signature=signature,
        query=query,
    )


def test_rank_layers_exact_prefix_contains_filename_path_and_fuzzy():
    ranks = [
        _rank(name="Target", query="Target"),
        _rank(name="TargetBuilder", query="Target"),
        _rank(name="MyTarget", query="Target"),
        _rank(path="src/Target.py", query="Target"),
        _rank(path="src/TargetHelpers.py", query="Target"),
        _rank(path="src/MyTargetHelpers.py", query="Target"),
        _rank(path="src/Target/module.py", query="Target"),
        _rank(path="context/providers/module.py", query="ctxprov"),
    ]

    assert all(rank is not None for rank in ranks)
    assert ranks == sorted(ranks)
    assert [rank[0] for rank in ranks if rank is not None] == list(range(8))


def test_rank_quality_outranks_case_sensitivity():
    exact_case_insensitive = _rank(name="target", query="Target")
    contains_case_sensitive = _rank(name="MyTarget", query="Target")

    assert exact_case_insensitive is not None
    assert contains_case_sensitive is not None
    assert exact_case_insensitive < contains_case_sensitive
    assert exact_case_insensitive[2] == 1


def test_rank_lowercase_query_does_not_demote_case_insensitive_match():
    lowercase = _rank(name="targetBuilder", query="target")
    uppercase_candidate = _rank(name="TargetBuilder", query="target")

    assert lowercase is not None
    assert uppercase_candidate is not None
    assert lowercase[2] == uppercase_candidate[2] == 0


def test_rank_requires_every_query_term_but_allows_cross_field_matches():
    matched = _rank(
        path="services/worker.py",
        name="TargetRunner",
        query="Target services",
    )
    missing = _rank(
        path="workers/worker.py",
        name="TargetRunner",
        query="Target services",
    )

    assert matched is not None
    assert missing is None


def test_rank_normalizes_windows_path_query():
    rank = _rank(
        path="src/Feature/TargetFile.py",
        query=r"src\Feature\Target",
    )

    assert rank is not None


def test_repo_map_orders_exact_symbol_before_partial_symbol(tmp_path, monkeypatch):
    from nz_coder import config
    from nz_coder.tools.repo_map import repo_map

    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    (tmp_path / "a_contains.py").write_text(
        "def MyTarget():\n    return 1\n",
        encoding="utf-8",
    )
    (tmp_path / "z_exact.py").write_text(
        "def Target():\n    return 2\n",
        encoding="utf-8",
    )

    result = repo_map(query="Target")

    assert result.index("z_exact.py:") < result.index("a_contains.py:")
