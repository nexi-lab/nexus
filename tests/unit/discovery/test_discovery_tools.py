"""Tests for discovery tools functions."""

import pytest

from nexus.discovery.discovery_tools import (
    DISCOVERY_TOOLS,
    get_tool_details,
    list_servers,
    load_tools,
    search_tools,
)
from nexus.discovery.tool_index import ToolIndex, ToolInfo


class TestDiscoveryToolsSchema:
    """Tests for DISCOVERY_TOOLS schema definitions."""

    def test_all_tools_defined(self) -> None:
        """Test that all 4 discovery tools are defined."""
        expected_tools = [
            "nexus_discovery:search_tools",
            "nexus_discovery:list_servers",
            "nexus_discovery:get_tool_details",
            "nexus_discovery:load_tools",
        ]
        assert set(DISCOVERY_TOOLS.keys()) == set(expected_tools)

    def test_search_tools_schema(self) -> None:
        """Test search_tools schema structure."""
        schema = DISCOVERY_TOOLS["nexus_discovery:search_tools"]
        assert schema["name"] == "nexus_discovery:search_tools"
        assert "description" in schema
        assert "inputSchema" in schema
        assert schema["inputSchema"]["required"] == ["query"]

    def test_list_servers_schema(self) -> None:
        """Test list_servers schema structure."""
        schema = DISCOVERY_TOOLS["nexus_discovery:list_servers"]
        assert schema["name"] == "nexus_discovery:list_servers"
        assert "description" in schema

    def test_get_tool_details_schema(self) -> None:
        """Test get_tool_details schema structure."""
        schema = DISCOVERY_TOOLS["nexus_discovery:get_tool_details"]
        assert schema["name"] == "nexus_discovery:get_tool_details"
        assert schema["inputSchema"]["required"] == ["tool_name"]

    def test_load_tools_schema(self) -> None:
        """Test load_tools schema structure."""
        schema = DISCOVERY_TOOLS["nexus_discovery:load_tools"]
        assert schema["name"] == "nexus_discovery:load_tools"
        assert schema["inputSchema"]["required"] == ["tool_names"]
        assert schema["inputSchema"]["properties"]["tool_names"]["type"] == "array"


class TestSearchToolsFunction:
    """Tests for search_tools function."""

    @pytest.fixture
    def index(self) -> ToolIndex:
        """Create a test index."""
        idx = ToolIndex()
        idx.add_tools([
            ToolInfo("calc:add", "Add two numbers", "calc"),
            ToolInfo("calc:sub", "Subtract numbers", "calc"),
            ToolInfo("fs:read", "Read a file", "fs"),
        ])
        return idx

    def test_search_returns_dict(self, index: ToolIndex) -> None:
        """Test search_tools returns proper dict structure."""
        result = search_tools(index, "add")
        assert isinstance(result, dict)
        assert "tools" in result
        assert "count" in result
        assert "query" in result

    def test_search_finds_matching_tools(self, index: ToolIndex) -> None:
        """Test search_tools finds relevant tools."""
        result = search_tools(index, "add numbers")
        assert result["count"] > 0
        assert result["tools"][0]["name"] == "calc:add"

    def test_search_respects_top_k(self, index: ToolIndex) -> None:
        """Test search_tools respects top_k parameter."""
        result = search_tools(index, "number", top_k=1)
        assert len(result["tools"]) <= 1

    def test_search_empty_results(self, index: ToolIndex) -> None:
        """Test search_tools with no matches."""
        result = search_tools(index, "nonexistent")
        assert result["count"] == 0
        assert result["tools"] == []


