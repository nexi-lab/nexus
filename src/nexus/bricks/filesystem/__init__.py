"""Filesystem bricks — same-Protocol wrappers for path scoping.

Per LEGO architecture (§4.3 Mechanism 2: Recursive Wrapping), these are
brick-tier wrappers that implement ``NexusFS`` and delegate to
an inner ``NexusFS``.  They are assembled by ``factory.py``.

Current bricks:
  - ScopedFilesystem       — sync path-scoping wrapper (async via asyncio.to_thread)
  - ScopedPathMixin        — shared path rewriting helpers
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.bricks.filesystem._scoped_base import ScopedPathMixin
    from nexus.bricks.filesystem.scoped_filesystem import ScopedFilesystem

_lazy_imports_cache: dict[str, Any] = {}

_LAZY_IMPORTS = {
    "ScopedFilesystem": (
        "nexus.bricks.filesystem.scoped_filesystem",
        "ScopedFilesystem",
    ),
    "ScopedPathMixin": (
        "nexus.bricks.filesystem._scoped_base",
        "ScopedPathMixin",
    ),
}


def __getattr__(name: str) -> Any:
    if name in _lazy_imports_cache:
        return _lazy_imports_cache[name]

    if name in _LAZY_IMPORTS:
        import importlib

        module_path, attr_name = _LAZY_IMPORTS[name]
        module = importlib.import_module(module_path)
        value = getattr(module, attr_name)
        _lazy_imports_cache[name] = value
        return value

    raise AttributeError(f"module 'nexus.bricks.filesystem' has no attribute {name!r}")


__all__ = [
    "ScopedFilesystem",
    "ScopedPathMixin",
]
