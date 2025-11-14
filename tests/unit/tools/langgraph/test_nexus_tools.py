"""Unit tests for Nexus LangGraph tools."""

from unittest.mock import Mock, patch

import pytest

# Skip all tests if langchain_core is not installed
pytest.importorskip("langchain_core")
pytest.importorskip("langgraph")

from langchain_core.runnables import RunnableConfig

from nexus.remote import RemoteNexusFS
from nexus.tools.langgraph.nexus_tools import NexusAgentState, get_nexus_tools


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

        with patch("nexus.tools.langgraph.nexus_tools.RemoteNexusFS", return_value=mock_nx):
            result = grep_tool('"async def"', state, config)

        assert "/test.py:10:async def test():" in result
        assert "/main.py:5:async def main():" in result
        mock_nx.grep.assert_called_once_with("async def", "/", ignore_case=False)

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

        with patch("nexus.tools.langgraph.nexus_tools.RemoteNexusFS", return_value=mock_nx):
            result = grep_tool("pattern /workspace", state, config)

        mock_nx.grep.assert_called_once_with("pattern", "/workspace", ignore_case=False)
        assert "No matches found" in result

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

        with patch("nexus.tools.langgraph.nexus_tools.RemoteNexusFS", return_value=mock_nx):
            grep_tool("pattern -i", state, config)

        mock_nx.grep.assert_called_once_with("pattern", "/", ignore_case=True)

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

        with patch(
            "nexus.tools.langgraph.nexus_tools.RemoteNexusFS", return_value=mock_nx
        ) as mock_fs_class:
            grep_tool("test", state, config)

        # Should use state context
        mock_fs_class.assert_called_once_with(
            server_url="http://localhost:9090", api_key="state-token"
        )

    def test_grep_missing_auth(self):
        """Test error when auth is missing."""
        tools = get_nexus_tools()
        grep_tool = tools[0].func

        state = {}
        config: RunnableConfig = {"metadata": {}}

        result = grep_tool("test", state, config)
        assert "Missing x_auth" in result

    def test_grep_invalid_auth_format(self):
        """Test error with invalid auth format."""
        tools = get_nexus_tools()
        grep_tool = tools[0].func

        state = {}
        config: RunnableConfig = {
            "metadata": {"x_auth": "Bearer ", "nexus_server_url": "http://localhost:8080"}
        }

        result = grep_tool("test", state, config)
        assert "Invalid x_auth format" in result

    def test_grep_empty_command(self):
        """Test grep with empty command."""
        tools = get_nexus_tools()
        grep_tool = tools[0].func

        state = {}
        config: RunnableConfig = {
            "metadata": {"x_auth": "Bearer test-token", "nexus_server_url": "http://localhost:8080"}
        }

        result = grep_tool("", state, config)
        assert "Error: Empty grep command" in result

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

        with patch("nexus.tools.langgraph.nexus_tools.RemoteNexusFS", return_value=mock_nx):
            result = grep_tool("pattern", state, config)

        assert "..." in result
        assert len(result) < len(long_content)

    def test_grep_limits_results(self):
        """Test that grep limits to first 50 matches."""
        tools = get_nexus_tools()
        grep_tool = tools[0].func

        mock_nx = Mock(spec=RemoteNexusFS)
        matches = [{"file": f"/file{i}.py", "line": i, "content": "match"} for i in range(100)]
        mock_nx.grep.return_value = matches

        state = {}
        config: RunnableConfig = {
            "metadata": {"x_auth": "Bearer test-token", "nexus_server_url": "http://localhost:8080"}
        }

        with patch("nexus.tools.langgraph.nexus_tools.RemoteNexusFS", return_value=mock_nx):
            result = grep_tool("pattern", state, config)

        assert "and 50 more matches" in result


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

        with patch("nexus.tools.langgraph.nexus_tools.RemoteNexusFS", return_value=mock_nx):
            result = glob_tool("*.py", state, config)

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

        with patch("nexus.tools.langgraph.nexus_tools.RemoteNexusFS", return_value=mock_nx):
            glob_tool("**/*.md", state, config, path="/workspace")

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

        with patch("nexus.tools.langgraph.nexus_tools.RemoteNexusFS", return_value=mock_nx):
            result = glob_tool("*.xyz", state, config)

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

        with patch("nexus.tools.langgraph.nexus_tools.RemoteNexusFS", return_value=mock_nx):
            result = glob_tool("*.py", state, config)

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

        with patch("nexus.tools.langgraph.nexus_tools.RemoteNexusFS", return_value=mock_nx):
            result = read_tool("cat /test.txt", state, config)

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

        with patch("nexus.tools.langgraph.nexus_tools.RemoteNexusFS", return_value=mock_nx):
            result = read_tool("less /test.txt", state, config)

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

        with patch("nexus.tools.langgraph.nexus_tools.RemoteNexusFS", return_value=mock_nx):
            result = read_tool("cat /test.txt 5 10", state, config)

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

        with patch("nexus.tools.langgraph.nexus_tools.RemoteNexusFS", return_value=mock_nx):
            result = read_tool("/test.txt", state, config)

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

        with patch("nexus.tools.langgraph.nexus_tools.RemoteNexusFS", return_value=mock_nx):
            result = read_tool("/missing.txt", state, config)

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

        with patch("nexus.tools.langgraph.nexus_tools.RemoteNexusFS", return_value=mock_nx):
            result = write_tool("/test.txt", "Hello World", state, config)

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

        with patch("nexus.tools.langgraph.nexus_tools.RemoteNexusFS", return_value=mock_nx):
            write_tool("/mnt/nexus/test.txt", "Content", state, config)

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

        with patch("nexus.tools.langgraph.nexus_tools.RemoteNexusFS", return_value=mock_nx):
            result = write_tool("/test.txt", "Content", state, config)

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

        with patch("nexus.tools.langgraph.nexus_tools.RemoteNexusFS", return_value=mock_nx):
            result = python_tool("print('Hello World')\nprint(42)", state, config)

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

        with patch("nexus.tools.langgraph.nexus_tools.RemoteNexusFS", return_value=mock_nx):
            result = python_tool("print(x)", state, config)

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

        result = python_tool("print('test')", state, config)
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

        with patch("nexus.tools.langgraph.nexus_tools.RemoteNexusFS", return_value=mock_nx):
            result = bash_tool("ls -la", state, config)

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

        result = bash_tool("echo test", state, config)
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

        with patch("nexus.tools.langgraph.nexus_tools.RemoteNexusFS", return_value=mock_nx):
            result = memory_tool(state, config)

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

        with patch("nexus.tools.langgraph.nexus_tools.RemoteNexusFS", return_value=mock_nx):
            result = memory_tool(state, config)

        assert "No memories found" in result


class TestNexusAgentState:
    """Tests for NexusAgentState."""

    def test_state_has_context_field(self):
        """Test that NexusAgentState accepts context field."""
        state: NexusAgentState = {
            "messages": [],
            "context": {"x_auth": "Bearer token", "nexus_server_url": "http://localhost:8080"},
        }

        assert "context" in state
        assert state["context"]["x_auth"] == "Bearer token"

    def test_state_context_optional(self):
        """Test that context field is optional."""
        state: NexusAgentState = {"messages": []}

        # Should not raise error
        assert "messages" in state
