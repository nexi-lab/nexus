"""Comprehensive tests for MCP server tools and functionality.

This test suite covers all tools, resources, prompts, and server creation scenarios
for the Nexus MCP server implementation.
"""

import json
from unittest.mock import ANY, AsyncMock, Mock, patch

import pytest

from nexus.bricks.mcp.server import create_mcp_server

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================


def _components_of(server, prefix: str) -> dict[str, object]:
    """Return all components of ``prefix`` type (``tool`` / ``prompt`` /
    ``resource`` / ``template``) keyed by their public name.

    Shim that works for both FastMCP 2.x and 3.x so individual test
    helpers don't have to repeat the version split.
    """
    # FastMCP 3.x — single component registry keyed by "<type>:<name>@..."
    if hasattr(server, "_local_provider"):
        lp = server._local_provider
        return {v.name: v for k, v in lp._components.items() if k.startswith(f"{prefix}:")}
    # FastMCP 2.x — per-type managers. Only the attribute mappings we
    # still exercise are listed here; anything else raises.
    attr = {
        "tool": "_tool_manager",
        "prompt": "_prompt_manager",
        "resource": "_resource_manager",
        "template": "_resource_manager",
    }[prefix]
    mgr = getattr(server, attr)
    if prefix == "tool":
        return dict(mgr._tools)
    if prefix == "prompt":
        return dict(mgr._prompts)
    if prefix == "resource":
        return dict(getattr(mgr, "_resources", {}))
    if prefix == "template":
        return dict(getattr(mgr, "_templates", {}))
    raise ValueError(f"unknown component prefix: {prefix}")


def get_tool(server, tool_name: str):
    """Helper to get a tool from the MCP server (FastMCP 2.x and 3.x compat)."""
    return _components_of(server, "tool")[tool_name]


def get_prompt(server, prompt_name: str):
    """Helper to get a prompt from the MCP server (FastMCP 2.x and 3.x compat)."""
    return _components_of(server, "prompt")[prompt_name]


def get_resource_template(server, uri_pattern: str):
    """Helper to get a resource template from the MCP server (FastMCP 2.x+3.x).

    FastMCP 3.x separates static resources from URI-pattern templates
    into two component buckets. We search the templates bucket first
    (matching by URI pattern) and fall back to resources for the
    static case so older tests keep working.
    """
    for prefix in ("template", "resource"):
        for key, template in _components_of(server, prefix).items():
            # In 3.x the component key is the component's .name (so we
            # match on the registered URI via the template's
            # ``uri_template`` or ``uri`` attribute). Fall back to
            # substring matching the key itself so pre-3.x templates
            # keyed by URI string still match.
            candidate_uri = (
                getattr(template, "uri_template", None) or getattr(template, "uri", None) or key
            )
            if uri_pattern in str(candidate_uri):
                return template
    raise KeyError(f"Resource template with pattern '{uri_pattern}' not found")


def tool_exists(server, tool_name: str) -> bool:
    """Check if a tool exists in the server (FastMCP 2.x and 3.x compat)."""
    return tool_name in _components_of(server, "tool")


# ============================================================================
# FIXTURES
# ============================================================================


@pytest.fixture
def mock_nx_basic():
    """Create a basic mock NexusFS with file operations."""
    nx = Mock()
    nx.sys_read = Mock(return_value=b"test content")
    nx.sys_write = Mock()
    nx.write = Mock()
    nx.sys_unlink = Mock()
    nx.sys_readdir = Mock(return_value=["/file1.txt", "/file2.txt"])
    _mock_search = Mock()
    _mock_search.glob = Mock(return_value=["test.py", "main.py"])
    _mock_search.grep = AsyncMock(
        return_value=[{"file": "test.py", "line": 10, "content": "match"}]
    )
    _service_map = {"search": _mock_search}
    nx.service = Mock(side_effect=lambda name: _service_map.get(name))
    nx._mock_search = _mock_search  # internal alias for assertion access
    nx.access = Mock(return_value=True)
    nx.is_directory = Mock(return_value=False)
    nx.mkdir = Mock()
    nx.rmdir = Mock()
    nx.edit = Mock(
        return_value={
            "success": True,
            "diff": "--- a/file.txt\n+++ b/file.txt\n@@ -1 +1 @@\n-old\n+new",
            "applied_count": 1,
            "matches": [{"edit_index": 0, "match_type": "exact", "similarity": 1.0}],
            "errors": [],
        }
    )
    return nx


@pytest.fixture
def mock_nx_with_workflows():
    """Create mock NexusFS with workflow system."""
    nx = Mock()
    nx.sys_read = Mock(return_value=b"test")
    nx.sys_write = Mock()

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
    nx = Mock()
    nx.sys_read = Mock(return_value=b"test")
    nx.sys_write = Mock()

    # Add async semantic_search method
    async def mock_semantic_search(query, path="/", search_mode="semantic", limit=10, **kwargs):
        return [{"path": "/file1.txt", "score": 0.95, "snippet": "relevant content"}]

    nx.semantic_search = AsyncMock(side_effect=mock_semantic_search)

    return nx


@pytest.fixture
def mock_nx_with_sandbox():
    """Create mock NexusFS with sandbox support."""
    nx = Mock()
    nx.sys_read = Mock(return_value=b"test")
    nx.sys_write = Mock()

    # Add sandbox support
    nx.sandbox_available = True

    _mock_sandbox_rpc = Mock()
    _mock_sandbox_rpc.sandbox_create = Mock(
        return_value={
            "sandbox_id": "test-sandbox-123",
            "name": "test",
            "provider": "docker",
            "status": "running",
        }
    )
    _mock_sandbox_rpc.sandbox_list = Mock(
        return_value=[{"sandbox_id": "test-sandbox-123", "name": "test", "status": "running"}]
    )
    _mock_sandbox_rpc.sandbox_run = Mock(
        return_value={
            "stdout": "Hello, World!",
            "stderr": "",
            "exit_code": 0,
            "execution_time": 0.123,
        }
    )
    _mock_sandbox_rpc.sandbox_stop = Mock()
    _service_map = {"sandbox_rpc": _mock_sandbox_rpc}
    nx.service = Mock(side_effect=lambda name: _service_map.get(name))
    nx._mock_sandbox_rpc = _mock_sandbox_rpc  # internal alias for assertion access

    return nx


@pytest.fixture
def mock_nx_no_sandbox():
    """Create mock NexusFS without sandbox support."""
    nx = Mock()
    nx.sys_read = Mock(return_value=b"test")
    nx.sys_write = Mock()
    nx.sandbox_available = False

    return nx


