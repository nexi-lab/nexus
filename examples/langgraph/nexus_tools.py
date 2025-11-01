"""Nexus File Operation Tools for LangGraph ReAct Agent.

This module provides six practical file operation tools that wrap Nexus filesystem
capabilities for use with LangGraph agents. Tools use familiar command-line syntax
to make them intuitive for agents to use.

1. grep_files: Search file content using grep-style commands
2. glob_files: Find files by name pattern using glob syntax
3. read_file: Read file content using cat/less-style commands
4. write_file: Write content to Nexus filesystem
5. execute_python: Execute Python code in an isolated E2B sandbox
6. execute_bash: Execute bash commands in an isolated E2B sandbox

These tools enable agents to interact with a remote Nexus filesystem, allowing them
to search, read, analyze, and persist data across agent runs.
"""

import shlex
import logging
import asyncio
from typing import Annotated, Any, Tuple, TYPE_CHECKING

from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig
from langgraph.prebuilt import InjectedStore



if TYPE_CHECKING:
    from e2b import Sandbox

try:
    from e2b import Sandbox
    E2B_AVAILABLE = True
except ImportError:
    E2B_AVAILABLE = False
    AsyncSandbox = Any  # type: ignore
    Execution = Any  # type: ignore
    logging.warning("e2b_code_interpreter not available. execute_python and execute_bash tools will not work.")


# E2B Sandbox Configuration
# _TEMPLATE_ID = "dmyd7e9m4ukc19m397jm"  # Default E2B template with common data science libraries
_TEMPLATE_ID = "7ebpm01g5wtzdvlf75lx"  # Default E2B template with common data science libraries


# Helper functions for execute_python tool
def _get_executor_id_from_config(config: RunnableConfig) -> str:
    """Get executor ID from thread_id in config."""
    # Try metadata first (custom thread_id), fallback to config thread_id
    thread_id = config.get("metadata", {}).get("thread_id") or config.get("thread_id")
    if not thread_id:
        raise ValueError("No thread_id found in config")
    return str(thread_id)


async def _wait_for_sandbox_ready(sandbox: Any, max_retries: int = 5, delay: float = 2.0) -> bool:
    """Wait for sandbox to be fully ready by attempting a simple execution with retries."""
    for attempt in range(max_retries):
        try:
            # Try a simple no-op command to check if Jupyter kernel is ready
            await sandbox.run_code("print('ready')")
            logging.info(f"Sandbox is ready after {attempt + 1} attempt(s)")
            return True
        except Exception as e:
            if "port is not open" in str(e) or "502" in str(e):
                if attempt < max_retries - 1:
                    logging.info(f"Sandbox not ready yet (attempt {attempt + 1}/{max_retries}), waiting {delay}s...")
                    await asyncio.sleep(delay)
                else:
                    logging.error(f"Sandbox failed to become ready after {max_retries} attempts: {e}")
                    return False
            else:
                # Different error, re-raise
                raise
    return False


async def _get_or_create_sandbox(
    store: Any,
    executor_id: str,
    timeout: int = 3000,
) -> Tuple[AsyncSandbox, bool]:
    """Get existing sandbox or create new one. Returns (sandbox, is_new)."""
    # Hardcoded sandbox for testing
    sandbox = await AsyncSandbox.connect(sandbox_id="iqa2iva7uwncrr9ufaqe4")

    create_new = False
    return sandbox, create_new


def _build_execution_response(execution: Execution) -> str:
    """Build formatted response from execution result."""
    response = "Code execution result:\n"

    if execution.logs.stdout:
        response += f"Stdout:\n{execution.logs.stdout}\n"
    if execution.logs.stderr:
        response += f"Stderr:\n{execution.logs.stderr}\n"
    if execution.error:
        response += f"Error:\n{execution.error}"

    return response


