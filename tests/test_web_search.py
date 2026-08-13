from __future__ import annotations

import json


class _FakeProvider:
    name = "fake-search"

    def search(self, query: str, *, limit: int = 8, timeout: float = 20.0):
        from nz_coder.web_search import SearchResult

        assert query == "current sdk api"
        assert limit == 2
        assert timeout == 3
        return [SearchResult(
            title="Official SDK release",
            url="https://docs.example.test/sdk",
            snippet="Current API details",
            source="docs.example.test",
            published_at="2026-08-01",
            score=0.9,
        )]


def test_web_search_provider_contract_and_tool_output():
    from nz_coder.tools.web_search import web_search
    from nz_coder.web_search import scoped_web_search_provider

    with scoped_web_search_provider(_FakeProvider()):
        result = web_search("current sdk api", limit=2, timeout=3)

    payload = json.loads(result)
    assert payload["provider"] == "fake-search"
    assert payload["results"][0]["url"] == "https://docs.example.test/sdk"
    assert payload["results"][0]["published_at"] == "2026-08-01"
    assert "webfetch" in payload["guidance"]
    assert result.metadata == {"provider": "fake-search", "result_count": 1}


def test_duckduckgo_parser_preserves_title_snippet_and_target_url():
    from nz_coder.web_search import _DuckDuckGoParser, _result_url

    parser = _DuckDuckGoParser()
    parser.feed("""
      <div class="result">
        <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.example.test%2Frelease">
          Release notes
        </a>
        <a class="result__snippet">Breaking change details</a>
      </div>
    """)

    assert parser.rows == [{
        "title": "\n          Release notes\n        ",
        "url": "//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.example.test%2Frelease",
        "snippet": "Breaking change details",
    }]
    assert _result_url(parser.rows[0]["url"]) == "https://docs.example.test/release"


def test_bing_rss_provider_parses_structured_results(monkeypatch):
    from nz_coder.web_search import BingRssWebSearchProvider

    payload = b"""<?xml version="1.0"?><rss><channel><item>
      <title>Official release</title><link>https://docs.example.test/release</link>
      <description>Breaking API change</description><pubDate>Tue, 12 Aug 2026</pubDate>
    </item></channel></rss>"""

    class _Response:
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def read(self, _limit): return payload

    class _Opener:
        def open(self, _request, timeout):
            assert timeout == 4
            return _Response()

    monkeypatch.setattr("nz_coder.web_search.build_opener", lambda: _Opener())
    results = BingRssWebSearchProvider().search("sdk release", limit=2, timeout=4)

    assert results[0].title == "Official release"
    assert results[0].source == "docs.example.test"
    assert results[0].published_at == "Tue, 12 Aug 2026"


def test_github_issue_provider_preserves_primary_issue_identity(monkeypatch):
    from nz_coder.web_search import GitHubIssueSearchProvider

    payload = json.dumps({"items": [{
        "title": "Remove deprecated proxies argument",
        "html_url": "https://github.com/encode/httpx/pull/2879",
        "body": "Use proxy and mounts instead.",
        "updated_at": "2026-08-01T00:00:00Z",
        "score": 4.25,
    }]}).encode()

    class _Response:
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def read(self, _limit): return payload

    class _Opener:
        def open(self, request, timeout):
            assert "api.github.com/search/issues" in request.full_url
            assert timeout == 5
            return _Response()

    monkeypatch.setattr("nz_coder.web_search.build_opener", lambda: _Opener())
    results = GitHubIssueSearchProvider().search(
        "GitHub issue httpx proxies removed", limit=3, timeout=5,
    )

    assert results[0].source == "github.com"
    assert results[0].url.endswith("/encode/httpx/pull/2879")
    assert results[0].score == 4.25


def test_default_provider_routes_issues_and_filters_irrelevant_results(monkeypatch):
    from nz_coder.web_search import DefaultWebSearchProvider, SearchResult

    provider = DefaultWebSearchProvider()
    monkeypatch.setattr(provider.github, "search", lambda *_args, **_kwargs: [
        SearchResult("HTTPX proxies removed", "https://github.com/x/1", "proxy migration", "github.com"),
        SearchResult("Unrelated UI bug", "https://github.com/x/2", "colors", "github.com"),
    ])

    results = provider.search("GitHub issue HTTPX proxies removed")

    assert [item.url for item in results] == ["https://github.com/x/1"]


def test_web_search_is_registered_as_read_tool_and_allowed_by_default():
    import nz_coder.tools.web_search  # noqa: F401
    from nz_coder.permissions import PermissionManager
    from nz_coder.tool_platform.permissions import PermissionRule
    from nz_coder.tools import get_execution_mode, get_specs

    specs = {item["function"]["name"]: item for item in get_specs()}
    assert specs["web_search"]["function"]["parameters"]["required"] == ["query"]
    assert get_execution_mode("web_search") == "read"
    manager = PermissionManager("acceptEdits")
    assert manager.check("web_search", {"query": "x"})["behavior"] == "ask"
    manager._allow_rules = [PermissionRule("web_search", "allow")]
    assert manager.check("web_search", {"query": "x"})["behavior"] == "allow"


def test_web_search_disabled_provider_is_diagnostic(monkeypatch):
    from nz_coder.tools.web_search import web_search

    monkeypatch.setenv("NZ_CODER_WEB_SEARCH_PROVIDER", "off")
    assert web_search("query").startswith("Error: Web search is disabled")
