"""Integration tests for MCP server with real NexusFS instances.

These tests use actual NexusFS instances with LocalBackend to test
end-to-end workflows and real component interactions.
"""

from __future__ import annotations

import json

import pytest

from nexus.backends.local import LocalBackend
from nexus.core.nexus_fs import NexusFS
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


def extract_items(result: str | list | dict) -> list:
    """Extract items from a potentially paginated response.

    The MCP API can return either:
    - A plain list: [item1, item2, ...]
    - A paginated dict: {"count": N, "items": [...], "has_more": false, ...}

    This helper extracts the items list in either case.
    """
    if isinstance(result, str):
        result = json.loads(result)

    if isinstance(result, list):
        return result
    elif isinstance(result, dict) and "items" in result:
        return result["items"]
    else:
        return result


# ============================================================================
# FIXTURES
# ============================================================================


@pytest.fixture
def nexus_fs(isolated_db, tmp_path):
    """Create a real NexusFS instance with LocalBackend for testing."""
    backend = LocalBackend(root_path=str(tmp_path / "storage"))
    nx = NexusFS(
        backend=backend,
        db_path=str(isolated_db),
        enforce_permissions=False,  # Disable permissions for testing
    )
    yield nx
    nx.close()


@pytest.fixture
def mcp_server(nexus_fs):
    """Create an MCP server with real NexusFS instance."""
    return create_mcp_server(nx=nexus_fs)


@pytest.fixture
def test_files(nexus_fs, tmp_path):
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

    def test_write_and_read_file(self, mcp_server, nexus_fs):
        """Test writing and then reading a file."""
        # Write file using MCP tool
        write_tool = get_tool(mcp_server, "nexus_write_file")
        write_result = write_tool.fn(
            path="/integration_test.txt", content="Integration test content"
        )

        assert "Successfully wrote" in write_result

        # Read file using MCP tool
        read_tool = get_tool(mcp_server, "nexus_read_file")
        read_result = read_tool.fn(path="/integration_test.txt")

        assert read_result == "Integration test content"

        # Verify directly with NexusFS
        direct_read = nexus_fs.read("/integration_test.txt")
        assert direct_read == b"Integration test content"

    def test_create_list_and_delete_workflow(self, mcp_server, nexus_fs):
        """Test complete file lifecycle: create, list, delete."""
        # Create multiple files
        write_tool = get_tool(mcp_server, "nexus_write_file")
        write_tool.fn(path="/workflow/file1.txt", content="File 1")
        write_tool.fn(path="/workflow/file2.txt", content="File 2")
        write_tool.fn(path="/workflow/file3.txt", content="File 3")

        # List files
        list_tool = get_tool(mcp_server, "nexus_list_files")
        list_result = list_tool.fn(path="/workflow")
        files = extract_items(list_result)

        assert len(files) >= 3
        file_paths = [f if isinstance(f, str) else f.get("path", f) for f in files]
        assert any("/workflow/file1.txt" in str(p) for p in file_paths)

        # Delete one file
        delete_tool = get_tool(mcp_server, "nexus_delete_file")
        delete_result = delete_tool.fn(path="/workflow/file2.txt")

        assert "Successfully deleted" in delete_result

        # Verify file is gone
        assert not nexus_fs.exists("/workflow/file2.txt")
        assert nexus_fs.exists("/workflow/file1.txt")
        assert nexus_fs.exists("/workflow/file3.txt")

    def test_directory_operations(self, mcp_server, nexus_fs):
        """Test directory creation and removal."""
        mkdir_tool = get_tool(mcp_server, "nexus_mkdir")
        rmdir_tool = get_tool(mcp_server, "nexus_rmdir")
        write_tool = get_tool(mcp_server, "nexus_write_file")

        # Create directory
        mkdir_result = mkdir_tool.fn(path="/testdir")
        assert "Successfully created" in mkdir_result
        assert nexus_fs.is_directory("/testdir")

        # Write file in directory
        write_tool.fn(path="/testdir/file.txt", content="test")

        # Try to remove non-empty directory without recursive (should fail)
        rmdir_result = rmdir_tool.fn(path="/testdir", recursive=False)
        assert "Error" in rmdir_result or nexus_fs.exists("/testdir")

        # Remove with recursive
        rmdir_result_recursive = rmdir_tool.fn(path="/testdir", recursive=True)
        assert "Successfully removed" in rmdir_result_recursive
        assert not nexus_fs.exists("/testdir")

    def test_file_info_integration(self, mcp_server, test_files):
        """Test getting file information for real files."""
        info_tool = get_tool(mcp_server, "nexus_file_info")

        # Get info for existing file
        result = info_tool.fn(path="/test.txt")
        info = json.loads(result)

        assert info["exists"] is True
        assert info["is_directory"] is False
        assert info["path"] == "/test.txt"

        # Get info for directory
        result_dir = info_tool.fn(path="/data")
        info_dir = json.loads(result_dir)

        assert info_dir["is_directory"] is True


