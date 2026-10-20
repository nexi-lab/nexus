"""Nexus MCP Server Implementation.

This module implements a Model Context Protocol (MCP) server that exposes
Nexus functionality to AI agents and tools using the fastmcp framework.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastmcp import Context, FastMCP
from fastmcp.server.dependencies import get_http_request

from nexus.bricks.mcp.credentials import http_api_key
from nexus.bricks.mcp.formatters import format_response
from nexus.bricks.mcp.service_calls import call_service_method
from nexus.bricks.mcp.tool_utils import handle_tool_errors, tool_error
from nexus.lib.pagination import build_paginated_list_response
from nexus.lib.request_credentials import request_api_key as _request_api_key

if TYPE_CHECKING:
    from nexus.bricks.approvals.policy_gate import PolicyGate
    from nexus.core.nexus_fs import NexusFS
    from nexus.remote.client import RemoteNexusFS

logger = logging.getLogger(__name__)


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


def _json_default(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return str(value)


def register_policy_gate_dependency(app: Any, gate: PolicyGate) -> None:
    """Attach PolicyGate to app.state so middlewares can call gate.check()."""
    app.state.policy_gate = gate


async def create_mcp_server(
    nx: NexusFS | RemoteNexusFS | None = None,
    name: str = "nexus",
    remote_url: str | None = None,
    api_key: str | None = None,
    tool_namespace_middleware: Any | None = None,
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

        The api_key parameter is the default for stdio and in-process calls.
        HTTP callers always supply their own credential to the native owner.

    Examples:
        >>> from nexus import connect
        >>> from nexus.bricks.mcp import create_mcp_server
        >>>
        >>> # Local filesystem
        >>> nx = connect()
        >>> server = create_mcp_server(nx)
        >>>
        >>> # Remote filesystem
        >>> server = create_mcp_server(remote_url="grpc://localhost:2028")
        >>>
        >>> # Remote filesystem with API key
        >>> server = create_mcp_server(
        ...     remote_url="grpc://localhost:2028",
        ...     api_key="your-api-key"
        ... )
    """
    supplied_filesystem = nx is not None
    # Initialize Nexus filesystem if not provided
    if nx is None:
        if remote_url:
            import nexus as _nexus

            nx = _nexus.connect(config={"profile": "remote", "url": remote_url, "api_key": api_key})
        else:
            import importlib as _il

            connect = _il.import_module("nexus").connect
            nx = connect()

    assert nx is not None
    try:
        return await _build_mcp_server(
            nx,
            name=name,
            tool_namespace_middleware=tool_namespace_middleware,
            owns_filesystem=not supplied_filesystem,
        )
    except BaseException:
        if not supplied_filesystem:
            await asyncio.to_thread(nx.close)
        raise


