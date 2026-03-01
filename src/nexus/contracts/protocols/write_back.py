"""WriteBackService protocol (Issue #696).

Defines the contract for bidirectional write-back sync from Nexus to source backends.

Existing implementation: ``nexus.services.write_back_service.WriteBackService``

``InMemoryWriteBack`` is provided as a lightweight fallback for deployments
without an event bus (standalone mode without Redis/NATS).

References:
    - docs/design/KERNEL-ARCHITECTURE.md §1 (service DI)
"""

from typing import Any, Protocol, runtime_checkable

from nexus.contracts.constants import ROOT_ZONE_ID


@runtime_checkable
class WriteBackProtocol(Protocol):
    """Service contract for bidirectional write-back sync."""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def push_mount(self, backend_name: str, zone_id: str) -> None: ...

    def get_stats(self) -> dict[str, Any]: ...

    def get_mount_for_path(self, path: str) -> dict[str, Any] | None: ...

    def get_metrics_snapshot(self) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------
# InMemoryWriteBack — lightweight fallback for non-event-bus deployments
# ---------------------------------------------------------------------------


class InMemoryWriteBack:
    """No-op write-back service for standalone deployments without event bus.

    Used as a fallback when ``NEXUS_WRITE_BACK=true`` but no event bus is
    available (standalone mode without Redis/NATS).  All push operations
    report zero changes — no data is lost because there is no remote
    backend to sync to.

    Structurally satisfies ``WriteBackProtocol``.
    """

    async def start(self) -> None:
        """No-op — in-memory write-back requires no startup."""

    async def stop(self) -> None:
        """No-op — nothing to tear down."""

    async def push_mount(
        self,
        backend_name: str,  # noqa: ARG002
        zone_id: str,  # noqa: ARG002
    ) -> None:
        """No-op — standalone mode has no remote backend to push to."""

    def get_stats(self) -> dict[str, Any]:
        return {
            "pending": 0,
            "pushed": 0,
            "failed": 0,
            "backends": {},
        }

    def get_mount_for_path(self, path: str) -> dict[str, Any] | None:
        """Return a default local mount for any path.

        In standalone mode, the local backend is the only mount.
        """
        return {
            "mount_point": path,
            "backend_name": "local",
            "zone_id": ROOT_ZONE_ID,
        }

    def get_metrics_snapshot(self) -> dict[str, Any]:
        return {
            "changes_pushed": 0,
            "changes_failed": 0,
            "conflicts_detected": 0,
        }
