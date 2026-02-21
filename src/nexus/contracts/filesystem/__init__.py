"""Filesystem ABCs (contracts tier).

Issue #2424 follow-up: Moved from ``services/filesystem/`` so the kernel
can inherit ``NexusFilesystemABC`` without an upward tier import.
``contracts/`` is tier-neutral — importable by all layers.

Uses the same lazy-import + cache pattern as ``services/filesystem/__init__.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
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

    raise AttributeError(f"module 'nexus.contracts.filesystem' has no attribute {name!r}")


__all__ = [
    "DirectoryOpsABC",
    "DiscoveryABC",
    "FileOpsABC",
    "LifecycleABC",
    "MemoryRegistryABC",
    "NexusFilesystemABC",
    "SandboxABC",
    "WorkspaceABC",
]
