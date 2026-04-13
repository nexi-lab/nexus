"""Workflow-related protocol interfaces (Issue #2137).

Tier-neutral contracts for the narrow service surfaces that workflow
actions need.  Bricks, services, and the factory all import from here.

Moved from ``nexus.contracts.protocols.workflow`` to ``nexus.contracts``
so that the workflows brick does not need a backwards import from the
services tier.

See: NEXUS-LEGO-ARCHITECTURE.md §2.4, §3.3
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class NexusOperationsProtocol(Protocol):
    """Filesystem operations that workflow actions may invoke.

    All methods are async — workflow actions execute in an async context
    and these operations involve I/O (Issue #3063).
    """

    async def parse(self, path: str, *, parser: str = "auto") -> Any: ...

    async def add_tag(self, path: str, tag: str) -> None: ...

    async def remove_tag(self, path: str, tag: str) -> None: ...

    async def rename(self, old_path: str, new_path: str) -> None: ...

    def mkdir(self, path: str, *, parents: bool = False) -> None: ...

    async def read(self, path: str) -> bytes: ...


@runtime_checkable
class MetadataStoreProtocol(Protocol):
    """Minimal metadata store surface used by MetadataAction.

    All methods are async — workflow actions execute in an async context
    and these operations involve I/O (Issue #3063).
    """

    async def get_path(self, path: str) -> Any: ...

    async def set_file_metadata(self, path_id: Any, key: str, value: str) -> None: ...