@pytest.fixture
def mock_nx_full():
    """Create mock NexusFS with all features enabled."""
    nx = Mock()

    # Basic file operations (async syscalls)
    nx.sys_read = Mock(return_value=b"test content")
    nx.sys_write = Mock()
    nx.write = Mock()
    nx.sys_unlink = Mock()
    nx.sys_readdir = Mock(return_value=["/file1.txt"])
    nx.glob = Mock(return_value=["test.py"])
    nx.grep = Mock(return_value=[{"file": "test.py", "line": 10, "content": "match"}])
    nx.access = Mock(return_value=True)
    nx.is_directory = Mock(return_value=False)
    nx.mkdir = Mock()
    nx.rmdir = Mock()

    # Memory system via service("memory_provider") (get_memory_api() reads this)
    mock_memory = Mock()
    mock_memory.store = Mock()
    mock_memory.search = Mock(return_value=[])
    mock_memory.session = Mock()
    mock_memory.session.commit = Mock()
    mock_memory.session.rollback = Mock()
    mock_provider = Mock()
    mock_provider.get_or_create.return_value = mock_memory
    nx.memory = mock_memory  # alias for assertions

    # Workflow system
    nx.workflows = Mock()
    nx.workflows.list_workflows = Mock(return_value=[])
    nx.workflows.execute = Mock(return_value={"status": "success"})

    # Search
    nx.search = Mock(return_value=[])

    # Sandbox
    nx.sandbox_available = True
    _mock_sandbox_rpc = Mock()
    _mock_sandbox_rpc.sandbox_create = Mock(return_value={"sandbox_id": "test-123"})
    _mock_sandbox_rpc.sandbox_list = Mock(return_value=[])
    _mock_sandbox_rpc.sandbox_run = Mock(
        return_value={"stdout": "output", "stderr": "", "exit_code": 0, "execution_time": 0.1}
    )
    _mock_sandbox_rpc.sandbox_stop = Mock()

    _service_map = {"memory_provider": mock_provider, "sandbox_rpc": _mock_sandbox_rpc}
    nx.service = Mock(side_effect=lambda name: _service_map.get(name))

    return nx


# ============================================================================
# FILE OPERATIONS TESTS
# ============================================================================


class TestFileOperationTools:
    """Test suite for file operation tools."""

    async def test_read_file_success(self, mock_nx_basic):
        """Test reading a file successfully."""
        server = await create_mcp_server(nx=mock_nx_basic)

        # Access tool via helper
        read_tool = get_tool(server, "nexus_read_file")
        result = await read_tool.fn(path="/test.txt")

        assert result == "test content"
        mock_nx_basic.sys_read.assert_called_once_with("/test.txt")

    async def test_read_file_bytes_content(self, mock_nx_basic):
        """Test reading file with bytes content."""
        mock_nx_basic.sys_read.return_value = b"binary content"
        server = await create_mcp_server(nx=mock_nx_basic)

        read_tool = get_tool(server, "nexus_read_file")
        result = await read_tool.fn(path="/test.bin")

        assert result == "binary content"

    @pytest.mark.parametrize(
        "tool_name,mock_method,error_class,error_msg,call_kwargs",
        [
            (
                "nexus_read_file",
                "sys_read",
                FileNotFoundError,
                "File not found",
                {"path": "/missing.txt"},
            ),
            (
                "nexus_write_file",
                "write",
                PermissionError,
                "Permission denied",
                {"path": "/test.txt", "content": "content"},
            ),
            (
                "nexus_delete_file",
                "sys_unlink",
                FileNotFoundError,
                "File not found",
                {"path": "/missing.txt"},
            ),
            (
                "nexus_list_files",
                "sys_readdir",
                FileNotFoundError,
                "Directory not found",
                {"path": "/missing"},
            ),
        ],
    )
    async def test_tool_error_handling(
        self, mock_nx_basic, tool_name, mock_method, error_class, error_msg, call_kwargs
    ):
        """Test error handling for file operation tools."""
        getattr(mock_nx_basic, mock_method).side_effect = error_class(error_msg)
        server = await create_mcp_server(nx=mock_nx_basic)
        tool = get_tool(server, tool_name)
        result = await tool.fn(**call_kwargs)
        assert "Error" in result or "error" in result.lower()

    async def test_write_file_success(self, mock_nx_basic):
        """Test writing a file successfully."""
        server = await create_mcp_server(nx=mock_nx_basic)

        write_tool = get_tool(server, "nexus_write_file")
        result = await write_tool.fn(path="/test.txt", content="new content")

        assert "Successfully wrote" in result
        assert "/test.txt" in result
        mock_nx_basic.write.assert_called_once()

        # Verify content was encoded
        call_args = mock_nx_basic.write.call_args[0]
        assert call_args[0] == "/test.txt"
        assert call_args[1] == b"new content"

    async def test_delete_file_success(self, mock_nx_basic):
        """Test deleting a file successfully."""
        server = await create_mcp_server(nx=mock_nx_basic)

        delete_tool = get_tool(server, "nexus_delete_file")
        result = await delete_tool.fn(path="/test.txt")

        assert "Successfully deleted" in result
        assert "/test.txt" in result
        mock_nx_basic.sys_unlink.assert_called_once_with("/test.txt")

    async def test_list_files_basic(self, mock_nx_basic):
        """Test listing files in a directory."""
        server = await create_mcp_server(nx=mock_nx_basic)

        list_tool = get_tool(server, "nexus_list_files")
        result = await list_tool.fn(path="/data")

        # Result should be JSON with pagination metadata
        response = json.loads(result)
        assert isinstance(response, dict)
        assert "items" in response
        assert "total" in response
        assert "count" in response
        assert isinstance(response["items"], list)
        assert "/file1.txt" in response["items"]
        mock_nx_basic.sys_readdir.assert_called_once_with("/data", recursive=False, details=True)

    async def test_list_files_recursive(self, mock_nx_basic):
        """Test listing files recursively."""
        server = await create_mcp_server(nx=mock_nx_basic)

        list_tool = get_tool(server, "nexus_list_files")
        await list_tool.fn(path="/data", recursive=True, details=True)

        mock_nx_basic.sys_readdir.assert_called_once_with("/data", recursive=True, details=True)

    async def test_file_info_exists(self, mock_nx_basic):
        """Test getting file info for existing file."""
        mock_nx_basic.access.return_value = True
        mock_nx_basic.is_directory.return_value = False
        server = await create_mcp_server(nx=mock_nx_basic)

        info_tool = get_tool(server, "nexus_file_info")
        result = await info_tool.fn(path="/test.txt")

        info = json.loads(result)
        assert info["exists"] is True
        assert info["is_directory"] is False
        assert info["path"] == "/test.txt"

    async def test_file_info_not_found(self, mock_nx_basic):
        """Test getting file info for non-existent file."""
        mock_nx_basic.access.return_value = False
        server = await create_mcp_server(nx=mock_nx_basic)

        info_tool = get_tool(server, "nexus_file_info")
        result = await info_tool.fn(path="/missing.txt")

        assert "File not found" in result
        assert "/missing.txt" in result

    async def test_file_info_directory(self, mock_nx_basic):
        """Test getting file info for directory."""
        mock_nx_basic.access.return_value = True
        mock_nx_basic.is_directory.return_value = True
        server = await create_mcp_server(nx=mock_nx_basic)

        info_tool = get_tool(server, "nexus_file_info")
        result = await info_tool.fn(path="/data")

        info = json.loads(result)
        assert info["is_directory"] is True


