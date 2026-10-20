"""Integration tests for MCP server with real NexusFS instances.

These tests use actual NexusFS instances with CASLocalBackend to test
end-to-end workflows and real component interactions.
"""

import json

import pytest

from nexus.backends.storage.cas_local import CASLocalBackend
from nexus.bricks.mcp.server import create_mcp_server
from nexus.core.config import PermissionConfig
from nexus.factory import create_nexus_fs
from nexus.storage.record_store import SQLAlchemyRecordStore

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================


async def get_tool(server, tool_name: str):
    """Helper to get a tool from the MCP server."""
    return await server.get_tool(tool_name)


async def get_prompt(server, prompt_name: str):
    """Helper to get a prompt from the MCP server."""
    return await server.get_prompt(prompt_name)


async def get_resource_template(server, uri_pattern: str):
    """Helper to get a resource template from the MCP server."""
    templates = await server.list_resource_templates()
    for template in templates:
        if uri_pattern in str(getattr(template, "uri_template", "")):
            return template
    raise KeyError(f"Resource template with pattern '{uri_pattern}' not found")


async def tool_exists(server, tool_name: str) -> bool:
    """Check if a tool exists in the server."""
    try:
        result = await server.get_tool(tool_name)
        return result is not None
    except (KeyError, Exception):
        return False


def extract_items(result: str | list | dict) -> list:
    """Extract items from a potentially paginated response.

    The MCP API can return either:
    - A plain list: [item1, item2, ...]
    - A paginated dict: {"count": N, "items": [...], "has_more": false, ...}
    - An error string: "Error in glob search: ..." (not valid JSON)

    This helper extracts the items list in either case.
    Raises ValueError if the result is an error string from an MCP tool.
    """
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError:
            raise ValueError(f"MCP tool returned an error: {result}")

    if isinstance(result, list):
        return result
    elif isinstance(result, dict) and "items" in result:
        return result["items"]
    else:
        return result


# ============================================================================
# FIXTURES
# ============================================================================


@pytest.fixture(scope="module")
async def nexus_fs(tmp_path_factory):
    """Create a real NexusFS instance with CASLocalBackend for testing."""
    base = tmp_path_factory.mktemp("nexus_mcp_integration")
    (base / "storage").mkdir()
    backend = CASLocalBackend(root_path=str(base / "storage"))
    nx = create_nexus_fs(
        backend=backend,
        metadata_store=str(base / "meta"),
        record_store=SQLAlchemyRecordStore(db_path=str(base / "records.db")),
        permissions=PermissionConfig(enforce=False),  # Disable permissions for testing
    )
    yield nx
    # Use sync close — aclose() drains all hooks with 10s timeouts per hook,
    # causing teardown hangs in CI.  Sync close is fine for SQLite tests with
    # no async background tasks (no delivery worker, no piped observer consumer).
    nx.close()


@pytest.fixture(scope="module")
async def mcp_server(nexus_fs):
    """Create an MCP server with real NexusFS instance."""
    return await create_mcp_server(nx=nexus_fs)


@pytest.fixture(scope="module")
async def test_files(nexus_fs):
    """Create some test files in the filesystem."""
    # Create test files
    test_data = {
        "/test.txt": b"Hello, World!",
        "/data/file1.txt": b"File 1 content",
        "/data/file2.txt": b"File 2 content",
        "/nested/deep/file.txt": b"Deeply nested file",
    }

    for path, content in test_data.items():
        nexus_fs.write(path, content)

    return test_data


# ============================================================================
# INTEGRATION TESTS
# ============================================================================


