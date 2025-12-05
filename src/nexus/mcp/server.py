"""Nexus MCP Server Implementation.

This module implements a Model Context Protocol (MCP) server that exposes
Nexus functionality to AI agents and tools using the fastmcp framework.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
from typing import Any

from fastmcp import Context, FastMCP

from nexus.core.filesystem import NexusFilesystem
from nexus.mcp.formatters import format_response

# Context variable for per-request API key (set by infrastructure, not AI)
_request_api_key: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_api_key", default=None
)


def set_request_api_key(api_key: str) -> contextvars.Token[str | None]:
    """Set the API key for the current request context.

    This function should be called by infrastructure (HTTP middleware, proxy,
    gateway) to set a per-request API key without exposing it to AI agents.

    Args:
        api_key: The API key to use for this request

    Returns:
        A token that can be used to reset the context variable

    Example:
        >>> from nexus.mcp import set_request_api_key
        >>> from nexus.mcp.server import _request_api_key
        >>>
        >>> # In middleware or proxy code:
        >>> token = set_request_api_key("sk-user-api-key-xyz")
        >>> try:
        ...     # Make MCP tool calls here - they will use this API key
        ...     result = mcp_server.call_tool("nexus_read_file", path="/data.txt")
        ... finally:
        ...     # Clean up context
        ...     _request_api_key.reset(token)
    """
    return _request_api_key.set(api_key)


def get_request_api_key() -> str | None:
    """Get the current request API key from context.

    This is primarily for testing and debugging. Infrastructure code should
    use set_request_api_key() to set the key.

    Returns:
        The current request API key, or None if not set
    """
    return _request_api_key.get()


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
        api_key: Optional API key for remote server authentication (default)

    Returns:
        FastMCP server instance

    Infrastructure API Key Support:
        The MCP server supports per-request API keys set by infrastructure
        (e.g., HTTP middleware, proxy, gateway) without exposing them to AI agents.

        Infrastructure should set the API key using:
            from nexus.mcp.server import set_request_api_key
            token = set_request_api_key("sk-user-api-key-xyz")
            try:
                # Make MCP tool calls here
                pass
            finally:
                token.reset()

        The api_key parameter serves as the default when no per-request key is set.

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

    # Store default connection and config for per-request API key support
    _default_nx = nx
    _remote_url = remote_url

    # Connection pool for per-request API keys (cached by API key)
    _connection_cache: dict[str, NexusFilesystem] = {}

    def _get_nexus_instance(ctx: Context | None = None) -> NexusFilesystem:
        """Get Nexus instance for current request using context API key.

        This function checks if infrastructure has set a per-request API key
        in the context variable or FastMCP's context state. If so, it creates/retrieves
        a connection with that API key. Otherwise, it returns the default connection.

        Args:
            ctx: Optional FastMCP Context object (if available from tool)

        Returns:
            NexusFilesystem instance (default or per-request based on context)

        Note:
            Per-request API keys are only supported when remote_url is configured.
            For local connections, the default connection is always used.
        """
        # Try to get API key from FastMCP context state first (if Context is available)
        request_api_key = None
        if ctx and hasattr(ctx, "get_state"):
            with contextlib.suppress(Exception):
                request_api_key = ctx.get_state("api_key")

        # Fallback to context variable (set by Starlette middleware)
        if not request_api_key:
            request_api_key = _request_api_key.get()

        # If no API key in context, use default connection
        if not request_api_key:
            return _default_nx

        # If remote_url not configured, can't use per-request API keys
        if not _remote_url:
            return _default_nx

        # Check cache for existing connection
        if request_api_key in _connection_cache:
            return _connection_cache[request_api_key]

        # Create new remote connection with API key from context
        from nexus.remote import RemoteNexusFS

        new_nx = RemoteNexusFS(_remote_url, api_key=request_api_key)
        _connection_cache[request_api_key] = new_nx
        return new_nx

    # Create FastMCP server
    mcp = FastMCP(name)

    # Add health check endpoint for HTTP transports
    # This is added here so it's available when the server is created via create_mcp_server()
    # The CLI command will also add it, but having it here ensures it works in all cases
    @mcp.custom_route("/health", methods=["GET"])
    async def health_check(_request: Any) -> Any:
        """Health check endpoint for Docker and monitoring."""
        from starlette.responses import JSONResponse

        return JSONResponse({"status": "healthy", "service": "nexus-mcp"})

    # Add FastMCP middleware to extract API key from HTTP headers and store in context state
    # This allows tools to access the API key via Context.get_state()
    from fastmcp.server.middleware import Middleware, MiddlewareContext

    class APIKeyExtractionMiddleware(Middleware):
        """Extract API key from HTTP headers and store in FastMCP context state."""

        async def on_message(self, context: MiddlewareContext, call_next: Any) -> Any:
            api_key = None

            # Try to get API key from context variable (set by Starlette middleware)
            # This bridges Starlette middleware (HTTP level) with FastMCP middleware (MCP message level)
            with contextlib.suppress(LookupError):
                api_key = _request_api_key.get()

            # Also try to get from FastMCP context if available
            # FastMCP's context might have access to HTTP request
            if not api_key and context.fastmcp_context:
                try:
                    # Try to get HTTP request from FastMCP context
                    # This might be available depending on FastMCP version
                    if hasattr(context.fastmcp_context, "get_http_request"):
                        http_request = context.fastmcp_context.get_http_request()
                        if http_request:
                            api_key = http_request.headers.get(
                                "X-Nexus-API-Key"
                            ) or http_request.headers.get("Authorization", "").replace(
                                "Bearer ", ""
                            )
                except Exception:
                    pass

            # Store in FastMCP's context state so tools can access it via Context.get_state()
            if api_key and context.fastmcp_context:
                try:
                    context.fastmcp_context.set_state("api_key", api_key)
                    # Also set in context variable for backward compatibility
                    _request_api_key.set(api_key)
                except Exception:
                    # If set_state fails, continue anyway
                    pass

            return await call_next(context)

    # Add the middleware to FastMCP
    mcp.add_middleware(APIKeyExtractionMiddleware())

    # =========================================================================
    # FILE OPERATIONS TOOLS
    # =========================================================================

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    def nexus_read_file(path: str, ctx: Context | None = None) -> str:
        """Read file content from Nexus filesystem.

        Args:
            path: File path to read (e.g., "/workspace/data.txt")
            ctx: FastMCP Context (automatically injected, optional for backward compatibility)

        Returns:
            File content as string
        """
        try:
            nx_instance = _get_nexus_instance(ctx)
            content = nx_instance.read(path)
            if isinstance(content, bytes):
                return content.decode("utf-8", errors="replace")
            return str(content)
        except FileNotFoundError:
            return (
                f"Error: File not found at '{path}'. Use nexus_list_files to check available files."
            )
        except PermissionError:
            return f"Error: Permission denied for '{path}'. Check file permissions."
        except Exception as e:
            return f"Error reading file: {str(e)}"

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,  # Can overwrite existing files
            "idempotentHint": False,
            "openWorldHint": True,
        }
    )
    def nexus_write_file(path: str, content: str, ctx: Context | None = None) -> str:
        """Write content to a file in Nexus filesystem.

        Args:
            path: File path to write (e.g., "/workspace/data.txt")
            content: Content to write
            ctx: FastMCP Context (automatically injected, optional for backward compatibility)

        Returns:
            Success message or error
        """
        try:
            nx_instance = _get_nexus_instance(ctx)
            content_bytes = content.encode("utf-8") if isinstance(content, str) else content
            nx_instance.write(path, content_bytes)
            return f"Successfully wrote {len(content_bytes)} bytes to {path}"
        except PermissionError:
            return f"Error: Permission denied for '{path}'. Check write permissions."
        except Exception as e:
            return f"Error writing file: {str(e)}"

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": True,  # Deleting already-deleted file is idempotent
            "openWorldHint": True,
        }
    )
    def nexus_delete_file(path: str, ctx: Context | None = None) -> str:
        """Delete a file from Nexus filesystem.

        Args:
            path: File path to delete (e.g., "/workspace/data.txt")

        Returns:
            Success message or error
        """
        try:
            nx_instance = _get_nexus_instance(ctx)
            nx_instance.delete(path)
            return f"Successfully deleted {path}"
        except FileNotFoundError:
            return f"Error: File not found at '{path}'. It may have already been deleted."
        except PermissionError:
            return f"Error: Permission denied for '{path}'. Check delete permissions."
        except Exception as e:
            return f"Error deleting file: {str(e)}"

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    def nexus_list_files(
        path: str = "/",
        recursive: bool = False,
        details: bool = True,
        limit: int = 50,
        offset: int = 0,
        response_format: str = "json",
        ctx: Context | None = None,
    ) -> str:
        """List files in a directory with pagination support.

        Args:
            path: Directory path to list (default: "/")
            recursive: Whether to list recursively (default: False)
            details: Whether to include detailed metadata including is_directory flag (default: True)
            limit: Maximum number of files to return (default: 50)
            offset: Number of files to skip (default: 0)
            response_format: Output format - "json" (structured) or "markdown" (readable) (default: "json")
            ctx: FastMCP Context (automatically injected, optional for backward compatibility)

        Returns:
            Formatted string (JSON or Markdown) with paginated file list and metadata:
            - total: Total number of files available
            - count: Number of files in this response
            - offset: Starting position of this page
            - items: Array of file objects (when details=True, each includes):
              - path: File/directory path
              - size: Size in bytes (0 for directories)
              - is_directory: Boolean indicating if this is a directory
              - modified_at: Last modification timestamp
              - etag: Content hash
              - mime_type: MIME type
            - has_more: Whether more files are available
            - next_offset: Offset for next page (null if no more results)

        Example:
            >>> nexus_list_files("/workspace", limit=10, offset=0)
            {"total": 150, "count": 10, "offset": 0, "items": [...], "has_more": true, "next_offset": 10}
            >>> nexus_list_files("/workspace", limit=10, response_format="markdown")
            **Total**: 150 | **Count**: 10 | **Offset**: 0
            _More results available (next offset: 10)_
            ### 1. /workspace/file1.txt
            - **size**: 1024
            ...
        """
        try:
            nx_instance = _get_nexus_instance(ctx)
            all_files = nx_instance.list(path, recursive=recursive, details=details)
            total = len(all_files)

            # Apply pagination
            paginated_files = all_files[offset : offset + limit]
            has_more = (offset + limit) < total

            result = {
                "total": total,
                "count": len(paginated_files),
                "offset": offset,
                "items": paginated_files,
                "has_more": has_more,
                "next_offset": offset + limit if has_more else None,
            }

            return format_response(result, response_format)
        except FileNotFoundError:
            return f"Error: Directory not found at '{path}'. Use nexus_list_files('/') to see root contents."
        except Exception as e:
            return f"Error listing files: {str(e)}"

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    def nexus_file_info(path: str, ctx: Context | None = None) -> str:
        """Get detailed information about a file.

        Args:
            path: File path to get info for

        Returns:
            JSON string with file metadata
        """
        try:
            nx_instance = _get_nexus_instance(ctx)
            # Use exists and other methods to get file info
            # info() is not in base NexusFilesystem interface
            if not nx_instance.exists(path):
                return f"Error: File not found at '{path}'. Use nexus_list_files to check available files."

            is_dir = nx_instance.is_directory(path)
            info_dict = {
                "path": path,
                "exists": True,
                "is_directory": is_dir,
            }

            # Try to get size if it's a file
            if not is_dir:
                try:
                    content = nx_instance.read(path)
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

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,  # Creating existing dir is typically idempotent
            "openWorldHint": True,
        }
    )
    def nexus_mkdir(path: str, ctx: Context | None = None) -> str:
        """Create a directory in Nexus filesystem.

        Args:
            path: Directory path to create (e.g., "/workspace/data")

        Returns:
            Success message or error
        """
        try:
            nx_instance = _get_nexus_instance(ctx)
            nx_instance.mkdir(path)
            return f"Successfully created directory {path}"
        except FileExistsError:
            return f"Directory already exists at '{path}'."
        except PermissionError:
            return f"Error: Permission denied for '{path}'. Check write permissions."
        except Exception as e:
            return f"Error creating directory: {str(e)}"

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": True,  # Removing already-removed dir is idempotent
            "openWorldHint": True,
        }
    )
    def nexus_rmdir(path: str, recursive: bool = False, ctx: Context | None = None) -> str:
        """Remove a directory from Nexus filesystem.

        Args:
            path: Directory path to remove (e.g., "/workspace/data")
            recursive: Whether to remove recursively (default: False)

        Returns:
            Success message or error
        """
        try:
            nx_instance = _get_nexus_instance(ctx)
            nx_instance.rmdir(path, recursive=recursive)
            return f"Successfully removed directory {path}"
        except FileNotFoundError:
            return f"Error: Directory not found at '{path}'. It may have already been removed."
        except OSError as e:
            if "not empty" in str(e).lower():
                return f"Error: Directory '{path}' is not empty. Use recursive=True to remove non-empty directories."
            return f"Error removing directory: {str(e)}"
        except Exception as e:
            return f"Error removing directory: {str(e)}"

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,  # Can overwrite target if exists
            # idempotentHint=False: Second call fails (source gone after first rename),
            # which produces different behavior/errors even if final state is the same
            "idempotentHint": False,
            "openWorldHint": True,
        }
    )
    def nexus_rename_file(old_path: str, new_path: str, ctx: Context | None = None) -> str:
        """Rename or move a file or directory in Nexus filesystem.

        Args:
            old_path: Current path of the file or directory (e.g., "/workspace/old.txt")
            new_path: New path for the file or directory (e.g., "/workspace/new.txt")

        Returns:
            Success message or error
        """
        try:
            nx_instance = _get_nexus_instance(ctx)
            nx_instance.rename(old_path, new_path)
            return f"Successfully renamed {old_path} to {new_path}"
        except FileNotFoundError:
            return f"Error: Source path '{old_path}' not found. Use nexus_list_files to check available files."
        except FileExistsError:
            return f"Error: Target path '{new_path}' already exists."
        except Exception as e:
            return f"Error renaming file: {str(e)}"

    # =========================================================================
    # SEARCH TOOLS
    # =========================================================================

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    def nexus_glob(
        pattern: str,
        path: str = "/",
        limit: int = 100,
        offset: int = 0,
        response_format: str = "json",
        ctx: Context | None = None,
    ) -> str:
        """Search files using glob pattern with pagination.

        Args:
            pattern: Glob pattern (e.g., "**/*.py", "*.txt")
            path: Base path to search from (default: "/")
            limit: Maximum number of results to return (default: 100)
            offset: Number of results to skip (default: 0)
            response_format: Output format - "json" or "markdown" (default: "json")

        Returns:
            Formatted string with paginated search results containing:
            - total: Total number of matches found
            - count: Number of matches in this page
            - offset: Current offset
            - items: List of matching file paths
            - has_more: Whether more results are available
            - next_offset: Offset for next page (if has_more is true)

        Example:
            To find all Python files: nexus_glob("**/*.py", "/workspace")
            With pagination: nexus_glob("**/*.py", "/workspace", limit=50, offset=0)
        """
        try:
            nx_instance = _get_nexus_instance(ctx)
            all_matches = nx_instance.glob(pattern, path)
            total = len(all_matches)

            # Apply pagination
            paginated_matches = all_matches[offset : offset + limit]
            has_more = (offset + limit) < total

            result = {
                "total": total,
                "count": len(paginated_matches),
                "offset": offset,
                "items": paginated_matches,
                "has_more": has_more,
                "next_offset": offset + limit if has_more else None,
            }

            return format_response(result, response_format)
        except Exception as e:
            return f"Error in glob search: {str(e)}. Check pattern syntax (e.g., '**/*.py' for recursive Python files)."

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    def nexus_grep(
        pattern: str,
        path: str = "/",
        ignore_case: bool = False,
        limit: int = 100,
        offset: int = 0,
        response_format: str = "json",
        ctx: Context | None = None,
    ) -> str:
        """Search file contents using regex pattern with pagination.

        Args:
            pattern: Regex pattern to search for
            path: Base path to search from (default: "/")
            ignore_case: Whether to ignore case (default: False)
            limit: Maximum number of results to return (default: 100)
            offset: Number of results to skip (default: 0)
            response_format: Output format - "json" or "markdown" (default: "json")

        Returns:
            Formatted string with paginated search results containing:
            - total: Total number of matches found
            - count: Number of matches in this page
            - offset: Current offset
            - items: List of matches (file paths, line numbers, content)
            - has_more: Whether more results are available
            - next_offset: Offset for next page (if has_more is true)

        Example:
            To get first 50 matches: nexus_grep("TODO", "/workspace", limit=50)
            To get next 50 matches: nexus_grep("TODO", "/workspace", limit=50, offset=50)
        """
        try:
            nx_instance = _get_nexus_instance(ctx)
            all_results = nx_instance.grep(pattern, path, ignore_case=ignore_case)
            total = len(all_results)

            # Apply pagination
            paginated_results = all_results[offset : offset + limit]
            has_more = (offset + limit) < total

            result = {
                "total": total,
                "count": len(paginated_results),
                "offset": offset,
                "items": paginated_results,
                "has_more": has_more,
                "next_offset": offset + limit if has_more else None,
            }

            return format_response(result, response_format)
        except Exception as e:
            return f"Error in grep search: {str(e)}. Check regex pattern syntax."

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    def nexus_semantic_search(
        query: str,
        limit: int = 10,
        offset: int = 0,
        response_format: str = "json",
        ctx: Context | None = None,
    ) -> str:
        """Search files semantically using natural language query with pagination.

        Args:
            query: Natural language search query
            limit: Maximum number of results to return (default: 10)
            offset: Number of results to skip (default: 0)
            response_format: Output format - "json" or "markdown" (default: "json")

        Returns:
            Formatted string with paginated search results containing:
            - total: Total number of results found
            - count: Number of results in this page
            - offset: Current offset
            - items: List of search results
            - has_more: Whether more results are available
            - next_offset: Offset for next page (if has_more is true)

        Example:
            First page: nexus_semantic_search("machine learning algorithms", limit=10)
            Next page: nexus_semantic_search("machine learning algorithms", limit=10, offset=10)
        """
        try:
            nx_instance = _get_nexus_instance(ctx)
            # Check if nx has semantic_search method (only available in NexusFS)
            if hasattr(nx_instance, "semantic_search"):
                import asyncio

                # Get results with high limit to support pagination
                # Note: We fetch offset+limit*2 to efficiently check if there are more results
                fetch_limit = offset + limit * 2
                # Call async semantic_search method
                all_results = asyncio.run(
                    nx_instance.semantic_search(query, path="/", limit=fetch_limit)
                )
                total = len(all_results)

                # Apply pagination
                paginated_results = all_results[offset : offset + limit]
                has_more = (offset + limit) < total

                result = {
                    "total": total,
                    "count": len(paginated_results),
                    "offset": offset,
                    "items": paginated_results,
                    "has_more": has_more,
                    "next_offset": offset + limit if has_more else None,
                }

                return format_response(result, response_format)
            return (
                "Semantic search not available (requires NexusFS with semantic search initialized)"
            )
        except Exception as e:
            # If semantic search is not initialized, return a consistent "not available" message
            error_msg = str(e)
            if "not initialized" in error_msg.lower():
                return "Semantic search not available (not initialized)"
            return f"Error in semantic search: {error_msg}"

    # =========================================================================
    # MEMORY TOOLS
    # =========================================================================

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,  # Each store creates a new memory entry
            "openWorldHint": True,
        }
    )
    def nexus_store_memory(
        content: str,
        memory_type: str | None = None,
        importance: float = 0.5,
        ctx: Context | None = None,
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
            nx_instance = _get_nexus_instance(ctx)
            # Check if nx has memory attribute (only available in NexusFS)
            if not hasattr(nx_instance, "memory"):
                return "Memory system not available (requires NexusFS)"

            nx_instance.memory.store(
                content,
                scope="user",
                memory_type=memory_type,
                importance=importance,
            )
            # Commit the session to persist (only for local NexusFS)
            # RemoteMemory handles commit/rollback server-side automatically
            if hasattr(nx_instance.memory, "session"):
                nx_instance.memory.session.commit()
            return f"Successfully stored memory: {content[:80]}..."
        except Exception as e:
            # Rollback on error (only for local NexusFS)
            if hasattr(nx_instance, "memory") and hasattr(nx_instance.memory, "session"):
                with contextlib.suppress(Exception):
                    nx_instance.memory.session.rollback()
            return f"Error storing memory: {str(e)}"

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    def nexus_query_memory(
        query: str,
        memory_type: str | None = None,
        limit: int = 5,
        ctx: Context | None = None,
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
            nx_instance = _get_nexus_instance(ctx)
            # Check if nx has memory attribute (only available in NexusFS)
            if not hasattr(nx_instance, "memory"):
                return "Memory system not available (requires NexusFS)"

            memories = nx_instance.memory.search(
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

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    def nexus_list_workflows(ctx: Context | None = None) -> str:
        """List available workflows in Nexus.

        Returns:
            JSON string with list of workflows
        """
        try:
            nx_instance = _get_nexus_instance(ctx)
            # Check if nx has workflows attribute (only available in NexusFS)
            if not hasattr(nx_instance, "workflows"):
                return "Workflow system not available (requires NexusFS with workflows enabled)"

            workflows = nx_instance.workflows.list_workflows()
            return json.dumps(workflows, indent=2)
        except Exception as e:
            return f"Error listing workflows: {str(e)}"

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,  # Workflows can modify state
            "idempotentHint": False,  # Workflow execution may have side effects
            "openWorldHint": True,
        }
    )
    def nexus_execute_workflow(
        name: str, inputs: str | None = None, ctx: Context | None = None
    ) -> str:
        """Execute a workflow by name.

        Args:
            name: Workflow name
            inputs: Optional JSON string with workflow inputs

        Returns:
            Workflow execution result
        """
        try:
            nx_instance = _get_nexus_instance(ctx)
            # Check if nx has workflows attribute (only available in NexusFS)
            if not hasattr(nx_instance, "workflows"):
                return "Workflow system not available (requires NexusFS with workflows enabled)"

            input_dict = json.loads(inputs) if inputs else {}
            result = nx_instance.workflows.execute(name, **input_dict)
            return json.dumps(result, indent=2)
        except json.JSONDecodeError:
            return "Error: Invalid JSON in inputs parameter. Provide valid JSON string."
        except Exception as e:
            return f"Error executing workflow: {str(e)}"

    # =========================================================================
    # SANDBOX EXECUTION TOOLS (Conditional Registration)
    # =========================================================================

    # Check if sandbox support is available (use default connection for check)
    sandbox_available = False
    try:
        if hasattr(_default_nx, "_ensure_sandbox_manager"):
            _default_nx._ensure_sandbox_manager()
            if (
                hasattr(_default_nx, "_sandbox_manager")
                and _default_nx._sandbox_manager is not None
            ):
                sandbox_available = len(_default_nx._sandbox_manager.providers) > 0
    except Exception:
        sandbox_available = False

    # Only register sandbox tools if available
    if sandbox_available:

        @mcp.tool()
        def nexus_python(code: str, sandbox_id: str, ctx: Context | None = None) -> str:
            """Execute Python code in Nexus sandbox.

            Args:
                code: Python code to execute
                sandbox_id: Sandbox ID (use nexus_sandbox_create to create one)

            Returns:
                Execution result with stdout, stderr, exit_code, and execution time
            """
            try:
                nx_instance = _get_nexus_instance(ctx)
                result = nx_instance.sandbox_run(
                    sandbox_id=sandbox_id, language="python", code=code, timeout=300
                )

                # Format output
                output_parts = []

                stdout = result.get("stdout", "").strip()
                if stdout:
                    output_parts.append(f"Output:\n{stdout}")

                stderr = result.get("stderr", "").strip()
                if stderr:
                    output_parts.append(f"Errors:\n{stderr}")

                exit_code = result.get("exit_code", -1)
                exec_time = result.get("execution_time", 0)
                output_parts.append(f"Exit code: {exit_code}")
                output_parts.append(f"Execution time: {exec_time:.3f}s")

                return (
                    "\n\n".join(output_parts)
                    if output_parts
                    else "Code executed successfully (no output)"
                )

            except Exception as e:
                return f"Error executing Python code: {str(e)}"

        @mcp.tool()
        def nexus_bash(command: str, sandbox_id: str, ctx: Context | None = None) -> str:
            """Execute bash commands in Nexus sandbox.

            Args:
                command: Bash command to execute
                sandbox_id: Sandbox ID (use nexus_sandbox_create to create one)

            Returns:
                Execution result with stdout, stderr, exit_code, and execution time
            """
            try:
                nx_instance = _get_nexus_instance(ctx)
                result = nx_instance.sandbox_run(
                    sandbox_id=sandbox_id, language="bash", code=command, timeout=300
                )

                # Format output
                output_parts = []

                stdout = result.get("stdout", "").strip()
                if stdout:
                    output_parts.append(f"Output:\n{stdout}")

                stderr = result.get("stderr", "").strip()
                if stderr:
                    output_parts.append(f"Errors:\n{stderr}")

                exit_code = result.get("exit_code", -1)
                exec_time = result.get("execution_time", 0)
                output_parts.append(f"Exit code: {exit_code}")
                output_parts.append(f"Execution time: {exec_time:.3f}s")

                return (
                    "\n\n".join(output_parts)
                    if output_parts
                    else "Command executed successfully (no output)"
                )

            except Exception as e:
                return f"Error executing bash command: {str(e)}"

        @mcp.tool()
        def nexus_sandbox_create(
            name: str, ttl_minutes: int = 10, ctx: Context | None = None
        ) -> str:
            """Create a new sandbox for code execution.

            Args:
                name: User-friendly sandbox name
                ttl_minutes: Idle timeout in minutes (default: 10)

            Returns:
                JSON string with sandbox_id and metadata
            """
            try:
                nx_instance = _get_nexus_instance(ctx)
                result = nx_instance.sandbox_create(name=name, ttl_minutes=ttl_minutes)
                return json.dumps(result, indent=2)
            except Exception as e:
                return f"Error creating sandbox: {str(e)}"

        @mcp.tool()
        def nexus_sandbox_list(ctx: Context | None = None) -> str:
            """List all active sandboxes.

            Returns:
                JSON string with list of sandboxes
            """
            try:
                nx_instance = _get_nexus_instance(ctx)
                result = nx_instance.sandbox_list()
                return json.dumps(result, indent=2)
            except Exception as e:
                return f"Error listing sandboxes: {str(e)}"

        @mcp.tool()
        def nexus_sandbox_stop(sandbox_id: str, ctx: Context | None = None) -> str:
            """Stop and destroy a sandbox.

            Args:
                sandbox_id: Sandbox ID to stop

            Returns:
                Success message or error
            """
            try:
                nx_instance = _get_nexus_instance(ctx)
                nx_instance.sandbox_stop(sandbox_id)
                return f"Successfully stopped sandbox {sandbox_id}"
            except Exception as e:
                return f"Error stopping sandbox: {str(e)}"

    # =========================================================================
    # RESOURCES
    # =========================================================================

    @mcp.resource("nexus://files/{path}")
    def get_file_resource(path: str, ctx: Context | None = None) -> str:
        """Browse files as MCP resources.

        Args:
            path: File path to access

        Returns:
            File content
        """
        try:
            nx_instance = _get_nexus_instance(ctx)
            content = nx_instance.read(path)
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

    # Get configuration from environment
    import os

    from nexus import connect

    remote_url = os.getenv("NEXUS_URL")
    api_key = os.getenv("NEXUS_API_KEY")

    # Transport configuration (supports both local and remote modes)
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_PORT", "8081"))

    # Create and run server
    nx = None
    if not remote_url:
        nx = connect()

    mcp = create_mcp_server(nx=nx, remote_url=remote_url, api_key=api_key)

    # Add API key middleware for HTTP transports
    # Note: We add it to the underlying Starlette app, not FastMCP's middleware system
    # because FastMCP's middleware works with MCP messages, not HTTP requests
    if transport in ["http", "sse"]:
        try:
            from starlette.middleware.base import BaseHTTPMiddleware

            class APIKeyMiddleware(BaseHTTPMiddleware):
                """Extract API key from HTTP headers and set in context."""

                async def dispatch(self, request: Any, call_next: Any) -> Any:
                    api_key = request.headers.get("X-Nexus-API-Key") or request.headers.get(
                        "Authorization", ""
                    ).replace("Bearer ", "")
                    token = set_request_api_key(api_key) if api_key else None
                    try:
                        response = await call_next(request)
                        return response
                    finally:
                        if token:
                            _request_api_key.reset(token)

            # Add middleware to the underlying Starlette app
            # FastMCP's http_app is a method that returns the Starlette application
            if hasattr(mcp, "http_app"):
                app = mcp.http_app()
                app.add_middleware(APIKeyMiddleware)
        except (ImportError, Exception) as e:
            # If middleware addition fails, log but don't crash
            import logging

            logger = logging.getLogger(__name__)
            logger.warning(f"Failed to add API key middleware: {e}")

    # Run with selected transport
    if transport == "stdio":
        mcp.run(transport="stdio")
    elif transport == "http":
        mcp.run(transport="http", host=host, port=port)
    elif transport == "sse":
        mcp.run(transport="sse", host=host, port=port)
    else:
        raise ValueError(f"Unknown transport: {transport}")


if __name__ == "__main__":
    main()