def _build_bash_response(exit_code: int, stdout: str, stderr: str) -> str:
    """Build formatted response from bash execution result."""
    response = f"Bash execution result (exit code: {exit_code}):\n"

    if stdout:
        response += f"Stdout:\n{stdout}\n"
    if stderr:
        response += f"Stderr:\n{stderr}\n"
    if exit_code != 0:
        response += f"\nCommand failed with exit code {exit_code}"

    return response


def get_nexus_tools(nx):
    """
    Create LangGraph tools from a Nexus filesystem instance.

    Args:
        nx: NexusFilesystem instance (local or remote)

    Returns:
        List of LangGraph tool functions
    """

    @tool
    def grep_files(grep_cmd: str) -> str:
        """Search file content using grep-style commands.

        Use this tool to find files containing specific text or code patterns.
        Follows grep command syntax for familiarity.

        Args:
            grep_cmd: Grep command in format: "pattern [path] [options]"
                     - pattern: Required. Text or regex to search for (quote if contains spaces)
                     - path: Optional. Directory to search (default: "/")
                     - Options: -i (case insensitive)

                     Examples:
                     - "async def /workspace"
                     - "'import pandas' /scripts -i"
                     - "TODO:"
                     - "function.*calculate /src"

        Returns:
            String describing matches found, including file paths, line numbers, and content.
            Returns "No matches found" if pattern doesn't match anything.

        Examples:
            - grep_files("async def /workspace") → Find all async function definitions
            - grep_files("TODO:") → Find all TODO comments in entire filesystem
            - grep_files("'import pandas' /scripts -i") → Case-insensitive pandas imports
        """
        try:
            # Parse grep command
            parts = shlex.split(grep_cmd)
            if not parts:
                return "Error: Empty grep command. Usage: grep_files('pattern [path] [options]')"

            pattern = parts[0]
            path = "/"
            case_sensitive = True

            # Parse remaining arguments
            i = 1
            while i < len(parts):
                arg = parts[i]
                if arg == "-i":
                    case_sensitive = False
                elif not arg.startswith("-"):
                    # Assume it's a path
                    path = arg
                i += 1

            # Execute grep
            results = nx.grep(pattern, path, ignore_case=not case_sensitive)

            if not results:
                return f"No matches found for pattern '{pattern}' in {path}"

            # Format results into readable output
            output_lines = [f"Found {len(results)} matches for pattern '{pattern}' in {path}:\n"]

            # Group by file for better readability
            current_file = None
            for match in results[:50]:  # Limit to first 50 matches
                file_path = match.get("file", "unknown")
                line_num = match.get("line", 0)
                content = match.get("content", "").strip()

                if file_path != current_file:
                    output_lines.append(f"\n{file_path}:")
                    current_file = file_path

                output_lines.append(f"  Line {line_num}: {content}")

            if len(results) > 50:
                output_lines.append(f"\n... and {len(results) - 50} more matches")

            return "\n".join(output_lines)

        except Exception as e:
            return f"Error executing grep: {str(e)}\nUsage: grep_files('pattern [path] [options]')"

    @tool
    def glob_files(pattern: str, path: str = "/") -> str:
        """Find files by name pattern using glob syntax.

        Use this tool to find files matching a specific naming pattern.
        Supports standard glob patterns like wildcards and recursive search.

        Args:
            pattern: Glob pattern to match filenames (e.g., "*.py", "**/*.md", "test_*.py")
            path: Directory path to search in (default: "/" for entire filesystem)

        Returns:
            String listing all matching file paths, one per line.
            Returns "No files found" if no matches.

        Examples:
            - glob_files("*.py", "/workspace") → Find all Python files
            - glob_files("**/*.md", "/docs") → Find all Markdown files recursively
            - glob_files("test_*.py", "/tests") → Find all test files
        """
        try:
            files = nx.glob(pattern, path)

            if not files:
                return f"No files found matching pattern '{pattern}' in {path}"

            # Format results
            output_lines = [f"Found {len(files)} files matching '{pattern}' in {path}:\n"]
            output_lines.extend(f"  {file}" for file in files[:100])  # Limit to first 100

            if len(files) > 100:
                output_lines.append(f"\n... and {len(files) - 100} more files")

            return "\n".join(output_lines)

        except Exception as e:
            return f"Error finding files: {str(e)}"

    @tool
    def read_file(read_cmd: str) -> str:
        """Read file content using cat/less-style commands.

        Use this tool to read and analyze file contents.
        Works with text files including code, documentation, and data files.
        Supports both 'cat' (full content) and 'less' (preview) commands.

        Args:
            read_cmd: Read command in format: "[cat|less] path"
                     - cat: Display entire file content
                     - less: Display first 100 lines as preview
                     - path: File path to read

                     Examples:
                     - "cat /workspace/README.md"
                     - "less /scripts/analysis.py"
                     - "/data/results.json" (defaults to cat)

        Returns:
            File content as string, or error message if file cannot be read.

        Examples:
            - read_file("cat /workspace/README.md") → Read entire README
            - read_file("less /scripts/large_file.py") → Preview first 100 lines
            - read_file("/data/results.json") → Read JSON file (defaults to cat)
        """
        try:
            # Parse read command
            parts = shlex.split(read_cmd.strip())
            if not parts:
                return "Error: Empty read command. Usage: read_file('[cat|less] path')"

            # Determine command type and path
            if parts[0] in ["cat", "less"]:
                command = parts[0]
                if len(parts) < 2:
                    return f"Error: Missing file path. Usage: read_file('{command} path')"
                path = parts[1]
            else:
                # Default to cat if no command specified
                command = "cat"
                path = parts[0]

            # Read file content
            content = nx.read(path)

            # Handle bytes
            if isinstance(content, bytes):
                content = content.decode("utf-8")

            # For 'less', show preview
            if command == "less":
                lines = content.split("\n")
                if len(lines) > 100:
                    preview_content = "\n".join(lines[:100])
                    output = f"Preview of {path} (first 100 of {len(lines)} lines):\n\n"
                    output += preview_content
                    output += f"\n\n... ({len(lines) - 100} more lines)"
                else:
                    output = f"Content of {path} ({len(lines)} lines):\n\n"
                    output += content
            else:
                # For 'cat', show full content
                output = f"Content of {path} ({len(content)} characters):\n\n"
                output += content

            return output

        except FileNotFoundError:
            return f"Error: File not found: {read_cmd}"
        except Exception as e:
            return f"Error reading file: {str(e)}\nUsage: read_file('[cat|less] path')"

    @tool
    def write_file(path: str, content: str) -> str:
        """Write content to Nexus filesystem.

        Use this tool to save analysis results, reports, or generated content.
        Creates parent directories automatically if they don't exist.
        Overwrites existing files.

        Args:
            path: Absolute path where file should be written (e.g., "/reports/summary.md")
            content: Text content to write to the file

        Returns:
            Success message with file path and size, or error message if write fails.

        Examples:
            - write_file("/reports/summary.md", "# Summary\\n...") → Save analysis report
            - write_file("/workspace/config.json", "{}") → Create config file
            - write_file("/data/results.txt", "Results:\\n...") → Save results
        """
        try:
            # Convert string to bytes for Nexus
            content_bytes = content.encode("utf-8") if isinstance(content, str) else content

            # Write file (Nexus creates parent directories automatically)
            nx.write(path, content_bytes)

            # Verify write was successful
            if nx.exists(path):
                size = len(content_bytes)
                return f"Successfully wrote {size} bytes to {path}"
            else:
                return f"Error: Failed to write file {path} (file does not exist after write)"

        except Exception as e:
            return f"Error writing file {path}: {str(e)}"

    @tool
    async def execute_python(
        code: str,
        config: RunnableConfig,
        store: Annotated[Any, InjectedStore]
    ) -> str:
        """Execute Python code in an isolated E2B sandbox environment.

        Use this tool to run Python code for data analysis, calculations, or testing.
        The sandbox has common data science libraries pre-installed (pandas, numpy,
        matplotlib, openpyxl, etc.). Working directory: /home/user

        Important notes:
        - Previous code execution state is preserved in the same sandbox (incremental work)
        - For plots: Always save to file (plt.savefig), don't use plt.show()
        - The sandbox persists across multiple tool calls in the same conversation thread

        Args:
            code: Python code to execute (as a string)
            config: RunnableConfig (automatically injected by LangGraph)
            store: Store for persisting sandbox state (automatically injected by LangGraph)

        Returns:
            String containing stdout, stderr, and any error messages from code execution.

        Examples:
            - execute_python("print('Hello, world!')") → Simple print statement
            - execute_python("import pandas as pd\\ndf = pd.DataFrame({'a': [1,2,3]})\\nprint(df)") → Use pandas
            - execute_python("import matplotlib.pyplot as plt\\nplt.plot([1,2,3])\\nplt.savefig('plot.png')") → Create plot
        """
        if not E2B_AVAILABLE:
            return "Error: e2b_code_interpreter is not installed. Install with: pip install e2b-code-interpreter"

        try:
            # Get executor ID from config
            executor_id = _get_executor_id_from_config(config)
            logging.info(f"Executor ID: {executor_id}")

            # Get or create sandbox
            sandbox, is_new_sandbox = await _get_or_create_sandbox(store, executor_id)

            # Execute code
            execution = await sandbox.run_code(code)

            # Build response from execution result
            response = _build_execution_response(execution)

            return response

        except Exception as e:
            logging.error(f"Error executing Python code: {e}")
            return f"Error executing Python code: {str(e)}"

    @tool
    async def execute_bash(
        command: str,
        config: RunnableConfig,
        store: Annotated[Any, InjectedStore]
    ) -> str:
        """Execute bash commands in an isolated E2B sandbox environment.

        Use this tool to run shell commands for file operations, system tasks, or
        command-line tools. The sandbox provides a full Linux environment with
        common utilities installed. Working directory: /home/user

        Important notes:
        - Uses the SAME sandbox as execute_python (state is shared)
        - Previous commands and Python code state are preserved
        - Commands run in /home/user directory
        - Common Linux utilities are available (git, curl, wget, etc.)

        Args:
            command: Bash command to execute (as a string)
            config: RunnableConfig (automatically injected by LangGraph)
            store: Store for persisting sandbox state (automatically injected by LangGraph)

        Returns:
            String containing stdout, stderr, exit code from command execution.

        Examples:
            - execute_bash("ls -la") → List files in current directory
            - execute_bash("cat myfile.txt") → Read file contents
            - execute_bash("wget https://example.com/data.csv") → Download file
            - execute_bash("git clone https://github.com/user/repo.git") → Clone repository
        """
        if not E2B_AVAILABLE:
            return "Error: e2b_code_interpreter is not installed. Install with: pip install e2b-code-interpreter"

        try:
            # Get executor ID from config
            executor_id = _get_executor_id_from_config(config)
            logging.info(f"Executor ID: {executor_id}")

            # Get or create sandbox (same one used by execute_python)
            sandbox, is_new_sandbox = await _get_or_create_sandbox(store, executor_id)
            logging.info(f"Sandbox: {sandbox}")
            logging.info(f"Is running: {await sandbox.is_running()}")

            
            # Execute bash command using process API
            execution = await sandbox.run_code(
                code=f"bash -c {shlex.quote(command)}",
                language="bash",
            )
            import pdb; pdb.set_trace()

            # Get execution result
            response = execution.text

            return response

        except Exception as e:
            logging.error(f"Error executing bash command: {e}")
            return f"Error executing bash command: {str(e)}"

    # Return all tools
    return [grep_files, glob_files, read_file, write_file, execute_python, execute_bash]
