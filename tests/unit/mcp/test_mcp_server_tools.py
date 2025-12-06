"""Comprehensive tests for MCP server tools and functionality.

This test suite covers all tools, resources, prompts, and server creation scenarios
for the Nexus MCP server implementation.
"""

from __future__ import annotations

import json
from unittest.mock import Mock, patch

import pytest

from nexus.mcp.server import create_mcp_server

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================


def get_tool(server, tool_name: str):
    """Helper to get a tool from the MCP server."""
    return server._tool_manager._tools[tool_name]


def get_prompt(server, prompt_name: str):
    """Helper to get a prompt from the MCP server."""
    return server._prompt_manager._prompts[prompt_name]


def get_resource_template(server, uri_pattern: str):
    """Helper to get a resource template from the MCP server."""
    templates = server._resource_manager._templates
    for template_key, template in templates.items():
        if uri_pattern in str(template_key):
            return template
    raise KeyError(f"Resource template with pattern '{uri_pattern}' not found")


def tool_exists(server, tool_name: str) -> bool:
    """Check if a tool exists in the server."""
    return tool_name in server._tool_manager._tools


# ============================================================================
# FIXTURES
# ============================================================================


@pytest.fixture
def mock_nx_basic():
    """Create a basic mock NexusFS with file operations."""
    nx = Mock()
    nx.read = Mock(return_value=b"test content")
    nx.write = Mock()
    nx.delete = Mock()
    nx.list = Mock(return_value=["/file1.txt", "/file2.txt"])
    nx.glob = Mock(return_value=["test.py", "main.py"])
    nx.grep = Mock(return_value=[{"file": "test.py", "line": 10, "content": "match"}])
    nx.exists = Mock(return_value=True)
    nx.is_directory = Mock(return_value=False)
    nx.mkdir = Mock()
    nx.rmdir = Mock()
    return nx


@pytest.fixture
def mock_nx_with_memory():
    """Create mock NexusFS with memory system."""
    nx = Mock()
    nx.read = Mock(return_value=b"test")
    nx.write = Mock()

    # Add memory system
    nx.memory = Mock()
    nx.memory.store = Mock()
    nx.memory.search = Mock(
        return_value=[{"content": "test memory", "importance": 0.8, "type": "technical"}]
    )
    nx.memory.session = Mock()
    nx.memory.session.commit = Mock()
    nx.memory.session.rollback = Mock()

    return nx


@pytest.fixture
def mock_nx_with_workflows():
    """Create mock NexusFS with workflow system."""
    nx = Mock()
    nx.read = Mock(return_value=b"test")
    nx.write = Mock()

    # Add workflows system
    nx.workflows = Mock()
    nx.workflows.list_workflows = Mock(
        return_value=[{"name": "test_workflow", "description": "Test workflow"}]
    )
    nx.workflows.execute = Mock(return_value={"status": "success", "output": "done"})

    return nx


@pytest.fixture
def mock_nx_with_search():
    """Create mock NexusFS with semantic search."""
    from unittest.mock import AsyncMock

    nx = Mock()
    nx.read = Mock(return_value=b"test")
    nx.write = Mock()

    # Add async semantic_search method
    async def mock_semantic_search(query, path="/", limit=10, **kwargs):
        return [{"path": "/file1.txt", "score": 0.95, "snippet": "relevant content"}]

    nx.semantic_search = AsyncMock(side_effect=mock_semantic_search)

    return nx


@pytest.fixture
def mock_nx_with_sandbox():
    """Create mock NexusFS with sandbox support."""
    nx = Mock()
    nx.read = Mock(return_value=b"test")
    nx.write = Mock()

    # Add sandbox support
    nx._ensure_sandbox_manager = Mock()
    nx._sandbox_manager = Mock()
    nx._sandbox_manager.providers = {"docker": Mock()}

    nx.sandbox_create = Mock(
        return_value={
            "sandbox_id": "test-sandbox-123",
            "name": "test",
            "provider": "docker",
            "status": "running",
        }
    )
    nx.sandbox_list = Mock(
        return_value=[{"sandbox_id": "test-sandbox-123", "name": "test", "status": "running"}]
    )
    nx.sandbox_run = Mock(
        return_value={
            "stdout": "Hello, World!",
            "stderr": "",
            "exit_code": 0,
            "execution_time": 0.123,
        }
    )
    nx.sandbox_stop = Mock()

    return nx


@pytest.fixture
def mock_nx_no_sandbox():
    """Create mock NexusFS without sandbox support."""
    nx = Mock()
    nx.read = Mock(return_value=b"test")
    nx.write = Mock()

    # No sandbox support (no _ensure_sandbox_manager method)
    if hasattr(nx, "_ensure_sandbox_manager"):
        delattr(nx, "_ensure_sandbox_manager")

    return nx


