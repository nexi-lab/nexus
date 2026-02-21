"""Unit tests for the discovery brick (ToolIndex + discovery_tools).

Validates that the discovery module works in isolation — no kernel needed.
"""

from nexus.bricks.discovery.discovery_tools import (
    DISCOVERY_TOOLS,
    get_tool_details,
    list_servers,
    search_tools,
)
from nexus.bricks.discovery.tool_index import ToolIndex, ToolInfo, ToolMatch

# ---------------------------------------------------------------------------
# ToolIndex tests
# ---------------------------------------------------------------------------


def _sample_tools() -> list[ToolInfo]:
    """Create a small set of tools for testing."""
    return [
        ToolInfo(
            name="server_a:search_files",
            description="Search for files by name or content",
            server="server_a",
            input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
        ),
        ToolInfo(
            name="server_a:read_file",
            description="Read the contents of a file",
            server="server_a",
            input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        ),
        ToolInfo(
            name="server_b:send_email",
            description="Send an email to a recipient",
            server="server_b",
            input_schema={"type": "object", "properties": {"to": {"type": "string"}}},
        ),
    ]


class TestToolIndex:
    """Tests for ToolIndex: add, search, list, remove."""

    def test_empty_index(self) -> None:
        idx = ToolIndex()
        assert idx.tool_count == 0
        assert idx.server_count == 0
        assert idx.search("anything") == []

    def test_add_and_search(self) -> None:
        idx = ToolIndex()
        idx.add_tools(_sample_tools())

        matches = idx.search("search files")
        assert len(matches) > 0
        assert all(isinstance(m, ToolMatch) for m in matches)
        # The most relevant result should mention "search" or "files"
        top = matches[0]
        assert "search" in top.tool.name or "search" in top.tool.description.lower()

    def test_get_tool_by_name(self) -> None:
        idx = ToolIndex()
        idx.add_tools(_sample_tools())

        tool = idx.get_tool("server_b:send_email")
        assert tool is not None
        assert tool.server == "server_b"

        assert idx.get_tool("nonexistent:tool") is None

    def test_list_servers(self) -> None:
        idx = ToolIndex()
        idx.add_tools(_sample_tools())

        servers = idx.list_servers()
        assert sorted(servers) == ["server_a", "server_b"]

    def test_list_tools_by_server(self) -> None:
        idx = ToolIndex()
        idx.add_tools(_sample_tools())

        a_tools = idx.list_tools(server="server_a")
        assert len(a_tools) == 2
        assert all(t.server == "server_a" for t in a_tools)

    def test_remove_tool(self) -> None:
        idx = ToolIndex()
        idx.add_tools(_sample_tools())
        assert idx.tool_count == 3

        assert idx.remove_tool("server_b:send_email") is True
        assert idx.tool_count == 2
        assert idx.get_tool("server_b:send_email") is None

        # Removing non-existent returns False
        assert idx.remove_tool("nonexistent") is False

    def test_search_empty_query(self) -> None:
        idx = ToolIndex()
        idx.add_tools(_sample_tools())
        assert idx.search("") == []

    def test_tool_info_to_dict(self) -> None:
        tool = _sample_tools()[0]
        d = tool.to_dict()
        assert d["name"] == "server_a:search_files"
        assert d["server"] == "server_a"
        assert "input_schema" in d


# ---------------------------------------------------------------------------
# Discovery tool function tests
# ---------------------------------------------------------------------------


class TestDiscoveryTools:
    """Tests for the discovery_tools helper functions."""

    def test_discovery_tools_constant(self) -> None:
        assert len(DISCOVERY_TOOLS) == 4
        assert "nexus_discovery:search_tools" in DISCOVERY_TOOLS
        assert "nexus_discovery:list_servers" in DISCOVERY_TOOLS

    def test_search_tools_fn(self) -> None:
        idx = ToolIndex()
        idx.add_tools(_sample_tools())

        result = search_tools(idx, "email")
        assert "tools" in result
        assert "count" in result
        assert result["count"] >= 0

    def test_list_servers_fn(self) -> None:
        idx = ToolIndex()
        idx.add_tools(_sample_tools())

        result = list_servers(idx)
        assert result["total_servers"] == 2
        assert result["total_tools"] == 3
        assert "server_a" in result["servers"]

    def test_get_tool_details_found(self) -> None:
        idx = ToolIndex()
        idx.add_tools(_sample_tools())

        result = get_tool_details(idx, "server_a:read_file")
        assert result["found"] is True
        assert result["name"] == "server_a:read_file"

    def test_get_tool_details_not_found(self) -> None:
        idx = ToolIndex()
        result = get_tool_details(idx, "nonexistent")
        assert result["found"] is False
        assert "error" in result