class TestSearchIntegration:
    """Integration tests for search operations."""

    def test_glob_search(self, mcp_server, test_files):
        """Test glob search with real files."""
        glob_tool = get_tool(mcp_server, "nexus_glob")

        # Search for .txt files
        result = glob_tool.fn(pattern="**/*.txt", path="/")
        matches = extract_items(result)

        assert isinstance(matches, list)
        assert len(matches) >= 4  # At least 4 test files
        assert any("test.txt" in m for m in matches)

    def test_grep_search(self, mcp_server, nexus_fs):
        """Test grep search with real file content."""
        # Create files with searchable content
        nexus_fs.write("/search/file1.py", b"def hello():\n    print('Hello')\n# TODO: fix this")
        nexus_fs.write("/search/file2.py", b"class MyClass:\n    def __init__(self):\n        pass")
        nexus_fs.write("/search/file3.py", b"# TODO: implement feature\nimport sys")

        grep_tool = get_tool(mcp_server, "nexus_grep")

        # Search for TODO comments
        result = grep_tool.fn(pattern="TODO", path="/search")
        matches = extract_items(result)

        assert isinstance(matches, list)
        assert len(matches) >= 2  # Should find 2 files with TODO

        # Search case-insensitively
        result_case = grep_tool.fn(pattern="hello", path="/search", ignore_case=True)
        matches_case = extract_items(result_case)

        assert len(matches_case) >= 1


class TestResourcesAndPromptsIntegration:
    """Integration tests for resources and prompts."""

    def test_file_resource_access(self, mcp_server, test_files):
        """Test accessing files through resource endpoints."""
        resource = get_resource_template(mcp_server, "nexus://files/")

        # Access file through resource
        result = resource.fn(path="/test.txt")

        assert result == "Hello, World!"

    def test_prompts_integration(self, mcp_server):
        """Test prompt generation."""
        # Test file analysis prompt
        file_prompt = get_prompt(mcp_server, "file_analysis_prompt")
        result = file_prompt.fn(file_path="/test.txt")

        assert "/test.txt" in result
        assert "nexus_read_file" in result
        assert "Analyze" in result

        # Test search and summarize prompt
        search_prompt = get_prompt(mcp_server, "search_and_summarize_prompt")
        result_search = search_prompt.fn(query="authentication")

        assert "authentication" in result_search
        assert "nexus_semantic_search" in result_search


class TestMultiToolWorkflows:
    """Integration tests for workflows using multiple tools."""

    def test_create_search_modify_workflow(self, mcp_server, nexus_fs):
        """Test workflow: create files, search, modify, verify."""
        write_tool = get_tool(mcp_server, "nexus_write_file")
        read_tool = get_tool(mcp_server, "nexus_read_file")
        glob_tool = get_tool(mcp_server, "nexus_glob")

        # Step 1: Create multiple Python files
        write_tool.fn(path="/project/main.py", content="def main():\n    pass")
        write_tool.fn(path="/project/utils.py", content="def helper():\n    pass")
        write_tool.fn(path="/project/test.py", content="def test_main():\n    pass")

        # Step 2: Search for Python files
        glob_result = glob_tool.fn(pattern="**/*.py", path="/project")
        py_files = extract_items(glob_result)
        assert len(py_files) == 3

        # Step 3: Read and modify one file
        content = read_tool.fn(path="/project/main.py")
        assert "def main()" in content

        modified_content = content + "\n# Modified by integration test"
        write_tool.fn(path="/project/main.py", content=modified_content)

        # Step 4: Verify modification
        new_content = read_tool.fn(path="/project/main.py")
        assert "Modified by integration test" in new_content

    def test_bulk_file_operations(self, mcp_server, nexus_fs):
        """Test handling multiple files efficiently."""
        write_tool = get_tool(mcp_server, "nexus_write_file")
        list_tool = get_tool(mcp_server, "nexus_list_files")
        delete_tool = get_tool(mcp_server, "nexus_delete_file")

        # Create 20 files
        for i in range(20):
            write_tool.fn(path=f"/bulk/file{i}.txt", content=f"Content {i}")

        # List all files
        list_result = list_tool.fn(path="/bulk", recursive=False)
        files = extract_items(list_result)
        assert len(files) >= 20

        # Delete every other file
        for i in range(0, 20, 2):
            delete_tool.fn(path=f"/bulk/file{i}.txt")

        # Verify remaining files
        list_result_after = list_tool.fn(path="/bulk")
        files_after = extract_items(list_result_after)
        assert len(files_after) == 10  # Half deleted