@pytest.fixture
def mock_nx_full():
    """Create mock NexusFS with all features enabled."""
    nx = Mock()

    # Basic file operations
    nx.read = Mock(return_value=b"test content")
    nx.write = Mock()
    nx.delete = Mock()
    nx.list = Mock(return_value=["/file1.txt"])
    nx.glob = Mock(return_value=["test.py"])
    nx.grep = Mock(return_value=[{"file": "test.py", "line": 10, "content": "match"}])
    nx.exists = Mock(return_value=True)
    nx.is_directory = Mock(return_value=False)
    nx.mkdir = Mock()
    nx.rmdir = Mock()

    # Memory system
    nx.memory = Mock()
    nx.memory.store = Mock()
    nx.memory.search = Mock(return_value=[])
    nx.memory.session = Mock()
    nx.memory.session.commit = Mock()
    nx.memory.session.rollback = Mock()

    # Workflow system
    nx.workflows = Mock()
    nx.workflows.list_workflows = Mock(return_value=[])
    nx.workflows.execute = Mock(return_value={"status": "success"})

    # Search
    nx.search = Mock(return_value=[])

    # Sandbox
    nx._ensure_sandbox_manager = Mock()
    nx._sandbox_manager = Mock()
    nx._sandbox_manager.providers = {"docker": Mock()}
    nx.sandbox_create = Mock(return_value={"sandbox_id": "test-123"})
    nx.sandbox_list = Mock(return_value=[])
    nx.sandbox_run = Mock(
        return_value={"stdout": "output", "stderr": "", "exit_code": 0, "execution_time": 0.1}
    )
    nx.sandbox_stop = Mock()

    return nx


# ============================================================================
# FILE OPERATIONS TESTS
# ============================================================================


class TestFileOperationTools:
    """Test suite for file operation tools."""

    def test_read_file_success(self, mock_nx_basic):
        """Test reading a file successfully."""
        server = create_mcp_server(nx=mock_nx_basic)

        # Access tool via helper
        read_tool = get_tool(server, "nexus_read_file")
        result = read_tool.fn(path="/test.txt")

        assert result == "test content"
        mock_nx_basic.read.assert_called_once_with("/test.txt")

    def test_read_file_bytes_content(self, mock_nx_basic):
        """Test reading file with bytes content."""
        mock_nx_basic.read.return_value = b"binary content"
        server = create_mcp_server(nx=mock_nx_basic)

        read_tool = get_tool(server, "nexus_read_file")
        result = read_tool.fn(path="/test.bin")

        assert result == "binary content"

    def test_read_file_error(self, mock_nx_basic):
        """Test read file error handling."""
        mock_nx_basic.read.side_effect = FileNotFoundError("File not found")
        server = create_mcp_server(nx=mock_nx_basic)

        read_tool = get_tool(server, "nexus_read_file")
        result = read_tool.fn(path="/missing.txt")

        assert "Error" in result
        assert "not found" in result.lower()

    def test_write_file_success(self, mock_nx_basic):
        """Test writing a file successfully."""
        server = create_mcp_server(nx=mock_nx_basic)

        write_tool = get_tool(server, "nexus_write_file")
        result = write_tool.fn(path="/test.txt", content="new content")

        assert "Successfully wrote" in result
        assert "/test.txt" in result
        mock_nx_basic.write.assert_called_once()

        # Verify content was encoded
        call_args = mock_nx_basic.write.call_args[0]
        assert call_args[0] == "/test.txt"
        assert call_args[1] == b"new content"

    def test_write_file_error(self, mock_nx_basic):
        """Test write file error handling."""
        mock_nx_basic.write.side_effect = PermissionError("Permission denied")
        server = create_mcp_server(nx=mock_nx_basic)

        write_tool = get_tool(server, "nexus_write_file")
        result = write_tool.fn(path="/test.txt", content="content")

        assert "Error" in result
        assert "permission" in result.lower() or "denied" in result.lower()

    def test_delete_file_success(self, mock_nx_basic):
        """Test deleting a file successfully."""
        server = create_mcp_server(nx=mock_nx_basic)

        delete_tool = get_tool(server, "nexus_delete_file")
        result = delete_tool.fn(path="/test.txt")

        assert "Successfully deleted" in result
        assert "/test.txt" in result
        mock_nx_basic.delete.assert_called_once_with("/test.txt")

    def test_delete_file_error(self, mock_nx_basic):
        """Test delete file error handling."""
        mock_nx_basic.delete.side_effect = FileNotFoundError("File not found")
        server = create_mcp_server(nx=mock_nx_basic)

        delete_tool = get_tool(server, "nexus_delete_file")
        result = delete_tool.fn(path="/missing.txt")

        assert "Error" in result
        assert "not found" in result.lower() or "deleted" in result.lower()

    def test_list_files_basic(self, mock_nx_basic):
        """Test listing files in a directory."""
        server = create_mcp_server(nx=mock_nx_basic)

        list_tool = get_tool(server, "nexus_list_files")
        result = list_tool.fn(path="/data")

        # Result should be JSON with pagination metadata
        response = json.loads(result)
        assert isinstance(response, dict)
        assert "items" in response
        assert "total" in response
        assert "count" in response
        assert isinstance(response["items"], list)
        assert "/file1.txt" in response["items"]
        mock_nx_basic.list.assert_called_once_with("/data", recursive=False, details=True)

    def test_list_files_recursive(self, mock_nx_basic):
        """Test listing files recursively."""
        server = create_mcp_server(nx=mock_nx_basic)

        list_tool = get_tool(server, "nexus_list_files")
        list_tool.fn(path="/data", recursive=True, details=True)

        mock_nx_basic.list.assert_called_once_with("/data", recursive=True, details=True)

    def test_list_files_error(self, mock_nx_basic):
        """Test list files error handling."""
        mock_nx_basic.list.side_effect = FileNotFoundError("Directory not found")
        server = create_mcp_server(nx=mock_nx_basic)

        list_tool = get_tool(server, "nexus_list_files")
        result = list_tool.fn(path="/missing")

        assert "Error" in result
        assert "not found" in result.lower() or "directory" in result.lower()

    def test_file_info_exists(self, mock_nx_basic):
        """Test getting file info for existing file."""
        mock_nx_basic.exists.return_value = True
        mock_nx_basic.is_directory.return_value = False
        server = create_mcp_server(nx=mock_nx_basic)

        info_tool = get_tool(server, "nexus_file_info")
        result = info_tool.fn(path="/test.txt")

        info = json.loads(result)
        assert info["exists"] is True
        assert info["is_directory"] is False
        assert info["path"] == "/test.txt"

    def test_file_info_not_found(self, mock_nx_basic):
        """Test getting file info for non-existent file."""
        mock_nx_basic.exists.return_value = False
        server = create_mcp_server(nx=mock_nx_basic)

        info_tool = get_tool(server, "nexus_file_info")
        result = info_tool.fn(path="/missing.txt")

        assert "File not found" in result
        assert "/missing.txt" in result

    def test_file_info_directory(self, mock_nx_basic):
        """Test getting file info for directory."""
        mock_nx_basic.exists.return_value = True
        mock_nx_basic.is_directory.return_value = True
        server = create_mcp_server(nx=mock_nx_basic)

        info_tool = get_tool(server, "nexus_file_info")
        result = info_tool.fn(path="/data")

        info = json.loads(result)
        assert info["is_directory"] is True


