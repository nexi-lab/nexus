"""Sync job manager for async sync operations (Issue #609).

Manages the lifecycle of async sync_mount jobs:
- Create and track job records in database
- Run sync operations as background asyncio tasks
- Progress tracking with hybrid update strategy (time + file count)
- Cancellation support
- Job listing and status queries

Example:
    >>> from nexus.core.sync_job_manager import SyncJobManager
    >>>
    >>> manager = SyncJobManager(session_factory)
    >>>
    >>> # Start an async sync
    >>> job_id = manager.create_job("/mnt/gmail", {"path": "/inbox"}, user_id="alice")
    >>> await manager.start_job(job_id, nexus_fs)
    >>>
    >>> # Monitor progress
    >>> job = manager.get_job(job_id)
    >>> print(f"Progress: {job['progress_pct']}%")
    >>>
    >>> # Cancel if needed
    >>> manager.cancel_job(job_id)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import desc, select

from nexus.storage.models import SyncJobModel

if TYPE_CHECKING:
    from nexus.core.nexus_fs import NexusFS

logger = logging.getLogger(__name__)


class SyncCancelled(Exception):
    """Raised when a sync job is cancelled."""

    pass


@dataclass
class ProgressState:
    """Tracks progress update state for hybrid strategy."""

    last_update_time: float = 0.0
    last_update_files: int = 0

    # Hybrid thresholds
    UPDATE_INTERVAL_SECONDS: float = 5.0
    UPDATE_INTERVAL_FILES: int = 20

    def should_update(self, files_scanned: int) -> bool:
        """Check if progress should be updated (hybrid: time OR file count)."""
        now = time.time()
        time_elapsed = now - self.last_update_time >= self.UPDATE_INTERVAL_SECONDS
        files_elapsed = files_scanned - self.last_update_files >= self.UPDATE_INTERVAL_FILES

        if time_elapsed or files_elapsed:
            self.last_update_time = now
            self.last_update_files = files_scanned
            return True
        return False


class SyncJobManager:
    """Manages async sync job lifecycle.

    Provides:
    - Job creation and persistence
    - Background task execution with progress updates
    - Cancellation via in-memory flags
    - Job queries and listing

    Thread-safety note:
        The in-memory cancellation flags are shared across async tasks in the same
        event loop. For multi-process deployments, cancellation should also check
        the database status.
    """

    # Class-level tracking for active jobs and cancellation flags
    # These are shared across all instances in the same process
    _active_jobs: dict[str, asyncio.Task] = {}
    _cancellation_flags: dict[str, bool] = {}

    def __init__(self, session_factory: Any) -> None:
        """Initialize sync job manager.

        Args:
            session_factory: SQLAlchemy sessionmaker instance
        """
        self.SessionLocal = session_factory

    def create_job(
        self,
        mount_point: str,
        sync_params: dict[str, Any],
        user_id: str | None = None,
    ) -> str:
        """Create a new sync job record.

        Args:
            mount_point: Mount point to sync
            sync_params: Sync parameters (path, patterns, etc.)
            user_id: User who initiated the sync

        Returns:
            job_id: UUID of the created job

        Example:
            >>> job_id = manager.create_job(
            ...     mount_point="/mnt/gmail",
            ...     sync_params={"path": "/inbox", "include_patterns": ["*.eml"]},
            ...     user_id="alice"
            ... )
        """
        job_id = str(uuid.uuid4())

        with self.SessionLocal() as session:
            job = SyncJobModel(
                id=job_id,
                mount_point=mount_point,
                status="pending",
                progress_pct=0,
                sync_params=json.dumps(sync_params),
                created_at=datetime.now(UTC),
                created_by=user_id,
            )
            session.add(job)
            session.commit()

        logger.info(f"Created sync job {job_id} for mount {mount_point}")
        return job_id

    async def start_job(self, job_id: str, nexus_fs: NexusFS) -> None:
        """Start a sync job as a background asyncio task.

        Args:
            job_id: Job ID to start
            nexus_fs: NexusFS instance to use for syncing

        Raises:
            ValueError: If job not found or already running
        """
        job = self.get_job(job_id)
        if not job:
            raise ValueError(f"Job not found: {job_id}")

        if job["status"] not in ("pending",):
            raise ValueError(f"Job {job_id} is not pending (status: {job['status']})")

        # Initialize cancellation flag
        SyncJobManager._cancellation_flags[job_id] = False

        # Create and store the background task
        task = asyncio.create_task(self._run_sync(job_id, nexus_fs))
        SyncJobManager._active_jobs[job_id] = task

        logger.info(f"Started sync job {job_id}")

    def cancel_job(self, job_id: str) -> bool:
        """Request cancellation of a running sync job.

        Args:
            job_id: Job ID to cancel

        Returns:
            True if cancellation was requested, False if job not found/not running
        """
        job = self.get_job(job_id)
        if not job:
            return False

        if job["status"] != "running":
            logger.warning(f"Cannot cancel job {job_id}: status is {job['status']}")
            return False

        # Set cancellation flag
        SyncJobManager._cancellation_flags[job_id] = True
        logger.info(f"Requested cancellation for sync job {job_id}")

        return True

    def get_job(self, job_id: str) -> dict | None:
        """Get job status and details.

        Args:
            job_id: Job ID to retrieve

        Returns:
            Job details dict or None if not found
        """
        with self.SessionLocal() as session:
            stmt = select(SyncJobModel).where(SyncJobModel.id == job_id)
            job = session.execute(stmt).scalar_one_or_none()

            if not job:
                return None

            result: dict[str, Any] = job.to_dict()
            return result

    def list_jobs(
        self,
        mount_point: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """List sync jobs with optional filters.

        Args:
            mount_point: Filter by mount point
            status: Filter by status (pending, running, completed, failed, cancelled)
            limit: Maximum number of jobs to return

        Returns:
            List of job dicts, ordered by created_at descending
        """
        with self.SessionLocal() as session:
            stmt = select(SyncJobModel)

            if mount_point:
                stmt = stmt.where(SyncJobModel.mount_point == mount_point)
            if status:
                stmt = stmt.where(SyncJobModel.status == status)

            stmt = stmt.order_by(desc(SyncJobModel.created_at)).limit(limit)

            jobs = session.execute(stmt).scalars().all()
            return [job.to_dict() for job in jobs]

    def _update_job_status(
        self,
        job_id: str,
        status: str,
        progress_pct: int | None = None,
        progress_detail: dict | None = None,
        result: dict | None = None,
        error_message: str | None = None,
    ) -> None:
        """Update job status in database.

        Internal method called during job execution.
        """
        with self.SessionLocal() as session:
            stmt = select(SyncJobModel).where(SyncJobModel.id == job_id)
            job = session.execute(stmt).scalar_one_or_none()

            if not job:
                logger.warning(f"Job {job_id} not found for status update")
                return

            job.status = status

            if progress_pct is not None:
                job.progress_pct = progress_pct
            if progress_detail is not None:
                job.progress_detail = json.dumps(progress_detail)
            if result is not None:
                job.result = json.dumps(result)
            if error_message is not None:
                job.error_message = error_message

            # Update timestamps
            if status == "running" and job.started_at is None:
                job.started_at = datetime.now(UTC)
            if status in ("completed", "failed", "cancelled"):
                job.completed_at = datetime.now(UTC)

            session.commit()

    def _check_cancellation(self, job_id: str) -> None:
        """Check if job should be cancelled.

        Raises:
            SyncCancelled: If cancellation was requested
        """
        if SyncJobManager._cancellation_flags.get(job_id, False):
            raise SyncCancelled(f"Job {job_id} was cancelled")

    def _create_progress_callback(
        self, job_id: str, progress_state: ProgressState, total_estimate: int | None = None
    ) -> Callable[[int, str], None]:
        """Create a progress callback function for sync operations.

        Args:
            job_id: Job ID being synced
            progress_state: Progress state tracker
            total_estimate: Estimated total files (for percentage calculation)

        Returns:
            Callback function: (files_scanned, current_path) -> None
        """

        def callback(files_scanned: int, current_path: str) -> None:
            # Check cancellation
            self._check_cancellation(job_id)

            # Check if we should update (hybrid strategy)
            if not progress_state.should_update(files_scanned):
                return

            # Calculate percentage
            if total_estimate and total_estimate > 0:
                progress_pct = min(99, int((files_scanned / total_estimate) * 100))
            else:
                # Unknown total - use logarithmic scale capped at 90%
                progress_pct = min(90, int(50 * (1 - 1 / (1 + files_scanned / 100))))

            # Update database
            self._update_job_status(
                job_id,
                status="running",
                progress_pct=progress_pct,
                progress_detail={
                    "files_scanned": files_scanned,
                    "total_estimate": total_estimate,
                    "current_path": current_path,
                },
            )

        return callback

    async def _run_sync(self, job_id: str, nexus_fs: NexusFS) -> None:
        """Internal: Execute sync with progress updates.

        This runs in a background asyncio task.
        """
        try:
            # Mark job as running
            self._update_job_status(job_id, status="running", progress_pct=0)

            # Get job parameters
            job = self.get_job(job_id)
            if not job:
                raise ValueError(f"Job {job_id} not found")

            mount_point = job["mount_point"]
            sync_params = job["sync_params"] or {}

            # Create progress callback
            progress_state = ProgressState(last_update_time=time.time())
            progress_callback = self._create_progress_callback(job_id, progress_state)

            # Run sync in executor to not block event loop
            # (sync_mount is synchronous)
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: nexus_fs.sync_mount(
                    mount_point=mount_point,
                    path=sync_params.get("path"),
                    recursive=sync_params.get("recursive", True),
                    dry_run=sync_params.get("dry_run", False),
                    sync_content=sync_params.get("sync_content", True),
                    include_patterns=sync_params.get("include_patterns"),
                    exclude_patterns=sync_params.get("exclude_patterns"),
                    generate_embeddings=sync_params.get("generate_embeddings", False),
                    progress_callback=progress_callback,
                ),
            )

            # Mark completed
            self._update_job_status(
                job_id,
                status="completed",
                progress_pct=100,
                progress_detail={"files_scanned": result.get("files_scanned", 0)},
                result=result,
            )

            logger.info(
                f"Sync job {job_id} completed: {result.get('files_scanned', 0)} files scanned"
            )

        except SyncCancelled:
            # Mark as cancelled
            self._update_job_status(
                job_id,
                status="cancelled",
                error_message="Job was cancelled by user",
            )
            logger.info(f"Sync job {job_id} was cancelled")

        except Exception as e:
            # Mark as failed
            error_msg = str(e)
            self._update_job_status(
                job_id,
                status="failed",
                error_message=error_msg,
            )
            logger.error(f"Sync job {job_id} failed: {error_msg}", exc_info=True)

        finally:
            # Cleanup
            SyncJobManager._active_jobs.pop(job_id, None)
            SyncJobManager._cancellation_flags.pop(job_id, None)
