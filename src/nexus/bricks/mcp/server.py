"""Nexus MCP Server Implementation.

This module implements a Model Context Protocol (MCP) server that exposes
Nexus functionality to AI agents and tools using the fastmcp framework.
"""

from __future__ import annotations

import contextlib
import contextvars
import inspect
import json
import logging
from typing import TYPE_CHECKING, Any, cast

from cachetools import LRUCache
from fastmcp import Context, FastMCP

from nexus.bricks.mcp.auth_bridge import op_context_to_auth_dict as _op_context_to_auth_dict
from nexus.bricks.mcp.auth_bridge import (
    resolve_mcp_operation_context as _resolve_mcp_operation_context,
)
from nexus.bricks.mcp.formatters import format_response
from nexus.bricks.mcp.tool_utils import handle_tool_errors, tool_error
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.lib.pagination import build_paginated_list_response

if TYPE_CHECKING:
    from nexus.core.nexus_fs import NexusFS

logger = logging.getLogger(__name__)

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
        >>> from nexus.bricks.mcp import set_request_api_key, reset_request_api_key
        >>>
        >>> # In middleware or proxy code:
        >>> token = set_request_api_key("sk-user-api-key-xyz")
        >>> try:
        ...     # Make MCP tool calls here - they will use this API key
        ...     result = mcp_server.call_tool("nexus_read_file", path="/data.txt")
        ... finally:
        ...     # Clean up context
        ...     reset_request_api_key(token)
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


def reset_request_api_key(token: contextvars.Token[str | None]) -> None:
    """Reset the request API key context variable using a previously saved token.

    Args:
        token: The token returned by set_request_api_key()
    """
    _request_api_key.reset(token)


