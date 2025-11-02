"""Nexus File Operation Tools for LangGraph ReAct Agent.

This module provides file operation tools and E2B sandbox tools that wrap Nexus filesystem
capabilities for use with LangGraph agents. Tools use familiar command-line syntax
to make them intuitive for agents to use.

Nexus Tools:
1. grep_files: Search file content using grep-style commands
2. glob_files: Find files by name pattern using glob syntax
3. read_file: Read file content using cat/less-style commands
4. write_file: Write content to Nexus filesystem

E2B Sandbox Tools:
5. python: Execute Python code in Jupyter notebook environment
6. bash: Execute bash commands in E2B sandbox
7. mount_nexus: Mount Nexus filesystem inside E2B sandbox

These tools enable agents to interact with a remote Nexus filesystem and execute
code in isolated cloud sandboxes, allowing them to search, read, analyze, persist
data, and run code across agent runs.

Authentication:
    API key is REQUIRED via metadata.x_auth: "Bearer <token>"
    Frontend automatically passes the authenticated user's API key in request metadata.
    Each tool creates an authenticated RemoteNexusFS instance using the extracted token.
"""

import contextlib
import os
import shlex

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from nexus.remote import RemoteNexusFS

try:
    from e2b_code_interpreter import Sandbox

    E2B_AVAILABLE = True
except ImportError:
    E2B_AVAILABLE = False
    print("Warning: e2b_code_interpreter not available. E2B sandbox tools will be disabled.")
    print("Install with: pip install e2b-code-interpreter")


# Global sandbox instance (reused across requests)
_sandbox_instance: Sandbox | None = None


