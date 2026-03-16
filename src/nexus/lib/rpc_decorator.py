"""RPC exposure decorator for marking methods to be exposed via RPC.

Tier-neutral utility (``nexus.lib``) — zero kernel dependency.
Also re-exported from ``nexus.contracts.protocols.rpc`` for convenience.
"""

from collections.abc import Callable
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


def rpc_expose(
    name: str | None = None,
    description: str | None = None,
    version: str = "1.0",
    admin_only: bool = False,
) -> Callable[[F], F]:
    """Mark a method for RPC exposure.

    This decorator marks methods that should be automatically
    exposed via RPC. The RPC server will auto-discover all decorated methods
    and make them available as endpoints.

    Args:
        name: Optional RPC method name (defaults to function name)
        description: Optional description for API docs
        version: API version (for versioning support)
        admin_only: If True, only admin callers can invoke this method.

    Example:
        @rpc_expose(description="Read file content")
        def read(self, path: str) -> bytes:
            ...
    """

    def decorator(fn: F) -> F:
        _fn: Any = fn
        _fn._rpc_exposed = True
        _fn._rpc_name = name or fn.__name__
        _fn._rpc_description = description or fn.__doc__
        _fn._rpc_version = version
        _fn._rpc_admin_only = admin_only
        return fn

    return decorator
