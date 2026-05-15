"""Core components for Nexus filesystem.

This module uses lazy imports for performance optimization.
Heavy modules (nexus_fs) are only loaded when accessed.
"""

import os
import sys
from typing import TYPE_CHECKING, Any


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
        # NEXUS_USE_UVLOOP=false nexusd
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
    from nexus.core.nexus_fs import NexusFS
    from nexus.lib.registry import BaseRegistry, BrickInfo, BrickRegistry

# Module-level cache for lazy imports
_lazy_imports_cache: dict[str, Any] = {}

# Mapping of attribute names to their import paths
_LAZY_IMPORTS = {
    "BaseRegistry": ("nexus.lib.registry", "BaseRegistry"),
    "BrickInfo": ("nexus.lib.registry", "BrickInfo"),
    "BrickRegistry": ("nexus.lib.registry", "BrickRegistry"),
    "NexusFS": ("nexus.core.nexus_fs", "NexusFS"),
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


__all__ = [
    # Event loop optimization
    "setup_uvloop",
    # Registry base classes (lazy)
    "BaseRegistry",
    "BrickInfo",
    "BrickRegistry",
    # Filesystem classes (lazy)
    "NexusFS",
]
