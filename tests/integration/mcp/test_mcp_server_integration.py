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
        files = json.loads(list_result)

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
        matches = json.loads(result)

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
        matches = json.loads(result)

        assert isinstance(matches, list)
        assert len(matches) >= 2  # Should find 2 files with TODO

        # Search case-insensitively
        result_case = grep_tool.fn(pattern="hello", path="/search", ignore_case=True)
        matches_case = json.loads(result_case)

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
        py_files = json.loads(glob_result)
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
        files = json.loads(list_result)
        assert len(files) >= 20

        # Delete every other file
        for i in range(0, 20, 2):
            delete_tool.fn(path=f"/bulk/file{i}.txt")

        # Verify remaining files
        list_result_after = list_tool.fn(path="/bulk")
        files_after = json.loads(list_result_after)
        assert len(files_after) == 10  # Half deleted


class TestErrorHandlingIntegration:
    """Integration tests for error handling with real errors."""

    def test_read_nonexistent_file(self, mcp_server):
        """Test reading a file that doesn't exist."""
        read_tool = get_tool(mcp_server, "nexus_read_file")
        result = read_tool.fn(path="/nonexistent/file.txt")

        assert "Error reading file" in result

    def test_delete_nonexistent_file(self, mcp_server):
        """Test deleting a file that doesn't exist."""
        delete_tool = get_tool(mcp_server, "nexus_delete_file")
        result = delete_tool.fn(path="/nonexistent/file.txt")

        assert "Error deleting file" in result

    def test_invalid_json_in_workflow_execute(self, mcp_server):
        """Test workflow execution with invalid JSON input."""
        # Get workflow tool (it may not be available without workflow system)
        if tool_exists(mcp_server, "nexus_execute_workflow"):
            exec_tool = get_tool(mcp_server, "nexus_execute_workflow")
            result = exec_tool.fn(name="test", inputs="{invalid json")

            # Should contain an error message (either from JSON parsing or workflow not available)
            assert "Error" in result or "not available" in result


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
        files = json.loads(result)

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