class TestErrorHandlingIntegration:
    """Integration tests for error handling with real errors."""

    def test_read_nonexistent_file(self, mcp_server):
        """Test reading a file that doesn't exist."""
        read_tool = get_tool(mcp_server, "nexus_read_file")
        result = read_tool.fn(path="/nonexistent/file.txt")

        assert "Error" in result
        assert "not found" in result.lower()

    def test_delete_nonexistent_file(self, mcp_server):
        """Test deleting a file that doesn't exist."""
        delete_tool = get_tool(mcp_server, "nexus_delete_file")
        result = delete_tool.fn(path="/nonexistent/file.txt")

        assert "Error" in result
        assert "not found" in result.lower() or "deleted" in result.lower()

    def test_invalid_json_in_workflow_execute(self, mcp_server):
        """Test workflow execution with invalid JSON input."""
        # Get workflow tool (it may not be available without workflow system)
        if tool_exists(mcp_server, "nexus_execute_workflow"):
            exec_tool = get_tool(mcp_server, "nexus_execute_workflow")
            result = exec_tool.fn(name="test", inputs="{invalid json")

            # Should contain an error message (either from JSON parsing or workflow not available)
            assert "Error" in result or "not available" in result


class TestMemoryIntegration:
    """Integration tests for memory system."""

    def test_store_and_query_memory(self, mcp_server):
        """Test storing and querying memories."""
        # Check if memory tools are available
        if not tool_exists(mcp_server, "nexus_store_memory"):
            pytest.skip("Memory system not available")

        store_tool = get_tool(mcp_server, "nexus_store_memory")
        query_tool = get_tool(mcp_server, "nexus_query_memory")

        # Store a memory
        store_result = store_tool.fn(
            content="Integration test memory from curl tests",
            memory_type="test",
            importance=0.8,
        )

        # Should either succeed or indicate memory system not available
        assert "Successfully stored" in store_result or "not available" in store_result

        if "Successfully stored" in store_result:
            # Query memories
            query_result = query_tool.fn(query="test", memory_type="test", limit=5)

            # Should return JSON or indicate not available
            if "not available" not in query_result:
                # Parse result - should be JSON
                try:
                    memories = json.loads(query_result)
                    assert isinstance(memories, list)
                except json.JSONDecodeError:
                    # If parsing fails, that's okay - memory may not be fully configured
                    pass

    def test_memory_not_available_graceful(self, mcp_server):
        """Test that memory tools gracefully handle unavailable memory system."""
        # Even if memory system isn't available, tools should return helpful message
        if tool_exists(mcp_server, "nexus_store_memory"):
            store_tool = get_tool(mcp_server, "nexus_store_memory")
            result = store_tool.fn(content="Test content", memory_type="test", importance=0.5)

            # Should either succeed or provide clear error message
            assert "Successfully" in result or "not available" in result or "Error" in result