def get_nexus_tools(server_url: str):
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

    def _get_sandbox() -> Sandbox:
        """Get or create E2B sandbox instance.

        Reuses the same sandbox across requests for better performance.
        """
        global _sandbox_instance

        if not E2B_AVAILABLE:
            raise RuntimeError(
                "E2B sandbox not available. Install with: pip install e2b-code-interpreter"
            )

        # Get template ID from environment
        template_id = os.getenv("E2B_TEMPLATE_ID")
        if not template_id:
            raise ValueError(
                "E2B_TEMPLATE_ID not set in environment. "
                "Run demo.sh with --start_sandbox to set up E2B template."
            )

        # Check if existing sandbox is still alive
        if _sandbox_instance is not None:
            try:
                # Try a simple operation to check if sandbox is alive
                _sandbox_instance.commands.run("echo test", timeout=5)
                return _sandbox_instance
            except Exception:
                # Sandbox is dead, will create a new one
                with contextlib.suppress(Exception):
                    _sandbox_instance.kill()
                _sandbox_instance = None

        # Create new sandbox
        print(f"Creating E2B sandbox with template: {template_id}")
        _sandbox_instance = Sandbox.create(template=template_id, timeout=3000)
        print(f"E2B sandbox created: {_sandbox_instance.sandbox_id}")

        return _sandbox_instance

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

    # E2B Sandbox Tools
    @tool
    def python(code: str) -> str:
        """Execute Python code in a Jupyter notebook environment.

        Use this tool to run Python code for data analysis, calculations, file processing,
        or any other computational tasks. The code runs in an isolated E2B cloud sandbox
        with common data science libraries pre-installed (pandas, numpy, matplotlib, etc.).

        The sandbox maintains state between calls, so variables and imports persist
        across multiple executions within the same session.

        Args:
            code: Python code to execute. Can be multiple lines.
                  Use print() to see output.
                  Can use matplotlib, pandas, numpy, etc.

        Returns:
            Execution results including:
            - Standard output (stdout)
            - Standard error (stderr) if any
            - Error information if execution failed
            - Text representation of any displayed results

        Examples:
            - python("print('Hello from E2B')") → Execute simple print
            - python("import pandas as pd\\ndf = pd.DataFrame({'a': [1,2,3]})\\nprint(df)")
            - python("import matplotlib.pyplot as plt\\nplt.plot([1,2,3])\\nplt.savefig('plot.png')")

        Note: State persists between calls in the same session. Variables defined
              in one call will be available in subsequent calls.
        """
        if not E2B_AVAILABLE:
            return (
                "Error: E2B sandbox not available. Install with: pip install e2b-code-interpreter"
            )

        try:
            sandbox = _get_sandbox()

            # Execute Python code in Jupyter notebook
            execution = sandbox.run_code(code)

            # Format output
            output_parts = []

            # Add stdout
            if execution.logs.stdout:
                output_parts.append(f"Output:\n{execution.logs.stdout.strip()}")

            # Add stderr
            if execution.logs.stderr:
                output_parts.append(f"Warnings/Errors:\n{execution.logs.stderr.strip()}")

            # Add execution results (for expressions that return values)
            if execution.results:
                results_text = []
                for result in execution.results:
                    if hasattr(result, "text") and result.text:
                        results_text.append(result.text)
                    elif hasattr(result, "data") and result.data:
                        results_text.append(str(result.data))
                if results_text:
                    output_parts.append(f"Results:\n{chr(10).join(results_text)}")

            # Add error information if any
            if execution.error:
                error_info = f"Error: {execution.error.name}\n"
                error_info += f"Message: {execution.error.value}\n"
                if execution.error.traceback:
                    error_info += f"Traceback:\n{execution.error.traceback}"
                output_parts.append(error_info)

            if not output_parts:
                return "Code executed successfully (no output)"

            return "\n\n".join(output_parts)

        except Exception as e:
            return f"Error executing Python code: {str(e)}"

    @tool
    def bash(command: str) -> str:
        """Execute bash commands in the E2B sandbox.

        Use this tool to run shell commands like file operations, system commands,
        or CLI tool executions. Commands run in an isolated cloud sandbox with
        common Unix tools available.

        Args:
            command: Bash command to execute. Can include pipes, redirects, etc.

        Returns:
            Command execution results including:
            - Standard output (stdout)
            - Standard error (stderr) if any
            - Exit code
            - Error message if command failed

        Examples:
            - bash("ls -la") → List files in current directory
            - bash("cat file.txt | grep pattern") → Search file content
            - bash("python script.py") → Run a Python script
            - bash("curl https://api.example.com/data") → Make HTTP request

        Note: Commands run in /home/user directory by default.
              File system changes persist between calls in the same session.
        """
        if not E2B_AVAILABLE:
            return (
                "Error: E2B sandbox not available. Install with: pip install e2b-code-interpreter"
            )

        try:
            sandbox = _get_sandbox()

            # Execute bash command
            result = sandbox.commands.run(command)

            # Format output
            output_parts = []

            # Add stdout
            if result.stdout:
                output_parts.append(f"Output:\n{result.stdout.strip()}")

            # Add stderr
            if result.stderr:
                output_parts.append(f"Errors:\n{result.stderr.strip()}")

            # Add exit code
            output_parts.append(f"Exit code: {result.exit_code}")

            # Add error message if command failed
            if result.error:
                output_parts.append(f"Error: {result.error}")

            return "\n\n".join(output_parts)

        except Exception as e:
            return f"Error executing bash command: {str(e)}"

    @tool
    def mount_nexus(mount_path: str, config: RunnableConfig) -> str:
        """Mount Nexus filesystem inside the E2B sandbox.

        Use this tool to mount the Nexus filesystem at a specific path in the E2B sandbox.
        Once mounted, you can access Nexus files directly from the sandbox filesystem
        using standard file operations (ls, cat, python, etc.).

        This enables seamless integration between Nexus storage and E2B code execution:
        - Read data files from Nexus into Python scripts
        - Process Nexus files with bash commands
        - Write results back to Nexus from the sandbox

        Args:
            mount_path: Path where Nexus should be mounted (e.g., "/home/user/nexus")
            config: Runtime config containing authentication metadata

        Returns:
            Status message indicating success or failure of the mount operation.
            On success, shows mount path and verification of accessible files.

        Examples:
            - mount_nexus("/home/user/nexus") → Mount at default location
            - mount_nexus("/mnt/nexus") → Mount at custom location

        Note: The mount persists for the lifetime of the sandbox session.
              Requires authenticated Nexus API key in config metadata.
        """
        if not E2B_AVAILABLE:
            return (
                "Error: E2B sandbox not available. Install with: pip install e2b-code-interpreter"
            )

        try:
            # Get authenticated Nexus client to retrieve server URL
            nx = _get_nexus_client(config)
            nexus_url = nx.server_url

            # Extract API key from config
            metadata = config.get("metadata", {})
            x_auth = metadata.get("x_auth", "")
            api_key = x_auth.removeprefix("Bearer ").strip()

            if not api_key:
                return "Error: Missing API key. Cannot mount Nexus without authentication."

            # Get sandbox
            sandbox = _get_sandbox()

            # Create mount directory
            print(f"Creating mount directory: {mount_path}")
            mkdir_result = sandbox.commands.run(f"mkdir -p {mount_path}")
            if mkdir_result.exit_code != 0:
                return f"Error: Failed to create mount directory: {mkdir_result.stderr}"

            # Build mount command with sudo and allow-other
            # Use nohup to properly detach the mount process
            base_mount = f"sudo NEXUS_API_KEY={api_key} nexus mount {mount_path} --remote-url {nexus_url} --allow-other"
            mount_cmd = f"nohup {base_mount} > /tmp/nexus-mount.log 2>&1 &"

            print(f"Mounting Nexus at {mount_path}...")
            print(f"Server: {nexus_url}")

            # Run mount in background
            mount_result = sandbox.commands.run(mount_cmd)
            if mount_result.exit_code != 0 and mount_result.error:
                return f"Error: Mount command failed: {mount_result.error}"

            # Wait for mount to initialize
            import time

            time.sleep(3)

            # Check mount log for errors
            log_result = sandbox.commands.run(
                "cat /tmp/nexus-mount.log 2>/dev/null || echo 'No log yet'"
            )
            log_output = log_result.stdout.strip()

            # Verify mount by listing directory
            ls_result = sandbox.commands.run(f"sudo ls -la {mount_path}")

            if ls_result.exit_code == 0 and ls_result.stdout:
                # Mount appears successful
                output = f"✅ Nexus mounted successfully at {mount_path}\n\n"
                output += f"Server: {nexus_url}\n"
                output += f"Mount path: {mount_path}\n\n"
                output += f"Files in mount:\n{ls_result.stdout.strip()}\n\n"

                if log_output and log_output != "No log yet":
                    output += f"Mount log:\n{log_output[:500]}"

                return output
            else:
                # Mount may have failed
                return f"⚠️  Mount command executed but verification failed.\n\nMount log:\n{log_output}\n\nDirectory listing failed:\n{ls_result.stderr}"

        except Exception as e:
            return f"Error mounting Nexus: {str(e)}"

    # Return all tools (Nexus + E2B)
    tools = [grep_files, glob_files, read_file, write_file]

    # Add E2B tools if available
    if E2B_AVAILABLE and os.getenv("E2B_TEMPLATE_ID"):
        tools.extend([python, bash, mount_nexus])

    return tools