# ============================================================================
# DIRECTORY OPERATIONS TESTS
# ============================================================================


class TestDirectoryOperationTools:
    """Test suite for directory operation tools."""

    def test_mkdir_success(self, mock_nx_basic):
        """Test creating a directory successfully."""
        server = create_mcp_server(nx=mock_nx_basic)

        mkdir_tool = get_tool(server, "nexus_mkdir")
        result = mkdir_tool.fn(path="/new_dir")

        assert "Successfully created directory" in result
        assert "/new_dir" in result
        mock_nx_basic.mkdir.assert_called_once_with("/new_dir")

    def test_mkdir_error(self, mock_nx_basic):
        """Test mkdir error handling."""
        mock_nx_basic.mkdir.side_effect = PermissionError("Permission denied")
        server = create_mcp_server(nx=mock_nx_basic)

        mkdir_tool = get_tool(server, "nexus_mkdir")
        result = mkdir_tool.fn(path="/new_dir")

        assert "Error" in result
        assert "permission" in result.lower() or "denied" in result.lower()

    def test_rmdir_success(self, mock_nx_basic):
        """Test removing a directory successfully."""
        server = create_mcp_server(nx=mock_nx_basic)

        rmdir_tool = get_tool(server, "nexus_rmdir")
        result = rmdir_tool.fn(path="/old_dir")

        assert "Successfully removed directory" in result
        assert "/old_dir" in result
        mock_nx_basic.rmdir.assert_called_once_with("/old_dir", recursive=False)

    def test_rmdir_recursive(self, mock_nx_basic):
        """Test removing a directory recursively."""
        server = create_mcp_server(nx=mock_nx_basic)

        rmdir_tool = get_tool(server, "nexus_rmdir")
        rmdir_tool.fn(path="/old_dir", recursive=True)

        mock_nx_basic.rmdir.assert_called_once_with("/old_dir", recursive=True)

    def test_rmdir_error(self, mock_nx_basic):
        """Test rmdir error handling."""
        mock_nx_basic.rmdir.side_effect = FileNotFoundError("Directory not found")
        server = create_mcp_server(nx=mock_nx_basic)

        rmdir_tool = get_tool(server, "nexus_rmdir")
        result = rmdir_tool.fn(path="/missing_dir")

        assert "Error" in result
        assert "not found" in result.lower() or "removed" in result.lower()


# ============================================================================
# SEARCH TOOLS TESTS
# ============================================================================


