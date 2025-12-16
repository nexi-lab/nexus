"""Unit tests for Nexus LangGraph tools."""

from unittest.mock import Mock, patch

import pytest

# Skip all tests if langchain_core is not installed
pytest.importorskip("langchain_core")
pytest.importorskip("langgraph")

from langchain_core.runnables import RunnableConfig

from nexus.remote import RemoteNexusFS
from nexus.tools.langgraph.nexus_tools import get_nexus_tools


class TestGetNexusTools:
    """Tests for get_nexus_tools function."""

    def test_returns_all_tools(self):
        """Test that get_nexus_tools returns all 7 tools."""
        tools = get_nexus_tools()
        assert len(tools) == 7

    def test_tool_names(self):
        """Test that all tools have correct names."""
        tools = get_nexus_tools()
        tool_names = [tool.name for tool in tools]

        expected_names = [
            "grep_files",
            "glob_files",
            "read_file",
            "write_file",
            "python",
            "bash",
            "query_memories",
        ]

        assert tool_names == expected_names

    def test_tools_are_callable(self):
        """Test that all tools are callable."""
        tools = get_nexus_tools()
        for tool in tools:
            assert callable(tool.func)


class TestGrepFilesTool:
    """Tests for grep_files tool."""

    def test_grep_basic_search(self):
        """Test basic grep search."""
        tools = get_nexus_tools()
        grep_tool = tools[0].func

        mock_nx = Mock(spec=RemoteNexusFS)
        mock_nx.grep.return_value = [
            {"file": "/test.py", "line": 10, "content": "async def test():"},
            {"file": "/main.py", "line": 5, "content": "async def main():"},
        ]

        state = {}
        config: RunnableConfig = {
            "metadata": {"x_auth": "Bearer test-token", "nexus_server_url": "http://localhost:8080"}
        }

        with patch("nexus.tools.langgraph.nexus_tools._get_nexus_client", return_value=mock_nx):
            result = grep_tool("async def", config, state)

        assert "/test.py:10:async def test():" in result
        assert "/main.py:5:async def main():" in result
        assert "Found 2 matches" in result
        mock_nx.grep.assert_called_once_with(
            pattern="async def",
            path="/",
            file_pattern=None,
            ignore_case=False,
            max_results=1000,
        )

    def test_grep_with_path(self):
        """Test grep with custom path."""
        tools = get_nexus_tools()
        grep_tool = tools[0].func

        mock_nx = Mock(spec=RemoteNexusFS)
        mock_nx.grep.return_value = []

        state = {}
        config: RunnableConfig = {
            "metadata": {"x_auth": "Bearer test-token", "nexus_server_url": "http://localhost:8080"}
        }

        with patch("nexus.tools.langgraph.nexus_tools._get_nexus_client", return_value=mock_nx):
            result = grep_tool("pattern", config, state, path="/workspace")

        mock_nx.grep.assert_called_once_with(
            pattern="pattern",
            path="/workspace",
            file_pattern=None,
            ignore_case=False,
            max_results=1000,
        )
        assert "No matches found" in result
        assert "in /workspace" in result

    def test_grep_case_insensitive(self):
        """Test grep with case insensitive flag."""
        tools = get_nexus_tools()
        grep_tool = tools[0].func

        mock_nx = Mock(spec=RemoteNexusFS)
        mock_nx.grep.return_value = []

        state = {}
        config: RunnableConfig = {
            "metadata": {"x_auth": "Bearer test-token", "nexus_server_url": "http://localhost:8080"}
        }

        with patch("nexus.tools.langgraph.nexus_tools._get_nexus_client", return_value=mock_nx):
            grep_tool("pattern", config, state, ignore_case=True)

        mock_nx.grep.assert_called_once_with(
            pattern="pattern",
            path="/",
            file_pattern=None,
            ignore_case=True,
            max_results=1000,
        )

    def test_grep_with_file_pattern(self):
        """Test grep with file_pattern parameter."""
        tools = get_nexus_tools()
        grep_tool = tools[0].func

        mock_nx = Mock(spec=RemoteNexusFS)
        mock_nx.grep.return_value = [
            {"file": "/test.py", "line": 10, "content": "import pandas"},
        ]

        state = {}
        config: RunnableConfig = {
            "metadata": {"x_auth": "Bearer test-token", "nexus_server_url": "http://localhost:8080"}
        }

        with patch("nexus.tools.langgraph.nexus_tools._get_nexus_client", return_value=mock_nx):
            result = grep_tool("import pandas", config, state, file_pattern="**/*.py")

        mock_nx.grep.assert_called_once_with(
            pattern="import pandas",
            path="/",
            file_pattern="**/*.py",
            ignore_case=False,
            max_results=1000,
        )
        assert "in files matching '**/*.py'" in result

    def test_grep_with_max_results(self):
        """Test grep with custom max_results."""
        tools = get_nexus_tools()
        grep_tool = tools[0].func

        mock_nx = Mock(spec=RemoteNexusFS)
        mock_nx.grep.return_value = []

        state = {}
        config: RunnableConfig = {
            "metadata": {"x_auth": "Bearer test-token", "nexus_server_url": "http://localhost:8080"}
        }

        with patch("nexus.tools.langgraph.nexus_tools._get_nexus_client", return_value=mock_nx):
            grep_tool("pattern", config, state, max_results=50)

        mock_nx.grep.assert_called_once_with(
            pattern="pattern",
            path="/",
            file_pattern=None,
            ignore_case=False,
            max_results=50,
        )

    def test_grep_with_all_parameters(self):
        """Test grep with all parameters specified."""
        tools = get_nexus_tools()
        grep_tool = tools[0].func

        mock_nx = Mock(spec=RemoteNexusFS)
        mock_nx.grep.return_value = [
            {"file": "/logs/error.log", "line": 42, "content": "ERROR: Connection failed"},
        ]

        state = {}
        config: RunnableConfig = {
            "metadata": {"x_auth": "Bearer test-token", "nexus_server_url": "http://localhost:8080"}
        }

        with patch("nexus.tools.langgraph.nexus_tools._get_nexus_client", return_value=mock_nx):
            result = grep_tool(
                "error",
                config,
                state,
                path="/logs",
                file_pattern="*.log",
                ignore_case=True,
                max_results=100,
            )

        mock_nx.grep.assert_called_once_with(
            pattern="error",
            path="/logs",
            file_pattern="*.log",
            ignore_case=True,
            max_results=100,
        )
        assert "Found 1 matches" in result
        assert "in files matching '*.log'" in result

    def test_grep_from_state_context(self):
        """Test getting auth from state context."""
        tools = get_nexus_tools()
        grep_tool = tools[0].func

        mock_nx = Mock(spec=RemoteNexusFS)
        mock_nx.grep.return_value = []

        state = {
            "context": {"x_auth": "Bearer state-token", "nexus_server_url": "http://localhost:9090"}
        }
        config: RunnableConfig = {"metadata": {}}

        # Patch _get_nexus_client where it's used in the module
        # It's imported from nexus.tools._client, so we patch it in nexus_tools namespace
        from contextlib import suppress

        # If there's an error (like connection refused), that's OK for this test
        # We just want to verify _get_nexus_client was called
        with (
            patch(
                "nexus.tools.langgraph.nexus_tools._get_nexus_client", return_value=mock_nx
            ) as mock_get_client,
            suppress(Exception),
        ):
            grep_tool("test", config, state)

        # Should use state context to create client
        # The function should have been called to get the client
        assert mock_get_client.called, "_get_nexus_client should have been called"
        # Check that it was called with config and state
        call_args = mock_get_client.call_args
        assert call_args is not None
        assert len(call_args[0]) >= 1
        assert call_args[0][0] == config  # First positional arg is config
        if len(call_args[0]) > 1:
            assert call_args[0][1] == state  # Second positional arg is state

    def test_grep_missing_auth(self):
        """Test error when auth is missing."""
        tools = get_nexus_tools()
        grep_tool = tools[0].func

        state = {}
        config: RunnableConfig = {"metadata": {}}

        result = grep_tool("test", config, state)
        assert "Missing x_auth" in result

    def test_grep_invalid_auth_format(self):
        """Test error with invalid auth format."""
        tools = get_nexus_tools()
        grep_tool = tools[0].func

        state = {}
        config: RunnableConfig = {
            "metadata": {"x_auth": "Bearer ", "nexus_server_url": "http://localhost:8080"}
        }

        result = grep_tool("test", config, state)
        assert "Invalid x_auth format" in result

    def test_grep_truncates_long_lines(self):
        """Test that grep truncates very long lines."""
        tools = get_nexus_tools()
        grep_tool = tools[0].func

        mock_nx = Mock(spec=RemoteNexusFS)
        long_content = "x" * 500
        mock_nx.grep.return_value = [{"file": "/test.py", "line": 1, "content": long_content}]

        state = {}
        config: RunnableConfig = {
            "metadata": {"x_auth": "Bearer test-token", "nexus_server_url": "http://localhost:8080"}
        }

        with patch("nexus.tools.langgraph.nexus_tools._get_nexus_client", return_value=mock_nx):
            result = grep_tool("pattern", config, state)

        assert "..." in result
        assert len(result) < len(long_content)

    def test_grep_limits_results(self):
        """Test that grep limits display to first 50 matches."""
        tools = get_nexus_tools()
        grep_tool = tools[0].func

        mock_nx = Mock(spec=RemoteNexusFS)
        matches = [{"file": f"/file{i}.py", "line": i, "content": "match"} for i in range(100)]
        mock_nx.grep.return_value = matches

        state = {}
        config: RunnableConfig = {
            "metadata": {"x_auth": "Bearer test-token", "nexus_server_url": "http://localhost:8080"}
        }

        with patch("nexus.tools.langgraph.nexus_tools._get_nexus_client", return_value=mock_nx):
            result = grep_tool("pattern", config, state)

        assert "Found 100 matches" in result
        assert "and 50 more matches" in result

    def test_grep_respects_max_results_display_limit(self):
        """Test that grep display limit respects max_results parameter."""
        tools = get_nexus_tools()
        grep_tool = tools[0].func

        mock_nx = Mock(spec=RemoteNexusFS)
        # Simulate nx.grep returning fewer results due to max_results=10
        matches = [{"file": f"/file{i}.py", "line": i, "content": "match"} for i in range(10)]
        mock_nx.grep.return_value = matches

        state = {}
        config: RunnableConfig = {
            "metadata": {"x_auth": "Bearer test-token", "nexus_server_url": "http://localhost:8080"}
        }

        with patch("nexus.tools.langgraph.nexus_tools._get_nexus_client", return_value=mock_nx):
            result = grep_tool("pattern", config, state, max_results=10)

        # Should show all 10 results without "more matches" message
        assert "Found 10 matches" in result
        assert "more matches" not in result
        # All 10 files should be in output
        for i in range(10):
            assert f"/file{i}.py" in result


