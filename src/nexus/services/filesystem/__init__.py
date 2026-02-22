"""Filesystem scoped wrappers and ABC re-exports (services tier).

Issue #2424: ABCs now live in ``nexus.contracts.filesystem`` (tier-neutral).
Scoped wrappers now live in ``nexus.bricks.filesystem`` (brick tier).
This package re-exports them for backward compatibility.

Uses the same lazy-import + cache pattern as ``core/__init__.py``.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.bricks.filesystem._scoped_base import ScopedPathMixin
    from nexus.bricks.filesystem.scoped_filesystem import ScopedFilesystem
    from nexus.contracts.filesystem.directory_ops_abc import DirectoryOpsABC
    from nexus.contracts.filesystem.discovery_abc import DiscoveryABC
    from nexus.contracts.filesystem.file_ops_abc import FileOpsABC
    from nexus.contracts.filesystem.filesystem_abc import NexusFilesystemABC
    from nexus.contracts.filesystem.lifecycle_abc import LifecycleABC
    from nexus.contracts.filesystem.memory_registry_abc import MemoryRegistryABC
    from nexus.contracts.filesystem.sandbox_abc import SandboxABC
    from nexus.contracts.filesystem.workspace_abc import WorkspaceABC

_lazy_imports_cache: dict[str, Any] = {}

_LAZY_IMPORTS = {
    # Scoped wrappers re-exported from bricks/ for backward compatibility
    "ScopedFilesystem": ("nexus.bricks.filesystem.scoped_filesystem", "ScopedFilesystem"),
    "ScopedPathMixin": ("nexus.bricks.filesystem._scoped_base", "ScopedPathMixin"),
    # ABCs re-exported from contracts/ for backward compatibility
    "DirectoryOpsABC": ("nexus.contracts.filesystem.directory_ops_abc", "DirectoryOpsABC"),
    "DiscoveryABC": ("nexus.contracts.filesystem.discovery_abc", "DiscoveryABC"),
    "FileOpsABC": ("nexus.contracts.filesystem.file_ops_abc", "FileOpsABC"),
    "LifecycleABC": ("nexus.contracts.filesystem.lifecycle_abc", "LifecycleABC"),
    "MemoryRegistryABC": ("nexus.contracts.filesystem.memory_registry_abc", "MemoryRegistryABC"),
    "NexusFilesystemABC": ("nexus.contracts.filesystem.filesystem_abc", "NexusFilesystemABC"),
    "SandboxABC": ("nexus.contracts.filesystem.sandbox_abc", "SandboxABC"),
    "WorkspaceABC": ("nexus.contracts.filesystem.workspace_abc", "WorkspaceABC"),
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

    raise AttributeError(f"module 'nexus.services.filesystem' has no attribute {name!r}")


__all__ = [
    "DirectoryOpsABC",
    "DiscoveryABC",
    "FileOpsABC",
    "LifecycleABC",
    "MemoryRegistryABC",
    "NexusFilesystemABC",
    "SandboxABC",
    "ScopedFilesystem",
    "ScopedPathMixin",
    "WorkspaceABC",
]
