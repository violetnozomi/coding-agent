"""Provider-neutral web discovery used by the web_search tool."""
from __future__ import annotations

import os
import re
import json
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from typing import Protocol, runtime_checkable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote_plus, unquote, urlencode, urlsplit
from urllib.request import Request, build_opener


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    source: str
    published_at: str | None = None
    score: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@runtime_checkable
class WebSearchProvider(Protocol):
    name: str

    def search(
        self,
        query: str,
        *,
        limit: int = 8,
        timeout: float = 20.0,
    ) -> list[SearchResult]: ...


class _DuckDuckGoParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._field = ""
        self._depth = 0
        self._append_on_close = False

    @staticmethod
    def _classes(attrs: list[tuple[str, str | None]]) -> set[str]:
        return set(dict(attrs).get("class", "").split())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = self._classes(attrs)
        values = dict(attrs)
        if tag == "a" and "result__a" in classes:
            self._current = {"title": "", "url": str(values.get("href") or ""), "snippet": ""}
            self._field = "title"
            self._depth = 1
            self._append_on_close = True
            return
        if "result__snippet" in classes and self.rows:
            self._current = self.rows[-1]
            self._field = "snippet"
            self._depth = 1
            self._append_on_close = False
            return
        if self._current is not None:
            self._depth += 1

    def handle_endtag(self, tag: str) -> None:
        if self._current is None:
            return
        self._depth -= 1
        if self._depth <= 0:
            if self._append_on_close:
                self.rows.append(self._current)
            self._current = None
            self._field = ""
            self._append_on_close = False

    def handle_data(self, data: str) -> None:
        if self._current is None or not self._field:
            return
        self._current[self._field] += data


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _result_url(value: str) -> str:
    raw = str(value or "").strip()
    if raw.startswith("//"):
        raw = "https:" + raw
    parsed = urlsplit(raw)
    if parsed.hostname and parsed.hostname.endswith("duckduckgo.com"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        if target:
            return unquote(target)
    return raw


class DuckDuckGoWebSearchProvider:
    """No-key HTML search backend used as the default discovery provider."""

    name = "duckduckgo-html"
    endpoint = "https://html.duckduckgo.com/html/"

    def search(
        self,
        query: str,
        *,
        limit: int = 8,
        timeout: float = 20.0,
    ) -> list[SearchResult]:
        normalized = _clean_text(query)
        if not normalized:
            raise ValueError("query must not be empty")
        request = Request(
            f"{self.endpoint}?q={quote_plus(normalized)}",
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; nz-coder/0.1; +https://github.com)",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.8",
            },
            method="GET",
        )
        with build_opener().open(request, timeout=max(1.0, min(float(timeout), 60.0))) as response:
            data = response.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            raise ValueError("search response exceeds 2MB")
        parser = _DuckDuckGoParser()
        parser.feed(data.decode("utf-8", errors="replace"))
        results: list[SearchResult] = []
        seen: set[str] = set()
        for row in parser.rows:
            url = _result_url(row.get("url", ""))
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or url in seen:
                continue
            seen.add(url)
            results.append(SearchResult(
                title=_clean_text(row.get("title", "")) or parsed.hostname,
                url=url,
                snippet=_clean_text(row.get("snippet", "")),
                source=parsed.hostname,
            ))
            if len(results) >= max(1, min(int(limit), 20)):
                break
        return results


class BingRssWebSearchProvider:
    """No-key RSS backend with a small, stable response surface."""

    name = "bing-rss"
    endpoint = "https://www.bing.com/search"

    def search(
        self,
        query: str,
        *,
        limit: int = 8,
        timeout: float = 20.0,
    ) -> list[SearchResult]:
        normalized = _clean_text(query)
        if not normalized:
            raise ValueError("query must not be empty")
        request = Request(
            f"{self.endpoint}?{urlencode({'format': 'rss', 'q': normalized, 'mkt': 'en-US', 'setlang': 'en-US', 'cc': 'US'})}",
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; nz-coder/0.1; +https://github.com)",
                "Accept": "application/rss+xml,application/xml,text/xml",
                "Accept-Language": "en-US,en;q=0.8",
            },
            method="GET",
        )
        with build_opener().open(request, timeout=max(1.0, min(float(timeout), 60.0))) as response:
            data = response.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            raise ValueError("search response exceeds 2MB")
        root = ET.fromstring(data)
        results: list[SearchResult] = []
        seen: set[str] = set()
        for item in root.findall(".//item"):
            url = _clean_text(item.findtext("link") or "")
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or url in seen:
                continue
            seen.add(url)
            results.append(SearchResult(
                title=_clean_text(item.findtext("title") or "") or parsed.hostname,
                url=url,
                snippet=_clean_text(item.findtext("description") or ""),
                source=parsed.hostname,
                published_at=_clean_text(item.findtext("pubDate") or "") or None,
            ))
            if len(results) >= max(1, min(int(limit), 20)):
                break
        return results