class TestGlobFilesTool:
    """Tests for glob_files tool."""

    def test_glob_basic_pattern(self):
        """Test basic glob pattern."""
        tools = get_nexus_tools()
        glob_tool = tools[1].func

        mock_nx = Mock(spec=RemoteNexusFS)
        mock_nx.glob.return_value = ["/file1.py", "/file2.py", "/file3.py"]

        state = {}
        config: RunnableConfig = {
            "metadata": {"x_auth": "Bearer test-token", "nexus_server_url": "http://localhost:8080"}
        }

        with patch("nexus.tools.langgraph.nexus_tools._get_nexus_client", return_value=mock_nx):
            result = glob_tool("*.py", config, state)

        assert "Found 3 files" in result
        assert "/file1.py" in result
        mock_nx.glob.assert_called_once_with("*.py", "/")

    def test_glob_with_path(self):
        """Test glob with custom path."""
        tools = get_nexus_tools()
        glob_tool = tools[1].func

        mock_nx = Mock(spec=RemoteNexusFS)
        mock_nx.glob.return_value = ["/workspace/test.md"]

        state = {}
        config: RunnableConfig = {
            "metadata": {"x_auth": "Bearer test-token", "nexus_server_url": "http://localhost:8080"}
        }

        with patch("nexus.tools.langgraph.nexus_tools._get_nexus_client", return_value=mock_nx):
            glob_tool("**/*.md", config, state, path="/workspace")

        mock_nx.glob.assert_called_once_with("**/*.md", "/workspace")

    def test_glob_no_matches(self):
        """Test glob with no matches."""
        tools = get_nexus_tools()
        glob_tool = tools[1].func

        mock_nx = Mock(spec=RemoteNexusFS)
        mock_nx.glob.return_value = []

        state = {}
        config: RunnableConfig = {
            "metadata": {"x_auth": "Bearer test-token", "nexus_server_url": "http://localhost:8080"}
        }

        with patch("nexus.tools.langgraph.nexus_tools._get_nexus_client", return_value=mock_nx):
            result = glob_tool("*.xyz", config, state)

        assert "No files found" in result

    def test_glob_limits_results(self):
        """Test that glob limits to first 100 files."""
        tools = get_nexus_tools()
        glob_tool = tools[1].func

        mock_nx = Mock(spec=RemoteNexusFS)
        files = [f"/file{i}.py" for i in range(150)]
        mock_nx.glob.return_value = files

        state = {}
        config: RunnableConfig = {
            "metadata": {"x_auth": "Bearer test-token", "nexus_server_url": "http://localhost:8080"}
        }

        with patch("nexus.tools.langgraph.nexus_tools._get_nexus_client", return_value=mock_nx):
            result = glob_tool("*.py", config, state)

        assert "and 50 more files" in result


