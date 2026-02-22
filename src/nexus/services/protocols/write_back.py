"""WriteBackService protocol (Issue #696).

Defines the contract for bidirectional write-back sync from Nexus to source backends.

Existing implementation: ``nexus.services.write_back_service.WriteBackService``

References:
    - docs/design/KERNEL-ARCHITECTURE.md §1 (service DI)
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class WriteBackProtocol(Protocol):
    """Service contract for bidirectional write-back sync."""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def push_mount(self, backend_name: str, zone_id: str) -> None: ...

    def get_stats(self) -> dict[str, Any]: ...

    def get_mount_for_path(self, path: str) -> dict[str, Any] | None: ...

    def get_metrics_snapshot(self) -> dict[str, Any]: ...