class TestFileOperationsIntegration:
    """Integration tests for file operations with real filesystem."""

    @pytest.mark.asyncio
    async def test_write_and_read_file(self, mcp_server, nexus_fs):
        """Test writing and then reading a file."""
        # Write file using MCP tool
        write_tool = await get_tool(mcp_server, "nexus_write_file")
        write_result = await write_tool.fn(
            path="/integration_test.txt", content="Integration test content"
        )

        assert "Successfully wrote" in write_result

        # Read file using MCP tool
        read_tool = await get_tool(mcp_server, "nexus_read_file")
        read_result = await read_tool.fn(path="/integration_test.txt")

        assert read_result == "Integration test content"

        # Verify directly with NexusFS
        direct_read = nexus_fs.sys_read("/integration_test.txt")
        assert direct_read == b"Integration test content"

    @pytest.mark.asyncio
    async def test_create_list_and_delete_workflow(self, mcp_server, nexus_fs):
        """Test complete file lifecycle: create, list, delete."""
        # Create multiple files
        write_tool = await get_tool(mcp_server, "nexus_write_file")
        await write_tool.fn(path="/workflow/file1.txt", content="File 1")
        await write_tool.fn(path="/workflow/file2.txt", content="File 2")
        await write_tool.fn(path="/workflow/file3.txt", content="File 3")

        # List files
        list_tool = await get_tool(mcp_server, "nexus_list_files")
        list_result = await list_tool.fn(path="/workflow")
        files = extract_items(list_result)

        assert len(files) >= 3
        file_paths = [f if isinstance(f, str) else f.get("path", f) for f in files]
        assert any("/workflow/file1.txt" in str(p) for p in file_paths)

        # Delete one file
        delete_tool = await get_tool(mcp_server, "nexus_delete_file")
        delete_result = await delete_tool.fn(path="/workflow/file2.txt")

        assert "Successfully deleted" in delete_result

        # Verify file is gone
        assert not nexus_fs.access("/workflow/file2.txt")
        assert nexus_fs.access("/workflow/file1.txt")
        assert nexus_fs.access("/workflow/file3.txt")

    @pytest.mark.asyncio
    async def test_directory_operations(self, mcp_server, nexus_fs):
        """Test directory creation and removal."""
        mkdir_tool = await get_tool(mcp_server, "nexus_mkdir")
        rmdir_tool = await get_tool(mcp_server, "nexus_rmdir")
        write_tool = await get_tool(mcp_server, "nexus_write_file")

        # Create directory
        mkdir_result = await mkdir_tool.fn(path="/testdir")
        assert "Successfully created" in mkdir_result
        assert nexus_fs.is_directory("/testdir")

        # Write file in directory
        await write_tool.fn(path="/testdir/file.txt", content="test")

        # Try to remove non-empty directory without recursive (should fail)
        rmdir_result = await rmdir_tool.fn(path="/testdir", recursive=False)
        assert "Error" in rmdir_result or nexus_fs.access("/testdir")

        # Remove with recursive
        rmdir_result_recursive = await rmdir_tool.fn(path="/testdir", recursive=True)
        assert "Successfully removed" in rmdir_result_recursive
        assert not nexus_fs.access("/testdir")

    @pytest.mark.asyncio
    async def test_file_info_integration(self, mcp_server, test_files):
        """Test getting file information for real files."""
        info_tool = await get_tool(mcp_server, "nexus_file_info")

        # Get info for existing file
        result = await info_tool.fn(path="/test.txt")
        info = json.loads(result)

        assert info["exists"] is True
        assert info["is_directory"] is False
        assert info["path"] == "/test.txt"

        # Get info for directory
        result_dir = await info_tool.fn(path="/data")
        info_dir = json.loads(result_dir)

        assert info_dir["is_directory"] is True


class TestResourcesAndPromptsIntegration:
    """Integration tests for resources and prompts."""

    @pytest.mark.skip(
        reason="fastmcp resources require MCP context - use MCP client for e2e testing"
    )
    async def test_file_resource_access(self, mcp_server, test_files):
        """Test accessing files through resource endpoints."""
        resource = await get_resource_template(mcp_server, "nexus://files/")

        # Access file through resource
        result = await resource.fn(path="/test.txt")

        assert result == "Hello, World!"

    @pytest.mark.asyncio
    async def test_prompts_integration(self, mcp_server):
        """Test prompt generation."""
        # Test file analysis prompt
        file_prompt = await get_prompt(mcp_server, "file_analysis_prompt")
        result = file_prompt.fn(file_path="/test.txt")

        assert "/test.txt" in result
        assert "nexus_read_file" in result
        assert "Analyze" in result

        # Test search and summarize prompt
        search_prompt = await get_prompt(mcp_server, "search_and_summarize_prompt")
        result_search = search_prompt.fn(query="authentication")

        assert "authentication" in result_search
        assert "nexus_semantic_search" in result_search