async def create_mcp_server(
    nx: NexusFS | None = None,
    name: str = "nexus",
    remote_url: str | None = None,
    api_key: str | None = None,
    tool_namespace_middleware: Any | None = None,
    manifest_resolver: Any | None = None,
    permission_enforcer: Any | None = None,
    auth_provider: Any | None = None,
) -> FastMCP:
    """Create an MCP server for Nexus operations.

    Args:
        nx: NexusFS instance (if None, will auto-connect)
        name: Server name (default: "nexus")
        remote_url: Remote Nexus URL for connecting to remote server
        api_key: Optional API key for remote server authentication (default)
        tool_namespace_middleware: Optional ToolNamespaceMiddleware for per-tool
            namespace filtering. When provided, discovery tools filter results
            to only show tools visible to the current subject.
        manifest_resolver: Optional callable for context manifest resolution
            (Issue #2984). When provided, enables the ``nexus_resolve_context``
            tool. Expected signature: ``(sources_json: str, variables_json: str)
            -> dict`` returning resolution results. Built by the factory via
            ``build_manifest_resolve_fn()``.
        permission_enforcer: Optional PermissionEnforcer for file-level ReBAC
            filtering on MCP search results (#3731). When provided, MCP
            ``nexus_grep`` and ``nexus_glob`` apply the same
            ``_apply_rebac_filter`` that the HTTP endpoints use.
        auth_provider: Optional auth provider for resolving per-request API
            keys to subject identity (#3731). Used by
            ``_resolve_mcp_operation_context`` to build an authoritative
            ``OperationContext`` from ``_request_api_key``.

    Returns:
        FastMCP server instance

    Infrastructure API Key Support:
        The MCP server supports per-request API keys set by infrastructure
        (e.g., HTTP middleware, proxy, gateway) without exposing them to AI agents.

        Infrastructure should set the API key using:
            from nexus.bricks.mcp import set_request_api_key, reset_request_api_key
            token = set_request_api_key("sk-user-api-key-xyz")
            try:
                # Make MCP tool calls here
                pass
            finally:
                reset_request_api_key(token)

        The api_key parameter serves as the default when no per-request key is set.

    Examples:
        >>> from nexus import connect
        >>> from nexus.bricks.mcp import create_mcp_server
        >>>
        >>> # Local filesystem
        >>> nx = connect()
        >>> server = create_mcp_server(nx)
        >>>
        >>> # Remote filesystem
        >>> server = create_mcp_server(remote_url="http://localhost:2026")
        >>>
        >>> # Remote filesystem with API key
        >>> server = create_mcp_server(
        ...     remote_url="http://localhost:2026",
        ...     api_key="your-api-key"
        ... )
    """
    # Initialize Nexus filesystem if not provided
    if nx is None:
        if remote_url:
            import nexus as _nexus

            nx = _nexus.connect(config={"profile": "remote", "url": remote_url, "api_key": api_key})
        else:
            import importlib as _il

            connect = _il.import_module("nexus").connect
            nx = connect()

    # Auto-detect manifest resolver from NexusFS if not explicitly provided.
    # Uses importlib to avoid a static cross-brick import chain that
    # import-linter would flag (mcp -> factory -> context_manifest).
    if manifest_resolver is None and nx is not None:
        _raw_resolver = getattr(nx, "manifest_resolver", None)
        if _raw_resolver is not None:
            try:
                import importlib as _il_manifest

                _adapter_mod = _il_manifest.import_module("nexus.factory.manifest_adapter")
                manifest_resolver = _adapter_mod.build_manifest_resolve_fn(_raw_resolver, nx)
            except Exception:
                pass  # Graceful degradation — tool returns "unavailable"

    # NOTE: permission_enforcer and auth_provider are intentionally NOT
    # auto-resolved from NexusFS services. A NexusFS with enforce=False
    # may still register a PermissionEnforcer service that denies all
    # requests (no grants → empty permit list). Callers that need ReBAC
    # must pass permission_enforcer explicitly. The HTTP server does
    # this via app.state.permission_enforcer; the CLI MCP command
    # should thread it when auth is configured (#3731).

    # Store default connection and config for per-request API key support
    assert nx is not None  # guaranteed by the if-block above
    _default_nx: NexusFS = nx
    _remote_url = remote_url

    # Connection pool for per-request API keys (bounded LRU, cached by API key)
    _connection_cache: LRUCache[str, NexusFS] = LRUCache(maxsize=256)

    def _get_nexus_instance(_ctx: Context | None = None) -> NexusFS:
        """Get Nexus instance for current request using context API key.

        This function checks if infrastructure has set a per-request API key
        in the context variable or FastMCP's context state. If so, it creates/retrieves
        a connection with that API key. Otherwise, it returns the default connection.

        Args:
            ctx: Optional FastMCP Context object (if available from tool)

        Returns:
            NexusFS instance (default or per-request based on context)

        Note:
            Per-request API keys are only supported when remote_url is configured.
            For local connections, the default connection is always used.
        """

        # Get API key from context variable (set by Starlette middleware or
        # APIKeyExtractionMiddleware). Context.get_state() is async in fastmcp
        # 3.x and cannot be called from sync tool functions, so we rely solely
        # on the sync contextvars path.
        request_api_key: str | None = _request_api_key.get()

        # If no API key in context, use default connection
        if not request_api_key:
            return _default_nx

        # If remote_url not configured, can't use per-request API keys
        if not _remote_url:
            return _default_nx

        # Check cache for existing connection
        if request_api_key in _connection_cache:
            return _connection_cache[request_api_key]

        # Create new remote connection with API key from context.
        # nexus.connect() is async, so run it via a background thread
        # to avoid blocking the current event loop.
        import concurrent.futures

        import nexus as _nexus

        def _connect_sync() -> NexusFS:
            return _nexus.connect(
                config={"profile": "remote", "url": _remote_url, "api_key": request_api_key}
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            new_nx = pool.submit(_connect_sync).result()

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
                except Exception as e:
                    logger.debug("Failed to extract API key from request: %s", e)

            # Store in FastMCP's context state so tools can access it via Context.get_state()
            if api_key and context.fastmcp_context:
                try:
                    _result = cast(Any, context.fastmcp_context.set_state)("api_key", api_key)
                    if inspect.isawaitable(_result):
                        await _result
                    # Also set in context variable (sync path for tool functions)
                    _request_api_key.set(api_key)
                except Exception:
                    # If set_state fails, continue anyway
                    pass

            return await call_next(context)

    # Add the middleware to FastMCP
    mcp.add_middleware(APIKeyExtractionMiddleware())

    # Add tool namespace middleware if provided (Issue #1272)
    if tool_namespace_middleware is not None:
        mcp.add_middleware(tool_namespace_middleware)

    def _get_visible_tool_names(ctx: Context | None) -> frozenset[str] | None:
        """Get visible tool names for the current subject via namespace middleware.

        Delegates to ``ToolNamespaceMiddleware.resolve_visible_tools()`` to
        avoid duplicating subject extraction logic (#1A DRY fix).

        Returns:
            frozenset of visible tool names, or None if namespace filtering
            is not configured (backward compat → all tools visible).
        """
        if tool_namespace_middleware is None:
            return None
        result: frozenset[str] | None = tool_namespace_middleware.resolve_visible_tools(ctx)
        return result

    # =========================================================================
    # Markdown structure helpers (Issue #3718)
    # =========================================================================

    def _md_get_etag(nx_instance: NexusFS, path: str) -> str:
        """Get the authoritative file content_id from the metastore primary row."""
        meta = getattr(nx_instance, "metadata", None)
        if meta is None:
            return ""
        try:
            file_meta = meta.get(path)
            return file_meta.content_id if file_meta and file_meta.content_id else ""
        except Exception:
            return ""

    def _md_section_read(
        nx_instance: NexusFS,
        path: str,
        content: bytes,
        section: str,
        block_type: str | None = None,
    ) -> str | None:
        """Attempt a partial markdown read using the structural index.

        Returns section content as string, or None to fall back to full read.
        Uses the service registry to access the md_structure hook (no
        cross-brick imports).
        """
        hook = nx_instance.service("md_structure") if hasattr(nx_instance, "service") else None
        if hook is None or not hasattr(hook, "read_section"):
            return None

        content_id = _md_get_etag(nx_instance, path)
        return hook.read_section(path, content, content_id, section, block_type)

    def _md_get_structure_listing(
        nx_instance: NexusFS,
        path: str,
        content: bytes | None = None,
        content_id: str = "",
    ) -> list[dict[str, Any]] | None:
        """Get the structure listing for a markdown file.

        Passes content + hash so the hook can lazily rebuild the index
        for files that were never indexed (pre-existing or cache miss).
        """
        hook = nx_instance.service("md_structure") if hasattr(nx_instance, "service") else None
        if hook is None or not hasattr(hook, "get_structure_listing"):
            return None

        return hook.get_structure_listing(path, content=content, content_id=content_id)

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
    @handle_tool_errors("reading file")
    async def nexus_read_file(
        path: str,
        section: str | None = None,
        block_type: str | None = None,
        ctx: Context | None = None,
    ) -> str:
        """Read file content from Nexus filesystem.

        For markdown files (.md), supports partial reads by section and block type
        to reduce context window usage (Issue #3718).

        Args:
            path: File path to read (e.g., "/workspace/data.txt")
            section: (Markdown only) Read a specific section by heading text.
                Case-insensitive, supports substring matching.
                Special values:
                  - ``"*"`` — list document structure (headings, token estimates, block types) without content
                  - ``"frontmatter"`` — read only the YAML frontmatter block
                Example: section="Authentication" reads only that section.
            block_type: (Markdown only) Filter by block type within a section.
                Requires ``section`` to be set. Values: "code", "table".
                Example: section="Auth", block_type="code" returns only code blocks.
            ctx: FastMCP Context (automatically injected, optional for backward compatibility)

        Returns:
            File content as string (full file, or section/block subset for markdown)
        """
        nx_instance = _get_nexus_instance(ctx)
        content = nx_instance.sys_read(path)
        content_bytes = content if isinstance(content, bytes) else str(content).encode("utf-8")

        # Partial read for markdown files when section is requested.
        if section and path.endswith(".md"):
            result = _md_section_read(nx_instance, path, content_bytes, section, block_type)
            if result is not None:
                return result
            # Any explicit section selector that returned None — don't leak full doc.
            if section == "frontmatter":
                return tool_error("not_found", f"No frontmatter found in {path}")
            if section == "*":
                return tool_error("not_found", f"No markdown structure available for {path}")
            return tool_error(
                "not_found",
                f"Section '{section}' not found in {path}. "
                f"Use section='*' to list available sections.",
            )

        return content_bytes.decode("utf-8", errors="replace")

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @handle_tool_errors("reading markdown structure")
    async def nexus_md_structure(path: str, ctx: Context | None = None) -> str:
        """List the structure of a markdown file without loading its content.

        Returns headings, section token estimates, and block types — enabling
        targeted reads via ``nexus_read_file(path, section=...)`` to minimize
        context window usage.

        Args:
            path: Path to a markdown file (e.g., "/workspace/docs/arch.md")
            ctx: FastMCP Context (automatically injected)

        Returns:
            JSON structure listing with sections, depths, token estimates,
            and block types present in each section.
        """
        nx_instance = _get_nexus_instance(ctx)
        # Permission gate + content fetch for lazy index rebuild.
        try:
            raw = nx_instance.sys_read(path)
        except Exception as e:
            return tool_error("access_denied", f"Cannot access {path}: {e}")
        content = raw if isinstance(raw, bytes) else str(raw).encode("utf-8")
        content_id = _md_get_etag(nx_instance, path)
        listing = _md_get_structure_listing(
            nx_instance, path, content=content, content_id=content_id
        )
        if listing is None:
            return tool_error("not_found", f"No markdown structure available for {path}")
        return json.dumps(listing, indent=2)

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,  # Can overwrite existing files
            "idempotentHint": False,
            "openWorldHint": True,
        }
    )
    @handle_tool_errors("writing file")
    async def nexus_write_file(path: str, content: str, ctx: Context | None = None) -> str:
        """Write content to a file in Nexus filesystem.

        Args:
            path: File path to write (e.g., "/workspace/data.txt")
            content: Content to write
            ctx: FastMCP Context (automatically injected, optional for backward compatibility)

        Returns:
            Success message or error
        """
        nx_instance = _get_nexus_instance(ctx)
        content_bytes = content.encode("utf-8") if isinstance(content, str) else content
        nx_instance.write(path, content_bytes)
        return f"Successfully wrote {len(content_bytes)} bytes to {path}"

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": True,
        }
    )
    @handle_tool_errors("editing file")
    def nexus_edit_file(
        path: str,
        edits: list[dict],
        fuzzy_threshold: float = 0.85,
        preview: bool = False,
        if_match: str | None = None,
        ctx: Context | None = None,
    ) -> str:
        """Apply surgical search/replace edits to an existing file.

        More reliable than nexus_write_file for partial changes:
        - Fuzzy matching if exact match fails
        - Returns diff of changes
        - Supports optimistic concurrency via if_match

        Args:
            path: File path (e.g., "/workspace/src/main.py")
            edits: List of {"old_str": "text to find", "new_str": "replacement"}
            fuzzy_threshold: Similarity threshold for fuzzy matching (0.0-1.0, default: 0.85)
            preview: If True, return preview without writing (default: False)
            if_match: Optional content_id for optimistic concurrency control
            ctx: FastMCP Context (automatically injected, optional for backward compatibility)

        Returns:
            JSON string with edit result (success, diff, matches, errors)

        Example:
            nexus_edit_file(
                path="/workspace/src/main.py",
                edits=[{"old_str": "print('hello')", "new_str": "print('world')"}]
            )
        """
        nx_instance = _get_nexus_instance(ctx)
        result = cast(Any, nx_instance).edit(
            path,
            edits,
            fuzzy_threshold=fuzzy_threshold,
            preview=preview,
            if_match=if_match,
        )
        return json.dumps(result, indent=2)

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": True,  # Deleting already-deleted file is idempotent
            "openWorldHint": True,
        }
    )
    @handle_tool_errors("deleting file")
    async def nexus_delete_file(path: str, ctx: Context | None = None) -> str:
        """Delete a file from Nexus filesystem.

        Args:
            path: File path to delete (e.g., "/workspace/data.txt")

        Returns:
            Success message or error
        """
        nx_instance = _get_nexus_instance(ctx)
        nx_instance.sys_unlink(path)
        return f"Successfully deleted {path}"

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @handle_tool_errors("listing files")
    async def nexus_list_files(
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
              - content_id: Content hash
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
        nx_instance = _get_nexus_instance(ctx)
        try:
            all_files = nx_instance.sys_readdir(path, recursive=recursive, details=details)
        except FileNotFoundError:
            return tool_error(
                "not_found",
                f"Directory not found at '{path}'. Use nexus_list_files('/') to see root contents.",
            )

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

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @handle_tool_errors("getting file info")
    async def nexus_file_info(path: str, ctx: Context | None = None) -> str:
        """Get detailed information about a file.

        Args:
            path: File path to get info for

        Returns:
            JSON string with file metadata
        """
        nx_instance = _get_nexus_instance(ctx)
        if not nx_instance.access(path):
            return tool_error(
                "not_found",
                f"File not found at '{path}'. Use nexus_list_files to check available files.",
            )

        is_dir = nx_instance.is_directory(path)
        info_dict: dict[str, Any] = {
            "path": path,
            "exists": True,
            "is_directory": is_dir,
        }

        # Try to get size if it's a file
        if not is_dir:
            try:
                content = nx_instance.sys_read(path)
                if isinstance(content, bytes):
                    info_dict["size"] = len(content)
            except Exception as e:
                logger.debug("Failed to read file size for %s: %s", path, e)

        return json.dumps(info_dict, indent=2)

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
    @handle_tool_errors("creating directory")
    async def nexus_mkdir(path: str, ctx: Context | None = None) -> str:
        """Create a directory in Nexus filesystem.

        Args:
            path: Directory path to create (e.g., "/workspace/data")

        Returns:
            Success message or error
        """
        nx_instance = _get_nexus_instance(ctx)
        try:
            nx_instance.mkdir(path)
        except FileExistsError:
            return f"Directory already exists at '{path}'."
        return f"Successfully created directory {path}"

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": True,  # Removing already-removed dir is idempotent
            "openWorldHint": True,
        }
    )
    @handle_tool_errors("removing directory")
    async def nexus_rmdir(path: str, recursive: bool = False, ctx: Context | None = None) -> str:
        """Remove a directory from Nexus filesystem.

        Args:
            path: Directory path to remove (e.g., "/workspace/data")
            recursive: Whether to remove recursively (default: False)

        Returns:
            Success message or error
        """
        nx_instance = _get_nexus_instance(ctx)
        try:
            nx_instance.rmdir(path, recursive=recursive)
        except OSError as e:
            if "not empty" in str(e).lower():
                return tool_error(
                    "invalid_input",
                    f"Directory '{path}' is not empty. Use recursive=True to remove non-empty directories.",
                )
            raise  # Let handle_tool_errors catch other OSErrors
        return f"Successfully removed directory {path}"

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
    @handle_tool_errors("renaming file")
    async def nexus_rename_file(old_path: str, new_path: str, ctx: Context | None = None) -> str:
        """Rename or move a file or directory in Nexus filesystem.

        Args:
            old_path: Current path of the file or directory (e.g., "/workspace/old.txt")
            new_path: New path for the file or directory (e.g., "/workspace/new.txt")

        Returns:
            Success message or error
        """
        nx_instance = _get_nexus_instance(ctx)
        try:
            nx_instance.sys_rename(old_path, new_path)
        except FileExistsError:
            return tool_error(
                "invalid_input",
                f"Target path '{new_path}' already exists.",
            )
        return f"Successfully renamed {old_path} to {new_path}"

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
    @handle_tool_errors("searching files (glob)")
    def nexus_glob(
        pattern: str,
        path: str = "/",
        limit: int = 100,
        offset: int = 0,
        response_format: str = "json",
        files: list[str] | None = None,
        ctx: Context | None = None,
    ) -> str:
        """Search files using glob pattern with pagination.

        Args:
            pattern: Glob pattern (e.g., "**/*.py", "*.txt")
            path: Base path to search from (default: "/")
            limit: Maximum number of results to return (default: 100)
            offset: Number of results to skip (default: 0)
            response_format: Output format - "json" or "markdown" (default: "json")
            files: Optional stateless narrowing working set (#3701). When
                provided, the glob pattern is matched against this list
                instead of walking the tree under ``path``.

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
            Narrowed: nexus_glob("**/*.py", files=["/src/a.py", "/src/b.py"])
        """
        from nexus.core.path_utils import split_zone_from_internal_path
        from nexus.lib.rebac_filter import (
            apply_rebac_filter,
            rebac_denial_stats,
        )

        nx_instance: Any = _get_nexus_instance(ctx)
        _search = nx_instance.service("search")
        if _search is None:
            raise ValueError("SearchService not available — glob requires the search brick")
        # Codex review #3 finding #1: build an explicit OperationContext
        # from the connection's authenticated whoami identity so ReBAC
        # filtering sees the real (subject_id, zone_id, is_admin) rather
        # than the ambient identity of whatever default connection the
        # MCP server was booted with. ``_resolve_mcp_operation_context``
        # fails closed if the identity can't be resolved.
        op_context = _resolve_mcp_operation_context(nx_instance, auth_provider=auth_provider)
        # #3731 R2: if a per-request key was set but identity resolution
        # failed (fail-closed → None), reject the request rather than
        # executing with an anonymous/ambient context.
        if op_context is None and _request_api_key.get():
            return tool_error(
                "unauthorized",
                "Per-request API key could not be verified; search denied.",
            )
        auth_result = _op_context_to_auth_dict(op_context)
        zone_id = auth_result.get("zone_id", ROOT_ZONE_ID)

        all_matches = _search.glob(pattern, path, files=files, context=op_context)

        # #3731: Apply file-level ReBAC filtering (second layer,
        # same as HTTP _do_glob_operation).
        pre_filter_count = len(all_matches)
        filtered_paths, filter_ms = apply_rebac_filter(
            all_matches,
            permission_enforcer,
            auth_result,
            zone_id,
            path_extractor=lambda p: p,
        )
        post_filter_count = len(filtered_paths)
        total = post_filter_count

        # Apply pagination
        paginated_matches = filtered_paths[offset : offset + limit]

        # #3731: Zone unscoping — convert internal zone-prefixed paths
        # to user-facing paths and build parallel zone list for
        # round-trip disambiguation (mirrors HTTP _do_glob_operation).
        item_zones: list[str | None] = []
        unscoped_items: list[str] = []
        for p in paginated_matches:
            zone, unscoped = split_zone_from_internal_path(p)
            unscoped_items.append(unscoped)
            item_zones.append(zone)
        paginated_matches = unscoped_items

        # Issue #538: Log truncation when results exceed limit
        if (offset + limit) < total or offset > 0:
            logger.info(
                f"[GLOB] Truncated {total} -> {len(paginated_matches)} results "
                f"(offset={offset}, limit={limit})"
            )

        # #3731: Include permission stats + zone disambiguation in
        # response (parity with HTTP).
        # #3731: Detect multi-zone ambiguity (parity with HTTP
        # _do_glob_operation).
        _keys = list(zip(paginated_matches, item_zones, strict=False))
        glob_multi_zone_ambiguous = len(set(_keys)) < len(_keys)

        extras: dict[str, Any] = {
            **rebac_denial_stats(pre_filter_count, post_filter_count, limit + offset),
            "item_zones": item_zones,
        }
        if glob_multi_zone_ambiguous:
            extras["multi_zone_ambiguous"] = True

        result = build_paginated_list_response(
            items=paginated_matches,
            total=total,
            offset=offset,
            limit=limit,
            extras=extras,
        )

        return format_response(result, response_format)

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @handle_tool_errors("searching file contents (grep)")
    async def nexus_grep(
        pattern: str,
        path: str = "/",
        ignore_case: bool = False,
        limit: int = 100,
        offset: int = 0,
        response_format: str = "json",
        files: list[str] | None = None,
        before_context: int = 0,
        after_context: int = 0,
        invert_match: bool = False,
        block_type: str | None = None,
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
            files: Optional stateless narrowing working set (#3701). When
                provided, grep searches only these files instead of walking
                the tree. Agents should pass the file list from a previous
                search/grep to drill down into its results.
            before_context: Number of lines to include before each match
                (#3701 follow-up). Use for displaying code context around
                hits — equivalent to ``grep -B N``.
            after_context: Number of lines to include after each match
                (#3701 follow-up). Equivalent to ``grep -A N``.
            invert_match: Return non-matching lines instead of matches
                (#3701 follow-up). Equivalent to ``grep -v`` / ``--invert-match``.
            block_type: Restrict matches to a specific markdown block type
                (#3720). Only matches inside blocks of this type are
                returned. Valid values: ``"code"``, ``"table"``,
                ``"frontmatter"``, ``"paragraph"``, ``"blockquote"``,
                ``"list"``, ``"heading"``. Non-markdown files pass
                through unfiltered. Omit for default full-file search.

        Returns:
            Formatted string with paginated search results containing:
            - total: Total number of matches found
            - count: Number of matches in this page
            - offset: Current offset
            - items: List of matches (file paths, line numbers, content, and
              before_context/after_context arrays when requested)
            - has_more: Whether more results are available
            - next_offset: Offset for next page (if has_more is true)

        Example:
            To get first 50 matches: nexus_grep("TODO", "/workspace", limit=50)
            To get next 50 matches: nexus_grep("TODO", "/workspace", limit=50, offset=50)
            Narrowed: nexus_grep("TODO", files=["/src/a.py", "/src/b.py"])
            With context lines: nexus_grep("error", before_context=2, after_context=2)
            Non-matching lines: nexus_grep("debug", invert_match=True)
        """
        from nexus.core.path_utils import split_zone_from_internal_path
        from nexus.lib.rebac_filter import (
            apply_rebac_filter,
            compute_rebac_fetch_limit,
            rebac_denial_stats,
        )

        nx_instance: Any = _get_nexus_instance(ctx)
        _search = nx_instance.service("search")
        if _search is None:
            raise ValueError("SearchService not available — grep requires the search brick")
        # Codex review #3 finding #1: build an explicit OperationContext
        # (see ``_resolve_mcp_operation_context`` for the fail-closed
        # semantics). Previously grep ran without any context so ReBAC
        # filtering fell back to the ambient connection identity.
        op_context = _resolve_mcp_operation_context(nx_instance, auth_provider=auth_provider)
        # #3731 R2: reject if per-request key present but auth failed.
        if op_context is None and _request_api_key.get():
            return tool_error(
                "unauthorized",
                "Per-request API key could not be verified; search denied.",
            )

        # #3731: Build auth_result dict from OperationContext for
        # _apply_rebac_filter. Falls back to anonymous if no context.
        auth_result = _op_context_to_auth_dict(op_context)
        zone_id = auth_result.get("zone_id", ROOT_ZONE_ID)

        # Sentinel fetch + ReBAC over-fetch (#3731).
        window_size = limit + offset
        sentinel_window = window_size + 1
        fetch_limit = compute_rebac_fetch_limit(
            sentinel_window, has_enforcer=permission_enforcer is not None
        )
        grep_kwargs: dict[str, Any] = {
            "ignore_case": ignore_case,
            "max_results": max(fetch_limit, 1),
            "files": files,
            "context": op_context,
        }
        # Only forward the context/invert flags when set so older servers
        # without the #3701 fields still accept the request.
        if before_context:
            grep_kwargs["before_context"] = before_context
        if after_context:
            grep_kwargs["after_context"] = after_context
        if invert_match:
            grep_kwargs["invert_match"] = True
        if block_type is not None:
            grep_kwargs["block_type"] = block_type

        # SearchService.grep() is async in local mode but the
        # RemoteServiceProxy returns a sync result. Handle both.
        _grep_result = _search.grep(pattern, path, **grep_kwargs)
        if inspect.isawaitable(_grep_result):
            _grep_result = await _grep_result
        all_results = _grep_result

        # #3731: Apply file-level ReBAC filtering (second layer,
        # same as HTTP _do_grep_operation).
        pre_filter_count = len(all_results)
        filtered_results, filter_ms = apply_rebac_filter(
            all_results,
            permission_enforcer,
            auth_result,
            zone_id,
            path_extractor=lambda r: r.get("file", ""),
        )
        post_filter_count = len(filtered_results)

        # Sentinel-based has_more (post-ReBAC).
        has_more = post_filter_count > window_size
        total = post_filter_count

        # Apply pagination.
        paginated_results = filtered_results[offset : offset + limit]

        # #3731: Zone unscoping — convert internal zone-prefixed paths
        # to user-facing paths and annotate with zone_id for round-trip
        # disambiguation (mirrors HTTP _do_grep_operation).
        annotated: list[dict[str, Any]] = []
        for r in paginated_results:
            out = dict(r)
            raw_file = r.get("file", "")
            zone, unscoped = split_zone_from_internal_path(raw_file)
            out["file"] = unscoped
            if zone is not None:
                out["zone_id"] = zone
            annotated.append(out)
        paginated_results = annotated

        # Issue #538: Log truncation when results exceed limit
        if has_more or offset > 0:
            logger.info(
                f"[GREP] Truncated ({'+' if has_more else ''}{total}) -> "
                f"{len(paginated_results)} results (offset={offset}, limit={limit})"
            )

        # #3731: Detect multi-zone ambiguity (parity with HTTP
        # _do_grep_operation). Two distinct raw paths that collapse to
        # the same (file, zone_id) tuple degrade round-trip safety.
        _keys = [(it["file"], it.get("zone_id")) for it in paginated_results]
        multi_zone_ambiguous = len(set(_keys)) < len(_keys)

        # #3731: Include permission stats in response (parity with HTTP).
        extras: dict[str, Any] = {
            **rebac_denial_stats(pre_filter_count, post_filter_count, window_size),
        }
        if multi_zone_ambiguous:
            extras["multi_zone_ambiguous"] = True

        result = build_paginated_list_response(
            items=paginated_results,
            total=total,
            offset=offset,
            limit=limit,
            has_more=has_more,
            extras=extras,
        )

        return format_response(result, response_format)

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @handle_tool_errors("semantic search")
    async def nexus_semantic_search(
        query: str,
        path: str = "/",
        search_mode: str = "semantic",
        limit: int = 10,
        offset: int = 0,
        response_format: str = "json",
        ctx: Context | None = None,
    ) -> str:
        """Search files semantically using natural language query with pagination.

        Args:
            query: Natural language search query
            path: Directory path to scope the search (default: "/" searches everywhere)
            search_mode: Search mode - "semantic" (default, embedding-based),
                "keyword" (BM25/text, faster), or "hybrid" (both pipelines,
                higher quality but more expensive)
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
            Scoped search: nexus_semantic_search("auth logic", path="/workspace/src")
            Hybrid mode: nexus_semantic_search("token refresh", search_mode="hybrid")
            First page: nexus_semantic_search("machine learning algorithms", limit=10)
            Next page: nexus_semantic_search("machine learning algorithms", limit=10, offset=10)
        """
        nx_instance = _get_nexus_instance(ctx)

        # Resolve SearchService via the kernel service registry (Issue #3778).
        # NexusFS does not expose ``semantic_search`` as a direct attribute —
        # the method lives on SearchService, reached through ``nx.service("search")``.
        search_service: Any = None
        try:
            svc_fn = getattr(nx_instance, "service", None)
            if svc_fn is not None:
                search_service = svc_fn("search")
        except Exception:
            search_service = None

        if search_service is None or not hasattr(search_service, "semantic_search"):
            return tool_error(
                "unavailable",
                "Semantic search not available (search brick not loaded).",
            )

        # R4 review: pass an authenticated OperationContext so SearchService
        # can enforce ReBAC permission filtering on semantic results. Without
        # it, broad SANDBOX degraded queries would return cross-zone hits to
        # any MCP caller. Fail closed if identity can't be resolved while a
        # per-request API key was set (#3731 pattern used in glob/grep).
        op_context = _resolve_mcp_operation_context(nx_instance, auth_provider=auth_provider)
        if op_context is None and _request_api_key.get():
            return tool_error(
                "unauthorized",
                "Per-request API key could not be verified; semantic search denied.",
            )

        try:
            # Over-fetch to allow has_more detection without a second round-trip
            fetch_limit = offset + limit * 2
            all_results = await search_service.semantic_search(
                query=query,
                path=path,
                search_mode=search_mode,
                limit=fetch_limit,
                context=op_context,
            )
        except Exception as e:
            if "not initialized" in str(e).lower() or "not available" in str(e).lower():
                return tool_error("unavailable", "Semantic search not available (not initialized).")
            return tool_error("internal", f"Error in semantic search: {e}", str(e))

        total = len(all_results)
        paginated_results = all_results[offset : offset + limit]
        has_more = (offset + limit) < total

        # Issue #3778: surface the SANDBOX BM25S-fallback flag at the envelope
        # level so clients can display a "degraded" indicator without having
        # to scan every item. Two sources:
        #   1. Per-item stamp (``semantic_degraded`` on a result dict) — works
        #      when fallback returned at least one hit.
        #   2. Per-request contextvar (LAST_SEMANTIC_DEGRADED) set inside the
        #      SearchService fallback — works even when fallback returned
        #      zero results, so an outage is still distinguishable from a
        #      genuine no-hit query (R2 review).
        from nexus.contracts.search_types import LAST_SEMANTIC_DEGRADED

        degraded = LAST_SEMANTIC_DEGRADED.get() or any(
            isinstance(r, dict) and r.get("semantic_degraded") is True for r in paginated_results
        )

        result: dict[str, Any] = {
            "total": total,
            "count": len(paginated_results),
            "offset": offset,
            "items": paginated_results,
            "has_more": has_more,
            "next_offset": offset + limit if has_more else None,
        }
        if degraded:
            result["semantic_degraded"] = True

        return format_response(result, response_format)

    # =========================================================================
    # CONTEXT MANIFEST TOOLS (Issue #2984)
    # =========================================================================

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
    )
    @handle_tool_errors("resolving context manifest")
    def nexus_resolve_context(
        sources: str,
        variables: str = "{}",
        response_format: str = "json",
        ctx: Context | None = None,  # noqa: ARG001
    ) -> str:
        """Resolve a context manifest by executing all sources in parallel.

        Implements the Stripe Minions "deterministic pre-execution" pattern:
        all sources are resolved in parallel before the agent starts reasoning.
        Supports 4 source types: file_glob, memory_query, workspace_snapshot,
        mcp_tool.

        Args:
            sources: JSON string containing an array of context source
                definitions. Each source must have a "type" field.
            variables: JSON string containing template variable values
                for substitution (default: "{}"). Supported variables:
                agent.id, agent.owner_id, agent.zone_id, workspace.root,
                task.description, task.id, workspace.id.
            response_format: Output format - "json" or "markdown" (default: "json")

        Returns:
            Formatted string with resolution results containing:
            - resolved_at: ISO-8601 timestamp
            - total_ms: Total resolution time in milliseconds
            - source_count: Number of sources resolved
            - sources: Array of per-source results with status, data, elapsed_ms
        """
        # manifest_resolver is a callable built by the factory — no cross-brick
        # imports needed. It handles validation, memory wiring, and resolution.
        if manifest_resolver is None:
            return tool_error(
                "unavailable",
                "Context manifest resolver not available.",
            )

        try:
            result = manifest_resolver(sources, variables)
        except Exception as e:
            return tool_error("internal", f"Error resolving context manifest: {e}", str(e))

        if isinstance(result, str) and result.startswith("Error:"):
            return result

        return format_response(result, response_format)

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
    @handle_tool_errors("listing workflows")
    def nexus_list_workflows(ctx: Context | None = None) -> str:
        """List available workflows in Nexus.

        Returns:
            JSON string with list of workflows
        """
        nx_instance = _get_nexus_instance(ctx)
        if not hasattr(nx_instance, "workflows"):
            return tool_error(
                "unavailable",
                "Workflow system not available (requires NexusFS with workflows enabled).",
            )

        workflows = nx_instance.workflows.list_workflows()
        return json.dumps(workflows, indent=2)

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,  # Workflows can modify state
            "idempotentHint": False,  # Workflow execution may have side effects
            "openWorldHint": True,
        }
    )
    @handle_tool_errors("executing workflow")
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
        nx_instance = _get_nexus_instance(ctx)
        if not hasattr(nx_instance, "workflows"):
            return tool_error(
                "unavailable",
                "Workflow system not available (requires NexusFS with workflows enabled).",
            )

        try:
            input_dict = json.loads(inputs) if inputs else {}
        except json.JSONDecodeError:
            return tool_error(
                "invalid_input", "Invalid JSON in inputs parameter. Provide valid JSON string."
            )

        result = nx_instance.workflows.execute(name, **input_dict)
        return json.dumps(result, indent=2)

    # =========================================================================
    # SANDBOX EXECUTION TOOLS (Conditional Registration)
    # =========================================================================

    def _format_sandbox_result(result: dict[str, Any]) -> str:
        """Format sandbox execution result into a readable string."""
        output_parts: list[str] = []

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
            "\n\n".join(output_parts) if output_parts else "Code executed successfully (no output)"
        )

    # Check if sandbox support is available
    # First check the explicit sandbox_available property, then probe internals
    sandbox_available = False
    try:
        sa = getattr(_default_nx, "sandbox_available", None)
        if sa is False:
            sandbox_available = False
        elif sa is True:
            sandbox_available = True
        elif hasattr(_default_nx, "_ensure_sandbox_manager"):
            _default_nx._ensure_sandbox_manager()
            if getattr(_default_nx, "sandbox_available", False):
                sandbox_available = True
    except Exception:
        sandbox_available = False

    # Only register sandbox tools if available
    if sandbox_available:

        @mcp.tool()
        @handle_tool_errors("executing Python code")
        def nexus_python(code: str, sandbox_id: str, ctx: Context | None = None) -> str:
            """Execute Python code in Nexus sandbox.

            Args:
                code: Python code to execute
                sandbox_id: Sandbox ID (use nexus_sandbox_create to create one)

            Returns:
                Execution result with stdout, stderr, exit_code, and execution time
            """
            nx_instance: Any = _get_nexus_instance(ctx)
            result = nx_instance.service("sandbox_rpc").sandbox_run(
                sandbox_id=sandbox_id, language="python", code=code, timeout=300
            )
            return _format_sandbox_result(result)

        @mcp.tool()
        @handle_tool_errors("executing bash command")
        def nexus_bash(command: str, sandbox_id: str, ctx: Context | None = None) -> str:
            """Execute bash commands in Nexus sandbox.

            Args:
                command: Bash command to execute
                sandbox_id: Sandbox ID (use nexus_sandbox_create to create one)

            Returns:
                Execution result with stdout, stderr, exit_code, and execution time
            """
            nx_instance: Any = _get_nexus_instance(ctx)
            result = nx_instance.service("sandbox_rpc").sandbox_run(
                sandbox_id=sandbox_id, language="bash", code=command, timeout=300
            )
            return _format_sandbox_result(result)

        @mcp.tool()
        @handle_tool_errors("creating sandbox")
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
            nx_instance: Any = _get_nexus_instance(ctx)
            result = nx_instance.service("sandbox_rpc").sandbox_create(
                name=name, ttl_minutes=ttl_minutes
            )
            return json.dumps(result, indent=2)

        @mcp.tool()
        @handle_tool_errors("listing sandboxes")
        def nexus_sandbox_list(ctx: Context | None = None) -> str:
            """List all active sandboxes.

            Returns:
                JSON string with list of sandboxes
            """
            nx_instance: Any = _get_nexus_instance(ctx)
            result = nx_instance.service("sandbox_rpc").sandbox_list()
            return json.dumps(result, indent=2)

        @mcp.tool()
        @handle_tool_errors("stopping sandbox")
        def nexus_sandbox_stop(sandbox_id: str, ctx: Context | None = None) -> str:
            """Stop and destroy a sandbox.

            Args:
                sandbox_id: Sandbox ID to stop

            Returns:
                Success message or error
            """
            nx_instance: Any = _get_nexus_instance(ctx)
            nx_instance.service("sandbox_rpc").sandbox_stop(sandbox_id)
            return f"Successfully stopped sandbox {sandbox_id}"

    # =========================================================================
    # RESOURCES
    # =========================================================================

    @mcp.resource("nexus://files/{path}")
    async def get_file_resource(path: str, ctx: Context | None = None) -> str:
        """Browse files as MCP resources.

        Args:
            path: File path to access

        Returns:
            File content
        """
        try:
            nx_instance = _get_nexus_instance(ctx)
            content = nx_instance.sys_read(path)
            if isinstance(content, bytes):
                return content.decode("utf-8", errors="replace")
            return str(content)
        except Exception as e:
            return f"Error reading resource: {str(e)}"

    # =========================================================================
    # DISCOVERY TOOLS
    # =========================================================================

    # Lazy import and create tool index for discovery
    import importlib as _il

    _tool_index_mod = _il.import_module("nexus.bricks.discovery.tool_index")
    ToolIndex = _tool_index_mod.ToolIndex
    ToolInfo = _tool_index_mod.ToolInfo

    tool_index = ToolIndex()

    # Index the Nexus MCP tools themselves (bootstrap)
    # This allows agents to discover what tools are available
    _nexus_tools = [
        ToolInfo("nexus_read_file", "Read the contents of a file", "nexus"),
        ToolInfo("nexus_write_file", "Write content to a file", "nexus"),
        ToolInfo("nexus_edit_file", "Apply surgical search/replace edits to a file", "nexus"),
        ToolInfo("nexus_list_files", "List files in a directory", "nexus"),
        ToolInfo("nexus_delete_file", "Delete a file", "nexus"),
        ToolInfo("nexus_mkdir", "Create a directory", "nexus"),
        ToolInfo("nexus_rmdir", "Remove an empty directory", "nexus"),
        ToolInfo("nexus_rename_file", "Rename or move a file", "nexus"),
        ToolInfo("nexus_file_info", "Get file metadata", "nexus"),
        ToolInfo("nexus_glob", "Find files matching a glob pattern", "nexus"),
        ToolInfo("nexus_grep", "Search file contents with regex", "nexus"),
        ToolInfo("nexus_semantic_search", "Search files by semantic similarity", "nexus"),
        ToolInfo("nexus_list_workflows", "List available workflows", "nexus"),
        ToolInfo("nexus_execute_workflow", "Execute a workflow", "nexus"),
    ]
    tool_index.add_tools(_nexus_tools)

    # Track actively loaded tools (for dynamic loading)
    _active_tools: dict[str, dict] = {}

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @handle_tool_errors("searching tools")
    def nexus_discovery_search_tools(query: str, top_k: int = 5, ctx: Context | None = None) -> str:
        """Search for MCP tools by query.

        Returns relevant tools ranked by BM25 score. Use this to find
        tools that can help accomplish a task. Results are filtered by
        the caller's tool namespace grants.

        Args:
            query: Search query describing the desired tool functionality
            top_k: Maximum number of results (default: 5)
            ctx: FastMCP Context (automatically injected)

        Returns:
            JSON with matching tools and scores
        """
        # Over-fetch to compensate for namespace filtering
        visible = _get_visible_tool_names(ctx)
        fetch_k = top_k * 3 if visible is not None else top_k

        matches = tool_index.search(query, top_k=fetch_k)

        # Post-filter through namespace
        if visible is not None:
            matches = [m for m in matches if m.tool.name in visible][:top_k]

        result = {
            "tools": [m.to_dict() for m in matches],
            "count": len(matches),
            "query": query,
        }
        return json.dumps(result, indent=2)

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @handle_tool_errors("listing servers")
    def nexus_discovery_list_servers(ctx: Context | None = None) -> str:
        """List all available MCP servers.

        Returns information about available tool providers. Tool counts
        are filtered by the caller's namespace grants.

        Args:
            ctx: FastMCP Context (automatically injected)

        Returns:
            JSON with server list and tool counts
        """
        visible = _get_visible_tool_names(ctx)
        servers = tool_index.list_servers()

        total_visible = 0
        server_tool_counts: dict[str, int] = {}
        for server in servers:
            tools = tool_index.list_tools(server=server)
            if visible is not None:
                tools = [t for t in tools if t.name in visible]
            server_tool_counts[server] = len(tools)
            total_visible += len(tools)

        result = {
            "servers": servers,
            "server_tool_counts": server_tool_counts,
            "total_servers": len(servers),
            "total_tools": total_visible if visible is not None else tool_index.tool_count,
        }
        return json.dumps(result, indent=2)

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @handle_tool_errors("getting tool details")
    def nexus_discovery_get_tool_details(tool_name: str, ctx: Context | None = None) -> str:
        """Get detailed information about a specific tool.

        Returns the full input schema and description. Use this after
        search_tools to get complete parameter information. Invisible
        tools (outside the caller's namespace) return "not found".

        Args:
            tool_name: Full tool name (e.g., 'nexus_read_file')
            ctx: FastMCP Context (automatically injected)

        Returns:
            JSON with tool details or error
        """
        # Namespace-as-security: invisible tools appear as "not found"
        visible = _get_visible_tool_names(ctx)
        if visible is not None and tool_name not in visible:
            result: dict[str, Any] = {
                "error": f"Tool '{tool_name}' not found",
                "found": False,
            }
            return json.dumps(result, indent=2)

        tool = tool_index.get_tool(tool_name)
        if tool is None:
            result = {"error": f"Tool '{tool_name}' not found", "found": False}
        else:
            result = {"found": True, **tool.to_dict()}
        return json.dumps(result, indent=2)

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @handle_tool_errors("loading tools")
    def nexus_discovery_load_tools(tool_names: list[str], ctx: Context | None = None) -> str:
        """Load specified tools into the active context.

        After loading, these tools become available for direct use.
        Use this after finding relevant tools with search_tools.
        Invisible tools (outside the caller's namespace) are treated
        as "not found" (namespace-as-security).

        Args:
            tool_names: List of tool names to load
            ctx: FastMCP Context (automatically injected)

        Returns:
            JSON with loaded tools and status
        """
        visible = _get_visible_tool_names(ctx)
        loaded = []
        not_found = []
        already_loaded = []

        for name in tool_names:
            # Namespace-as-security: invisible tools appear as "not found"
            if visible is not None and name not in visible:
                not_found.append(name)
                continue

            if name in _active_tools:
                already_loaded.append(name)
                continue

            tool = tool_index.get_tool(name)
            if tool is None:
                not_found.append(name)
                continue

            _active_tools[name] = tool.to_dict()
            loaded.append(name)

        result = {
            "loaded": loaded,
            "already_loaded": already_loaded,
            "not_found": not_found,
            "active_tool_count": len(_active_tools),
        }
        return json.dumps(result, indent=2)

    # =========================================================================
    # CONTEXT BRANCHING TOOLS (Issue #1315)
    # =========================================================================

    def _get_branch_service(ctx: Context | None = None):  # type: ignore[no-untyped-def]
        """Get ContextBranchService via ServiceRegistry (Issue #1771)."""
        nx_instance = _get_nexus_instance(ctx)
        return nx_instance.service("context_branch") if nx_instance else None

    def _get_namespace_fork_service(ctx: Context | None = None) -> Any:
        """Get AgentNamespaceForkService via ServiceRegistry (Issue #1771)."""
        nx_instance = _get_nexus_instance(ctx)
        return nx_instance.service("namespace_fork") if nx_instance else None

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        }
    )
    @handle_tool_errors("committing context snapshot")
    def nexus_context_commit(
        workspace: str,
        message: str | None = None,
        branch: str | None = None,
        ctx: Context | None = None,
    ) -> str:
        """Create a snapshot and advance branch HEAD.

        Args:
            workspace: Workspace path (e.g., "/workspace")
            message: Commit message
            branch: Branch to commit to (default: current branch)
            ctx: FastMCP Context

        Returns:
            JSON with snapshot and branch info
        """
        svc = _get_branch_service(ctx)
        if not svc:
            return tool_error("unavailable", "Context branching not available.")
        result = svc.commit(workspace, message=message, branch_name=branch)
        return json.dumps(result, indent=2, default=str)

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        }
    )
    @handle_tool_errors("creating context branch")
    def nexus_context_branch(
        workspace: str,
        name: str,
        from_branch: str | None = None,
        ctx: Context | None = None,
    ) -> str:
        """Create a new named branch (zero-copy, instant).

        Args:
            workspace: Workspace path
            name: Branch name
            from_branch: Fork from this branch (default: current)
            ctx: FastMCP Context

        Returns:
            JSON with branch info
        """
        svc = _get_branch_service(ctx)
        if not svc:
            return tool_error("unavailable", "Context branching not available.")
        result = svc.create_branch(workspace, name, from_branch=from_branch)
        return json.dumps(
            {
                "branch_name": result.branch_name,
                "parent_branch": result.parent_branch,
                "fork_point_id": result.fork_point_id,
                "id": result.id,
            },
            indent=2,
        )

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
    )
    @handle_tool_errors("checking out context branch")
    def nexus_context_checkout(workspace: str, target: str, ctx: Context | None = None) -> str:
        """Switch to a different branch and restore its workspace state.

        Args:
            workspace: Workspace path
            target: Branch name to switch to
            ctx: FastMCP Context

        Returns:
            JSON with checkout result
        """
        svc = _get_branch_service(ctx)
        if not svc:
            return tool_error("unavailable", "Context branching not available.")
        result = svc.checkout(workspace, target)
        return json.dumps(result, indent=2, default=str)

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        }
    )
    @handle_tool_errors("merging context branches")
    def nexus_context_merge(
        workspace: str,
        source: str,
        target: str | None = None,
        strategy: str = "fail",
        ctx: Context | None = None,
    ) -> str:
        """Merge a branch into another (three-way merge).

        Args:
            workspace: Workspace path
            source: Branch to merge FROM
            target: Branch to merge INTO (default: current)
            strategy: 'fail' (default) or 'source-wins'
            ctx: FastMCP Context

        Returns:
            JSON with merge result
        """
        svc = _get_branch_service(ctx)
        if not svc:
            return tool_error("unavailable", "Context branching not available.")
        result = svc.merge(workspace, source, target_branch=target, strategy=strategy)
        return json.dumps(
            {
                "merged": result.merged,
                "fast_forward": result.fast_forward,
                "files_added": result.files_added,
                "files_removed": result.files_removed,
                "files_modified": result.files_modified,
                "strategy": result.strategy,
            },
            indent=2,
        )

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
    )
    @handle_tool_errors("listing context branches")
    def nexus_context_branches(
        workspace: str, include_inactive: bool = False, ctx: Context | None = None
    ) -> str:
        """List all branches for a workspace.

        Args:
            workspace: Workspace path
            include_inactive: Include merged/discarded branches
            ctx: FastMCP Context

        Returns:
            JSON array of branch info
        """
        svc = _get_branch_service(ctx)
        if not svc:
            return tool_error("unavailable", "Context branching not available.")
        branches = svc.list_branches(workspace, include_inactive=include_inactive)
        return json.dumps(
            [
                {
                    "branch_name": b.branch_name,
                    "status": b.status,
                    "is_current": b.is_current,
                    "head_snapshot_id": b.head_snapshot_id,
                    "parent_branch": b.parent_branch,
                }
                for b in branches
            ],
            indent=2,
        )

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
    )
    @handle_tool_errors("viewing context log")
    def nexus_context_log(workspace: str, limit: int = 20, ctx: Context | None = None) -> str:
        """Show snapshot history for a workspace.

        Args:
            workspace: Workspace path
            limit: Max entries to show
            ctx: FastMCP Context

        Returns:
            JSON array of snapshots
        """
        svc = _get_branch_service(ctx)
        if not svc:
            return tool_error("unavailable", "Context branching not available.")
        snapshots = svc.log(workspace, limit=limit)
        return json.dumps(snapshots, indent=2, default=str)

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        }
    )
    @handle_tool_errors("starting context exploration")
    def nexus_context_explore(
        workspace: str,
        description: str,
        fork_namespace: bool = True,
        ctx: Context | None = None,
    ) -> str:
        """Start an exploration: auto-commit + create branch + checkout.

        Optionally forks the agent's namespace for isolated visibility during
        exploration (Issue #1273).

        Args:
            workspace: Workspace path
            description: Description of exploration (used for branch name)
            fork_namespace: If True (default), fork namespace for isolated visibility
            ctx: FastMCP Context

        Returns:
            JSON with exploration branch info and optional namespace_fork metadata
        """
        svc = _get_branch_service(ctx)
        if not svc:
            return tool_error("unavailable", "Context branching not available.")
        result = svc.explore(workspace, description)
        response: dict[str, object] = {
            "branch_name": result.branch_name,
            "branch_id": result.branch_id,
            "fork_point_snapshot_id": result.fork_point_snapshot_id,
            "skipped_commit": result.skipped_commit,
            "message": result.message,
        }

        # Namespace fork (non-fatal — exploration works without it)
        if fork_namespace:
            fork_svc = _get_namespace_fork_service(ctx)
            if fork_svc is not None:
                try:
                    from nexus.contracts.namespace_fork_types import ForkMode

                    fork_info = fork_svc.fork(
                        agent_id=result.branch_name,
                        mode=ForkMode.COPY,
                    )
                    response["namespace_fork"] = {
                        "fork_id": fork_info.fork_id,
                        "mount_count": fork_info.mount_count,
                        "mode": fork_info.mode.value,
                    }
                except Exception:
                    logger.warning(
                        "[MCP] Namespace fork failed during explore, continuing without",
                        exc_info=True,
                    )

        return json.dumps(response, indent=2, default=str)

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": False,
        }
    )
    @handle_tool_errors("finishing context exploration")
    def nexus_context_finish(
        workspace: str,
        branch: str,
        outcome: str = "merge",
        strategy: str = "source-wins",
        fork_id: str | None = None,
        ctx: Context | None = None,
    ) -> str:
        """Finish an exploration: merge or discard the branch.

        If *fork_id* is provided, the corresponding namespace fork is merged
        (on outcome='merge') or discarded (on outcome='discard') alongside
        the workspace branch (Issue #1273).

        Args:
            workspace: Workspace path
            branch: Exploration branch to finish
            outcome: 'merge' (default) or 'discard'
            strategy: Merge strategy if outcome='merge' ('source-wins' default)
            fork_id: Namespace fork to merge/discard alongside branch
            ctx: FastMCP Context

        Returns:
            JSON with outcome details and optional namespace_fork result
        """
        svc = _get_branch_service(ctx)
        if not svc:
            return tool_error("unavailable", "Context branching not available.")
        result = svc.finish_explore(workspace, branch, outcome=outcome, strategy=strategy)

        # Wrap in dict if needed for namespace_fork info
        response = dict(result) if isinstance(result, dict) else {"result": result}

        # Namespace fork merge/discard (non-fatal)
        if fork_id is not None:
            fork_svc = _get_namespace_fork_service(ctx)
            if fork_svc is not None:
                try:
                    if outcome == "merge":
                        merge_result = fork_svc.merge(fork_id, strategy=strategy)
                        response["namespace_fork"] = {
                            "action": "merged",
                            "fork_id": merge_result.fork_id,
                            "entries_added": merge_result.entries_added,
                            "entries_removed": merge_result.entries_removed,
                            "entries_modified": merge_result.entries_modified,
                        }
                    else:
                        fork_svc.discard(fork_id)
                        response["namespace_fork"] = {
                            "action": "discarded",
                            "fork_id": fork_id,
                        }
                except Exception:
                    logger.warning(
                        "[MCP] Namespace fork %s during finish failed, continuing",
                        outcome,
                        exc_info=True,
                    )

        return json.dumps(response, indent=2, default=str)

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