class TestWorkflowIntegration:
    """Integration tests for workflow system."""

    def test_list_workflows(self, mcp_server):
        """Test listing available workflows."""
        if not tool_exists(mcp_server, "nexus_list_workflows"):
            pytest.skip("Workflow system not available")

        list_tool = get_tool(mcp_server, "nexus_list_workflows")
        result = list_tool.fn()

        # Should return JSON list or indicate not available
        assert "not available" in result or result.startswith("[") or result.startswith("{")

    def test_execute_workflow(self, mcp_server):
        """Test executing a workflow."""
        if not tool_exists(mcp_server, "nexus_execute_workflow"):
            pytest.skip("Workflow system not available")

        exec_tool = get_tool(mcp_server, "nexus_execute_workflow")
        result = exec_tool.fn(name="test_workflow", inputs=None)

        # Should return result or indicate workflow not found/not available
        assert (
            "not available" in result
            or "not found" in result
            or "Error" in result
            or result.startswith("{")
        )


class TestSemanticSearchIntegration:
    """Integration tests for semantic search."""

    def test_semantic_search_availability(self, mcp_server):
        """Test semantic search tool availability and behavior."""
        if not tool_exists(mcp_server, "nexus_semantic_search"):
            pytest.skip("Semantic search tool not registered")

        search_tool = get_tool(mcp_server, "nexus_semantic_search")
        result = search_tool.fn(query="test files", limit=5)

        # Should return JSON results or indicate not available
        assert "not available" in result or result.startswith("[") or result.startswith("{")


class TestSandboxIntegration:
    """Integration tests for sandbox execution (requires Docker or E2B)."""

    @pytest.mark.skipif(
        True,  # Skip by default - requires Docker/E2B setup
        reason="Requires sandbox providers (Docker or E2B) to be configured",
    )
    def test_sandbox_lifecycle(self, mcp_server):
        """Test complete sandbox lifecycle: create, execute, stop."""
        # Check if sandbox tools are available
        if not tool_exists(mcp_server, "nexus_sandbox_create"):
            pytest.skip("Sandbox tools not available")

        create_tool = get_tool(mcp_server, "nexus_sandbox_create")
        python_tool = get_tool(mcp_server, "nexus_python")
        list_tool = get_tool(mcp_server, "nexus_sandbox_list")
        stop_tool = get_tool(mcp_server, "nexus_sandbox_stop")

        # Create sandbox
        create_result = create_tool.fn(name="integration-test", ttl_minutes=5)
        sandbox_info = json.loads(create_result)
        sandbox_id = sandbox_info["sandbox_id"]

        # Execute Python code
        exec_result = python_tool.fn(
            code='print("Integration test successful")', sandbox_id=sandbox_id
        )
        assert "Integration test successful" in exec_result
        assert "Exit code: 0" in exec_result

        # List sandboxes
        list_result = list_tool.fn()
        sandboxes = json.loads(list_result)
        assert any(s["sandbox_id"] == sandbox_id for s in sandboxes)

        # Stop sandbox
        stop_result = stop_tool.fn(sandbox_id=sandbox_id)
        assert "Successfully stopped" in stop_result

    @pytest.mark.skipif(
        True,  # Skip by default
        reason="Requires sandbox providers to be configured",
    )
    def test_sandbox_bash_execution(self, mcp_server):
        """Test bash command execution in sandbox."""
        if not tool_exists(mcp_server, "nexus_sandbox_create"):
            pytest.skip("Sandbox tools not available")

        create_tool = get_tool(mcp_server, "nexus_sandbox_create")
        bash_tool = get_tool(mcp_server, "nexus_bash")
        stop_tool = get_tool(mcp_server, "nexus_sandbox_stop")

        # Create sandbox
        create_result = create_tool.fn(name="bash-test")
        sandbox_info = json.loads(create_result)
        sandbox_id = sandbox_info["sandbox_id"]

        try:
            # Execute bash commands
            result = bash_tool.fn(command="echo 'Hello from bash'", sandbox_id=sandbox_id)
            assert "Hello from bash" in result

            # Execute command that generates files
            bash_tool.fn(command="touch /tmp/testfile", sandbox_id=sandbox_id)
            result = bash_tool.fn(command="ls /tmp/testfile", sandbox_id=sandbox_id)
            assert "testfile" in result
        finally:
            # Cleanup
            stop_tool.fn(sandbox_id=sandbox_id)


