"""Centralized error-handling decorator for API v2 endpoints.

Eliminates repeated try/except boilerplate across routers by mapping
well-known exception types to HTTP status codes.

Issue #995: API versioning strategy — code quality improvements.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException

logger = logging.getLogger(__name__)

# Default exception → (status_code, detail_template) mapping.
# detail_template uses {error} as placeholder for str(e).
DEFAULT_ERROR_MAP: dict[type[Exception], tuple[int, str]] = {
    ValueError: (404, "{error}"),
    KeyError: (404, "Resource not found: {error}"),
    PermissionError: (403, "Permission denied: {error}"),
}


def api_error_handler(
    *,
    context: str = "API",
    error_map: dict[type[Exception], tuple[int, str]] | None = None,
) -> Callable[..., Any]:
    """Decorator that wraps an async endpoint with standardized error handling.

    Usage::

        @router.post("/something")
        @api_error_handler(context="create widget")
        async def create_widget(request: WidgetRequest) -> WidgetResponse:
            ...  # just the happy path

    Args:
        context: Human-readable label for log messages (e.g. "store memory").
        error_map: Optional per-endpoint overrides merged on top of
            DEFAULT_ERROR_MAP.  Keys are exception types, values are
            ``(status_code, detail_template)`` tuples.
    """
    merged_map = {**DEFAULT_ERROR_MAP, **(error_map or {})}

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await func(*args, **kwargs)
            except HTTPException:
                # Already an HTTP error — let it propagate unchanged.
                raise
            except tuple(merged_map.keys()) as e:
                # Look up the most specific matching class.
                for exc_type, (status, template) in merged_map.items():
                    if isinstance(e, exc_type):
                        detail = template.format(error=str(e))
                        raise HTTPException(status_code=status, detail=detail) from e
                # Shouldn't reach here, but fall through to generic handler.
                raise  # pragma: no cover
            except Exception as e:
                logger.error("%s error: %s", context.capitalize(), e, exc_info=True)
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to {context}",
                ) from e

        return wrapper

    return decorator