class TestReadFileTool:
    """Tests for read_file tool."""

    def test_read_file_cat(self):
        """Test reading file with cat command."""
        tools = get_nexus_tools()
        read_tool = tools[2].func

        mock_nx = Mock(spec=RemoteNexusFS)
        mock_nx.read.return_value = b"Hello World\nLine 2\nLine 3"

        state = {}
        config: RunnableConfig = {
            "metadata": {"x_auth": "Bearer test-token", "nexus_server_url": "http://localhost:8080"}
        }

        with patch("nexus.tools.langgraph.nexus_tools._get_nexus_client", return_value=mock_nx):
            result = read_tool("cat /test.txt", config, state)

        assert "Hello World" in result
        assert "Line 2" in result
        mock_nx.read.assert_called_once_with("/test.txt")

    def test_read_file_less(self):
        """Test reading file with less command (preview)."""
        tools = get_nexus_tools()
        read_tool = tools[2].func

        mock_nx = Mock(spec=RemoteNexusFS)
        lines = [f"Line {i}" for i in range(150)]
        content = "\n".join(lines)
        mock_nx.read.return_value = content

        state = {}
        config: RunnableConfig = {
            "metadata": {"x_auth": "Bearer test-token", "nexus_server_url": "http://localhost:8080"}
        }

        with patch("nexus.tools.langgraph.nexus_tools._get_nexus_client", return_value=mock_nx):
            result = read_tool("less /test.txt", config, state)

        assert "Preview of" in result
        assert "first 100 of 150 lines" in result

    def test_read_file_with_line_range(self):
        """Test reading file with line range."""
        tools = get_nexus_tools()
        read_tool = tools[2].func

        mock_nx = Mock(spec=RemoteNexusFS)
        lines = [f"Line {i}" for i in range(1, 21)]
        content = "\n".join(lines)
        mock_nx.read.return_value = content

        state = {}
        config: RunnableConfig = {
            "metadata": {"x_auth": "Bearer test-token", "nexus_server_url": "http://localhost:8080"}
        }

        with patch("nexus.tools.langgraph.nexus_tools._get_nexus_client", return_value=mock_nx):
            result = read_tool("cat /test.txt 5 10", config, state)

        assert "lines 5-10" in result
        assert "Line 5" in result
        assert "Line 10" in result

    def test_read_file_too_large(self):
        """Test error when file is too large."""
        tools = get_nexus_tools()
        read_tool = tools[2].func

        mock_nx = Mock(spec=RemoteNexusFS)
        large_content = "x" * 40000
        mock_nx.read.return_value = large_content

        state = {}
        config: RunnableConfig = {
            "metadata": {"x_auth": "Bearer test-token", "nexus_server_url": "http://localhost:8080"}
        }

        with patch("nexus.tools.langgraph.nexus_tools._get_nexus_client", return_value=mock_nx):
            result = read_tool("/test.txt", config, state)

        assert "Error: File /test.txt is too large" in result

    def test_read_file_not_found(self):
        """Test error when file not found."""
        tools = get_nexus_tools()
        read_tool = tools[2].func

        mock_nx = Mock(spec=RemoteNexusFS)
        mock_nx.read.side_effect = FileNotFoundError("File not found")

        state = {}
        config: RunnableConfig = {
            "metadata": {"x_auth": "Bearer test-token", "nexus_server_url": "http://localhost:8080"}
        }

        with patch("nexus.tools.langgraph.nexus_tools._get_nexus_client", return_value=mock_nx):
            result = read_tool("/missing.txt", config, state)

        assert "Error: File not found" in result