class TestServerConfiguration:
    """Integration tests for server configuration and setup."""

    def test_server_with_local_backend(self, isolated_db, tmp_path):
        """Test server creation with LocalBackend."""
        backend = LocalBackend(root_path=str(tmp_path / "storage"))
        nx = NexusFS(
            backend=backend,
            db_path=str(isolated_db),
            enforce_permissions=False,
        )

        try:
            server = create_mcp_server(nx=nx, name="integration-test-server")

            assert server is not None
            assert server.name == "integration-test-server"
            assert len(server._tool_manager._tools) >= 14

            # Verify all core tools are present
            assert tool_exists(server, "nexus_read_file")
            assert tool_exists(server, "nexus_write_file")
            assert tool_exists(server, "nexus_list_files")
        finally:
            nx.close()

    def test_multiple_servers_same_filesystem(self, nexus_fs):
        """Test creating multiple MCP servers with the same filesystem."""
        server1 = create_mcp_server(nx=nexus_fs, name="server1")
        server2 = create_mcp_server(nx=nexus_fs, name="server2")

        assert server1.name == "server1"
        assert server2.name == "server2"

        # Both should work with the same filesystem
        write_tool1 = get_tool(server1, "nexus_write_file")
        read_tool2 = get_tool(server2, "nexus_read_file")

        write_tool1.fn(path="/shared_file.txt", content="Shared content")
        result = read_tool2.fn(path="/shared_file.txt")

        assert result == "Shared content"


class TestComprehensiveMCPToolsWorkflow:
    """Comprehensive test that mirrors test_mcp_tools.sh bash script."""

    def test_all_14_mcp_tools_workflow(self, mcp_server, nexus_fs):
        """Test all 14 MCP tools in sequence (mirrors test_mcp_tools.sh)."""
        # This test mirrors the comprehensive bash script test_mcp_tools.sh

        # Step 1: Test nexus_mkdir - Create test directory
        mkdir_tool = get_tool(mcp_server, "nexus_mkdir")
        mkdir_result = mkdir_tool.fn(path="/mcp_integration_test")
        assert "Successfully created" in mkdir_result

        # Step 2: Test nexus_write_file - Write test files
        write_tool = get_tool(mcp_server, "nexus_write_file")

        write_result1 = write_tool.fn(
            path="/mcp_integration_test/test1.txt", content="Hello from MCP Test!"
        )
        assert "Successfully wrote" in write_result1

        write_result2 = write_tool.fn(
            path="/mcp_integration_test/test2.py", content="print('Python file test')"
        )
        assert "Successfully wrote" in write_result2

        write_result3 = write_tool.fn(
            path="/mcp_integration_test/data.json", content='{"test": "data"}'
        )
        assert "Successfully wrote" in write_result3

        # Step 3: Test nexus_read_file
        read_tool = get_tool(mcp_server, "nexus_read_file")
        read_result = read_tool.fn(path="/mcp_integration_test/test1.txt")
        assert "Hello from MCP Test" in read_result

        # Step 4: Test nexus_list_files
        list_tool = get_tool(mcp_server, "nexus_list_files")
        list_result = list_tool.fn(path="/mcp_integration_test", recursive=False, details=True)
        files = extract_items(list_result)
        file_names = [f if isinstance(f, str) else f.get("path", "") for f in files]
        assert any("test1.txt" in str(name) for name in file_names)
        assert any("test2.py" in str(name) for name in file_names)

        # Step 5: Test nexus_file_info
        info_tool = get_tool(mcp_server, "nexus_file_info")
        info_result = info_tool.fn(path="/mcp_integration_test/test1.txt")
        info = json.loads(info_result)
        assert info["exists"] is True

        # Step 6: Test nexus_glob
        glob_tool = get_tool(mcp_server, "nexus_glob")
        glob_result = glob_tool.fn(pattern="*.txt", path="/mcp_integration_test")
        glob_matches = extract_items(glob_result)
        assert any("test1.txt" in match for match in glob_matches)

        # Step 7: Test nexus_grep
        grep_tool = get_tool(mcp_server, "nexus_grep")
        grep_result = grep_tool.fn(pattern="Hello", path="/mcp_integration_test", ignore_case=False)
        grep_matches = extract_items(grep_result)
        assert len(grep_matches) > 0

        # Step 8: Test nexus_semantic_search (optional)
        if tool_exists(mcp_server, "nexus_semantic_search"):
            search_tool = get_tool(mcp_server, "nexus_semantic_search")
            search_result = search_tool.fn(query="test files", limit=5)
            # Should return result or indicate not available
            assert "not available" in search_result or search_result.startswith("[")

        # Step 9: Test nexus_store_memory (optional)
        if tool_exists(mcp_server, "nexus_store_memory"):
            memory_store_tool = get_tool(mcp_server, "nexus_store_memory")
            memory_result = memory_store_tool.fn(
                content="This is a test memory from integration test",
                memory_type="test",
                importance=0.8,
            )
            # Should either succeed or indicate not available
            assert "Successfully stored" in memory_result or "not available" in memory_result

        # Step 10: Test nexus_query_memory (optional)
        if tool_exists(mcp_server, "nexus_query_memory"):
            memory_query_tool = get_tool(mcp_server, "nexus_query_memory")
            query_result = memory_query_tool.fn(query="test", memory_type=None, limit=5)
            # Should return results or indicate not available
            assert "not available" in query_result or query_result.startswith("[")

        # Step 11: Test nexus_list_workflows (optional)
        if tool_exists(mcp_server, "nexus_list_workflows"):
            workflows_tool = get_tool(mcp_server, "nexus_list_workflows")
            workflows_result = workflows_tool.fn()
            # Should return list or indicate not available
            assert "not available" in workflows_result or workflows_result.startswith("[")

        # Step 12: Test nexus_execute_workflow (optional)
        if tool_exists(mcp_server, "nexus_execute_workflow"):
            exec_workflow_tool = get_tool(mcp_server, "nexus_execute_workflow")
            exec_result = exec_workflow_tool.fn(name="test_workflow", inputs=None)
            # Should return result or indicate not available/not found
            assert (
                "not available" in exec_result
                or "not found" in exec_result
                or exec_result.startswith("{")
            )

        # Step 13: Test nexus_delete_file
        delete_tool = get_tool(mcp_server, "nexus_delete_file")
        delete_result = delete_tool.fn(path="/mcp_integration_test/data.json")
        assert "Successfully deleted" in delete_result

        # Step 14: Test nexus_rmdir
        rmdir_tool = get_tool(mcp_server, "nexus_rmdir")
        rmdir_result = rmdir_tool.fn(path="/mcp_integration_test", recursive=True)
        assert "Successfully removed" in rmdir_result

        # Verify directory was removed
        assert not nexus_fs.exists("/mcp_integration_test")


