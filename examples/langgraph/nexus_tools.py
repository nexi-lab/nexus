"""Nexus File Operation Tools for LangGraph ReAct Agent.

This module provides file operation tools and Nexus sandbox tools that wrap Nexus filesystem
capabilities for use with LangGraph agents. Tools use familiar command-line syntax
to make them intuitive for agents to use.

Nexus Tools:
1. grep_files: Search file content using grep-style commands
2. glob_files: Find files by name pattern using glob syntax
3. read_file: Read file content using cat/less-style commands
4. write_file: Write content to Nexus filesystem
5. python: Execute Python code in Nexus-managed sandbox
6. bash: Execute bash commands in Nexus-managed sandbox

These tools enable agents to interact with a remote Nexus filesystem and execute
code in isolated Nexus-managed sandboxes, allowing them to search, read, analyze, persist
data, and run code across agent runs.

Authentication:
    API key is REQUIRED via metadata.x_auth: "Bearer <token>"
    Frontend automatically passes the authenticated user's API key in request metadata.
    Each tool creates an authenticated RemoteNexusFS instance using the extracted token.
"""

import shlex

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from nexus.remote import RemoteNexusFS


def get_nexus_tools():
    """
    Create LangGraph tools that connect to Nexus server with per-request authentication.

    Args:
        server_url: Nexus server URL (e.g., "http://localhost:8080" or ngrok URL)

    Returns:
        List of LangGraph tool functions that require x_auth in metadata

    Usage:
        tools = get_nexus_tools("http://localhost:8080")
        agent = create_react_agent(model=llm, tools=tools)

        # Frontend passes API key in metadata:
        result = agent.invoke(
            {"messages": [{"role": "user", "content": "Find Python files"}]},
            metadata={"x_auth": "Bearer sk-your-api-key"}
        )
    """

    def _get_nexus_client(config: RunnableConfig) -> RemoteNexusFS:
        """Create authenticated RemoteNexusFS from config.

        Requires authentication via metadata.x_auth: "Bearer <token>"
        """
        # Get API key from metadata.x_auth (required)
        metadata = config.get("metadata", {})
        x_auth = metadata.get("x_auth", "")
        server_url = metadata.get("nexus_server_url", "")

        if not x_auth:
            raise ValueError(
                "Missing x_auth in metadata. "
                "Frontend must pass API key via metadata: {'x_auth': 'Bearer <token>'}"
            )

        # Strip "Bearer " prefix if present
        api_key = x_auth.removeprefix("Bearer ").strip()

        if not api_key:
            raise ValueError("Invalid x_auth format. Expected 'Bearer <token>', got: " + x_auth)

        return RemoteNexusFS(server_url=server_url, api_key=api_key)

    @tool
    def grep_files(grep_cmd: str, config: RunnableConfig) -> str:
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
            # Get authenticated client
            nx = _get_nexus_client(config)

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
    def glob_files(pattern: str, config: RunnableConfig, path: str = "/") -> str:
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
            # Get authenticated client
            nx = _get_nexus_client(config)

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
    def read_file(read_cmd: str, config: RunnableConfig) -> str:
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
            # Get authenticated client
            nx = _get_nexus_client(config)

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
    def write_file(path: str, content: str, config: RunnableConfig) -> str:
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
            # Get authenticated client
            nx = _get_nexus_client(config)

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

    # Nexus Sandbox Tools
    @tool
    def python(code: str, config: RunnableConfig) -> str:
        """Execute Python code in a Nexus-managed sandbox.

        Use this tool to run Python code for data analysis, calculations, file processing,
        or any other computational tasks. The code runs in an isolated Nexus-managed sandbox.

        Args:
            code: Python code to execute. Can be multiple lines.
                  Use print() to see output.

        Returns:
            Execution results including:
            - Standard output (stdout)
            - Standard error (stderr) if any
            - Exit code
            - Execution time

        Examples:
            - python("print('Hello from Nexus')") → Execute simple print
            - python("import pandas as pd\\ndf = pd.DataFrame({'a': [1,2,3]})\\nprint(df)")
            - python("result = 2 + 2\\nprint(f'Result: {result}')")
        """
        try:
            nx = _get_nexus_client(config)

            # Get sandbox_id from metadata
            metadata = config.get("metadata", {})
            sandbox_id = metadata.get("sandbox_id")

            if not sandbox_id:
                return "Error: sandbox_id not found in metadata. Please start a sandbox first."

            # Execute Python code in sandbox
            result = nx.sandbox_run(sandbox_id=sandbox_id, language="python", code=code, timeout=30)

            # Format output
            output_parts = []

            # Add stdout
            stdout = result.get("stdout", "").strip()
            if stdout:
                output_parts.append(f"Output:\n{stdout}")

            # Add stderr
            stderr = result.get("stderr", "").strip()
            if stderr:
                output_parts.append(f"Errors:\n{stderr}")

            # Add execution info
            exit_code = result.get("exit_code", -1)
            exec_time = result.get("execution_time", 0)
            output_parts.append(f"Exit code: {exit_code}")
            output_parts.append(f"Execution time: {exec_time:.3f}s")

            if not output_parts:
                return "Code executed successfully (no output)"

            return "\n\n".join(output_parts)

        except Exception as e:
            return f"Error executing Python code: {str(e)}"

    @tool
    def bash(command: str, config: RunnableConfig) -> str:
        """Execute bash commands in a Nexus-managed sandbox.

        Use this tool to run shell commands like file operations, system commands,
        or CLI tool executions. Commands run in an isolated Nexus-managed sandbox.

        Args:
            command: Bash command to execute. Can include pipes, redirects, etc.

        Returns:
            Command execution results including:
            - Standard output (stdout)
            - Standard error (stderr) if any
            - Exit code
            - Execution time

        Examples:
            - bash("ls -la") → List files in current directory
            - bash("echo 'Hello World'") → Print text
            - bash("cat file.txt | grep pattern") → Search file content
            - bash("python script.py") → Run a Python script

        Note: Commands run in the sandbox's working directory.
              File system changes persist between calls in the same session.
        """
        try:
            nx = _get_nexus_client(config)

            # Get sandbox_id from metadata
            metadata = config.get("metadata", {})
            sandbox_id = metadata.get("sandbox_id")

            if not sandbox_id:
                return "Error: sandbox_id not found in metadata. Please start a sandbox first."

            # Execute bash command in sandbox
            result = nx.sandbox_run(
                sandbox_id=sandbox_id, language="bash", code=command, timeout=30
            )

            # Format output
            output_parts = []

            # Add stdout
            stdout = result.get("stdout", "").strip()
            if stdout:
                output_parts.append(f"Output:\n{stdout}")

            # Add stderr
            stderr = result.get("stderr", "").strip()
            if stderr:
                output_parts.append(f"Errors:\n{stderr}")

            # Add execution info
            exit_code = result.get("exit_code", -1)
            exec_time = result.get("execution_time", 0)
            output_parts.append(f"Exit code: {exit_code}")
            output_parts.append(f"Execution time: {exec_time:.3f}s")

            if not output_parts:
                return "Command executed successfully (no output)"

            return "\n\n".join(output_parts)

        except Exception as e:
            return f"Error executing bash command: {str(e)}"

    # Return all tools
    tools = [grep_files, glob_files, read_file, write_file, python, bash]

    return tools