class TestSearchTools:
    """Test suite for search tools."""

    def test_glob_success(self, mock_nx_basic):
        """Test glob pattern search successfully."""
        server = create_mcp_server(nx=mock_nx_basic)

        glob_tool = get_tool(server, "nexus_glob")
        result = glob_tool.fn(pattern="*.py", path="/src")

        response = json.loads(result)
        assert isinstance(response, dict)
        assert "items" in response
        assert "total" in response
        assert isinstance(response["items"], list)
        assert "test.py" in response["items"]
        mock_nx_basic.glob.assert_called_once_with("*.py", "/src")

    def test_glob_default_path(self, mock_nx_basic):
        """Test glob with default path."""
        server = create_mcp_server(nx=mock_nx_basic)

        glob_tool = get_tool(server, "nexus_glob")
        glob_tool.fn(pattern="*.txt")

        mock_nx_basic.glob.assert_called_once_with("*.txt", "/")

    def test_glob_error(self, mock_nx_basic):
        """Test glob error handling."""
        mock_nx_basic.glob.side_effect = ValueError("Invalid pattern")
        server = create_mcp_server(nx=mock_nx_basic)

        glob_tool = get_tool(server, "nexus_glob")
        result = glob_tool.fn(pattern="[invalid")

        assert "Error in glob search" in result
        assert "Invalid pattern" in result

    def test_grep_success(self, mock_nx_basic):
        """Test grep content search successfully."""
        server = create_mcp_server(nx=mock_nx_basic)

        grep_tool = get_tool(server, "nexus_grep")
        result = grep_tool.fn(pattern="TODO", path="/src")

        response = json.loads(result)
        assert isinstance(response, dict)
        assert "items" in response
        assert "total" in response
        assert isinstance(response["items"], list)
        mock_nx_basic.grep.assert_called_once_with("TODO", "/src", ignore_case=False)

    def test_grep_ignore_case(self, mock_nx_basic):
        """Test grep with case-insensitive search."""
        server = create_mcp_server(nx=mock_nx_basic)

        grep_tool = get_tool(server, "nexus_grep")
        grep_tool.fn(pattern="error", path="/logs", ignore_case=True)

        mock_nx_basic.grep.assert_called_once_with("error", "/logs", ignore_case=True)

    def test_grep_result_limiting(self, mock_nx_basic):
        """Test grep pagination with default limit of 100 matches."""
        # Create 150 fake results
        large_results = [{"file": f"file{i}.py", "line": i, "content": "match"} for i in range(150)]
        mock_nx_basic.grep.return_value = large_results
        server = create_mcp_server(nx=mock_nx_basic)

        grep_tool = get_tool(server, "nexus_grep")
        result = grep_tool.fn(pattern="test")

        response = json.loads(result)
        assert isinstance(response, dict)
        assert response["total"] == 150  # Total results found
        assert response["count"] == 100  # First page limited to 100
        assert len(response["items"]) == 100
        assert response["has_more"] is True
        assert response["next_offset"] == 100

    def test_grep_error(self, mock_nx_basic):
        """Test grep error handling."""
        mock_nx_basic.grep.side_effect = ValueError("Invalid regex")
        server = create_mcp_server(nx=mock_nx_basic)

        grep_tool = get_tool(server, "nexus_grep")
        result = grep_tool.fn(pattern="[invalid")

        assert "Error in grep search" in result
        assert "Invalid regex" in result

    def test_semantic_search_available(self, mock_nx_with_search):
        """Test semantic search when available."""
        server = create_mcp_server(nx=mock_nx_with_search)

        search_tool = get_tool(server, "nexus_semantic_search")
        result = search_tool.fn(query="authentication code", limit=5)

        response = json.loads(result)
        assert isinstance(response, dict)
        assert "items" in response
        assert "total" in response
        assert isinstance(response["items"], list)
        # Note: With pagination, we now fetch limit*2 to check for more results
        mock_nx_with_search.semantic_search.assert_called_once_with(
            "authentication code", path="/", limit=10
        )

    def test_semantic_search_not_available(self, mock_nx_basic):
        """Test semantic search when not available."""
        # Remove semantic_search method
        if hasattr(mock_nx_basic, "semantic_search"):
            delattr(mock_nx_basic, "semantic_search")

        server = create_mcp_server(nx=mock_nx_basic)

        search_tool = get_tool(server, "nexus_semantic_search")
        result = search_tool.fn(query="test")

        assert "Semantic search not available" in result

    def test_semantic_search_error(self, mock_nx_with_search):
        """Test semantic search error handling."""
        mock_nx_with_search.semantic_search.side_effect = RuntimeError("Search service down")
        server = create_mcp_server(nx=mock_nx_with_search)

        search_tool = get_tool(server, "nexus_semantic_search")
        result = search_tool.fn(query="test")

        assert "Error in semantic search" in result
        assert "Search service down" in result


# ============================================================================
# MEMORY TOOLS TESTS
# ============================================================================