class TestListServersFunction:
    """Tests for list_servers function."""

    def test_list_empty_index(self) -> None:
        """Test list_servers on empty index."""
        index = ToolIndex()
        result = list_servers(index)

        assert result["servers"] == []
        assert result["total_servers"] == 0
        assert result["total_tools"] == 0

    def test_list_with_tools(self) -> None:
        """Test list_servers with populated index."""
        index = ToolIndex()
        index.add_tools([
            ToolInfo("calc:add", "desc", "calc"),
            ToolInfo("calc:sub", "desc", "calc"),
            ToolInfo("fs:read", "desc", "fs"),
            ToolInfo("http:get", "desc", "http"),
        ])

        result = list_servers(index)

        assert result["total_servers"] == 3
        assert result["total_tools"] == 4
        assert set(result["servers"]) == {"calc", "fs", "http"}
        assert result["server_tool_counts"]["calc"] == 2
        assert result["server_tool_counts"]["fs"] == 1


class TestGetToolDetailsFunction:
    """Tests for get_tool_details function."""

    @pytest.fixture
    def index(self) -> ToolIndex:
        """Create a test index."""
        idx = ToolIndex()
        idx.add_tool(ToolInfo(
            name="test:tool",
            description="A test tool",
            server="test",
            input_schema={"param": "string"},
        ))
        return idx

    def test_get_existing_tool(self, index: ToolIndex) -> None:
        """Test getting details of existing tool."""
        result = get_tool_details(index, "test:tool")

        assert result["found"] is True
        assert result["name"] == "test:tool"
        assert result["description"] == "A test tool"
        assert result["server"] == "test"
        assert result["input_schema"] == {"param": "string"}

    def test_get_nonexistent_tool(self, index: ToolIndex) -> None:
        """Test getting details of non-existent tool."""
        result = get_tool_details(index, "nonexistent")

        assert result["found"] is False
        assert "error" in result


class TestLoadToolsFunction:
    """Tests for load_tools function."""

    @pytest.fixture
    def index(self) -> ToolIndex:
        """Create a test index."""
        idx = ToolIndex()
        idx.add_tools([
            ToolInfo("calc:add", "Add", "calc"),
            ToolInfo("calc:sub", "Sub", "calc"),
            ToolInfo("fs:read", "Read", "fs"),
        ])
        return idx

    def test_load_single_tool(self, index: ToolIndex) -> None:
        """Test loading a single tool."""
        active: dict = {}
        result = load_tools(index, ["calc:add"], active)

        assert result["loaded"] == ["calc:add"]
        assert result["not_found"] == []
        assert result["already_loaded"] == []
        assert result["active_tool_count"] == 1
        assert "calc:add" in active

    def test_load_multiple_tools(self, index: ToolIndex) -> None:
        """Test loading multiple tools."""
        active: dict = {}
        result = load_tools(index, ["calc:add", "fs:read"], active)

        assert set(result["loaded"]) == {"calc:add", "fs:read"}
        assert result["active_tool_count"] == 2

    def test_load_nonexistent_tool(self, index: ToolIndex) -> None:
        """Test loading non-existent tool."""
        active: dict = {}
        result = load_tools(index, ["nonexistent"], active)

        assert result["loaded"] == []
        assert result["not_found"] == ["nonexistent"]

    def test_load_already_loaded_tool(self, index: ToolIndex) -> None:
        """Test loading an already loaded tool."""
        active: dict = {"calc:add": {"name": "calc:add"}}
        result = load_tools(index, ["calc:add"], active)

        assert result["loaded"] == []
        assert result["already_loaded"] == ["calc:add"]
        assert result["active_tool_count"] == 1

    def test_load_mixed_tools(self, index: ToolIndex) -> None:
        """Test loading mix of new, existing, and non-existent tools."""
        active: dict = {"calc:add": {"name": "calc:add"}}
        result = load_tools(
            index,
            ["calc:add", "calc:sub", "nonexistent"],
            active,
        )

        assert result["loaded"] == ["calc:sub"]
        assert result["already_loaded"] == ["calc:add"]
        assert result["not_found"] == ["nonexistent"]
        assert result["active_tool_count"] == 2

    def test_load_modifies_active_tools(self, index: ToolIndex) -> None:
        """Test that load_tools modifies active_tools dict in place."""
        active: dict = {}
        load_tools(index, ["calc:add"], active)

        assert "calc:add" in active
        assert active["calc:add"]["name"] == "calc:add"
        assert active["calc:add"]["server"] == "calc"
