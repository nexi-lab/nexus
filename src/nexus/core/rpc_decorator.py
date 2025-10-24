"""RPC exposure decorator for marking methods to be exposed via RPC.

This module is separate to avoid circular imports between core and server modules.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


def rpc_expose(
    name: str | None = None,
    description: str | None = None,
    version: str = "1.0",
) -> Callable[[F], F]:
    """Mark a method for RPC exposure.

    This decorator marks methods in NexusFS that should be automatically
    exposed via RPC. The RPC server will auto-discover all decorated methods
    and make them available as endpoints.

    Args:
        name: Optional RPC method name (defaults to function name)
        description: Optional description for API docs
        version: API version (for versioning support)

    Example:
        @rpc_expose(description="Read file content")
        def read(self, path: str) -> bytes:
            ...
    """

    def decorator(fn: F) -> F:
        fn._rpc_exposed = True  # type: ignore[attr-defined]
        fn._rpc_name = name or fn.__name__  # type: ignore[attr-defined]
        fn._rpc_description = description or fn.__doc__  # type: ignore[attr-defined]
        fn._rpc_version = version  # type: ignore[attr-defined]
        return fn

    return decorator