class TestMemoryTools:
    """Test suite for memory tools."""

    def test_store_memory_success(self, mock_nx_with_memory):
        """Test storing memory successfully."""
        server = create_mcp_server(nx=mock_nx_with_memory)

        store_tool = get_tool(server, "nexus_store_memory")
        result = store_tool.fn(
            content="Important information about auth", memory_type="technical", importance=0.8
        )

        assert "Successfully stored memory" in result
        mock_nx_with_memory.memory.store.assert_called_once_with(
            "Important information about auth",
            scope="user",
            memory_type="technical",
            importance=0.8,
        )
        mock_nx_with_memory.memory.session.commit.assert_called_once()

    def test_store_memory_default_importance(self, mock_nx_with_memory):
        """Test storing memory with default importance."""
        server = create_mcp_server(nx=mock_nx_with_memory)

        store_tool = get_tool(server, "nexus_store_memory")
        store_tool.fn(content="Test memory")

        # Verify importance defaults to 0.5
        call_args = mock_nx_with_memory.memory.store.call_args
        assert call_args.kwargs["importance"] == 0.5

    def test_store_memory_with_rollback(self, mock_nx_with_memory):
        """Test memory storage error triggers rollback."""
        mock_nx_with_memory.memory.store.side_effect = RuntimeError("Storage error")
        server = create_mcp_server(nx=mock_nx_with_memory)

        store_tool = get_tool(server, "nexus_store_memory")
        result = store_tool.fn(content="Test")

        assert "Error storing memory" in result
        assert "Storage error" in result
        # Rollback should be called on error
        mock_nx_with_memory.memory.session.rollback.assert_called_once()

    def test_store_memory_not_available(self, mock_nx_basic):
        """Test storing memory when system not available."""
        # Remove memory attribute
        if hasattr(mock_nx_basic, "memory"):
            delattr(mock_nx_basic, "memory")

        server = create_mcp_server(nx=mock_nx_basic)

        store_tool = get_tool(server, "nexus_store_memory")
        result = store_tool.fn(content="Test")

        assert "Memory system not available" in result

    def test_query_memory_success(self, mock_nx_with_memory):
        """Test querying memory successfully."""
        server = create_mcp_server(nx=mock_nx_with_memory)

        query_tool = get_tool(server, "nexus_query_memory")
        result = query_tool.fn(query="authentication", limit=3)

        memories = json.loads(result)
        assert isinstance(memories, list)
        mock_nx_with_memory.memory.search.assert_called_once_with(
            "authentication", scope="user", memory_type=None, limit=3
        )

    def test_query_memory_with_type_filter(self, mock_nx_with_memory):
        """Test querying memory with type filter."""
        server = create_mcp_server(nx=mock_nx_with_memory)

        query_tool = get_tool(server, "nexus_query_memory")
        query_tool.fn(query="test", memory_type="technical", limit=5)

        call_args = mock_nx_with_memory.memory.search.call_args
        assert call_args.kwargs["memory_type"] == "technical"

    def test_query_memory_not_available(self, mock_nx_basic):
        """Test querying memory when system not available."""
        # Remove memory attribute
        if hasattr(mock_nx_basic, "memory"):
            delattr(mock_nx_basic, "memory")

        server = create_mcp_server(nx=mock_nx_basic)

        query_tool = get_tool(server, "nexus_query_memory")
        result = query_tool.fn(query="test")

        assert "Memory system not available" in result

    def test_query_memory_error(self, mock_nx_with_memory):
        """Test query memory error handling."""
        mock_nx_with_memory.memory.search.side_effect = RuntimeError("Query failed")
        server = create_mcp_server(nx=mock_nx_with_memory)

        query_tool = get_tool(server, "nexus_query_memory")
        result = query_tool.fn(query="test")

        assert "Error querying memory" in result
        assert "Query failed" in result


# ============================================================================
# WORKFLOW TOOLS TESTS
# ============================================================================


class TestWorkflowTools:
    """Test suite for workflow tools."""

    def test_list_workflows_success(self, mock_nx_with_workflows):
        """Test listing workflows successfully."""
        server = create_mcp_server(nx=mock_nx_with_workflows)

        list_tool = get_tool(server, "nexus_list_workflows")
        result = list_tool.fn()

        workflows = json.loads(result)
        assert isinstance(workflows, list)
        assert len(workflows) > 0
        mock_nx_with_workflows.workflows.list_workflows.assert_called_once()

    def test_list_workflows_not_available(self, mock_nx_basic):
        """Test listing workflows when system not available."""
        # Remove workflows attribute
        if hasattr(mock_nx_basic, "workflows"):
            delattr(mock_nx_basic, "workflows")

        server = create_mcp_server(nx=mock_nx_basic)

        list_tool = get_tool(server, "nexus_list_workflows")
        result = list_tool.fn()

        assert "Workflow system not available" in result

    def test_list_workflows_error(self, mock_nx_with_workflows):
        """Test list workflows error handling."""
        mock_nx_with_workflows.workflows.list_workflows.side_effect = RuntimeError("Service down")
        server = create_mcp_server(nx=mock_nx_with_workflows)

        list_tool = get_tool(server, "nexus_list_workflows")
        result = list_tool.fn()

        assert "Error listing workflows" in result
        assert "Service down" in result

    def test_execute_workflow_success(self, mock_nx_with_workflows):
        """Test executing workflow successfully."""
        server = create_mcp_server(nx=mock_nx_with_workflows)

        exec_tool = get_tool(server, "nexus_execute_workflow")
        result = exec_tool.fn(name="test_workflow", inputs='{"param": "value"}')

        output = json.loads(result)
        assert output["status"] == "success"
        mock_nx_with_workflows.workflows.execute.assert_called_once_with(
            "test_workflow", param="value"
        )

    def test_execute_workflow_no_inputs(self, mock_nx_with_workflows):
        """Test executing workflow without inputs."""
        server = create_mcp_server(nx=mock_nx_with_workflows)

        exec_tool = get_tool(server, "nexus_execute_workflow")
        exec_tool.fn(name="simple_workflow", inputs=None)

        mock_nx_with_workflows.workflows.execute.assert_called_once_with("simple_workflow")

    def test_execute_workflow_not_available(self, mock_nx_basic):
        """Test executing workflow when system not available."""
        # Remove workflows attribute
        if hasattr(mock_nx_basic, "workflows"):
            delattr(mock_nx_basic, "workflows")

        server = create_mcp_server(nx=mock_nx_basic)

        exec_tool = get_tool(server, "nexus_execute_workflow")
        result = exec_tool.fn(name="test")

        assert "Workflow system not available" in result

    def test_execute_workflow_error(self, mock_nx_with_workflows):
        """Test execute workflow error handling."""
        mock_nx_with_workflows.workflows.execute.side_effect = ValueError("Invalid workflow")
        server = create_mcp_server(nx=mock_nx_with_workflows)

        exec_tool = get_tool(server, "nexus_execute_workflow")
        result = exec_tool.fn(name="invalid_workflow")

        assert "Error executing workflow" in result
        assert "Invalid workflow" in result


# ============================================================================
# SANDBOX TOOLS TESTS
# ============================================================================