Start by running the semantic search.
"""

    return mcp


def main() -> None:
    """Main entry point for running MCP server from command line."""
    import asyncio

    asyncio.run(_async_main())


async def _async_main() -> None:
    """Async implementation of main entry point."""

    # Get configuration from environment
    import importlib as _il
    import os

    connect = _il.import_module("nexus").connect

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

    mcp = await create_mcp_server(nx=nx, remote_url=remote_url, api_key=api_key)

    # Middleware chain for HTTP transports (#3779).
    # Order (outermost to innermost, applied via Starlette's reverse-registration):
    #   1. RateLimit — enforce per-token quotas before work is done
    #   2. AuditLog  — wrap every request with structured logging
    #   3. APIKey    — set `_request_api_key` contextvar for tool handlers
    if transport in ["http", "sse"]:
        try:
            from starlette.middleware.base import BaseHTTPMiddleware

            from nexus.bricks.mcp.middleware_audit import MCPAuditLogMiddleware
            from nexus.bricks.mcp.middleware_ratelimit import install_rate_limit

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
                            reset_request_api_key(token)

            if hasattr(mcp, "http_app"):
                app = mcp.http_app()
                # Innermost first: APIKey sets contextvar before tool dispatch.
                app.add_middleware(APIKeyMiddleware)
                # Middle: audit sees the final response status.
                app.add_middleware(MCPAuditLogMiddleware)
                # Outermost: rate-limit short-circuits before any work.
                install_rate_limit(app)
        except Exception as e:
            import logging

            logger_ = logging.getLogger(__name__)
            logger_.warning("Failed to add MCP HTTP middleware: %s", e)

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