class GitHubIssueSearchProvider:
    """GitHub's public issue search for explicit issue/bug discovery queries."""

    name = "github-issues"
    endpoint = "https://api.github.com/search/issues"

    def search(
        self,
        query: str,
        *,
        limit: int = 8,
        timeout: float = 20.0,
    ) -> list[SearchResult]:
        normalized = _clean_text(query)
        if not normalized:
            raise ValueError("query must not be empty")
        # Provider routing words add no relevance inside GitHub's own corpus.
        github_query = re.sub(r"\b(?:github|issue|issues)\b", " ", normalized, flags=re.I)
        github_query = _clean_text(github_query) or normalized
        request = Request(
            f"{self.endpoint}?{urlencode({'q': github_query, 'per_page': max(1, min(int(limit), 20))})}",
            headers={
                "User-Agent": "nz-coder/0.1",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            method="GET",
        )
        with build_opener().open(request, timeout=max(1.0, min(float(timeout), 60.0))) as response:
            data = response.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            raise ValueError("search response exceeds 2MB")
        payload = json.loads(data)
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise ValueError("GitHub issue search returned an invalid response")
        results: list[SearchResult] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            url = _clean_text(item.get("html_url") or "")
            title = _clean_text(item.get("title") or "")
            if not url or not title:
                continue
            body = _clean_text(item.get("body") or "")
            results.append(SearchResult(
                title=title, url=url, snippet=body[:500], source="github.com",
                published_at=_clean_text(item.get("updated_at") or "") or None,
                score=float(item["score"]) if isinstance(item.get("score"), (int, float)) else None,
            ))
        return results


_QUERY_STOPWORDS = frozenset({
    "about", "after", "before", "change", "current", "docs", "documentation",
    "error", "find", "fix", "for", "from", "github", "how", "issue", "latest",
    "migration", "official", "release", "search", "the", "this", "using", "what",
    "where", "with", "workaround",
})


def _relevant_results(query: str, results: list[SearchResult]) -> list[SearchResult]:
    tokens = {
        token for token in re.findall(r"[a-z0-9_.+-]{3,}", query.casefold())
        if token not in _QUERY_STOPWORDS and not token.startswith("site:")
    }
    if not tokens:
        return results
    accepted: list[SearchResult] = []
    for result in results:
        candidate = " ".join((result.title, result.url, result.snippet)).casefold()
        if any(token in candidate for token in tokens):
            accepted.append(result)
    return accepted


class DefaultWebSearchProvider:
    """Route issue discovery to GitHub and general discovery to web search."""

    name = "default-web"

    def __init__(self) -> None:
        self.general = BingRssWebSearchProvider()
        self.github = GitHubIssueSearchProvider()

    def search(
        self,
        query: str,
        *,
        limit: int = 8,
        timeout: float = 20.0,
    ) -> list[SearchResult]:
        normalized = _clean_text(query)
        issue_query = bool(re.search(r"\b(?:github|issue|pull request|upstream bug)\b", normalized, re.I))
        if issue_query:
            try:
                return _relevant_results(
                    normalized,
                    self.github.search(normalized, limit=limit, timeout=timeout),
                )
            except (HTTPError, URLError, ValueError, json.JSONDecodeError):
                pass
        return _relevant_results(
            normalized,
            self.general.search(normalized, limit=limit, timeout=timeout),
        )


_PROVIDER: ContextVar[WebSearchProvider | None] = ContextVar(
    "nz_coder_web_search_provider",
    default=None,
)


def default_web_search_provider() -> WebSearchProvider | None:
    selected = os.environ.get("NZ_CODER_WEB_SEARCH_PROVIDER", "auto").strip().lower()
    if selected in {"", "off", "none", "disabled"}:
        return None
    if selected in {"duckduckgo", "ddg", "duckduckgo-html"}:
        return DuckDuckGoWebSearchProvider()
    if selected in {"auto", "default"}:
        return DefaultWebSearchProvider()
    if selected in {"bing", "bing-rss"}:
        return BingRssWebSearchProvider()
    if selected in {"github", "github-issues"}:
        return GitHubIssueSearchProvider()
    raise ValueError(f"Unsupported web search provider: {selected}")


def current_web_search_provider() -> WebSearchProvider | None:
    return _PROVIDER.get() or default_web_search_provider()


@contextmanager
def scoped_web_search_provider(provider: WebSearchProvider | None):
    token = _PROVIDER.set(provider)
    try:
        yield provider
    finally:
        _PROVIDER.reset(token)


def search_web(query: str, *, limit: int = 8, timeout: float = 20.0) -> tuple[str, list[SearchResult]]:
    provider = current_web_search_provider()
    if provider is None:
        raise RuntimeError("Web search is disabled; set NZ_CODER_WEB_SEARCH_PROVIDER")
    try:
        return provider.name, provider.search(query, limit=limit, timeout=timeout)
    except HTTPError as exc:
        raise RuntimeError(f"Web search HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"Web search request failed: {exc.reason}") from exc


__all__ = [
    "DuckDuckGoWebSearchProvider",
    "BingRssWebSearchProvider",
    "DefaultWebSearchProvider",
    "SearchResult",
    "GitHubIssueSearchProvider",
    "WebSearchProvider",
    "current_web_search_provider",
    "scoped_web_search_provider",
    "search_web",
]
