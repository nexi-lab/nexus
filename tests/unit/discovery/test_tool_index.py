"""Tests for the ToolIndex class."""

import pytest

from nexus.discovery.tool_index import ToolIndex, ToolInfo, ToolMatch


class TestToolInfo:
    """Tests for ToolInfo dataclass."""

    def test_create_tool_info(self) -> None:
        """Test creating a ToolInfo instance."""
        tool = ToolInfo(
            name="calculator:add",
            description="Add two numbers",
            server="calculator",
            input_schema={"a": "number", "b": "number"},
        )
        assert tool.name == "calculator:add"
        assert tool.description == "Add two numbers"
        assert tool.server == "calculator"
        assert tool.input_schema == {"a": "number", "b": "number"}

    def test_tool_info_to_dict(self) -> None:
        """Test converting ToolInfo to dictionary."""
        tool = ToolInfo(
            name="test:tool",
            description="Test tool",
            server="test",
        )
        result = tool.to_dict()
        assert result["name"] == "test:tool"
        assert result["description"] == "Test tool"
        assert result["server"] == "test"
        assert result["input_schema"] == {}

    def test_tool_info_default_schema(self) -> None:
        """Test that input_schema defaults to empty dict."""
        tool = ToolInfo(name="a", description="b", server="c")
        assert tool.input_schema == {}


class TestToolMatch:
    """Tests for ToolMatch dataclass."""

    def test_create_tool_match(self) -> None:
        """Test creating a ToolMatch instance."""
        tool = ToolInfo(name="test", description="desc", server="srv")
        match = ToolMatch(tool=tool, score=0.95)
        assert match.tool == tool
        assert match.score == 0.95

    def test_tool_match_to_dict(self) -> None:
        """Test converting ToolMatch to dictionary."""
        tool = ToolInfo(name="test", description="desc", server="srv")
        match = ToolMatch(tool=tool, score=0.12345678)
        result = match.to_dict()
        assert result["name"] == "test"
        assert result["score"] == 0.1235  # Rounded to 4 decimal places


class TestToolIndexBasic:
    """Basic tests for ToolIndex."""

    def test_empty_index(self) -> None:
        """Test empty index properties."""
        index = ToolIndex()
        assert index.tool_count == 0
        assert index.server_count == 0
        assert index.list_servers() == []
        assert index.list_tools() == []

    def test_add_single_tool(self) -> None:
        """Test adding a single tool."""
        index = ToolIndex()
        tool = ToolInfo(name="test:tool", description="A test tool", server="test")
        index.add_tool(tool)

        assert index.tool_count == 1
        assert index.server_count == 1
        assert "test" in index.list_servers()

    def test_add_multiple_tools(self) -> None:
        """Test adding multiple tools."""
        index = ToolIndex()
        tools = [
            ToolInfo(name="calc:add", description="Add numbers", server="calc"),
            ToolInfo(name="calc:sub", description="Subtract numbers", server="calc"),
            ToolInfo(name="fs:read", description="Read file", server="fs"),
        ]
        index.add_tools(tools)

        assert index.tool_count == 3
        assert index.server_count == 2

    def test_get_tool(self) -> None:
        """Test getting a tool by name."""
        index = ToolIndex()
        tool = ToolInfo(name="test:tool", description="desc", server="test")
        index.add_tool(tool)

        result = index.get_tool("test:tool")
        assert result is not None
        assert result.name == "test:tool"

        # Non-existent tool
        assert index.get_tool("nonexistent") is None

    def test_remove_tool(self) -> None:
        """Test removing a tool."""
        index = ToolIndex()
        tool = ToolInfo(name="test:tool", description="desc", server="test")
        index.add_tool(tool)

        assert index.remove_tool("test:tool") is True
        assert index.tool_count == 0
        assert index.get_tool("test:tool") is None

        # Removing non-existent tool
        assert index.remove_tool("nonexistent") is False

    def test_list_tools_by_server(self) -> None:
        """Test listing tools filtered by server."""
        index = ToolIndex()
        index.add_tools([
            ToolInfo(name="calc:add", description="Add", server="calc"),
            ToolInfo(name="calc:sub", description="Sub", server="calc"),
            ToolInfo(name="fs:read", description="Read", server="fs"),
        ])

        calc_tools = index.list_tools(server="calc")
        assert len(calc_tools) == 2
        assert all(t.server == "calc" for t in calc_tools)

        fs_tools = index.list_tools(server="fs")
        assert len(fs_tools) == 1


