"""Sync Job Service - Async sync job management.

This service handles background execution of sync jobs with
progress tracking and cancellation support.

Phase 2: Mount Mixin Refactoring
Extracted from: nexus_fs_mounts.py (async sync methods)

Example:
    ```python
    job_service = SyncJobService(gateway, sync_service)
    job_id = job_service.create_job("/mnt/gcs", {"recursive": True})
    job_service.start_job(job_id)

    # Later...
    status = job_service.get_job(job_id)
    print(f"Progress: {status['progress_pct']}%")
    ```
"""

import logging
import threading
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.services.gateway import NexusFSGateway

    from .sync_service import SyncService

logger = logging.getLogger(__name__)


class SyncJobService:
    """Manages async sync jobs (background execution).

    Jobs run in background threads with progress tracking.
    """

    def __init__(self, gateway: "NexusFSGateway", sync_service: "SyncService"):
        """Initialize sync job service.

        Args:
            gateway: NexusFSGateway for session factory access
            sync_service: SyncService for sync operations
        """
        self._gw = gateway
        self._sync = sync_service
        self._manager: Any = None  # Lazy init (SyncJobManager)

    def _get_manager(self) -> Any:
        """Get or create SyncJobManager.

        Returns:
            SyncJobManager instance
        """
        if self._manager is None:
            from .sync_job_manager import SyncJobManager

            record_store = self._gw.record_store
            if not record_store:
                raise RuntimeError(
                    "SyncJobService requires record_store. "
                    "Ensure NexusFS is initialized with a database."
                )
            self._manager = SyncJobManager(record_store)
        return self._manager

    def create_job(
        self,
        mount_point: str,
        params: dict[str, Any],
        user_id: str | None = None,
    ) -> str:
        """Create a sync job record.

        Args:
            mount_point: Mount point to sync
            params: Sync parameters (recursive, dry_run, etc.)
            user_id: User who initiated the job

        Returns:
            Job ID (UUID string)
        """
        manager = self._get_manager()
        result: str = manager.create_job(mount_point, params, user_id)
        return result

    def start_job(self, job_id: str) -> None:
        """Start job execution in background thread.

        Args:
            job_id: Job ID to start
        """
        from nexus.contracts.types import SyncContext

        from .sync_job_manager import SyncCancelled

        manager = self._get_manager()

        def execute() -> None:
            try:
                manager.mark_running(job_id)

                job = manager.get_job(job_id)
                if not job:
                    logger.error(f"[SYNC_JOB] Job not found: {job_id}")
                    return

                params = job.get("params", {})
                if isinstance(params, str):
                    import json

                    params = json.loads(params)

                # Create progress callback that checks for cancellation
                def progress_callback(files_scanned: int, current_path: str) -> None:
                    if manager.is_cancelled(job_id):
                        raise SyncCancelled(job_id)
                    manager.update_progress(job_id, files_scanned, current_path)

                ctx = SyncContext(
                    mount_point=job["mount_point"],
                    path=params.get("path"),
                    recursive=params.get("recursive", True),
                    dry_run=params.get("dry_run", False),
                    sync_content=params.get("sync_content", True),
                    include_patterns=params.get("include_patterns"),
                    exclude_patterns=params.get("exclude_patterns"),
                    generate_embeddings=params.get("generate_embeddings", False),
                    progress_callback=progress_callback,
                )

                result = self._sync.sync_mount(ctx)
                manager.complete_job(job_id, asdict(result))
                logger.info(f"[SYNC_JOB] Job {job_id} completed successfully")

            except SyncCancelled:
                manager.mark_cancelled(job_id)
                logger.info(f"[SYNC_JOB] Job {job_id} was cancelled")
            except Exception as e:
                manager.fail_job(job_id, str(e))
                logger.error(f"[SYNC_JOB] Job {job_id} failed: {e}")

        thread = threading.Thread(target=execute, daemon=True, name=f"sync-job-{job_id[:8]}")
        thread.start()
        logger.info(f"[SYNC_JOB] Started job {job_id} in background thread")

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        """Get job status and progress.

        Args:
            job_id: Job ID to look up

        Returns:
            Job details dict or None if not found
        """
        manager = self._get_manager()
        result: dict[str, Any] | None = manager.get_job(job_id)
        return result

    def cancel_job(self, job_id: str) -> bool:
        """Request job cancellation.

        Args:
            job_id: Job ID to cancel

        Returns:
            True if cancellation was requested
        """
        manager = self._get_manager()
        result: bool = manager.cancel_job(job_id)
        return result

    def sync_mount_async(
        self,
        mount_point: str,
        path: str | None = None,
        recursive: bool = True,
        dry_run: bool = False,
        sync_content: bool = True,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        generate_embeddings: bool = False,
        context: Any = None,
    ) -> dict[str, Any]:
        """Convenience method to create and start an async sync job.

        Accepts flat parameters matching the NexusFS.sync_mount_async signature,
        avoiding parameter transformation at the call site.

        Args:
            mount_point: Mount point to sync (required)
            path: Optional sub-path within mount
            recursive: Whether to sync recursively
            dry_run: If True, scan without making changes
            sync_content: Whether to sync content cache
            include_patterns: Glob patterns for files to include
            exclude_patterns: Glob patterns for files to exclude
            generate_embeddings: Whether to generate embeddings
            context: Operation context for user identification

        Returns:
            Dictionary with job_id, status, and mount_point

        Raises:
            ValueError: If mount_point is None
        """
        if mount_point is None:
            raise ValueError("mount_point is required for async sync")
        user_id = getattr(context, "subject_id", None) if context else None
        params: dict[str, Any] = {
            "path": path,
            "recursive": recursive,
            "dry_run": dry_run,
            "sync_content": sync_content,
            "include_patterns": include_patterns,
            "exclude_patterns": exclude_patterns,
            "generate_embeddings": generate_embeddings,
        }
        job_id = self.create_job(mount_point, params, user_id)
        self.start_job(job_id)
        return {"job_id": job_id, "status": "pending", "mount_point": mount_point}

    def cancel_sync_job(self, job_id: str) -> dict[str, Any]:
        """Convenience method to cancel a sync job with a descriptive result.

        Wraps cancel_job with structured return values indicating success
        or failure reason.

        Args:
            job_id: Job ID to cancel

        Returns:
            Dictionary with success flag, job_id, and message
        """
        success = self.cancel_job(job_id)
        if success:
            return {"success": True, "job_id": job_id, "message": "Cancellation requested"}
        job = self.get_job(job_id)
        if not job:
            return {"success": False, "job_id": job_id, "message": "Job not found"}
        return {
            "success": False,
            "job_id": job_id,
            "message": f"Cannot cancel job with status: {job['status']}",
        }

    def list_jobs(
        self,
        mount_point: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List sync jobs with optional filters.

        Args:
            mount_point: Filter by mount point
            status: Filter by status
            limit: Maximum jobs to return

        Returns:
            List of job dictionaries
        """
        manager = self._get_manager()
        result: list[dict[str, Any]] = manager.list_jobs(
            mount_point=mount_point, status=status, limit=limit
        )
        return result