# ============================================================================
# EDIT FILE TOOL TESTS
# ============================================================================


class TestEditFileTool:
    """Test suite for nexus_edit_file tool."""

    async def test_edit_file_success(self, mock_nx_basic):
        """Test successful file edit returns JSON with diff."""
        server = await create_mcp_server(nx=mock_nx_basic)

        edit_tool = get_tool(server, "nexus_edit_file")
        result = edit_tool.fn(
            path="/test.py",
            edits=[{"old_str": "old", "new_str": "new"}],
        )

        response = json.loads(result)
        assert response["success"] is True
        assert "diff" in response
        assert response["applied_count"] == 1
        mock_nx_basic.edit.assert_called_once_with(
            "/test.py",
            [{"old_str": "old", "new_str": "new"}],
            fuzzy_threshold=0.85,
            preview=False,
            if_match=None,
        )

    async def test_edit_file_failure(self, mock_nx_basic):
        """Test failed edit returns error details."""
        mock_nx_basic.edit.return_value = {
            "success": False,
            "diff": "",
            "applied_count": 0,
            "matches": [{"edit_index": 0, "match_type": "failed", "similarity": 0.5}],
            "errors": ["No match found for edit 0"],
        }
        server = await create_mcp_server(nx=mock_nx_basic)

        edit_tool = get_tool(server, "nexus_edit_file")
        result = edit_tool.fn(
            path="/test.py",
            edits=[{"old_str": "nonexistent", "new_str": "new"}],
        )

        response = json.loads(result)
        assert response["success"] is False
        assert "No match found" in response["errors"][0]

    async def test_edit_file_with_preview(self, mock_nx_basic):
        """Test preview mode passes through correctly."""
        server = await create_mcp_server(nx=mock_nx_basic)

        edit_tool = get_tool(server, "nexus_edit_file")
        edit_tool.fn(
            path="/test.py",
            edits=[{"old_str": "old", "new_str": "new"}],
            preview=True,
        )

        mock_nx_basic.edit.assert_called_once_with(
            "/test.py",
            [{"old_str": "old", "new_str": "new"}],
            fuzzy_threshold=0.85,
            preview=True,
            if_match=None,
        )

    async def test_edit_file_with_if_match(self, mock_nx_basic):
        """Test if_match etag passes through correctly."""
        server = await create_mcp_server(nx=mock_nx_basic)

        edit_tool = get_tool(server, "nexus_edit_file")
        edit_tool.fn(
            path="/test.py",
            edits=[{"old_str": "old", "new_str": "new"}],
            if_match="abc123",
        )

        mock_nx_basic.edit.assert_called_once_with(
            "/test.py",
            [{"old_str": "old", "new_str": "new"}],
            fuzzy_threshold=0.85,
            preview=False,
            if_match="abc123",
        )

    async def test_edit_file_custom_fuzzy_threshold(self, mock_nx_basic):
        """Test custom fuzzy_threshold passes through."""
        server = await create_mcp_server(nx=mock_nx_basic)

        edit_tool = get_tool(server, "nexus_edit_file")
        edit_tool.fn(
            path="/test.py",
            edits=[{"old_str": "old", "new_str": "new"}],
            fuzzy_threshold=0.7,
        )

        mock_nx_basic.edit.assert_called_once_with(
            "/test.py",
            [{"old_str": "old", "new_str": "new"}],
            fuzzy_threshold=0.7,
            preview=False,
            if_match=None,
        )

    async def test_edit_file_not_found(self, mock_nx_basic):
        """Test FileNotFoundError handling."""
        mock_nx_basic.edit.side_effect = FileNotFoundError("File not found")
        server = await create_mcp_server(nx=mock_nx_basic)

        edit_tool = get_tool(server, "nexus_edit_file")
        result = edit_tool.fn(
            path="/missing.py",
            edits=[{"old_str": "old", "new_str": "new"}],
        )

        assert "Error" in result
        assert "not found" in result.lower()
        assert "/missing.py" in result

    async def test_edit_file_permission_denied(self, mock_nx_basic):
        """Test PermissionError handling."""
        mock_nx_basic.edit.side_effect = PermissionError("Permission denied")
        server = await create_mcp_server(nx=mock_nx_basic)

        edit_tool = get_tool(server, "nexus_edit_file")
        result = edit_tool.fn(
            path="/readonly.py",
            edits=[{"old_str": "old", "new_str": "new"}],
        )

        assert "Error" in result
        assert "permission" in result.lower() or "denied" in result.lower()

    async def test_edit_file_generic_error(self, mock_nx_basic):
        """Test generic exception handling."""
        mock_nx_basic.edit.side_effect = RuntimeError("Connection refused")
        server = await create_mcp_server(nx=mock_nx_basic)

        edit_tool = get_tool(server, "nexus_edit_file")
        result = edit_tool.fn(
            path="/test.py",
            edits=[{"old_str": "old", "new_str": "new"}],
        )

        assert "Error editing file" in result
        assert "Connection refused" in result


# ============================================================================
# DIRECTORY OPERATIONS TESTS
# ============================================================================