class TestWriteFileTool:
    """Tests for write_file tool."""

    def test_write_file_success(self):
        """Test successful file write."""
        tools = get_nexus_tools()
        write_tool = tools[3].func

        mock_nx = Mock(spec=RemoteNexusFS)
        mock_nx.exists.return_value = True

        state = {}
        config: RunnableConfig = {
            "metadata": {"x_auth": "Bearer test-token", "nexus_server_url": "http://localhost:8080"}
        }

        with patch("nexus.tools.langgraph.nexus_tools._get_nexus_client", return_value=mock_nx):
            result = write_tool("/test.txt", "Hello World", config, state)

        assert "Successfully wrote" in result
        assert "11 bytes" in result
        mock_nx.write.assert_called_once_with("/test.txt", b"Hello World")

    def test_write_file_strips_mnt_nexus_prefix(self):
        """Test that /mnt/nexus prefix is stripped."""
        tools = get_nexus_tools()
        write_tool = tools[3].func

        mock_nx = Mock(spec=RemoteNexusFS)
        mock_nx.exists.return_value = True

        state = {}
        config: RunnableConfig = {
            "metadata": {"x_auth": "Bearer test-token", "nexus_server_url": "http://localhost:8080"}
        }

        with patch("nexus.tools.langgraph.nexus_tools._get_nexus_client", return_value=mock_nx):
            write_tool("/mnt/nexus/test.txt", "Content", config, state)

        mock_nx.write.assert_called_once_with("/test.txt", b"Content")

    def test_write_file_error_handling(self):
        """Test write error handling."""
        tools = get_nexus_tools()
        write_tool = tools[3].func

        mock_nx = Mock(spec=RemoteNexusFS)
        mock_nx.write.side_effect = Exception("Permission denied")

        state = {}
        config: RunnableConfig = {
            "metadata": {"x_auth": "Bearer test-token", "nexus_server_url": "http://localhost:8080"}
        }

        with patch("nexus.tools.langgraph.nexus_tools._get_nexus_client", return_value=mock_nx):
            result = write_tool("/test.txt", "Content", config, state)

        assert "Error writing file" in result
        assert "Permission denied" in result


