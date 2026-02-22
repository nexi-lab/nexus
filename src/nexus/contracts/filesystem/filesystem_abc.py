"""Composed filesystem ABC — union of all 7 sub-ABCs.

Issue #2424: Decompose monolithic NexusFilesystem into domain-specific
sub-ABCs following the ``collections.abc`` pattern::

    FileOpsABC + DiscoveryABC + DirectoryOpsABC + WorkspaceABC
    + MemoryRegistryABC + SandboxABC + LifecycleABC
    → NexusFilesystemABC

The narrow Protocol at ``services/protocols/filesystem.py`` is kept as-is
for brick consumers that only need 7 methods.
"""

from nexus.contracts.filesystem.directory_ops_abc import DirectoryOpsABC
from nexus.contracts.filesystem.discovery_abc import DiscoveryABC
from nexus.contracts.filesystem.file_ops_abc import FileOpsABC
from nexus.contracts.filesystem.lifecycle_abc import LifecycleABC
from nexus.contracts.filesystem.memory_registry_abc import MemoryRegistryABC
from nexus.contracts.filesystem.sandbox_abc import SandboxABC
from nexus.contracts.filesystem.workspace_abc import WorkspaceABC


class NexusFilesystemABC(
    FileOpsABC,
    DiscoveryABC,
    DirectoryOpsABC,
    WorkspaceABC,
    MemoryRegistryABC,
    SandboxABC,
    LifecycleABC,
):
    """Abstract base class for Nexus filesystem implementations.

    All filesystem modes (Standalone, Remote, Federation) must implement
    this interface to ensure consistent behavior across modes.

    This is the composed ABC that inherits all 7 domain-specific sub-ABCs.
    It adds no new methods — it is purely a union type following the
    ``collections.abc`` pattern (Sized + Iterable + Container → Collection).
    """
