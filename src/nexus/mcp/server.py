"""Nexus MCP Server Implementation.

This module implements a Model Context Protocol (MCP) server that exposes
Nexus functionality to AI agents and tools using the fastmcp framework.
"""

from __future__ import annotations

import contextlib
import json

from fastmcp import FastMCP

from nexus.core.filesystem import NexusFilesystem


def create_mcp_server(
    nx: NexusFilesystem | None = None,
    name: str = "nexus",
    remote_url: str | None = None,
    api_key: str | None = None,
) -> FastMCP:
    """Create an MCP server for Nexus operations.

    Args:
        nx: NexusFilesystem instance (if None, will auto-connect)
        name: Server name (default: "nexus")
        remote_url: Remote Nexus URL for connecting to remote server
        api_key: Optional API key for remote server authentication

    Returns:
        FastMCP server instance

    Examples:
        >>> from nexus import connect
        >>> from nexus.mcp import create_mcp_server
        >>>
        >>> # Local filesystem
        >>> nx = connect()
        >>> server = create_mcp_server(nx)
        >>>
        >>> # Remote filesystem
        >>> server = create_mcp_server(remote_url="http://localhost:8080")
        >>>
        >>> # Remote filesystem with API key
        >>> server = create_mcp_server(
        ...     remote_url="http://localhost:8080",
        ...     api_key="your-api-key"
        ... )
    """
    # Initialize Nexus filesystem if not provided
    if nx is None:
        if remote_url:
            from nexus.remote import RemoteNexusFS

            nx = RemoteNexusFS(remote_url, api_key=api_key)
        else:
            from nexus import connect

            nx = connect()

    # Create FastMCP server
    mcp = FastMCP(name)

    # =========================================================================
    # FILE OPERATIONS TOOLS
    # =========================================================================

    @mcp.tool()
    def nexus_read_file(path: str) -> str:
        """Read file content from Nexus filesystem.

        Args:
            path: File path to read (e.g., "/workspace/data.txt")

        Returns:
            File content as string
        """
        try:
            content = nx.read(path)
            if isinstance(content, bytes):
                return content.decode("utf-8", errors="replace")
            return str(content)
        except Exception as e:
            return f"Error reading file: {str(e)}"

    @mcp.tool()
    def nexus_write_file(path: str, content: str) -> str:
        """Write content to a file in Nexus filesystem.

        Args:
            path: File path to write (e.g., "/workspace/data.txt")
            content: Content to write

        Returns:
            Success message or error
        """
        try:
            content_bytes = content.encode("utf-8") if isinstance(content, str) else content
            nx.write(path, content_bytes)
            return f"Successfully wrote {len(content_bytes)} bytes to {path}"
        except Exception as e:
            return f"Error writing file: {str(e)}"

    @mcp.tool()
    def nexus_delete_file(path: str) -> str:
        """Delete a file from Nexus filesystem.

        Args:
            path: File path to delete (e.g., "/workspace/data.txt")

        Returns:
            Success message or error
        """
        try:
            nx.delete(path)
            return f"Successfully deleted {path}"
        except Exception as e:
            return f"Error deleting file: {str(e)}"

    @mcp.tool()
    def nexus_list_files(path: str = "/", recursive: bool = False) -> str:
        """List files in a directory.

        Args:
            path: Directory path to list (default: "/")
            recursive: Whether to list recursively (default: False)

        Returns:
            JSON string with list of files
        """
        try:
            files = nx.list(path, recursive=recursive)
            return json.dumps(files, indent=2)
        except Exception as e:
            return f"Error listing files: {str(e)}"

    @mcp.tool()
    def nexus_file_info(path: str) -> str:
        """Get detailed information about a file.

        Args:
            path: File path to get info for

        Returns:
            JSON string with file metadata
        """
        try:
            # Use exists and other methods to get file info
            # info() is not in base NexusFilesystem interface
            if not nx.exists(path):
                return f"File not found: {path}"

            is_dir = nx.is_directory(path)
            info_dict = {
                "path": path,
                "exists": True,
                "is_directory": is_dir,
            }

            # Try to get size if it's a file
            if not is_dir:
                try:
                    content = nx.read(path)
                    if isinstance(content, bytes):
                        info_dict["size"] = len(content)
                except Exception:
                    pass

            return json.dumps(info_dict, indent=2)
        except Exception as e:
            return f"Error getting file info: {str(e)}"

    # =========================================================================
    # DIRECTORY OPERATIONS TOOLS
    # =========================================================================

    @mcp.tool()
    def nexus_mkdir(path: str) -> str:
        """Create a directory in Nexus filesystem.

        Args:
            path: Directory path to create (e.g., "/workspace/data")

        Returns:
            Success message or error
        """
        try:
            nx.mkdir(path)
            return f"Successfully created directory {path}"
        except Exception as e:
            return f"Error creating directory: {str(e)}"

    @mcp.tool()
    def nexus_rmdir(path: str, recursive: bool = False) -> str:
        """Remove a directory from Nexus filesystem.

        Args:
            path: Directory path to remove (e.g., "/workspace/data")
            recursive: Whether to remove recursively (default: False)

        Returns:
            Success message or error
        """
        try:
            nx.rmdir(path, recursive=recursive)
            return f"Successfully removed directory {path}"
        except Exception as e:
            return f"Error removing directory: {str(e)}"

    # =========================================================================
    # SEARCH TOOLS
    # =========================================================================

    @mcp.tool()
    def nexus_glob(pattern: str, path: str = "/") -> str:
        """Search files using glob pattern.

        Args:
            pattern: Glob pattern (e.g., "**/*.py", "*.txt")
            path: Base path to search from (default: "/")

        Returns:
            JSON string with list of matching file paths
        """
        try:
            matches = nx.glob(pattern, path)
            return json.dumps(matches, indent=2)
        except Exception as e:
            return f"Error in glob search: {str(e)}"

    @mcp.tool()
    def nexus_grep(pattern: str, path: str = "/", ignore_case: bool = False) -> str:
        """Search file contents using regex pattern.

        Args:
            pattern: Regex pattern to search for
            path: Base path to search from (default: "/")
            ignore_case: Whether to ignore case (default: False)

        Returns:
            JSON string with search results (file paths, line numbers, content)
        """
        try:
            results = nx.grep(pattern, path, ignore_case=ignore_case)
            # Limit results to first 100 matches
            if len(results) > 100:
                results = results[:100]
            return json.dumps(results, indent=2)
        except Exception as e:
            return f"Error in grep search: {str(e)}"

    @mcp.tool()
    def nexus_semantic_search(query: str, limit: int = 10) -> str:
        """Search files semantically using natural language query.

        Args:
            query: Natural language search query
            limit: Maximum number of results (default: 10)

        Returns:
            JSON string with search results
        """
        try:
            # Check if nx has search method (only available in NexusFS)
            if hasattr(nx, "search"):
                # Calling search() - available in NexusFS but not base interface
                results = nx.search(query, limit=limit)
                return json.dumps(results, indent=2)
            return "Semantic search not available (requires NexusFS with search enabled)"
        except Exception as e:
            return f"Error in semantic search: {str(e)}"

    # =========================================================================
    # MEMORY TOOLS
    # =========================================================================

    @mcp.tool()
    def nexus_store_memory(
        content: str,
        memory_type: str | None = None,
        importance: float = 0.5,
    ) -> str:
        """Store a memory in Nexus memory system.

        Args:
            content: Memory content to store
            memory_type: Optional memory type/category
            importance: Importance score 0.0-1.0 (default: 0.5)

        Returns:
            Success message or error
        """
        try:
            # Check if nx has memory attribute (only available in NexusFS)
            if not hasattr(nx, "memory"):
                return "Memory system not available (requires NexusFS)"

            nx.memory.store(  # type: ignore[attr-defined]
                content,
                scope="user",
                memory_type=memory_type,
                importance=importance,
            )
            # Commit the session to persist
            nx.memory.session.commit()  # type: ignore[attr-defined]
            return f"Successfully stored memory: {content[:80]}..."
        except Exception as e:
            # Rollback on error
            if hasattr(nx, "memory"):
                with contextlib.suppress(Exception):
                    nx.memory.session.rollback()
            return f"Error storing memory: {str(e)}"

    @mcp.tool()
    def nexus_query_memory(
        query: str,
        memory_type: str | None = None,
        limit: int = 5,
    ) -> str:
        """Query memories using semantic search.

        Args:
            query: Search query
            memory_type: Optional filter by memory type
            limit: Maximum number of results (default: 5)

        Returns:
            JSON string with matching memories
        """
        try:
            # Check if nx has memory attribute (only available in NexusFS)
            if not hasattr(nx, "memory"):
                return "Memory system not available (requires NexusFS)"

            memories = nx.memory.search(  # type: ignore[attr-defined]
                query,
                scope="user",
                memory_type=memory_type,
                limit=limit,
            )
            return json.dumps(memories, indent=2)
        except Exception as e:
            return f"Error querying memory: {str(e)}"

    # =========================================================================
    # WORKFLOW TOOLS
    # =========================================================================

    @mcp.tool()
    def nexus_list_workflows() -> str:
        """List available workflows in Nexus.

        Returns:
            JSON string with list of workflows
        """
        try:
            # Check if nx has workflows attribute (only available in NexusFS)
            if not hasattr(nx, "workflows"):
                return "Workflow system not available (requires NexusFS with workflows enabled)"

            workflows = nx.workflows.list_workflows()  # type: ignore[attr-defined]
            return json.dumps(workflows, indent=2)
        except Exception as e:
            return f"Error listing workflows: {str(e)}"

    @mcp.tool()
    def nexus_execute_workflow(name: str, inputs: str | None = None) -> str:
        """Execute a workflow by name.

        Args:
            name: Workflow name
            inputs: Optional JSON string with workflow inputs

        Returns:
            Workflow execution result
        """
        try:
            # Check if nx has workflows attribute (only available in NexusFS)
            if not hasattr(nx, "workflows"):
                return "Workflow system not available (requires NexusFS with workflows enabled)"

            input_dict = json.loads(inputs) if inputs else {}
            result = nx.workflows.execute(name, **input_dict)  # type: ignore[attr-defined]
            return json.dumps(result, indent=2)
        except Exception as e:
            return f"Error executing workflow: {str(e)}"

    # =========================================================================
    # RESOURCES
    # =========================================================================

    @mcp.resource("nexus://files/{path}")
    def get_file_resource(path: str) -> str:
        """Browse files as MCP resources.

        Args:
            path: File path to access

        Returns:
            File content
        """
        try:
            content = nx.read(path)
            if isinstance(content, bytes):
                return content.decode("utf-8", errors="replace")
            return str(content)
        except Exception as e:
            return f"Error reading resource: {str(e)}"

    # =========================================================================
    # PROMPTS
    # =========================================================================

    @mcp.prompt()
    def file_analysis_prompt(file_path: str) -> str:
        """Generate a prompt for analyzing a file.

        Args:
            file_path: Path to the file to analyze

        Returns:
            Analysis prompt
        """
        return f"""Analyze the file at {file_path}.

1. Read the file content
2. Identify the file type and purpose
3. Summarize the key information
4. Suggest potential improvements or issues

Use the nexus_read_file tool to read the content first.
"""

    @mcp.prompt()
    def search_and_summarize_prompt(query: str) -> str:
        """Generate a prompt for searching and summarizing content.

        Args:
            query: Search query

        Returns:
            Search and summarize prompt
        """
        return f"""Search for content related to: {query}

1. Use nexus_semantic_search to find relevant files
2. Read the most relevant files using nexus_read_file
3. Summarize the findings
4. Store key insights in memory using nexus_store_memory

Start by running the semantic search.
"""

    return mcp


def main() -> None:
    """Main entry point for running MCP server from command line."""

    # Get remote URL and API key from environment if available
    import os

    from nexus import connect

    remote_url = os.getenv("NEXUS_URL")
    api_key = os.getenv("NEXUS_API_KEY")

    # Create and run server
    nx = None
    if not remote_url:
        nx = connect()

    mcp = create_mcp_server(nx=nx, remote_url=remote_url, api_key=api_key)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