class TestDirectoryOperationTools:
    """Test suite for directory operation tools."""

    async def test_mkdir_success(self, mock_nx_basic):
        """Test creating a directory successfully."""
        server = await create_mcp_server(nx=mock_nx_basic)

        mkdir_tool = get_tool(server, "nexus_mkdir")
        result = await mkdir_tool.fn(path="/new_dir")

        assert "Successfully created directory" in result
        assert "/new_dir" in result
        mock_nx_basic.mkdir.assert_called_once_with("/new_dir")

    @pytest.mark.parametrize(
        "tool_name,mock_method,error_class,error_msg,call_kwargs",
        [
            ("nexus_mkdir", "mkdir", PermissionError, "Permission denied", {"path": "/new_dir"}),
            (
                "nexus_rmdir",
                "rmdir",
                FileNotFoundError,
                "Directory not found",
                {"path": "/missing_dir"},
            ),
        ],
    )
    async def test_dir_tool_error_handling(
        self, mock_nx_basic, tool_name, mock_method, error_class, error_msg, call_kwargs
    ):
        """Test error handling for directory operation tools."""
        getattr(mock_nx_basic, mock_method).side_effect = error_class(error_msg)
        server = await create_mcp_server(nx=mock_nx_basic)
        tool = get_tool(server, tool_name)
        result = await tool.fn(**call_kwargs)
        assert "Error" in result or "error" in result.lower()

    async def test_rmdir_success(self, mock_nx_basic):
        """Test removing a directory successfully."""
        server = await create_mcp_server(nx=mock_nx_basic)

        rmdir_tool = get_tool(server, "nexus_rmdir")
        result = await rmdir_tool.fn(path="/old_dir")

        assert "Successfully removed directory" in result
        assert "/old_dir" in result
        mock_nx_basic.rmdir.assert_called_once_with("/old_dir", recursive=False)

    async def test_rmdir_recursive(self, mock_nx_basic):
        """Test removing a directory recursively."""
        server = await create_mcp_server(nx=mock_nx_basic)

        rmdir_tool = get_tool(server, "nexus_rmdir")
        await rmdir_tool.fn(path="/old_dir", recursive=True)

        mock_nx_basic.rmdir.assert_called_once_with("/old_dir", recursive=True)


# ============================================================================
# SEARCH TOOLS TESTS
# ============================================================================