class TestToolIndexSearch:
    """Tests for ToolIndex search functionality."""

    @pytest.fixture
    def populated_index(self) -> ToolIndex:
        """Create an index with test tools."""
        index = ToolIndex()
        index.add_tools([
            ToolInfo("calculator:add", "Add two numbers together", "calculator"),
            ToolInfo("calculator:subtract", "Subtract one number from another", "calculator"),
            ToolInfo("calculator:multiply", "Multiply two numbers", "calculator"),
            ToolInfo("filesystem:read", "Read contents of a file from disk", "filesystem"),
            ToolInfo("filesystem:write", "Write content to a file on disk", "filesystem"),
            ToolInfo("filesystem:delete", "Delete a file from the filesystem", "filesystem"),
            ToolInfo("http:get", "Make an HTTP GET request", "http"),
            ToolInfo("http:post", "Make an HTTP POST request with data", "http"),
            ToolInfo("database:query", "Execute a database SQL query", "database"),
            ToolInfo("database:insert", "Insert data into database table", "database"),
        ])
        return index

    def test_search_empty_query(self, populated_index: ToolIndex) -> None:
        """Test search with empty query."""
        results = populated_index.search("")
        assert results == []

    def test_search_no_results(self, populated_index: ToolIndex) -> None:
        """Test search with no matching results."""
        results = populated_index.search("xyznonexistent")
        assert results == []

    def test_search_single_term(self, populated_index: ToolIndex) -> None:
        """Test search with single term."""
        results = populated_index.search("add")
        assert len(results) > 0
        assert results[0].tool.name == "calculator:add"

    def test_search_multiple_terms(self, populated_index: ToolIndex) -> None:
        """Test search with multiple terms."""
        results = populated_index.search("read file")
        assert len(results) > 0
        # filesystem:read should rank high
        tool_names = [r.tool.name for r in results]
        assert "filesystem:read" in tool_names

    def test_search_top_k(self, populated_index: ToolIndex) -> None:
        """Test search with top_k limit."""
        results = populated_index.search("data", top_k=2)
        assert len(results) <= 2

    def test_search_ranking(self, populated_index: ToolIndex) -> None:
        """Test that results are sorted by score descending."""
        results = populated_index.search("file")
        scores = [r.score for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_search_case_insensitive(self, populated_index: ToolIndex) -> None:
        """Test that search is case insensitive."""
        results_lower = populated_index.search("http")
        results_upper = populated_index.search("HTTP")
        results_mixed = populated_index.search("Http")

        assert len(results_lower) == len(results_upper) == len(results_mixed)

    def test_search_partial_match(self, populated_index: ToolIndex) -> None:
        """Test search with partial word matches."""
        # "number" appears in calculator descriptions
        results = populated_index.search("number")
        assert len(results) > 0
        # Should find calculator tools
        servers = {r.tool.server for r in results}
        assert "calculator" in servers


class TestToolIndexBM25:
    """Tests for BM25 scoring specifics."""

    def test_bm25_term_frequency(self) -> None:
        """Test that repeated terms increase score."""
        index = ToolIndex()
        index.add_tools([
            ToolInfo("tool1", "file file file", "srv"),  # 3x "file"
            ToolInfo("tool2", "file", "srv"),  # 1x "file"
        ])

        results = index.search("file")
        # tool1 should score higher due to term frequency
        assert results[0].tool.name == "tool1"

    def test_bm25_document_length_normalization(self) -> None:
        """Test that long documents don't unfairly dominate."""
        index = ToolIndex()
        # Short doc with target term
        index.add_tool(ToolInfo("short", "read file", "srv"))
        # Long doc with same term but lots of other words
        long_desc = "read " + " ".join(["word"] * 50)
        index.add_tool(ToolInfo("long", long_desc, "srv"))

        results = index.search("read")
        # Short doc should rank higher due to length normalization
        assert results[0].tool.name == "short"

    def test_bm25_idf(self) -> None:
        """Test inverse document frequency scoring."""
        index = ToolIndex()
        # Common term appears in all docs
        index.add_tools([
            ToolInfo("tool1", "common rare", "srv"),
            ToolInfo("tool2", "common", "srv"),
            ToolInfo("tool3", "common", "srv"),
        ])

        # "rare" is more discriminative
        results = index.search("rare")
        assert results[0].tool.name == "tool1"


class TestToolIndexEdgeCases:
    """Edge case tests for ToolIndex."""

    def test_empty_description(self) -> None:
        """Test tool with empty description."""
        index = ToolIndex()
        index.add_tool(ToolInfo("tool", "", "srv"))
        # Should still be searchable by name
        results = index.search("tool")
        assert len(results) == 1

    def test_special_characters(self) -> None:
        """Test tools with special characters."""
        index = ToolIndex()
        index.add_tool(ToolInfo("srv:tool-name_v2", "desc (with parens)", "srv"))
        results = index.search("tool")
        assert len(results) == 1

    def test_duplicate_tool_name(self) -> None:
        """Test adding tool with duplicate name overwrites."""
        index = ToolIndex()
        index.add_tool(ToolInfo("test", "old description", "srv"))
        index.add_tool(ToolInfo("test", "new description", "srv"))

        assert index.tool_count == 1
        tool = index.get_tool("test")
        assert tool is not None
        assert tool.description == "new description"

    def test_unicode_content(self) -> None:
        """Test handling of unicode content."""
        index = ToolIndex()
        index.add_tool(ToolInfo("tool", "描述 description émoji 🔧", "srv"))
        # Should not crash
        results = index.search("description")
        assert len(results) == 1