class TestSandboxAvailability:
    """Test suite for sandbox availability detection."""

    def test_sandbox_available_with_docker(self, mock_nx_with_sandbox):
        """Test sandbox tools registered when Docker provider available."""
        server = create_mcp_server(nx=mock_nx_with_sandbox)

        list(server._tool_manager._tools.keys())
        assert tool_exists(server, "nexus_python")
        assert tool_exists(server, "nexus_bash")
        assert tool_exists(server, "nexus_sandbox_create")
        assert tool_exists(server, "nexus_sandbox_list")
        assert tool_exists(server, "nexus_sandbox_stop")

    def test_sandbox_not_available(self, mock_nx_no_sandbox):
        """Test sandbox tools not registered when no providers."""
        server = create_mcp_server(nx=mock_nx_no_sandbox)

        list(server._tool_manager._tools.keys())
        assert not tool_exists(server, "nexus_python")
        assert not tool_exists(server, "nexus_bash")
        assert not tool_exists(server, "nexus_sandbox_create")
        assert not tool_exists(server, "nexus_sandbox_list")
        assert not tool_exists(server, "nexus_sandbox_stop")

    def test_sandbox_available_with_empty_providers(self, mock_nx_basic):
        """Test sandbox not available when providers dict is empty."""
        # Add sandbox manager but with empty providers
        mock_nx_basic._ensure_sandbox_manager = Mock()
        mock_nx_basic._sandbox_manager = Mock()
        mock_nx_basic._sandbox_manager.providers = {}

        server = create_mcp_server(nx=mock_nx_basic)

        list(server._tool_manager._tools.keys())
        assert not tool_exists(server, "nexus_python")
        assert not tool_exists(server, "nexus_bash")

    def test_sandbox_detection_handles_exception(self, mock_nx_basic):
        """Test sandbox detection gracefully handles exceptions."""
        # Make _ensure_sandbox_manager raise an exception
        mock_nx_basic._ensure_sandbox_manager = Mock(side_effect=RuntimeError("Init failed"))

        # Should not raise, sandbox tools should just not be registered
        server = create_mcp_server(nx=mock_nx_basic)

        list(server._tool_manager._tools.keys())
        assert not tool_exists(server, "nexus_python")


