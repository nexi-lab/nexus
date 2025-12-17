"""Core components for Nexus filesystem.

This module uses lazy imports for performance optimization.
Heavy modules (nexus_fs, async_scoped_filesystem) are only loaded when accessed.
"""

import os
import sys
from typing import TYPE_CHECKING, Any

# =============================================================================
# Lightweight imports (always loaded) - these are fast
# =============================================================================
from nexus.core.exceptions import (
    BackendError,
    InvalidPathError,
    MetadataError,
    NexusError,
    NexusFileNotFoundError,
    NexusPermissionError,
    ValidationError,
)


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


# =============================================================================
# LAZY IMPORTS for performance optimization
# =============================================================================
if TYPE_CHECKING:
    from nexus.core.async_scoped_filesystem import AsyncScopedFilesystem
    from nexus.core.filesystem import NexusFilesystem
    from nexus.core.nexus_fs import NexusFS
    from nexus.core.scoped_filesystem import ScopedFilesystem

# Module-level cache for lazy imports
_lazy_imports_cache: dict[str, Any] = {}

# Mapping of attribute names to their import paths
_LAZY_IMPORTS = {
    "AsyncScopedFilesystem": ("nexus.core.async_scoped_filesystem", "AsyncScopedFilesystem"),
    "NexusFilesystem": ("nexus.core.filesystem", "NexusFilesystem"),
    "NexusFS": ("nexus.core.nexus_fs", "NexusFS"),
    "ScopedFilesystem": ("nexus.core.scoped_filesystem", "ScopedFilesystem"),
}


def __getattr__(name: str) -> Any:
    """Lazy import for heavy dependencies."""
    # Check cache first
    if name in _lazy_imports_cache:
        return _lazy_imports_cache[name]

    # Check if this is a lazy import
    if name in _LAZY_IMPORTS:
        module_path, attr_name = _LAZY_IMPORTS[name]
        import importlib

        module = importlib.import_module(module_path)
        value = getattr(module, attr_name)
        _lazy_imports_cache[name] = value
        return value

    raise AttributeError(f"module 'nexus.core' has no attribute {name!r}")


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
    # Filesystem classes (lazy)
    "AsyncScopedFilesystem",
    "NexusFilesystem",
    "NexusFS",
    "ScopedFilesystem",
    # Exceptions (always loaded - lightweight)
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