class TestSearchTools:
    """Test suite for search tools."""

    async def test_glob_success(self, mock_nx_basic):
        """Test glob pattern search successfully."""
        server = await create_mcp_server(nx=mock_nx_basic)

        glob_tool = get_tool(server, "nexus_glob")
        result = glob_tool.fn(pattern="*.py", path="/src")

        response = json.loads(result)
        assert isinstance(response, dict)
        assert "items" in response
        assert "total" in response
        assert isinstance(response["items"], list)
        assert "test.py" in response["items"]
        # #3701: files=None forwarded by default so SearchService can
        # distinguish "no filter" from "explicit empty filter".
        # Codex review #3 finding #1: ``context=...`` is now always
        # supplied (resolved from the connection's identity) so the
        # underlying SearchService sees an explicit OperationContext
        # instead of running under an ambient default.
        mock_nx_basic._mock_search.glob.assert_called_once_with(
            "*.py", "/src", files=None, context=ANY
        )

    async def test_glob_default_path(self, mock_nx_basic):
        """Test glob with default path."""
        server = await create_mcp_server(nx=mock_nx_basic)

        glob_tool = get_tool(server, "nexus_glob")
        glob_tool.fn(pattern="*.txt")

        mock_nx_basic._mock_search.glob.assert_called_once_with(
            "*.txt", "/", files=None, context=ANY
        )

    async def test_glob_error(self, mock_nx_basic):
        """Test glob error handling."""
        mock_nx_basic._mock_search.glob.side_effect = ValueError("Invalid pattern")
        server = await create_mcp_server(nx=mock_nx_basic)

        glob_tool = get_tool(server, "nexus_glob")
        result = glob_tool.fn(pattern="[invalid")

        assert "Error" in result
        assert "Invalid pattern" in result

    async def test_grep_success(self, mock_nx_basic):
        """Test grep content search successfully.

        Asserts the #3701 Issue 14 fix: MCP grep passes ``max_results`` to
        SearchService so the underlying call can return enough matches for
        the caller's requested page rather than silently capping at 100.

        Codex review #1 finding #2: ``max_results`` is now
        ``limit + offset + 1`` (sentinel fetch) so ``has_more`` can be
        detected reliably. Codex review #3 finding #1: ``context=...``
        is now always passed so SearchService runs under an explicit
        identity.
        """
        server = await create_mcp_server(nx=mock_nx_basic)

        grep_tool = get_tool(server, "nexus_grep")
        result = await grep_tool.fn(pattern="TODO", path="/src")

        response = json.loads(result)
        assert isinstance(response, dict)
        assert "items" in response
        assert "total" in response
        assert isinstance(response["items"], list)
        # Default limit=100, offset=0 → sentinel_window = 100 + 0 + 1 = 101
        mock_nx_basic._mock_search.grep.assert_called_once_with(
            "TODO",
            "/src",
            ignore_case=False,
            max_results=101,
            files=None,
            context=ANY,
        )

    async def test_grep_ignore_case(self, mock_nx_basic):
        """Test grep with case-insensitive search."""
        server = await create_mcp_server(nx=mock_nx_basic)

        grep_tool = get_tool(server, "nexus_grep")
        await grep_tool.fn(pattern="error", path="/logs", ignore_case=True)

        mock_nx_basic._mock_search.grep.assert_called_once_with(
            "error",
            "/logs",
            ignore_case=True,
            max_results=101,  # sentinel_window = 100 + 0 + 1
            files=None,
            context=ANY,
        )

    async def test_grep_result_limiting(self, mock_nx_basic):
        """Test grep pagination with default limit of 100 matches."""
        # Create 150 fake results
        large_results = [{"file": f"file{i}.py", "line": i, "content": "match"} for i in range(150)]
        mock_nx_basic._mock_search.grep.return_value = large_results
        server = await create_mcp_server(nx=mock_nx_basic)

        grep_tool = get_tool(server, "nexus_grep")
        result = await grep_tool.fn(pattern="test")

        response = json.loads(result)
        assert isinstance(response, dict)
        assert response["total"] == 150  # Total results found
        assert response["count"] == 100  # First page limited to 100
        assert len(response["items"]) == 100
        assert response["has_more"] is True
        assert response["next_offset"] == 100

    async def test_grep_large_limit_requests_max_results_through(self, mock_nx_basic):
        """Issue #3701 #14: caller asking for limit=200 must not be capped at 100.

        The pre-fix behaviour silently truncated to 100 because MCP did
        not pass ``max_results`` to SearchService. This test locks in the
        new behaviour: MCP passes ``limit + offset + 1`` (sentinel fetch
        from Codex review #1 finding #2) so SearchService returns enough
        matches for the requested page plus a sentinel row for has_more
        detection.
        """
        large_results = [{"file": f"file{i}.py", "line": i, "content": "match"} for i in range(200)]
        mock_nx_basic._mock_search.grep.return_value = large_results
        server = await create_mcp_server(nx=mock_nx_basic)

        grep_tool = get_tool(server, "nexus_grep")
        result = await grep_tool.fn(pattern="test", limit=200)

        # sentinel_window = 200 + 0 + 1 = 201
        mock_nx_basic._mock_search.grep.assert_called_once_with(
            "test",
            "/",
            ignore_case=False,
            max_results=201,
            files=None,
            context=ANY,
        )
        response = json.loads(result)
        assert response["total"] == 200
        assert response["count"] == 200
        assert response["has_more"] is False

    async def test_grep_offset_requests_enough_matches(self, mock_nx_basic):
        """Issue #3701 #14: offset=150 with limit=50 must fetch at least 200.

        Plus Codex review #1 finding #2 sentinel: request 201 to detect
        has_more reliably.
        """
        large_results = [{"file": f"file{i}.py", "line": i, "content": "match"} for i in range(200)]
        mock_nx_basic._mock_search.grep.return_value = large_results
        server = await create_mcp_server(nx=mock_nx_basic)

        grep_tool = get_tool(server, "nexus_grep")
        result = await grep_tool.fn(pattern="test", limit=50, offset=150)

        # sentinel_window = 50 + 150 + 1 = 201
        mock_nx_basic._mock_search.grep.assert_called_once_with(
            "test",
            "/",
            ignore_case=False,
            max_results=201,
            files=None,
            context=ANY,
        )
        response = json.loads(result)
        assert response["total"] == 200
        assert response["count"] == 50
        assert response["offset"] == 150
        assert response["has_more"] is False

    async def test_grep_files_parameter_forwarded(self, mock_nx_basic):
        """Issue #3701 Issue 2A: files=[...] flows through to SearchService."""
        mock_nx_basic._mock_search.grep.return_value = []
        server = await create_mcp_server(nx=mock_nx_basic)

        grep_tool = get_tool(server, "nexus_grep")
        await grep_tool.fn(pattern="TODO", files=["/src/a.py", "/src/b.py"])

        # sentinel_window = 100 + 0 + 1 = 101
        mock_nx_basic._mock_search.grep.assert_called_once_with(
            "TODO",
            "/",
            ignore_case=False,
            max_results=101,
            files=["/src/a.py", "/src/b.py"],
            context=ANY,
        )

    async def test_glob_files_parameter_forwarded(self, mock_nx_basic):
        """Issue #3701 Issue 2A: files=[...] flows through to SearchService."""
        mock_nx_basic._mock_search.glob.return_value = []
        server = await create_mcp_server(nx=mock_nx_basic)

        glob_tool = get_tool(server, "nexus_glob")
        glob_tool.fn(pattern="*.py", files=["/src/a.py", "/src/b.py"])

        mock_nx_basic._mock_search.glob.assert_called_once_with(
            "*.py", "/", files=["/src/a.py", "/src/b.py"], context=ANY
        )

    async def test_grep_before_and_after_context_forwarded(self, mock_nx_basic):
        """#3701 follow-up: before_context/after_context flow through MCP."""
        mock_nx_basic._mock_search.grep.return_value = []
        server = await create_mcp_server(nx=mock_nx_basic)

        grep_tool = get_tool(server, "nexus_grep")
        await grep_tool.fn(pattern="TODO", before_context=3, after_context=2)

        kwargs = mock_nx_basic._mock_search.grep.call_args.kwargs
        assert kwargs["before_context"] == 3
        assert kwargs["after_context"] == 2

    async def test_grep_invert_match_forwarded(self, mock_nx_basic):
        """#3701 follow-up: invert_match flows through MCP."""
        mock_nx_basic._mock_search.grep.return_value = []
        server = await create_mcp_server(nx=mock_nx_basic)

        grep_tool = get_tool(server, "nexus_grep")
        await grep_tool.fn(pattern="TODO", invert_match=True)

        kwargs = mock_nx_basic._mock_search.grep.call_args.kwargs
        assert kwargs["invert_match"] is True

    async def test_grep_no_context_flags_omitted_from_kwargs(self, mock_nx_basic):
        """Defaults (before_context=0, after_context=0, invert_match=False)
        must NOT appear in the kwargs forwarded to SearchService — old
        servers without these fields would reject them otherwise."""
        mock_nx_basic._mock_search.grep.return_value = []
        server = await create_mcp_server(nx=mock_nx_basic)

        grep_tool = get_tool(server, "nexus_grep")
        await grep_tool.fn(pattern="TODO")

        kwargs = mock_nx_basic._mock_search.grep.call_args.kwargs
        assert "before_context" not in kwargs
        assert "after_context" not in kwargs
        assert "invert_match" not in kwargs

    async def test_grep_error(self, mock_nx_basic):
        """Test grep error handling."""
        mock_nx_basic._mock_search.grep.side_effect = ValueError("Invalid regex")
        server = await create_mcp_server(nx=mock_nx_basic)

        grep_tool = get_tool(server, "nexus_grep")
        result = await grep_tool.fn(pattern="[invalid")

        assert "Error" in result
        assert "Invalid regex" in result

    async def test_semantic_search_available(self, mock_nx_with_search):
        """Test semantic search when available."""
        server = await create_mcp_server(nx=mock_nx_with_search)

        search_tool = get_tool(server, "nexus_semantic_search")
        result = await search_tool.fn(query="authentication code", limit=5)

        response = json.loads(result)
        assert isinstance(response, dict)
        assert "items" in response
        assert "total" in response
        assert isinstance(response["items"], list)
        # Over-fetches limit*2 to allow has_more detection without a second round-trip
        mock_nx_with_search.semantic_search.assert_called_once_with(
            "authentication code", path="/", search_mode="semantic", limit=10
        )

    async def test_semantic_search_with_scoped_path(self, mock_nx_with_search):
        """Test that path parameter is forwarded to semantic_search (regression for #3702)."""
        server = await create_mcp_server(nx=mock_nx_with_search)

        search_tool = get_tool(server, "nexus_semantic_search")
        result = await search_tool.fn(query="auth", path="/workspace/src", limit=5)

        response = json.loads(result)
        assert "items" in response
        mock_nx_with_search.semantic_search.assert_called_once_with(
            "auth", path="/workspace/src", search_mode="semantic", limit=10
        )

    async def test_semantic_search_with_search_mode(self, mock_nx_with_search):
        """Test that search_mode is forwarded to semantic_search."""
        server = await create_mcp_server(nx=mock_nx_with_search)

        search_tool = get_tool(server, "nexus_semantic_search")
        result = await search_tool.fn(query="token refresh", search_mode="hybrid", limit=5)

        response = json.loads(result)
        assert "items" in response
        mock_nx_with_search.semantic_search.assert_called_once_with(
            "token refresh", path="/", search_mode="hybrid", limit=10
        )

    async def test_semantic_search_not_available(self, mock_nx_basic):
        """Test semantic search when not available."""
        # Remove semantic_search method
        if hasattr(mock_nx_basic, "semantic_search"):
            delattr(mock_nx_basic, "semantic_search")

        server = await create_mcp_server(nx=mock_nx_basic)

        search_tool = get_tool(server, "nexus_semantic_search")
        result = await search_tool.fn(query="test")

        assert "Semantic search not available" in result

    async def test_semantic_search_error(self, mock_nx_with_search):
        """Test semantic search error handling."""
        mock_nx_with_search.semantic_search.side_effect = RuntimeError("Search service down")
        server = await create_mcp_server(nx=mock_nx_with_search)

        search_tool = get_tool(server, "nexus_semantic_search")
        result = await search_tool.fn(query="test")

        assert "Error in semantic search" in result
        assert "Search service down" in result