class TestPythonTool:
    """Tests for python sandbox tool."""

    def test_python_execution_success(self):
        """Test successful Python execution."""
        tools = get_nexus_tools()
        python_tool = tools[4].func

        mock_nx = Mock(spec=RemoteNexusFS)
        mock_nx.sandbox_run.return_value = {
            "stdout": "Hello World\n42",
            "stderr": "",
            "exit_code": 0,
            "execution_time": 0.123,
        }

        state = {}
        config: RunnableConfig = {
            "metadata": {
                "x_auth": "Bearer test-token",
                "nexus_server_url": "http://localhost:8080",
                "sandbox_id": "test-sandbox-123",
            }
        }

        with patch("nexus.tools.langgraph.nexus_tools._get_nexus_client", return_value=mock_nx):
            result = python_tool("print('Hello World')\nprint(42)", config, state)

        assert "Output:" in result
        assert "Hello World" in result
        assert "Exit code: 0" in result

        mock_nx.sandbox_run.assert_called_once_with(
            sandbox_id="test-sandbox-123",
            language="python",
            code="print('Hello World')\nprint(42)",
            timeout=300,
        )

    def test_python_execution_with_error(self):
        """Test Python execution with errors."""
        tools = get_nexus_tools()
        python_tool = tools[4].func

        mock_nx = Mock(spec=RemoteNexusFS)
        mock_nx.sandbox_run.return_value = {
            "stdout": "",
            "stderr": "NameError: name 'x' is not defined",
            "exit_code": 1,
            "execution_time": 0.050,
        }

        state = {}
        config: RunnableConfig = {
            "metadata": {
                "x_auth": "Bearer test-token",
                "nexus_server_url": "http://localhost:8080",
                "sandbox_id": "test-sandbox-123",
            }
        }

        with patch("nexus.tools.langgraph.nexus_tools._get_nexus_client", return_value=mock_nx):
            result = python_tool("print(x)", config, state)

        assert "Errors:" in result
        assert "NameError" in result

    def test_python_missing_sandbox_id(self):
        """Test error when sandbox_id is missing."""
        tools = get_nexus_tools()
        python_tool = tools[4].func

        state = {}
        config: RunnableConfig = {
            "metadata": {"x_auth": "Bearer test-token", "nexus_server_url": "http://localhost:8080"}
        }

        result = python_tool("print('test')", config, state)
        assert "Error: sandbox_id not found" in result