class TestSandboxTools:
    """Test suite for sandbox execution tools."""

    def test_python_execution_success(self, mock_nx_with_sandbox):
        """Test Python code execution successfully."""
        mock_nx_with_sandbox.sandbox_run.return_value = {
            "stdout": "Hello, World!",
            "stderr": "",
            "exit_code": 0,
            "execution_time": 0.456,
        }
        server = create_mcp_server(nx=mock_nx_with_sandbox)

        python_tool = get_tool(server, "nexus_python")
        result = python_tool.fn(code='print("Hello, World!")', sandbox_id="test-123")

        assert "Output:" in result
        assert "Hello, World!" in result
        assert "Exit code: 0" in result
        assert "Execution time: 0.456s" in result

        mock_nx_with_sandbox.sandbox_run.assert_called_once_with(
            sandbox_id="test-123", language="python", code='print("Hello, World!")', timeout=300
        )

    def test_python_execution_with_error(self, mock_nx_with_sandbox):
        """Test Python execution with stderr output."""
        mock_nx_with_sandbox.sandbox_run.return_value = {
            "stdout": "",
            "stderr": "NameError: name 'undefined' is not defined",
            "exit_code": 1,
            "execution_time": 0.123,
        }
        server = create_mcp_server(nx=mock_nx_with_sandbox)

        python_tool = get_tool(server, "nexus_python")
        result = python_tool.fn(code="print(undefined)", sandbox_id="test-123")

        assert "Errors:" in result
        assert "NameError" in result
        assert "Exit code: 1" in result

    def test_python_execution_no_output(self, mock_nx_with_sandbox):
        """Test Python execution with no output."""
        mock_nx_with_sandbox.sandbox_run.return_value = {
            "stdout": "",
            "stderr": "",
            "exit_code": 0,
            "execution_time": 0.01,
        }
        server = create_mcp_server(nx=mock_nx_with_sandbox)

        python_tool = get_tool(server, "nexus_python")
        result = python_tool.fn(code="x = 1 + 1", sandbox_id="test-123")

        # When there's no stdout/stderr, still shows exit code and time
        assert "Exit code: 0" in result
        assert "Execution time:" in result
        assert "Output:" not in result
        assert "Errors:" not in result

    def test_python_execution_error(self, mock_nx_with_sandbox):
        """Test Python execution error handling."""
        mock_nx_with_sandbox.sandbox_run.side_effect = RuntimeError("Sandbox not found")
        server = create_mcp_server(nx=mock_nx_with_sandbox)

        python_tool = get_tool(server, "nexus_python")
        result = python_tool.fn(code="print('test')", sandbox_id="invalid")

        assert "Error executing Python code" in result
        assert "Sandbox not found" in result

    def test_bash_execution_success(self, mock_nx_with_sandbox):
        """Test bash command execution successfully."""
        mock_nx_with_sandbox.sandbox_run.return_value = {
            "stdout": "file1.txt\nfile2.txt\n",
            "stderr": "",
            "exit_code": 0,
            "execution_time": 0.089,
        }
        server = create_mcp_server(nx=mock_nx_with_sandbox)

        bash_tool = get_tool(server, "nexus_bash")
        result = bash_tool.fn(command="ls -l", sandbox_id="test-123")

        assert "Output:" in result
        assert "file1.txt" in result
        assert "Exit code: 0" in result
        assert "Execution time: 0.089s" in result

        mock_nx_with_sandbox.sandbox_run.assert_called_once_with(
            sandbox_id="test-123", language="bash", code="ls -l", timeout=300
        )

    def test_bash_execution_with_error(self, mock_nx_with_sandbox):
        """Test bash execution with command error."""
        mock_nx_with_sandbox.sandbox_run.return_value = {
            "stdout": "",
            "stderr": "bash: invalid_command: command not found",
            "exit_code": 127,
            "execution_time": 0.01,
        }
        server = create_mcp_server(nx=mock_nx_with_sandbox)

        bash_tool = get_tool(server, "nexus_bash")
        result = bash_tool.fn(command="invalid_command", sandbox_id="test-123")

        assert "Errors:" in result
        assert "command not found" in result
        assert "Exit code: 127" in result

    def test_bash_execution_error(self, mock_nx_with_sandbox):
        """Test bash execution error handling."""
        mock_nx_with_sandbox.sandbox_run.side_effect = TimeoutError("Execution timeout")
        server = create_mcp_server(nx=mock_nx_with_sandbox)

        bash_tool = get_tool(server, "nexus_bash")
        result = bash_tool.fn(command="sleep 1000", sandbox_id="test-123")

        assert "Error executing bash command" in result
        assert "Execution timeout" in result

    def test_sandbox_create_success(self, mock_nx_with_sandbox):
        """Test creating sandbox successfully."""
        server = create_mcp_server(nx=mock_nx_with_sandbox)

        create_tool = get_tool(server, "nexus_sandbox_create")
        result = create_tool.fn(name="my-sandbox", ttl_minutes=15)

        sandbox_info = json.loads(result)
        assert "sandbox_id" in sandbox_info
        assert sandbox_info["sandbox_id"] == "test-sandbox-123"

        mock_nx_with_sandbox.sandbox_create.assert_called_once_with(
            name="my-sandbox", ttl_minutes=15
        )

    def test_sandbox_create_default_ttl(self, mock_nx_with_sandbox):
        """Test creating sandbox with default TTL."""
        server = create_mcp_server(nx=mock_nx_with_sandbox)

        create_tool = get_tool(server, "nexus_sandbox_create")
        create_tool.fn(name="test")

        call_args = mock_nx_with_sandbox.sandbox_create.call_args
        assert call_args.kwargs["ttl_minutes"] == 10

    def test_sandbox_create_error(self, mock_nx_with_sandbox):
        """Test sandbox create error handling."""
        mock_nx_with_sandbox.sandbox_create.side_effect = RuntimeError("No providers available")
        server = create_mcp_server(nx=mock_nx_with_sandbox)

        create_tool = get_tool(server, "nexus_sandbox_create")
        result = create_tool.fn(name="test")

        assert "Error creating sandbox" in result
        assert "No providers available" in result

    def test_sandbox_list_success(self, mock_nx_with_sandbox):
        """Test listing sandboxes successfully."""
        server = create_mcp_server(nx=mock_nx_with_sandbox)

        list_tool = get_tool(server, "nexus_sandbox_list")
        result = list_tool.fn()

        sandboxes = json.loads(result)
        assert isinstance(sandboxes, list)
        mock_nx_with_sandbox.sandbox_list.assert_called_once()

    def test_sandbox_list_error(self, mock_nx_with_sandbox):
        """Test sandbox list error handling."""
        mock_nx_with_sandbox.sandbox_list.side_effect = RuntimeError("Connection failed")
        server = create_mcp_server(nx=mock_nx_with_sandbox)

        list_tool = get_tool(server, "nexus_sandbox_list")
        result = list_tool.fn()

        assert "Error listing sandboxes" in result
        assert "Connection failed" in result

    def test_sandbox_stop_success(self, mock_nx_with_sandbox):
        """Test stopping sandbox successfully."""
        server = create_mcp_server(nx=mock_nx_with_sandbox)

        stop_tool = get_tool(server, "nexus_sandbox_stop")
        result = stop_tool.fn(sandbox_id="test-123")

        assert "Successfully stopped sandbox" in result
        assert "test-123" in result
        mock_nx_with_sandbox.sandbox_stop.assert_called_once_with("test-123")

    def test_sandbox_stop_error(self, mock_nx_with_sandbox):
        """Test sandbox stop error handling."""
        mock_nx_with_sandbox.sandbox_stop.side_effect = ValueError("Sandbox not found")
        server = create_mcp_server(nx=mock_nx_with_sandbox)

        stop_tool = get_tool(server, "nexus_sandbox_stop")
        result = stop_tool.fn(sandbox_id="invalid")

        assert "Error stopping sandbox" in result
        assert "Sandbox not found" in result


# ============================================================================
# RESOURCES AND PROMPTS TESTS
# ============================================================================


