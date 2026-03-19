"""SyncJobService protocol (Issue #696).

Defines the contract for async sync-job lifecycle management.

Existing implementation: ``nexus.services.sync_job_service.SyncJobService``

References:
    - docs/design/KERNEL-ARCHITECTURE.md §1 (service DI)
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SyncJobProtocol(Protocol):
    """Service contract for sync-job management."""

    def create_job(
        self,
        mount_point: str,
        params: dict[str, Any],
        user_id: str | None = None,
    ) -> str: ...

    def start_job(self, job_id: str) -> None: ...

    def get_job(self, job_id: str) -> dict[str, Any] | None: ...

    def cancel_job(self, job_id: str) -> bool: ...

    def list_jobs(
        self,
        mount_point: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]: ...
