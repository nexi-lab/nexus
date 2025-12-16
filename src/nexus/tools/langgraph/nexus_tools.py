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
7. query_memories: Query and retrieve stored memory records

These tools enable agents to interact with a remote Nexus filesystem and execute
code in isolated Nexus-managed sandboxes, allowing them to search, read, analyze, persist
data, run code, and discover reusable skills across agent runs.

Authentication:
    API key is REQUIRED via metadata.x_auth: "Bearer <token>"
    Frontend automatically passes the authenticated user's API key in request metadata.
    Each tool creates an authenticated RemoteNexusFS instance using the extracted token.
"""

import shlex
from typing import Annotated, Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt import InjectedState

from nexus.remote import RemoteNexusFS  # re-export for backward compatibility
from nexus.tools._client import _get_nexus_client

__all__ = [
    "RemoteNexusFS",
    "get_nexus_tools",
    "list_skills",
]


def list_skills(
    config: RunnableConfig,
    state: Annotated[Any, InjectedState] = None,
    tier: str = "all",
    include_metadata: bool = True,
) -> dict[str, Any]:
    """List available skills from Nexus.

    This is a standalone function (not a LangGraph tool) that returns skill data
    for programmatic use in agents or scripts.

    Args:
        config: Runtime configuration (provided by framework) containing auth metadata
        state: Agent state (injected by LangGraph, not used directly)
        tier: Tier filter - "all" (default), "agent", "user", "tenant", or "system"
        include_metadata: Whether to include full metadata (default: True)

    Available Tiers:
        - "all": Show skills from all tiers (default)
        - "agent": Agent-level skills
        - "user": User-level skills
        - "tenant": Tenant-wide skills
        - "system": System-level skills

    Returns:
        Dictionary with:
            - "skills": List of skill dictionaries with name, description, version, tier, etc.
            - "count": Total number of skills
            - "tier": Filter tier (if specified)

    Examples:
        >>> from langchain_core.runnables import RunnableConfig
        >>> from nexus.tools.langgraph.nexus_tools import list_skills
        >>>
        >>> config = RunnableConfig(metadata={
        ...     "x_auth": "Bearer sk-your-api-key",
        ...     "nexus_server_url": "http://localhost:8080"
        ... })
        >>>
        >>> # Get all skills (default)
        >>> result = list_skills(config)
        >>> print(f"Found {result['count']} skills")
        >>>
        >>> # Explicit "all"
        >>> result = list_skills(config, tier="all")
        >>>
        >>> # Filter by specific tier
        >>> result = list_skills(config, tier="system")
        >>> print(f"Found {result['count']} system skills")
        >>>
        >>> # Get user-level skills
        >>> result = list_skills(config, tier="user")
    """
    nx = _get_nexus_client(config, state)

    # Map "all" to None for the backend API
    tier_filter = None if tier == "all" else tier

    return nx.skills_list(tier=tier_filter, include_metadata=include_metadata)


def get_nexus_tools() -> list[BaseTool]:
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

    @tool
    def grep_files(
        pattern: str,
        config: RunnableConfig,
        state: Annotated[Any, InjectedState] = None,  # noqa: ARG001
        path: str = "/",
        file_pattern: str | None = None,
        ignore_case: bool = False,
        max_results: int = 1000,
    ) -> str:
        """Search file content for text patterns.

        Args:
            pattern: Text/regex pattern to search for
            state: Agent state (injected by LangGraph, not used directly)
            config: Runtime configuration (provided by framework)
            path: Directory to search (default: "/")
            file_pattern: Optional glob pattern to filter files (e.g., "*.py", "**/*.md")
            ignore_case: If True, perform case-insensitive search (default: False)
            max_results: Maximum number of results to return (default: 1000)

        Examples:
            grep_files("async def", path="/workspace")
            grep_files("TODO", file_pattern="**/*.py")
            grep_files("error", ignore_case=True, max_results=100)
            grep_files("import pandas", path="/notebooks", file_pattern="*.ipynb")
        """
        try:
            # Get authenticated client
            nx = _get_nexus_client(config, state)

            # Execute grep with provided parameters
            results = nx.grep(
                pattern=pattern,
                path=path,
                file_pattern=file_pattern,
                ignore_case=ignore_case,
                max_results=max_results,
            )

            if not results:
                search_info = f"pattern '{pattern}' in {path}"
                if file_pattern:
                    search_info += f" (files: {file_pattern})"
                return f"No matches found for {search_info}"

            # Format results in standard grep format: file_path:line_number:content
            output_lines = []
            max_line_length = 300  # Limit line length to prevent overwhelming output
            display_limit = min(50, max_results)  # Show at most 50 results in output

            for match in results[:display_limit]:
                file_path = match.get("file", "unknown")
                line_num = match.get("line", "")
                content = match.get("content", "").strip()

                # Truncate long lines if needed
                if len(content) > max_line_length:
                    content = content[:max_line_length] + "..."

                # Standard grep format: file:line:content
                output_lines.append(f"{file_path}:{line_num}:{content}")

            # Add summary footer
            total_results = len(results)
            if total_results > display_limit:
                output_lines.append(f"\n... and {total_results - display_limit} more matches")

            # Add header with search info
            header_parts = [f"Found {total_results} matches"]
            if file_pattern:
                header_parts.append(f"in files matching '{file_pattern}'")
            output_lines.insert(0, " ".join(header_parts) + "\n")

            return "\n".join(output_lines)

        except Exception as e:
            return f"Error executing grep: {str(e)}"

    @tool
    def glob_files(
        pattern: str,
        config: RunnableConfig,
        state: Annotated[Any, InjectedState] = None,  # noqa: ARG001
        path: str = "/",
    ) -> str:
        """Find files by name pattern.

        Args:
            pattern: Glob pattern (e.g., "*.py", "**/*.md", "test_*.py")
            state: Agent state (injected by LangGraph, not used directly)
            config: Runtime configuration (provided by framework)
            path: Directory to search (default "/")

        Examples: glob_files("*.py", "/workspace"), glob_files("**/*.md")
        """
        try:
            # Get authenticated client
            nx = _get_nexus_client(config, state)

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
    def read_file(
        read_cmd: str,
        config: RunnableConfig,
        state: Annotated[Any, InjectedState] = None,  # noqa: ARG001
    ) -> str:
        """Read file content.

        Args:
            read_cmd: "[cat|less] path [start] [end]" or just "path"
                     - cat: Full content (default)
                     - less: First 100 lines preview
                     - start: Starting line number (1-indexed, optional)
                     - end: Ending line number (inclusive, optional)
            state: Agent state (injected by LangGraph, not used directly)
            config: Runtime configuration (provided by framework)

        Examples:
            "cat /workspace/README.md" - read entire file
            "less /scripts/large.py" - preview first 100 lines
            "cat /data/file.json 10 20" - read lines 10-20
            "cat /data/file.json 50" - read from line 50 to end
        """
        try:
            # Get authenticated client
            nx = _get_nexus_client(config, state)
            # Parse read command
            parts = shlex.split(read_cmd.strip())
            if not parts:
                return (
                    "Error: Empty read command. Usage: read_file('[cat|less] path [start] [end]')"
                )

            # Determine command type, path, and line range
            start_line = None
            end_line = None

            if parts[0] in ["cat", "less"]:
                command = parts[0]
                if len(parts) < 2:
                    return f"Error: Missing file path. Usage: read_file('{command} path [start] [end]')"
                path = parts[1]

                # Parse optional start and end line numbers
                if len(parts) >= 3:
                    try:
                        start_line = int(parts[2])
                    except ValueError:
                        return f"Error: Invalid start line number: {parts[2]}"

                if len(parts) >= 4:
                    try:
                        end_line = int(parts[3])
                    except ValueError:
                        return f"Error: Invalid end line number: {parts[3]}"
            else:
                # Default to cat if no command specified
                command = "cat"
                path = parts[0]

                # Parse optional start and end line numbers
                if len(parts) >= 2:
                    try:
                        start_line = int(parts[1])
                    except ValueError:
                        return f"Error: Invalid start line number: {parts[1]}"

                if len(parts) >= 3:
                    try:
                        end_line = int(parts[2])
                    except ValueError:
                        return f"Error: Invalid end line number: {parts[2]}"

            # Read file content
            if path.startswith("/mnt/nexus"):
                path = path[len("/mnt/nexus") :]

            content = nx.read(path)

            # Handle dict response (when return_metadata=True or edge cases)
            if isinstance(content, dict):
                # Extract content and encoding from metadata dict
                encoding = content.get("encoding", "")
                content_value = content.get("content")
                if content_value is None:
                    return f"Error: nx.read() returned dict without 'content' key: {content}"
                content = content_value

                # Decode base64 if needed
                if encoding == "base64" and isinstance(content, str):
                    import base64

                    content = base64.b64decode(content)

            # Handle bytes - decode to str
            content_str: str
            if isinstance(content, bytes):
                content_str = content.decode("utf-8")
            elif isinstance(content, str):
                content_str = content
            else:
                return f"Error: Unexpected content type from {path}: {type(content)}"

            # Split into lines for line-based operations
            lines = content_str.split("\n")
            total_lines = len(lines)

            # Validate line range if specified
            if start_line is not None:
                if start_line < 1:
                    return f"Error: Start line must be >= 1, got {start_line}"
                if start_line > total_lines:
                    return (
                        f"Error: Start line {start_line} exceeds file length ({total_lines} lines)"
                    )

            if end_line is not None:
                if end_line < 1:
                    return f"Error: End line must be >= 1, got {end_line}"
                if start_line is not None and end_line < start_line:
                    return f"Error: End line {end_line} must be >= start line {start_line}"

            # Extract the requested line range
            if start_line is not None or end_line is not None:
                # Convert to 0-indexed
                start_idx = (start_line - 1) if start_line is not None else 0
                end_idx = end_line if end_line is not None else total_lines

                # Extract lines
                selected_lines = lines[start_idx:end_idx]
                content_str = "\n".join(selected_lines)

                # Check content length and return error if too large
                max_content_length = 30000
                if len(content_str) > max_content_length:
                    return (
                        f"Error: Requested content is too large ({len(content_str)} characters). "
                        f"Maximum allowed is {max_content_length} characters. "
                        f"Requested lines {start_line or 1}-{end_line or total_lines} from {path}. "
                        f"Try a smaller line range."
                    )

                output = (
                    f"Content of {path} (lines {start_line or 1}-{end_idx} of {total_lines}):\n\n"
                )
                output += content_str
                return output

            # Check content length and return error if too large (for full file)
            max_content_length = 30000
            if len(content_str) > max_content_length:
                return (
                    f"Error: File {path} is too large ({len(content_str)} characters). "
                    f"Maximum allowed is {max_content_length} characters. "
                    f"Use 'less {path}' to preview first 100 lines, or use line range like 'cat {path} 1 100'."
                )

            # For 'less', show preview
            if command == "less":
                if total_lines > 100:
                    preview_content = "\n".join(lines[:100])
                    output = f"Preview of {path} (first 100 of {total_lines} lines):\n\n"
                    output += preview_content
                    output += f"\n\n... ({total_lines - 100} more lines)"
                else:
                    output = f"Content of {path} ({total_lines} lines):\n\n"
                    output += content_str
            else:
                # For 'cat', show full content
                output = f"Content of {path} ({len(content_str)} characters):\n\n"
                output += content_str

            return output

        except FileNotFoundError:
            return f"Error: File not found: {read_cmd}"
        except Exception as e:
            return f"Error reading file: {str(e)}\nUsage: read_file('[cat|less] path')"

    @tool
    def write_file(
        path: str,
        content: str,
        config: RunnableConfig,
        state: Annotated[Any, InjectedState] = None,  # noqa: ARG001
    ) -> str:
        """Write content to file. Creates parent directories automatically, overwrites if exists.

        Args:
            path: Absolute file path (e.g., "/reports/summary.md")
            content: Text content to write
            state: Agent state (injected by LangGraph, not used directly)
            config: Runtime configuration (provided by framework)

        Examples: write_file("/reports/summary.md", "# Summary\\n..."), write_file("/data/results.txt", "...")
        """
        try:
            # Get authenticated client
            nx = _get_nexus_client(config, state)

            # Convert string to bytes for Nexus
            content_bytes = content.encode("utf-8") if isinstance(content, str) else content

            # Write file (Nexus creates parent directories automatically)
            if path.startswith("/mnt/nexus"):
                path = path[len("/mnt/nexus") :]
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
    def python(
        code: str,
        config: RunnableConfig,
        state: Annotated[Any, InjectedState] = None,  # noqa: ARG001
    ) -> str:
        """Execute Python code in sandbox. Use print() for output.

        Args:
            code: Python code (multi-line supported)
            state: Agent state (injected by LangGraph, not used directly)
            config: Runtime configuration (provided by framework)

        Examples: python("print('Hello')"), python("import pandas as pd\\nprint(pd.DataFrame({'a': [1,2,3]}))")
        """
        try:
            nx = _get_nexus_client(config)

            # Get sandbox_id from metadata
            metadata = config.get("metadata", {})
            sandbox_id = metadata.get("sandbox_id")

            if not sandbox_id:
                return "Error: sandbox_id not found in metadata. Please start a sandbox first."

            # Execute Python code in sandbox
            result = nx.sandbox_run(
                sandbox_id=sandbox_id, language="python", code=code, timeout=300
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
                return "Code executed successfully (no output)"

            return "\n\n".join(output_parts)

        except Exception as e:
            return f"Error executing Python code: {str(e)}"

    @tool
    def bash(
        command: str,
        config: RunnableConfig,
        state: Annotated[Any, InjectedState] = None,  # noqa: ARG001
    ) -> str:
        """Execute bash commands in sandbox. Supports pipes, redirects. Changes persist in session.

        Args:
            command: Bash command to execute
            state: Agent state (injected by LangGraph, not used directly)
            config: Runtime configuration (provided by framework)

        Examples: bash("ls -la"), bash("echo 'Hello'"), bash("cat file.txt | grep pattern")
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
                sandbox_id=sandbox_id, language="bash", code=command, timeout=300
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

    # Memory Tools
    @tool
    def query_memories(
        config: RunnableConfig,
        state: Annotated[Any, InjectedState] = None,  # noqa: ARG001
    ) -> str:
        """Query all stored active memory records. Returns content, namespace, scope, importance.

        Args:
            state: Agent state (injected by LangGraph, not used directly)
            config: Runtime configuration (provided by framework)

        Example: query_memories()
        """
        try:
            nx = _get_nexus_client(config)

            # Query active memories using RemoteMemory API
            memories = nx.memory.query(state="active", limit=100)

            if not memories:
                return "No memories found"

            # Format results
            output_lines = [f"Found {len(memories)} memories:\n"]

            for i, memory in enumerate(memories, 1):
                content = memory.get("content", "")
                mem_namespace = memory.get("namespace", "N/A")
                importance = memory.get("importance")

                # Truncate content if too long
                display_content = content[:200] + "..." if len(content) > 200 else content

                output_lines.append(f"\n{i}. {display_content}")
                output_lines.append(f"   Namespace: {mem_namespace}")
                if importance is not None:
                    output_lines.append(f"   Importance: {importance:.2f}")

            return "\n".join(output_lines)

        except Exception as e:
            return f"Error querying memories: {str(e)}"

    # Return all tools
    tools = [
        grep_files,
        glob_files,
        read_file,
        write_file,
        python,
        bash,
        query_memories,
    ]

    return tools