# ============================================================================
# WORKFLOW TOOLS TESTS
# ============================================================================


class TestWorkflowTools:
    """Test suite for workflow tools."""

    async def test_list_workflows_success(self, mock_nx_with_workflows):
        """Test listing workflows successfully."""
        server = await create_mcp_server(nx=mock_nx_with_workflows)

        list_tool = get_tool(server, "nexus_list_workflows")
        result = list_tool.fn()

        workflows = json.loads(result)
        assert isinstance(workflows, list)
        assert len(workflows) > 0
        mock_nx_with_workflows.workflows.list_workflows.assert_called_once()

    async def test_list_workflows_not_available(self, mock_nx_basic):
        """Test listing workflows when system not available."""
        # Remove workflows attribute
        if hasattr(mock_nx_basic, "workflows"):
            delattr(mock_nx_basic, "workflows")

        server = await create_mcp_server(nx=mock_nx_basic)

        list_tool = get_tool(server, "nexus_list_workflows")
        result = list_tool.fn()

        assert "Workflow system not available" in result

    async def test_list_workflows_error(self, mock_nx_with_workflows):
        """Test list workflows error handling."""
        mock_nx_with_workflows.workflows.list_workflows.side_effect = RuntimeError("Service down")
        server = await create_mcp_server(nx=mock_nx_with_workflows)

        list_tool = get_tool(server, "nexus_list_workflows")
        result = list_tool.fn()

        assert "Error listing workflows" in result
        assert "Service down" in result

    async def test_execute_workflow_success(self, mock_nx_with_workflows):
        """Test executing workflow successfully."""
        server = await create_mcp_server(nx=mock_nx_with_workflows)

        exec_tool = get_tool(server, "nexus_execute_workflow")
        result = exec_tool.fn(name="test_workflow", inputs='{"param": "value"}')

        output = json.loads(result)
        assert output["status"] == "success"
        mock_nx_with_workflows.workflows.execute.assert_called_once_with(
            "test_workflow", param="value"
        )

    async def test_execute_workflow_no_inputs(self, mock_nx_with_workflows):
        """Test executing workflow without inputs."""
        server = await create_mcp_server(nx=mock_nx_with_workflows)

        exec_tool = get_tool(server, "nexus_execute_workflow")
        exec_tool.fn(name="simple_workflow", inputs=None)

        mock_nx_with_workflows.workflows.execute.assert_called_once_with("simple_workflow")

    async def test_execute_workflow_not_available(self, mock_nx_basic):
        """Test executing workflow when system not available."""
        # Remove workflows attribute
        if hasattr(mock_nx_basic, "workflows"):
            delattr(mock_nx_basic, "workflows")

        server = await create_mcp_server(nx=mock_nx_basic)

        exec_tool = get_tool(server, "nexus_execute_workflow")
        result = exec_tool.fn(name="test")

        assert "Workflow system not available" in result

    async def test_execute_workflow_error(self, mock_nx_with_workflows):
        """Test execute workflow error handling."""
        mock_nx_with_workflows.workflows.execute.side_effect = ValueError("Invalid workflow")
        server = await create_mcp_server(nx=mock_nx_with_workflows)

        exec_tool = get_tool(server, "nexus_execute_workflow")
        result = exec_tool.fn(name="invalid_workflow")

        assert "Error executing workflow" in result
        assert "Invalid workflow" in result


# ============================================================================
# SANDBOX TOOLS TESTS
# ============================================================================


class TestSandboxAvailability:
    """Test suite for sandbox availability detection."""

    async def test_sandbox_available_with_docker(self, mock_nx_with_sandbox):
        """Test sandbox tools registered when Docker provider available."""
        server = await create_mcp_server(nx=mock_nx_with_sandbox)

        assert tool_exists(server, "nexus_python")
        assert tool_exists(server, "nexus_bash")
        assert tool_exists(server, "nexus_sandbox_create")
        assert tool_exists(server, "nexus_sandbox_list")
        assert tool_exists(server, "nexus_sandbox_stop")

    async def test_sandbox_not_available(self, mock_nx_no_sandbox):
        """Test sandbox tools not registered when sandbox_available is False."""
        # Explicitly set sandbox_available to False (Mock returns truthy by default)
        mock_nx_no_sandbox.sandbox_available = False

        server = await create_mcp_server(nx=mock_nx_no_sandbox)

        assert not tool_exists(server, "nexus_python")
        assert not tool_exists(server, "nexus_bash")
        assert not tool_exists(server, "nexus_sandbox_create")
        assert not tool_exists(server, "nexus_sandbox_list")
        assert not tool_exists(server, "nexus_sandbox_stop")