async def _build_mcp_server(
    nx: NexusFS | RemoteNexusFS,
    *,
    name: str,
    tool_namespace_middleware: Any | None,
    owns_filesystem: bool,
) -> FastMCP:
    _default_nx: NexusFS | RemoteNexusFS = nx

    def _get_nexus_instance(_ctx: Context | None = None) -> NexusFS | RemoteNexusFS:
        """Borrow the filesystem; its transport reads the request credential."""
        return _default_nx

    @asynccontextmanager
    async def filesystem_lifespan(_server: FastMCP) -> AsyncIterator[None]:
        try:
            yield None
        finally:
            if owns_filesystem:
                await asyncio.to_thread(nx.close)

    mcp = FastMCP(name, lifespan=filesystem_lifespan)

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
        """Bind the caller credential for one MCP message."""

        async def on_message(self, context: MiddlewareContext, call_next: Any) -> Any:
            api_key = _request_api_key.get()
            try:
                http_request = get_http_request()
            except RuntimeError:
                pass  # In-process and stdio transports have no HTTP request.
            else:
                from nexus.remote.client import RemoteNexusFS

                if not isinstance(_default_nx, RemoteNexusFS):
                    raise PermissionError("HTTP/SSE MCP requires a remote native filesystem")
                api_key = http_api_key(http_request.headers)

            scope_token = _request_api_key.set(api_key)
            try:
                return await call_next(context)
            finally:
                _request_api_key.reset(scope_token)

    # Add the middleware to FastMCP
    mcp.add_middleware(APIKeyExtractionMiddleware())

    # Add tool namespace middleware if provided (Issue #1272). The factory
    # may pass a zero-arg callable that builds the middleware on demand —
    # this defers the fastmcp/beartype import until an MCP server is
    # actually started, keeping ``nexus.connect()`` lightweight.
    if (
        tool_namespace_middleware is not None
        and not hasattr(tool_namespace_middleware, "on_call_tool")
        and callable(tool_namespace_middleware)
    ):
        tool_namespace_middleware = tool_namespace_middleware()
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
    async def nexus_read_file(path: str, ctx: Context | None = None) -> str:
        """Read the contents of a file through its native owner."""
        nx_instance = _get_nexus_instance(ctx)
        content = await asyncio.to_thread(nx_instance.sys_read, path)
        if not isinstance(content, bytes):
            raise TypeError("A file read must return bytes")
        return content.decode("utf-8", errors="replace")

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
        await call_service_method(nx_instance.write, path, content_bytes)
        return f"Successfully wrote {len(content_bytes)} bytes to {path}"

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
        await asyncio.to_thread(nx_instance.sys_unlink, path)
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
            all_files = await asyncio.to_thread(
                nx_instance.sys_readdir, path, recursive=recursive, details=details
            )
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
        metadata = await call_service_method(nx_instance.sys_stat, path)
        if metadata is None:
            return tool_error(
                "not_found",
                f"File not found at '{path}'. Use nexus_list_files to check available files.",
            )

        is_dir = bool(metadata.get("is_directory", False))
        info_dict: dict[str, Any] = {
            "path": path,
            "exists": True,
            "is_directory": is_dir,
        }

        if not is_dir:
            info_dict["size"] = metadata["size"]

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
            await call_service_method(nx_instance.mkdir, path)
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
            await call_service_method(nx_instance.rmdir, path, recursive=recursive)
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
            await asyncio.to_thread(nx_instance.sys_rename, old_path, new_path)
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
    async def nexus_glob(
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

        nx_instance: Any = _get_nexus_instance(ctx)
        _search = nx_instance.service("search")
        if _search is None:
            raise ValueError("SearchService not available — glob requires the search brick")
        op_context = None
        all_matches = await call_service_method(
            _search.glob, pattern, path, files=files, context=op_context
        )
        total = len(all_matches)
        paginated_matches = all_matches[offset : offset + limit]

        # #3731: Zone unscoping — convert internal zone-prefixed paths
        # to user-facing paths and build parallel zone list for
        # round-trip disambiguation.
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

        # #3731: Detect multi-zone ambiguity.
        _keys = list(zip(paginated_matches, item_zones, strict=False))
        glob_multi_zone_ambiguous = len(set(_keys)) < len(_keys)

        extras: dict[str, Any] = {"item_zones": item_zones}
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
        section: str | None = None,
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
            section: Restrict matches to a markdown or parsed-content
                section heading (#4186), e.g. ``"API"`` or ``"## API"``.
                Missing sections return an empty result envelope rather
                than falling back to whole-file search.

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

        nx_instance: Any = _get_nexus_instance(ctx)
        _search = nx_instance.service("search")
        if _search is None:
            raise ValueError("SearchService not available — grep requires the search brick")
        op_context = None

        window_size = limit + offset
        fetch_limit = window_size + 1
        grep_kwargs: dict[str, Any] = {
            "ignore_case": ignore_case,
            "max_results": fetch_limit,
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
        if section is not None:
            grep_kwargs["section"] = section

        all_results = await call_service_method(_search.grep, pattern, path, **grep_kwargs)

        total = len(all_results)
        has_more = total > window_size
        paginated_results = all_results[offset : offset + limit]

        # #3731: Zone unscoping — convert internal zone-prefixed paths
        # to user-facing paths and annotate with zone_id for round-trip
        # disambiguation.
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

        # #3731: Two distinct raw paths that collapse to
        # the same (file, zone_id) tuple degrade round-trip safety.
        _keys = [(it["file"], it.get("zone_id")) for it in paginated_results]
        multi_zone_ambiguous = len(set(_keys)) < len(_keys)

        extras: dict[str, Any] = {}
        if multi_zone_ambiguous:
            extras["multi_zone_ambiguous"] = True
        if section is not None:
            extras["section_filter"] = section
            extras["section_status"] = "matched" if paginated_results else "no_matches"

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

        # Resolve the Search service registered on this filesystem.
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

        op_context = None

        # Over-fetch to detect another page without a second round-trip.
        fetch_limit = offset + limit * 2
        all_results = await call_service_method(
            search_service.semantic_search,
            query=query,
            path=path,
            search_mode=search_mode,
            limit=fetch_limit,
            context=op_context,
        )

        total = len(all_results)
        paginated_results = all_results[offset : offset + limit]
        has_more = (offset + limit) < total

        result: dict[str, Any] = {
            "total": total,
            "count": len(paginated_results),
            "offset": offset,
            "items": paginated_results,
            "has_more": has_more,
            "next_offset": offset + limit if has_more else None,
        }
        return format_response(result, response_format)

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
            content = await asyncio.to_thread(nx_instance.sys_read, path)
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

    tools = await mcp.list_tools(run_middleware=False)
    tool_index.add_tools(
        [
            ToolInfo(tool.name, tool.description or "", name, input_schema=tool.parameters)
            for tool in tools
        ]
    )
    return mcp
