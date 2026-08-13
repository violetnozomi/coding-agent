"""Agent-facing discovery tests over an already connected MCP runtime."""
from __future__ import annotations

import json

from nz_coder.mcp import current_mcp_runtime, scoped_mcp_runtime
from nz_coder.tools.mcp_catalog import _mcp_catalog


class _Runtime:
    def status_summary(self):
        return [{"name": "docs", "status": "connected", "tool_count": 1, "error": ""}]

    def tool_bindings(self):
        return [{
            "name": "mcp_docs_search_api",
            "description": "Search API documentation",
            "parameters": {"type": "object"},
            "server": "docs",
            "original_name": "search_api",
        }]

    def prompt_definitions(self):
        return [{"server": "docs", "name": "review", "description": "Review an API change"}]

    def resource_definitions(self):
        return [{
            "server": "docs",
            "name": "API guide",
            "uri": "docs://api/guide",
            "description": "Current API guide",
            "mimeType": "text/markdown",
        }]

    def get_prompt(self, server_name, prompt_name, arguments=None):
        assert (server_name, prompt_name) == ("docs", "review")
        return {"description": "Review", "messages": [{"role": "user", "content": arguments or {}}]}

    def read_resource(self, server_name, uri):
        assert (server_name, uri) == ("docs", "docs://api/guide")
        return {"contents": [{"uri": uri, "text": "API CONTENT"}]}


def _run(**kwargs):
    with scoped_mcp_runtime(_Runtime()):
        return _mcp_catalog(**kwargs)


def test_scoped_mcp_runtime_is_context_local_and_resets():
    first = object()
    second = object()
    assert current_mcp_runtime() is None
    with scoped_mcp_runtime(first):
        assert current_mcp_runtime() is first
        with scoped_mcp_runtime(second):
            assert current_mcp_runtime() is second
        assert current_mcp_runtime() is first
    assert current_mcp_runtime() is None


def test_catalog_search_discovers_all_mcp_entry_kinds():
    payload = json.loads(_run(operation="search", query="", limit=20))

    assert {item["kind"] for item in payload["items"]} == {
        "server", "tool", "prompt", "resource",
    }
    assert payload["total"] == 4
    assert all("command" not in item and "env" not in item for item in payload["items"])


def test_catalog_search_filters_kind_query_and_bounds_limit():
    payload = json.loads(_run(
        operation="search",
        query="api",
        kind="resource",
        limit=1,
    ))

    assert payload["total"] == 1
    assert payload["items"][0]["uri"] == "docs://api/guide"


def test_catalog_get_prompt_and_read_resource_use_exact_runtime_calls():
    prompt = json.loads(_run(
        operation="get_prompt",
        server="docs",
        name="review",
        arguments={"scope": "public"},
    ))
    resource = json.loads(_run(
        operation="read_resource",
        server="docs",
        uri="docs://api/guide",
    ))

    assert prompt["messages"][0]["content"] == {"scope": "public"}
    assert resource["contents"][0]["text"] == "API CONTENT"


def test_catalog_fails_closed_outside_active_runtime_and_on_bad_operation():
    assert _mcp_catalog(operation="search").startswith("Error: MCP runtime is not active")
    assert _run(operation="delete_server").startswith("Error: operation must be")