class TestSandboxTools:
    """Test suite for sandbox execution tools."""

    async def test_python_execution_success(self, mock_nx_with_sandbox):
        """Test Python code execution successfully."""
        mock_nx_with_sandbox._mock_sandbox_rpc.sandbox_run.return_value = {
            "stdout": "Hello, World!",
            "stderr": "",
            "exit_code": 0,
            "execution_time": 0.456,
        }
        server = await create_mcp_server(nx=mock_nx_with_sandbox)

        python_tool = get_tool(server, "nexus_python")
        result = python_tool.fn(code='print("Hello, World!")', sandbox_id="test-123")

        assert "Output:" in result
        assert "Hello, World!" in result
        assert "Exit code: 0" in result
        assert "Execution time: 0.456s" in result

        mock_nx_with_sandbox._mock_sandbox_rpc.sandbox_run.assert_called_once_with(
            sandbox_id="test-123", language="python", code='print("Hello, World!")', timeout=300
        )

    async def test_python_execution_with_error(self, mock_nx_with_sandbox):
        """Test Python execution with stderr output."""
        mock_nx_with_sandbox._mock_sandbox_rpc.sandbox_run.return_value = {
            "stdout": "",
            "stderr": "NameError: name 'undefined' is not defined",
            "exit_code": 1,
            "execution_time": 0.123,
        }
        server = await create_mcp_server(nx=mock_nx_with_sandbox)

        python_tool = get_tool(server, "nexus_python")
        result = python_tool.fn(code="print(undefined)", sandbox_id="test-123")

        assert "Errors:" in result
        assert "NameError" in result
        assert "Exit code: 1" in result

    async def test_python_execution_no_output(self, mock_nx_with_sandbox):
        """Test Python execution with no output."""
        mock_nx_with_sandbox._mock_sandbox_rpc.sandbox_run.return_value = {
            "stdout": "",
            "stderr": "",
            "exit_code": 0,
            "execution_time": 0.01,
        }
        server = await create_mcp_server(nx=mock_nx_with_sandbox)

        python_tool = get_tool(server, "nexus_python")
        result = python_tool.fn(code="x = 1 + 1", sandbox_id="test-123")

        # When there's no stdout/stderr, still shows exit code and time
        assert "Exit code: 0" in result
        assert "Execution time:" in result
        assert "Output:" not in result
        assert "Errors:" not in result

    async def test_python_execution_error(self, mock_nx_with_sandbox):
        """Test Python execution error handling."""
        mock_nx_with_sandbox._mock_sandbox_rpc.sandbox_run.side_effect = RuntimeError(
            "Sandbox not found"
        )
        server = await create_mcp_server(nx=mock_nx_with_sandbox)

        python_tool = get_tool(server, "nexus_python")
        result = python_tool.fn(code="print('test')", sandbox_id="invalid")

        assert "Error executing Python code" in result
        assert "Sandbox not found" in result

    async def test_bash_execution_success(self, mock_nx_with_sandbox):
        """Test bash command execution successfully."""
        mock_nx_with_sandbox._mock_sandbox_rpc.sandbox_run.return_value = {
            "stdout": "file1.txt\nfile2.txt\n",
            "stderr": "",
            "exit_code": 0,
            "execution_time": 0.089,
        }
        server = await create_mcp_server(nx=mock_nx_with_sandbox)

        bash_tool = get_tool(server, "nexus_bash")
        result = bash_tool.fn(command="ls -l", sandbox_id="test-123")

        assert "Output:" in result
        assert "file1.txt" in result
        assert "Exit code: 0" in result
        assert "Execution time: 0.089s" in result

        mock_nx_with_sandbox._mock_sandbox_rpc.sandbox_run.assert_called_once_with(
            sandbox_id="test-123", language="bash", code="ls -l", timeout=300
        )

    async def test_bash_execution_with_error(self, mock_nx_with_sandbox):
        """Test bash execution with command error."""
        mock_nx_with_sandbox._mock_sandbox_rpc.sandbox_run.return_value = {
            "stdout": "",
            "stderr": "bash: invalid_command: command not found",
            "exit_code": 127,
            "execution_time": 0.01,
        }
        server = await create_mcp_server(nx=mock_nx_with_sandbox)

        bash_tool = get_tool(server, "nexus_bash")
        result = bash_tool.fn(command="invalid_command", sandbox_id="test-123")

        assert "Errors:" in result
        assert "command not found" in result
        assert "Exit code: 127" in result

    async def test_bash_execution_error(self, mock_nx_with_sandbox):
        """Test bash execution error handling."""
        mock_nx_with_sandbox._mock_sandbox_rpc.sandbox_run.side_effect = TimeoutError(
            "Execution timeout"
        )
        server = await create_mcp_server(nx=mock_nx_with_sandbox)

        bash_tool = get_tool(server, "nexus_bash")
        result = bash_tool.fn(command="sleep 1000", sandbox_id="test-123")

        assert "Error executing bash command" in result
        assert "Execution timeout" in result

    async def test_sandbox_create_success(self, mock_nx_with_sandbox):
        """Test creating sandbox successfully."""
        server = await create_mcp_server(nx=mock_nx_with_sandbox)

        create_tool = get_tool(server, "nexus_sandbox_create")
        result = create_tool.fn(name="my-sandbox", ttl_minutes=15)

        sandbox_info = json.loads(result)
        assert "sandbox_id" in sandbox_info
        assert sandbox_info["sandbox_id"] == "test-sandbox-123"

        mock_nx_with_sandbox._mock_sandbox_rpc.sandbox_create.assert_called_once_with(
            name="my-sandbox", ttl_minutes=15
        )

    async def test_sandbox_create_default_ttl(self, mock_nx_with_sandbox):
        """Test creating sandbox with default TTL."""
        server = await create_mcp_server(nx=mock_nx_with_sandbox)

        create_tool = get_tool(server, "nexus_sandbox_create")
        create_tool.fn(name="test")

        call_args = mock_nx_with_sandbox._mock_sandbox_rpc.sandbox_create.call_args
        assert call_args.kwargs["ttl_minutes"] == 10

    async def test_sandbox_create_error(self, mock_nx_with_sandbox):
        """Test sandbox create error handling."""
        mock_nx_with_sandbox._mock_sandbox_rpc.sandbox_create.side_effect = RuntimeError(
            "No providers available"
        )
        server = await create_mcp_server(nx=mock_nx_with_sandbox)

        create_tool = get_tool(server, "nexus_sandbox_create")
        result = create_tool.fn(name="test")

        assert "Error creating sandbox" in result
        assert "No providers available" in result

    async def test_sandbox_list_success(self, mock_nx_with_sandbox):
        """Test listing sandboxes successfully."""
        server = await create_mcp_server(nx=mock_nx_with_sandbox)

        list_tool = get_tool(server, "nexus_sandbox_list")
        result = list_tool.fn()

        sandboxes = json.loads(result)
        assert isinstance(sandboxes, list)
        mock_nx_with_sandbox._mock_sandbox_rpc.sandbox_list.assert_called_once()

    async def test_sandbox_list_error(self, mock_nx_with_sandbox):
        """Test sandbox list error handling."""
        mock_nx_with_sandbox._mock_sandbox_rpc.sandbox_list.side_effect = RuntimeError(
            "Connection failed"
        )
        server = await create_mcp_server(nx=mock_nx_with_sandbox)

        list_tool = get_tool(server, "nexus_sandbox_list")
        result = list_tool.fn()

        assert "Error listing sandboxes" in result
        assert "Connection failed" in result

    async def test_sandbox_stop_success(self, mock_nx_with_sandbox):
        """Test stopping sandbox successfully."""
        server = await create_mcp_server(nx=mock_nx_with_sandbox)

        stop_tool = get_tool(server, "nexus_sandbox_stop")
        result = stop_tool.fn(sandbox_id="test-123")

        assert "Successfully stopped sandbox" in result
        assert "test-123" in result
        mock_nx_with_sandbox._mock_sandbox_rpc.sandbox_stop.assert_called_once_with("test-123")

    async def test_sandbox_stop_error(self, mock_nx_with_sandbox):
        """Test sandbox stop error handling."""
        mock_nx_with_sandbox._mock_sandbox_rpc.sandbox_stop.side_effect = ValueError(
            "Sandbox not found"
        )
        server = await create_mcp_server(nx=mock_nx_with_sandbox)

        stop_tool = get_tool(server, "nexus_sandbox_stop")
        result = stop_tool.fn(sandbox_id="invalid")

        assert "Error stopping sandbox" in result
        assert "Sandbox not found" in result