class TestErrorHandlingIntegration:
    """Integration tests for error handling with real errors."""

    @pytest.mark.asyncio
    async def test_read_nonexistent_file(self, mcp_server):
        """Test reading a file that doesn't exist."""
        read_tool = await get_tool(mcp_server, "nexus_read_file")
        result = await read_tool.fn(path="/nonexistent/file.txt")

        assert "Error" in result
        assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_delete_nonexistent_file(self, mcp_server):
        """Test deleting a file that doesn't exist."""
        delete_tool = await get_tool(mcp_server, "nexus_delete_file")
        result = await delete_tool.fn(path="/nonexistent/file.txt")

        assert "Error" in result
        assert "not found" in result.lower() or "deleted" in result.lower()


class TestServerConfiguration:
    """Integration tests for server configuration and setup."""

    @pytest.mark.asyncio
    async def test_server_with_local_backend(self, isolated_db, tmp_path):
        """Test server creation with CASLocalBackend."""
        backend = CASLocalBackend(root_path=str(tmp_path / "storage"))
        nx = create_nexus_fs(
            backend=backend,
            metadata_store=str(isolated_db).replace(".db", "-raft"),
            record_store=SQLAlchemyRecordStore(db_path=str(isolated_db)),
            permissions=PermissionConfig(enforce=False),
        )

        try:
            server = await create_mcp_server(nx=nx, name="integration-test-server")

            assert server is not None
            assert server.name == "integration-test-server"
            _list_fn = getattr(server, "list_tools", None) or server.get_tools
            assert len(await _list_fn()) >= 14

            # Verify all core tools are present
            assert await tool_exists(server, "nexus_read_file")
            assert await tool_exists(server, "nexus_write_file")
            assert await tool_exists(server, "nexus_list_files")
        finally:
            nx.close()

    @pytest.mark.asyncio
    async def test_multiple_servers_same_filesystem(self, nexus_fs):
        """Test creating multiple MCP servers with the same filesystem."""
        server1 = await create_mcp_server(nx=nexus_fs, name="server1")
        server2 = await create_mcp_server(nx=nexus_fs, name="server2")

        assert server1.name == "server1"
        assert server2.name == "server2"

        # Both should work with the same filesystem
        write_tool1 = await get_tool(server1, "nexus_write_file")
        read_tool2 = await get_tool(server2, "nexus_read_file")

        await write_tool1.fn(path="/shared_file.txt", content="Shared content")
        result = await read_tool2.fn(path="/shared_file.txt")

        assert result == "Shared content"


class TestPerformanceCharacteristics:
    """Integration tests for performance characteristics."""

    @pytest.mark.asyncio
    async def test_large_file_handling(self, mcp_server, nexus_fs):
        """Test handling of large files."""
        write_tool = await get_tool(mcp_server, "nexus_write_file")
        read_tool = await get_tool(mcp_server, "nexus_read_file")

        # Create a moderately large file (1MB)
        large_content = "x" * (1024 * 1024)  # 1MB

        write_result = await write_tool.fn(path="/large_file.txt", content=large_content)
        assert "Successfully wrote" in write_result
        assert "1048576" in write_result  # Size in bytes

        # Read it back
        read_result = await read_tool.fn(path="/large_file.txt")
        assert len(read_result) == len(large_content)

    @pytest.mark.asyncio
    async def test_deep_directory_nesting(self, mcp_server, nexus_fs):
        """Test handling deeply nested directories."""
        write_tool = await get_tool(mcp_server, "nexus_write_file")
        read_tool = await get_tool(mcp_server, "nexus_read_file")

        # Create deeply nested file
        deep_path = "/" + "/".join([f"level{i}" for i in range(20)]) + "/file.txt"

        write_result = await write_tool.fn(path=deep_path, content="Deep file")
        assert "Successfully wrote" in write_result

        # Read it back
        read_result = await read_tool.fn(path=deep_path)
        assert read_result == "Deep file"