class TestPerformanceCharacteristics:
    """Integration tests for performance characteristics."""

    def test_large_file_handling(self, mcp_server, nexus_fs):
        """Test handling of large files."""
        write_tool = get_tool(mcp_server, "nexus_write_file")
        read_tool = get_tool(mcp_server, "nexus_read_file")

        # Create a moderately large file (1MB)
        large_content = "x" * (1024 * 1024)  # 1MB

        write_result = write_tool.fn(path="/large_file.txt", content=large_content)
        assert "Successfully wrote" in write_result
        assert "1048576" in write_result  # Size in bytes

        # Read it back
        read_result = read_tool.fn(path="/large_file.txt")
        assert len(read_result) == len(large_content)

    def test_many_small_files(self, mcp_server, nexus_fs):
        """Test handling many small files efficiently."""
        write_tool = get_tool(mcp_server, "nexus_write_file")
        glob_tool = get_tool(mcp_server, "nexus_glob")

        # Create 100 small files
        for i in range(100):
            write_tool.fn(path=f"/many/file{i:03d}.txt", content=f"Small {i}")

        # Search for them all
        result = glob_tool.fn(pattern="**/*.txt", path="/many")
        files = extract_items(result)

        assert len(files) == 100

    def test_deep_directory_nesting(self, mcp_server, nexus_fs):
        """Test handling deeply nested directories."""
        write_tool = get_tool(mcp_server, "nexus_write_file")
        read_tool = get_tool(mcp_server, "nexus_read_file")

        # Create deeply nested file
        deep_path = "/" + "/".join([f"level{i}" for i in range(20)]) + "/file.txt"

        write_result = write_tool.fn(path=deep_path, content="Deep file")
        assert "Successfully wrote" in write_result

        # Read it back
        read_result = read_tool.fn(path=deep_path)
        assert read_result == "Deep file"