# ============================================================================
# RESOURCES AND PROMPTS TESTS
# ============================================================================


class TestResources:
    """Test suite for MCP resource endpoints."""

    async def test_file_resource_success(self, mock_nx_basic):
        """Test accessing file resource successfully."""
        import inspect

        from fastmcp.server.context import Context, _current_context

        mock_nx_basic.sys_read.return_value = b"resource content"
        server = await create_mcp_server(nx=mock_nx_basic)

        resource = get_resource_template(server, "nexus://files/")
        token = _current_context.set(Context(fastmcp=server))
        try:
            result = resource.fn(path="/data/file.txt")
            if inspect.iscoroutine(result):
                result = await result
        finally:
            _current_context.reset(token)

        assert result == "resource content"
        mock_nx_basic.sys_read.assert_called_once_with("/data/file.txt")

    async def test_file_resource_bytes(self, mock_nx_basic):
        """Test file resource with bytes content."""
        import inspect

        from fastmcp.server.context import Context, _current_context

        mock_nx_basic.sys_read.return_value = b"binary data"
        server = await create_mcp_server(nx=mock_nx_basic)

        resource = get_resource_template(server, "nexus://files/")
        token = _current_context.set(Context(fastmcp=server))
        try:
            result = resource.fn(path="/data/binary.dat")
            if inspect.iscoroutine(result):
                result = await result
        finally:
            _current_context.reset(token)

        assert result == "binary data"

    async def test_file_resource_error(self, mock_nx_basic):
        """Test file resource error handling."""
        import inspect

        from fastmcp.server.context import Context, _current_context

        mock_nx_basic.sys_read.side_effect = FileNotFoundError("File not found")
        server = await create_mcp_server(nx=mock_nx_basic)

        resource = get_resource_template(server, "nexus://files/")
        token = _current_context.set(Context(fastmcp=server))
        try:
            result = resource.fn(path="/missing.txt")
            if inspect.iscoroutine(result):
                result = await result
        finally:
            _current_context.reset(token)

        assert "Error reading resource" in result
        assert "File not found" in result


class TestPrompts:
    """Test suite for MCP prompt templates."""

    async def test_file_analysis_prompt(self, mock_nx_basic):
        """Test file analysis prompt generation."""
        server = await create_mcp_server(nx=mock_nx_basic)

        # Find the prompt
        prompt = get_prompt(server, "file_analysis_prompt")
        result = prompt.fn(file_path="/src/main.py")

        assert "/src/main.py" in result
        assert "Read the file content" in result
        assert "nexus_read_file" in result
        assert "Analyze" in result

    async def test_search_and_summarize_prompt(self, mock_nx_basic):
        """Test search and summarize prompt generation."""
        server = await create_mcp_server(nx=mock_nx_basic)

        # Find the prompt
        prompt = get_prompt(server, "search_and_summarize_prompt")
        result = prompt.fn(query="authentication logic")

        assert "authentication logic" in result
        assert "nexus_semantic_search" in result
        assert "nexus_read_file" in result


# ============================================================================
# SERVER CREATION TESTS
# ============================================================================


class TestServerCreation:
    """Test suite for server creation scenarios."""

    async def test_server_with_provided_nx(self, mock_nx_full):
        """Test creating server with provided NexusFS instance."""
        server = await create_mcp_server(nx=mock_nx_full)

        assert server is not None
        assert len(_components_of(server, "tool")) > 0

    async def test_server_with_remote_url(self):
        """Test creating server with remote URL."""
        with patch("nexus.connect", new_callable=AsyncMock) as mock_connect:
            mock_instance = Mock()
            mock_instance.sys_read = Mock(return_value=b"test")
            mock_instance.sys_write = Mock()
            mock_connect.return_value = mock_instance

            server = await create_mcp_server(remote_url="http://localhost:2026", api_key="test-key")

            mock_connect.assert_called_once_with(
                config={"profile": "remote", "url": "http://localhost:2026", "api_key": "test-key"}
            )
            assert server is not None

    async def test_server_with_auto_connect(self):
        """Test creating server with auto-connect."""
        with patch("nexus.connect", new_callable=AsyncMock) as mock_connect:
            mock_nx = Mock()
            mock_nx.sys_read = Mock(return_value=b"test")
            mock_nx.sys_write = Mock()
            mock_nx.write = Mock()
            mock_connect.return_value = mock_nx

            server = await create_mcp_server()

            mock_connect.assert_called_once()
            assert server is not None

    async def test_server_with_custom_name(self, mock_nx_basic):
        """Test creating server with custom name."""
        server = await create_mcp_server(nx=mock_nx_basic, name="custom-nexus")

        assert server.name == "custom-nexus"

    async def test_server_default_name(self, mock_nx_basic):
        """Test creating server with default name."""
        server = await create_mcp_server(nx=mock_nx_basic)

        assert server.name == "nexus"

    async def test_server_tool_count_without_optional_features(self, mock_nx_basic):
        """Test server has correct tool count with basic features only."""
        server = await create_mcp_server(nx=mock_nx_basic)

        # Basic tools: read, write, delete, list, file_info, mkdir, rmdir,
        # glob, grep, semantic_search, store_memory, query_memory,
        # list_workflows, execute_workflow
        # = 14 tools minimum
        assert len(_components_of(server, "tool")) >= 15

    async def test_server_tool_count_with_all_features(self, mock_nx_full):
        """Test server has correct tool count with all features."""
        server = await create_mcp_server(nx=mock_nx_full)

        # Verify sandbox tools are included (all basic tools + 5 sandbox
        # tools = 19 tools minimum on the full-featured server).
        assert tool_exists(server, "nexus_python")
        assert tool_exists(server, "nexus_bash")
        assert tool_exists(server, "nexus_sandbox_create")
        assert tool_exists(server, "nexus_sandbox_list")
        assert tool_exists(server, "nexus_sandbox_stop")
