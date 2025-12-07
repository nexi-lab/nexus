"""Core components for Nexus filesystem."""

import os
import sys


def setup_uvloop() -> bool:
    """Install uvloop as the default asyncio event loop policy.

    uvloop provides significantly better performance for async I/O operations
    (2-4x faster than the default asyncio event loop).

    This function should be called early in the process, before any asyncio
    event loops are created. After calling this, all asyncio.run(),
    asyncio.new_event_loop(), etc. will automatically use uvloop.

    Environment Variables:
        NEXUS_USE_UVLOOP: Set to "false", "0", or "no" to disable uvloop.
                          Useful for debugging or compatibility testing.

    Returns:
        True if uvloop was installed, False otherwise (disabled, Windows, or import error)

    Example:
        from nexus.core import setup_uvloop
        setup_uvloop()  # Call once at startup

        import asyncio
        asyncio.run(my_async_function())  # Now uses uvloop

        # To disable uvloop:
        # NEXUS_USE_UVLOOP=false nexus serve
    """
    # Check environment variable to allow disabling uvloop
    use_uvloop = os.environ.get("NEXUS_USE_UVLOOP", "true").lower()
    if use_uvloop in ("false", "0", "no"):
        return False

    # uvloop only works on Unix (macOS, Linux)
    if sys.platform == "win32":
        return False

    try:
        import uvloop

        uvloop.install()
        return True
    except ImportError:
        # uvloop not installed - fallback to default asyncio
        return False


# Imports after setup_uvloop() so it can be called before loading heavy modules
from nexus.core.async_scoped_filesystem import AsyncScopedFilesystem  # noqa: E402
from nexus.core.exceptions import (  # noqa: E402
    BackendError,
    InvalidPathError,
    MetadataError,
    NexusError,
    NexusFileNotFoundError,
    NexusPermissionError,
    ValidationError,
)
from nexus.core.filesystem import NexusFilesystem  # noqa: E402
from nexus.core.nexus_fs import NexusFS  # noqa: E402
from nexus.core.scoped_filesystem import ScopedFilesystem  # noqa: E402


# Async ReBAC components (v0.6.0+)
# Import lazily to avoid circular imports and missing dependencies
def get_async_rebac_manager() -> type:
    """Get AsyncReBACManager class (lazy import)."""
    from nexus.core.async_rebac_manager import AsyncReBACManager

    return AsyncReBACManager


def get_async_rebac_bridge() -> type:
    """Get AsyncReBACBridge class (lazy import)."""
    from nexus.core.async_bridge import AsyncReBACBridge

    return AsyncReBACBridge


__all__ = [
    # Event loop optimization
    "setup_uvloop",
    # Filesystem classes
    "AsyncScopedFilesystem",
    "NexusFilesystem",
    "NexusFS",
    "ScopedFilesystem",
    # Exceptions
    "NexusError",
    "NexusFileNotFoundError",
    "NexusPermissionError",
    "BackendError",
    "InvalidPathError",
    "MetadataError",
    "ValidationError",
    # Async ReBAC (lazy imports via functions)
    "get_async_rebac_manager",
    "get_async_rebac_bridge",
]