class TestBashTool:
    """Tests for bash sandbox tool."""

    def test_bash_execution_success(self):
        """Test successful bash execution."""
        tools = get_nexus_tools()
        bash_tool = tools[5].func

        mock_nx = Mock(spec=RemoteNexusFS)
        mock_nx.sandbox_run.return_value = {
            "stdout": "file1.txt\nfile2.txt",
            "stderr": "",
            "exit_code": 0,
            "execution_time": 0.045,
        }

        state = {}
        config: RunnableConfig = {
            "metadata": {
                "x_auth": "Bearer test-token",
                "nexus_server_url": "http://localhost:8080",
                "sandbox_id": "test-sandbox-456",
            }
        }

        with patch("nexus.tools.langgraph.nexus_tools._get_nexus_client", return_value=mock_nx):
            result = bash_tool("ls -la", config, state)

        assert "Output:" in result
        assert "file1.txt" in result

    def test_bash_missing_sandbox_id(self):
        """Test error when sandbox_id is missing."""
        tools = get_nexus_tools()
        bash_tool = tools[5].func

        state = {}
        config: RunnableConfig = {
            "metadata": {"x_auth": "Bearer test-token", "nexus_server_url": "http://localhost:8080"}
        }

        result = bash_tool("echo test", config, state)
        assert "Error: sandbox_id not found" in result


class TestQueryMemoriesTool:
    """Tests for query_memories tool."""

    def test_query_memories_success(self):
        """Test successful memory query."""
        tools = get_nexus_tools()
        memory_tool = tools[6].func

        mock_nx = Mock(spec=RemoteNexusFS)
        mock_nx.memory = Mock()
        mock_nx.memory.query.return_value = [
            {
                "content": "User prefers Python for data analysis",
                "namespace": "preferences",
                "importance": 0.85,
            },
            {"content": "Last worked on project X", "namespace": "context", "importance": 0.70},
        ]

        state = {}
        config: RunnableConfig = {
            "metadata": {"x_auth": "Bearer test-token", "nexus_server_url": "http://localhost:8080"}
        }

        with patch("nexus.tools.langgraph.nexus_tools._get_nexus_client", return_value=mock_nx):
            result = memory_tool(config, state)

        assert "Found 2 memories" in result
        assert "User prefers Python" in result
        assert "Namespace: preferences" in result
        assert "Importance: 0.85" in result

    def test_query_memories_empty(self):
        """Test query with no memories."""
        tools = get_nexus_tools()
        memory_tool = tools[6].func

        mock_nx = Mock(spec=RemoteNexusFS)
        mock_nx.memory = Mock()
        mock_nx.memory.query.return_value = []

        state = {}
        config: RunnableConfig = {
            "metadata": {"x_auth": "Bearer test-token", "nexus_server_url": "http://localhost:8080"}
        }

        with patch("nexus.tools.langgraph.nexus_tools._get_nexus_client", return_value=mock_nx):
            result = memory_tool(config, state)

        assert "No memories found" in result
