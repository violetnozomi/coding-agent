"""Deterministic layered relevance ranking for repository-map candidates."""
from __future__ import annotations

from pathlib import Path

MatchTier = tuple[int, int]
MatchRank = tuple[int, int, int]

_EXACT_SYMBOL = 0
_PREFIX_SYMBOL = 1
_CONTAINS_SYMBOL = 2
_EXACT_FILENAME = 3
_PREFIX_FILENAME = 4
_CONTAINS_FILENAME = 5
_CONTAINS_PATH = 6
_FUZZY = 7


def _case_rank(value: str, query: str) -> int:
    """Prefer exact case only when the query itself contains uppercase."""
    if not any(character.isupper() for character in query):
        return 0
    return 0 if query in value else 1


def _layered_tier(
    value: str,
    query: str,
    *,
    exact: int,
    prefix: int,
    contains: int,
) -> MatchTier | None:
    value_folded = value.casefold()
    query_folded = query.casefold()
    if value_folded == query_folded:
        return exact, _case_rank(value, query)
    if value_folded.startswith(query_folded):
        return prefix, _case_rank(value, query)
    if query_folded in value_folded:
        return contains, _case_rank(value, query)
    return None


def _is_subsequence(query: str, value: str) -> bool:
    iterator = iter(value.casefold())
    return all(
        any(candidate == character for candidate in iterator)
        for character in query.casefold()
    )


def _term_tier(
    *,
    path: str,
    symbol_name: str,
    qualified_name: str,
    signature: str,
    term: str,
) -> MatchTier | None:
    normalized_path = path.replace("\\", "/")
    filename = Path(normalized_path).name
    stem = Path(normalized_path).stem
    normalized_term = term.replace("\\", "/")
    candidates = [
        _layered_tier(
            symbol_name,
            term,
            exact=_EXACT_SYMBOL,
            prefix=_PREFIX_SYMBOL,
            contains=_CONTAINS_SYMBOL,
        ),
        _layered_tier(
            qualified_name,
            term,
            exact=_EXACT_SYMBOL,
            prefix=_PREFIX_SYMBOL,
            contains=_CONTAINS_SYMBOL,
        ),
        _layered_tier(
            filename,
            term,
            exact=_EXACT_FILENAME,
            prefix=_PREFIX_FILENAME,
            contains=_CONTAINS_FILENAME,
        ),
        _layered_tier(
            stem,
            term,
            exact=_EXACT_FILENAME,
            prefix=_PREFIX_FILENAME,
            contains=_CONTAINS_FILENAME,
        ),
    ]
    signature_folded = signature.casefold()
    if term.casefold() in signature_folded:
        candidates.append((_CONTAINS_SYMBOL, _case_rank(signature, term)))
    path_folded = normalized_path.casefold()
    if normalized_term.casefold() in path_folded:
        candidates.append((_CONTAINS_PATH, _case_rank(normalized_path, normalized_term)))
    available = [candidate for candidate in candidates if candidate is not None]
    if available:
        return min(available)
    if (
        _is_subsequence(normalized_term, normalized_path)
        or _is_subsequence(term, qualified_name)
    ):
        combined = f"{normalized_path} {qualified_name}"
        return _FUZZY, _case_rank(combined, normalized_term)
    return None


def rank_repo_symbol(
    *,
    path: str,
    symbol_name: str,
    qualified_name: str,
    signature: str,
    query: str,
) -> MatchRank | None:
    """Rank a symbol with exact/prefix/contains/path/fuzzy layers.

    Every whitespace-separated query term must match. Lower tuples are more
    relevant; quality outranks case sensitivity, matching InfCode's stable
    candidate-ranking principle.
    """
    terms = [term for term in query.split() if term.strip()]
    if not terms:
        return 0, 0, 0
    tiers: list[MatchTier] = []
    for term in terms:
        tier = _term_tier(
            path=path,
            symbol_name=symbol_name,
            qualified_name=qualified_name,
            signature=signature,
            term=term,
        )
        if tier is None:
            return None
        tiers.append(tier)
    return (
        max(tier[0] for tier in tiers),
        sum(tier[0] for tier in tiers),
        sum(tier[1] for tier in tiers),
    )