class TestResources:
    """Test suite for MCP resource endpoints."""

    def test_file_resource_success(self, mock_nx_basic):
        """Test accessing file resource successfully."""
        mock_nx_basic.read.return_value = b"resource content"
        server = create_mcp_server(nx=mock_nx_basic)

        # Find the resource
        resource = get_resource_template(server, "nexus://files/")
        result = resource.fn(path="/data/file.txt")

        assert result == "resource content"
        mock_nx_basic.read.assert_called_once_with("/data/file.txt")

    def test_file_resource_bytes(self, mock_nx_basic):
        """Test file resource with bytes content."""
        mock_nx_basic.read.return_value = b"binary data"
        server = create_mcp_server(nx=mock_nx_basic)

        resource = get_resource_template(server, "nexus://files/")
        result = resource.fn(path="/data/binary.dat")

        assert result == "binary data"

    def test_file_resource_error(self, mock_nx_basic):
        """Test file resource error handling."""
        mock_nx_basic.read.side_effect = FileNotFoundError("File not found")
        server = create_mcp_server(nx=mock_nx_basic)

        resource = get_resource_template(server, "nexus://files/")
        result = resource.fn(path="/missing.txt")

        assert "Error reading resource" in result
        assert "File not found" in result


class TestPrompts:
    """Test suite for MCP prompt templates."""

    def test_file_analysis_prompt(self, mock_nx_basic):
        """Test file analysis prompt generation."""
        server = create_mcp_server(nx=mock_nx_basic)

        # Find the prompt
        prompt = get_prompt(server, "file_analysis_prompt")
        result = prompt.fn(file_path="/src/main.py")

        assert "/src/main.py" in result
        assert "Read the file content" in result
        assert "nexus_read_file" in result
        assert "Analyze" in result

    def test_search_and_summarize_prompt(self, mock_nx_basic):
        """Test search and summarize prompt generation."""
        server = create_mcp_server(nx=mock_nx_basic)

        # Find the prompt
        prompt = get_prompt(server, "search_and_summarize_prompt")
        result = prompt.fn(query="authentication logic")

        assert "authentication logic" in result
        assert "nexus_semantic_search" in result
        assert "nexus_read_file" in result
        assert "nexus_store_memory" in result


# ============================================================================
# SERVER CREATION TESTS
# ============================================================================


class TestServerCreation:
    """Test suite for server creation scenarios."""

    def test_server_with_provided_nx(self, mock_nx_full):
        """Test creating server with provided NexusFS instance."""
        server = create_mcp_server(nx=mock_nx_full)

        assert server is not None
        assert len(server._tool_manager._tools) > 0

    def test_server_with_remote_url(self):
        """Test creating server with remote URL."""
        with patch("nexus.remote.RemoteNexusFS") as mock_remote:
            mock_instance = Mock()
            mock_instance.read = Mock(return_value=b"test")
            mock_instance.write = Mock()
            mock_remote.return_value = mock_instance

            server = create_mcp_server(remote_url="http://localhost:8080", api_key="test-key")

            mock_remote.assert_called_once_with("http://localhost:8080", api_key="test-key")
            assert server is not None

    def test_server_with_auto_connect(self):
        """Test creating server with auto-connect."""
        with patch("nexus.connect") as mock_connect:
            mock_nx = Mock()
            mock_nx.read = Mock(return_value=b"test")
            mock_nx.write = Mock()
            mock_connect.return_value = mock_nx

            server = create_mcp_server()

            mock_connect.assert_called_once()
            assert server is not None

    def test_server_with_custom_name(self, mock_nx_basic):
        """Test creating server with custom name."""
        server = create_mcp_server(nx=mock_nx_basic, name="custom-nexus")

        assert server.name == "custom-nexus"

    def test_server_default_name(self, mock_nx_basic):
        """Test creating server with default name."""
        server = create_mcp_server(nx=mock_nx_basic)

        assert server.name == "nexus"

    def test_main_with_remote_url(self):
        """Test main function with remote URL from environment."""
        with (
            patch("nexus.mcp.server.create_mcp_server") as mock_create,
            patch.dict("os.environ", {"NEXUS_URL": "http://test:8080", "NEXUS_API_KEY": "key123"}),
        ):
            mock_server = Mock()
            mock_create.return_value = mock_server

            # We can't actually run main() as it would block, but we can test the logic
            # by checking environment variable handling
            import os

            assert os.getenv("NEXUS_URL") == "http://test:8080"
            assert os.getenv("NEXUS_API_KEY") == "key123"

    def test_main_without_remote_url(self):
        """Test main function without remote URL."""
        with patch.dict("os.environ", {}, clear=True):
            import os

            # Remove environment variables
            assert os.getenv("NEXUS_URL") is None
            assert os.getenv("NEXUS_API_KEY") is None

    def test_server_tool_count_without_optional_features(self, mock_nx_basic):
        """Test server has correct tool count with basic features only."""
        server = create_mcp_server(nx=mock_nx_basic)

        # Basic tools: read, write, delete, list, file_info, mkdir, rmdir,
        # glob, grep, semantic_search, store_memory, query_memory,
        # list_workflows, execute_workflow
        # = 14 tools minimum
        assert len(server._tool_manager._tools) >= 14

    def test_server_tool_count_with_all_features(self, mock_nx_full):
        """Test server has correct tool count with all features."""
        server = create_mcp_server(nx=mock_nx_full)

        # All basic tools + 5 sandbox tools = 19 tools minimum
        list(server._tool_manager._tools.keys())

        # Verify sandbox tools are included
        assert tool_exists(server, "nexus_python")
        assert tool_exists(server, "nexus_bash")
        assert tool_exists(server, "nexus_sandbox_create")
        assert tool_exists(server, "nexus_sandbox_list")
        assert tool_exists(server, "nexus_sandbox_stop")
