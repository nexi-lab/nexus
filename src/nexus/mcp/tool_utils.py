"""MCP tool utilities — error envelope + decorator (Issue #1272).

Provides standardized error handling for MCP tools:
    - ``tool_error()``: Consistent error response formatting.
    - ``handle_tool_errors()``: Decorator for common try/except pattern.

Design decisions:
    - Error envelope uses ``"Error: {message}"`` format (consistent with
      ToolNamespaceMiddleware's "not found" response).
    - ``detail`` is logged server-side but never returned to the agent
      (prevents stack trace leakage).
    - Decorator preserves function signature for FastMCP introspection.
    - Custom error handling remains possible — tools can catch specific
      exceptions before the decorator's generic handler.

References:
    - Issue #1272: MCP tool-level namespace — per-tool ReBAC grants
    - MiniScope: mechanical enforcement > prompt-based
"""

from __future__ import annotations

import functools
import logging
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Error envelope
# ---------------------------------------------------------------------------


def tool_error(
    category: str,
    message: str,
    detail: str | None = None,
) -> str:
    """Return a standardized tool error response.

    The response format is always ``"Error: {message}"``. The ``detail``
    parameter is logged server-side at WARNING level but never included
    in the response (prevents stack trace / internal info leakage).

    Categories (for monitoring, not returned to agent):
        - ``"not_found"``: Resource does not exist or is invisible.
        - ``"permission_denied"``: Insufficient permissions.
        - ``"invalid_input"``: Bad arguments from the agent.
        - ``"internal"``: Unexpected server-side error.
        - ``"unavailable"``: Feature or service not available.

    Args:
        category: Error category for logging/metrics.
        message: User-facing error message.
        detail: Server-side diagnostic detail (logged, not returned).

    Returns:
        Formatted error string: ``"Error: {message}"``.
    """
    if detail:
        logger.warning("[TOOL-ERROR] %s: %s — %s", category, message, detail)
    else:
        logger.debug("[TOOL-ERROR] %s: %s", category, message)
    return f"Error: {message}"


# ---------------------------------------------------------------------------
# Error handling decorator
# ---------------------------------------------------------------------------


def handle_tool_errors(operation: str):
    """Decorator that wraps MCP tool functions with standard error handling.

    Catches common exceptions and returns standardized error responses
    via ``tool_error()``. Preserves function signature for FastMCP
    parameter introspection.

    Caught exceptions (in order):
        - ``FileNotFoundError`` → ``"not_found"``
        - ``PermissionError`` → ``"permission_denied"``
        - ``Exception`` → ``"internal"``

    Tools with custom error handling (e.g., JSON parse errors, feature
    detection) should handle those exceptions inside the function body.
    The decorator only catches exceptions that escape the function.

    Args:
        operation: Human-readable operation name for error messages
            (e.g., ``"reading file"``, ``"creating directory"``).

    Returns:
        Decorator function.

    Example::

        @mcp.tool(annotations={...})
        @handle_tool_errors("reading file")
        def nexus_read_file(path: str, ctx: Context | None = None) -> str:
            nx = _get_nexus_instance(ctx)
            content = nx.read(path)
            return content.decode("utf-8")
    """

    def decorator(fn: Any) -> Any:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> str:
            try:
                return fn(*args, **kwargs)
            except FileNotFoundError as exc:
                path = _extract_path_hint(args, kwargs)
                hint = f" at '{path}'" if path else ""
                return tool_error(
                    "not_found",
                    f"File not found{hint}. Use nexus_list_files to check available files.",
                    str(exc),
                )
            except PermissionError as exc:
                path = _extract_path_hint(args, kwargs)
                hint = f" for '{path}'" if path else ""
                return tool_error(
                    "permission_denied",
                    f"Permission denied{hint}. Check file permissions.",
                    str(exc),
                )
            except Exception as exc:
                return tool_error(
                    "internal",
                    f"Error {operation}: {exc}",
                    f"{type(exc).__name__}: {exc}",
                )

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_path_hint(
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> str | None:
    """Best-effort extraction of 'path' from tool arguments.

    Checks ``kwargs["path"]`` first, then ``args[0]`` if it looks like
    a path string (starts with ``/``).

    Returns:
        Path string, or None if not found.
    """
    # Check kwargs first
    path = kwargs.get("path")
    if isinstance(path, str):
        return path

    # Check first positional arg
    if args and isinstance(args[0], str) and args[0].startswith("/"):
        return args[0]

    return None
