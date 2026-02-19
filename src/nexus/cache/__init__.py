"""Backward-compatibility shim — cache module moved to nexus.bricks.cache.

.. deprecated:: Issue #1524
    Import from ``nexus.bricks.cache`` instead of ``nexus.cache``.
"""

from nexus.bricks.cache import *  # noqa: F401, F403
from nexus.bricks.cache import __all__ as _upstream_all  # noqa: F811

__all__ = list(_upstream_all)


def __getattr__(name: str) -> object:
    """Lazy import for DragonflyCacheStore (mirrors nexus.bricks.cache)."""
    if name == "DragonflyCacheStore":
        from nexus.bricks.cache.dragonfly import DragonflyCacheStore

        globals()["DragonflyCacheStore"] = DragonflyCacheStore
        return DragonflyCacheStore
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
