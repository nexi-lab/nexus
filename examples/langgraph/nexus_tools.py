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
8. web_search: Search the web for current information
9. web_crawl: Fetch and extract web page content as markdown

These tools enable agents to interact with a remote Nexus filesystem and execute
code in isolated Nexus-managed sandboxes, allowing them to search, read, analyze, persist
data, and run code across agent runs.

Authentication:
    API key is REQUIRED via metadata.x_auth: "Bearer <token>"
    Frontend automatically passes the authenticated user's API key in request metadata.
    Each tool creates an authenticated RemoteNexusFS instance using the extracted token.
"""

import os
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
        """Search file content for text patterns.

        Args:
            grep_cmd: Format "pattern [path] [options]"
                     - pattern: Text/regex to search (quote if spaces)
                     - path: Directory to search (default "/")
                     - -i: Case insensitive

        Examples: "async def /workspace", "TODO:", "'import pandas' -i"
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

            # Format results in standard grep format: file_path:line_number:content
            output_lines = []
            max_line_length = 300  # Limit line length to prevent overwhelming output

            for match in results[:50]:  # Limit to first 50 matches
                file_path = match.get("file", "unknown")
                line_num = match.get("line", "")
                content = match.get("content", "").strip()

                # Truncate long lines if needed
                if len(content) > max_line_length:
                    content = content[:max_line_length] + "..."

                # Standard grep format: file:line:content
                output_lines.append(f"{file_path}:{line_num}:{content}")

            if len(results) > 50:
                output_lines.append(f"\n... and {len(results) - 50} more matches")

            return "\n".join(output_lines)

        except Exception as e:
            return f"Error executing grep: {str(e)}\nUsage: grep_files('pattern [path] [options]')"

    @tool
    def glob_files(pattern: str, config: RunnableConfig, path: str = "/") -> str:
        """Find files by name pattern.

        Args:
            pattern: Glob pattern (e.g., "*.py", "**/*.md", "test_*.py")
            path: Directory to search (default "/")

        Examples: glob_files("*.py", "/workspace"), glob_files("**/*.md")
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
        """Read file content.

        Args:
            read_cmd: "[cat|less] path [start] [end]" or just "path"
                     - cat: Full content (default)
                     - less: First 100 lines preview
                     - start: Starting line number (1-indexed, optional)
                     - end: Ending line number (inclusive, optional)

        Examples:
            "cat /workspace/README.md" - read entire file
            "less /scripts/large.py" - preview first 100 lines
            "cat /data/file.json 10 20" - read lines 10-20
            "cat /data/file.json 50" - read from line 50 to end
        """
        try:
            # Get authenticated client
            nx = _get_nexus_client(config)

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

            # Handle bytes
            if isinstance(content, bytes):
                content = content.decode("utf-8")

            # Split into lines for line-based operations
            lines = content.split("\n")
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
                content = "\n".join(selected_lines)

                # Check content length and return error if too large
                max_content_length = 30000
                if len(content) > max_content_length:
                    return (
                        f"Error: Requested content is too large ({len(content)} characters). "
                        f"Maximum allowed is {max_content_length} characters. "
                        f"Requested lines {start_line or 1}-{end_line or total_lines} from {path}. "
                        f"Try a smaller line range."
                    )

                output = (
                    f"Content of {path} (lines {start_line or 1}-{end_idx} of {total_lines}):\n\n"
                )
                output += content
                return output

            # Check content length and return error if too large (for full file)
            max_content_length = 30000
            if len(content) > max_content_length:
                return (
                    f"Error: File {path} is too large ({len(content)} characters). "
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
        """Write content to file. Creates parent directories automatically, overwrites if exists.

        Args:
            path: Absolute file path (e.g., "/reports/summary.md")
            content: Text content to write

        Examples: write_file("/reports/summary.md", "# Summary\\n..."), write_file("/data/results.txt", "...")
        """
        try:
            # Get authenticated client
            nx = _get_nexus_client(config)

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
    def python(code: str, config: RunnableConfig) -> str:
        """Execute Python code in sandbox. Use print() for output.

        Args:
            code: Python code (multi-line supported)

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
    def bash(command: str, config: RunnableConfig) -> str:
        """Execute bash commands in sandbox. Supports pipes, redirects. Changes persist in session.

        Args:
            command: Bash command to execute

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
    def query_memories(config: RunnableConfig) -> str:
        """Query all stored active memory records. Returns content, namespace, scope, importance.

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

    # Web Search and Fetch Tools
    @tool
    def web_search(query: str, config: RunnableConfig, max_results: int = 5) -> str:  # noqa: ARG001
        """Search the web for current information. Returns titles, URLs, snippets.

        Args:
            query: Search query
            max_results: Max results (default 5, max 20)

        Examples: web_search("Python asyncio best practices"), web_search("latest AI research 2024", max_results=10)
        """
        try:
            # Import here to avoid requiring tavily when not used
            from tavily import TavilyClient

            # Get API key from environment
            api_key = os.getenv("TAVILY_API_KEY")
            if not api_key:
                return "Error: TAVILY_API_KEY not found in environment variables"

            # Initialize Tavily client
            client = TavilyClient(api_key=api_key)

            # Perform search
            response = client.search(query=query, max_results=max_results)

            # Format results
            if not response or "results" not in response:
                return f"No results found for query: {query}"

            results = response.get("results", [])
            if not results:
                return f"No results found for query: {query}"

            output_lines = [f"Found {len(results)} results for '{query}':\n"]

            for i, result in enumerate(results, 1):
                title = result.get("title", "No title")
                url = result.get("url", "No URL")
                content = result.get("content", "No content")

                output_lines.append(f"\n{i}. {title}")
                output_lines.append(f"   URL: {url}")
                output_lines.append(
                    f"   {content[:300]}..." if len(content) > 300 else f"   {content}"
                )

            # Add answer if available
            if "answer" in response and response["answer"]:
                output_lines.insert(1, f"\nAnswer: {response['answer']}\n")

            return "\n".join(output_lines)

        except ImportError:
            return "Error: tavily-python package not installed. Run: pip install tavily-python"
        except Exception as e:
            return f"Error performing web search: {str(e)}"

    @tool
    def web_crawl(url: str, config: RunnableConfig) -> str:  # noqa: ARG001
        """Fetch and extract web page content as clean markdown with metadata.

        Args:
            url: Web page URL to fetch

        Examples: web_crawl("https://docs.python.org/3/library/asyncio.html"), web_crawl("https://github.com/example/repo")
        """
        try:
            # Import here to avoid requiring firecrawl when not used
            from firecrawl import FirecrawlApp

            # Get API key from environment
            api_key = os.getenv("FIRECRAWL_API_KEY")
            if not api_key:
                return "Error: FIRECRAWL_API_KEY not found in environment variables"

            # Initialize Firecrawl client
            app = FirecrawlApp(api_key=api_key)

            # Scrape the URL (API v4.5.0+ returns Document object with formats parameter)
            result = app.scrape(url, formats=["markdown", "html"])

            if not result:
                return f"Error: Failed to fetch content from {url}"

            # Extract content from Document object (Firecrawl v4.5.0+)
            # Result is a Document object with attributes: markdown, metadata, html, etc.
            markdown_content = getattr(result, "markdown", "")
            metadata_obj = getattr(result, "metadata", None)

            # Extract metadata fields
            metadata = {}
            if metadata_obj:
                # metadata_obj might be a dict or an object with attributes
                if isinstance(metadata_obj, dict):
                    metadata = metadata_obj
                else:
                    # Extract common metadata fields
                    for field in ["title", "description", "url", "language", "author"]:
                        value = getattr(metadata_obj, field, None)
                        if value:
                            metadata[field] = value

            if not markdown_content:
                return f"Error: No content extracted from {url}"

            # Format output
            output_lines = [f"Content from {url}:\n"]

            # Add metadata if available
            if metadata:
                title = metadata.get("title", "")
                description = metadata.get("description", "")
                if title:
                    output_lines.append(f"Title: {title}")
                if description:
                    output_lines.append(f"Description: {description}")
                output_lines.append("")

            # Check content length and return error if too large
            max_length = 30000
            if len(markdown_content) > max_length:
                return (
                    f"Error: Web page content from {url} is too large ({len(markdown_content)} characters). "
                    f"Maximum allowed is {max_length} characters. "
                    f"Consider fetching a more specific page or processing the content in smaller sections."
                )

            output_lines.append(markdown_content)

            return "\n".join(output_lines)

        except ImportError:
            return "Error: firecrawl-py package not installed. Run: pip install firecrawl-py"
        except Exception as e:
            import traceback

            error_details = traceback.format_exc()
            return f"Error fetching web page: {str(e)}\n\nDetails:\n{error_details}"

    # Return all tools
    tools = [
        grep_files,
        glob_files,
        read_file,
        write_file,
        python,
        bash,
        query_memories,
        web_search,
        web_crawl,
    ]

    return tools
