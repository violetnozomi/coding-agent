"""Contracts for immutable tool catalog and bounded search."""
from __future__ import annotations

from nz_coder.tool_platform.catalog import ToolCatalog
from nz_coder.tool_platform.search import ToolSearchIndex


def _spec(name: str, description: str = "", parameter: str = "query") -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {parameter: {"type": "string"}},
            },
        },
    }


def test_catalog_adapts_specs_without_sharing_mutable_schema() -> None:
    source = _spec("mcp_issue_search", "Search external issue tracker")
    catalog = ToolCatalog.from_specs([source])
    source["function"]["parameters"]["properties"]["query"]["type"] = "number"

    definition = catalog.require("mcp_issue_search")
    assert definition.parameters["properties"]["query"]["type"] == "string"
    assert definition.schema_tokens > 0
    assert catalog.names() == ("mcp_issue_search",)


def test_catalog_rejects_duplicate_names() -> None:
    try:
        ToolCatalog.from_specs([_spec("read_file"), _spec("read_file")])
    except ValueError as error:
        assert "duplicate" in str(error).lower()
    else:
        raise AssertionError("duplicate tool definitions must fail closed")


def test_search_supports_exact_and_required_keyword_ranking() -> None:
    catalog = ToolCatalog.from_specs([
        _spec("read_file", "Read a local file by path", "path"),
        _spec("mcp_issue_search", "Search external issue tracker tickets"),
        _spec("semantic_lookup", "Find conceptually related source modules"),
    ])
    index = ToolSearchIndex(catalog)

    assert [item.name for item in index.search("select:semantic_lookup")] == [
        "semantic_lookup",
    ]
    assert [item.name for item in index.search("+external tickets", limit=3)] == [
        "mcp_issue_search",
    ]
