"""Mount management operations for NexusFS.

This module provides a thin facade over mount services:
- MountCoreService: add/remove/list mounts
- SyncService: sync operations
- SyncJobService: async job management
- MountPersistService: database persistence

Phase 2: Mount Mixin Refactoring
- Original: 2,065 lines monolithic mixin
- New: ~150 lines thin facade delegating to services

All business logic is in services. This mixin only:
1. Provides @rpc_expose decorated methods
2. Instantiates services via cached_property
3. Delegates to services
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import asdict
from functools import cached_property
from typing import TYPE_CHECKING, Any

from nexus.core.rpc_decorator import rpc_expose

if TYPE_CHECKING:
    from nexus.core.mount_manager import MountManager
    from nexus.core.permissions import OperationContext
    from nexus.core.router import PathRouter
    from nexus.services.gateway import NexusFSGateway
    from nexus.services.mount_core_service import MountCoreService
    from nexus.services.mount_persist_service import MountPersistService
    from nexus.services.sync_job_service import SyncJobService
    from nexus.services.sync_service import SyncService

# Module-level logger
logger = logging.getLogger(__name__)

# Type alias for progress callback (backward compatibility)
ProgressCallback = Callable[[int, str], None]


class NexusFSMountsMixin:
    """Thin facade exposing mount operations via RPC.

    All logic delegated to services:
    - MountCoreService: add/remove/list mounts
    - SyncService: sync operations
    - SyncJobService: async job management
    - MountPersistService: database persistence

    AI-Friendly Design:
    - Services accessed via self._mount_core_service, etc.
    - Each service uses Gateway pattern (self._gw)
    - Clear delegation, no business logic here
    """

    # Type hints for attributes provided by NexusFS parent class
    if TYPE_CHECKING:
        router: PathRouter
        mount_manager: MountManager | None

    # =========================================================================
    # Service Properties (lazy initialization)
    # =========================================================================

    @cached_property
    def _gateway(self) -> "NexusFSGateway":
        """Get or create NexusFSGateway."""
        from nexus.services.gateway import NexusFSGateway

        return NexusFSGateway(self)  # type: ignore[arg-type]

    @cached_property
    def _mount_core_service(self) -> "MountCoreService":
        """Get or create MountCoreService."""
        from nexus.services.mount_core_service import MountCoreService

        return MountCoreService(self._gateway)

    @cached_property
    def _sync_service(self) -> "SyncService":
        """Get or create SyncService."""
        from nexus.services.sync_service import SyncService

        return SyncService(self._gateway)

    @cached_property
    def _sync_job_service(self) -> "SyncJobService":
        """Get or create SyncJobService."""
        from nexus.services.sync_job_service import SyncJobService

        return SyncJobService(self._gateway, self._sync_service)

    @cached_property
    def _mount_persist_service(self) -> "MountPersistService":
        """Get or create MountPersistService."""
        from nexus.services.mount_persist_service import MountPersistService

        return MountPersistService(
            mount_manager=getattr(self, "mount_manager", None),
            mount_service=self._mount_core_service,
            sync_service=self._sync_service,
        )

    # =========================================================================
    # Core Mount Operations
    # =========================================================================

    @rpc_expose(description="Add dynamic backend mount")
    def add_mount(
        self,
        mount_point: str,
        backend_type: str,
        backend_config: dict[str, Any],
        priority: int = 0,
        readonly: bool = False,
        context: "OperationContext | None" = None,
    ) -> str:
        """Add a dynamic backend mount to the filesystem.

        Args:
            mount_point: Virtual path where backend is mounted
            backend_type: Backend type identifier
            backend_config: Backend-specific configuration
            priority: Mount priority (higher takes precedence)
            readonly: Whether mount is read-only
            context: Operation context

        Returns:
            Mount ID (mount_point)
        """
        return self._mount_core_service.add_mount(
            mount_point=mount_point,
            backend_type=backend_type,
            backend_config=backend_config,
            priority=priority,
            readonly=readonly,
            context=context,
        )

    @rpc_expose(description="Remove backend mount")
    def remove_mount(
        self,
        mount_point: str,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any]:
        """Remove a backend mount from the filesystem.

        Args:
            mount_point: Virtual path of mount to remove
            context: Operation context

        Returns:
            Dictionary with removal details
        """
        return self._mount_core_service.remove_mount(
            mount_point=mount_point,
            context=context,
        )

    @rpc_expose(description="List available connector types")
    def list_connectors(self, category: str | None = None) -> list[dict[str, Any]]:
        """List all available connector types.

        Args:
            category: Optional filter by category

        Returns:
            List of connector info dictionaries
        """
        return self._mount_core_service.list_connectors(category)

    @rpc_expose(description="List all backend mounts")
    def list_mounts(self, context: "OperationContext | None" = None) -> list[dict[str, Any]]:
        """List all active backend mounts.

        Args:
            context: Operation context for permission filtering

        Returns:
            List of mount info dictionaries
        """
        return self._mount_core_service.list_mounts(context)

    @rpc_expose(description="Get mount details")
    def get_mount(self, mount_point: str) -> dict[str, Any] | None:
        """Get details about a specific mount.

        Args:
            mount_point: Virtual path of mount

        Returns:
            Mount info dict or None
        """
        return self._mount_core_service.get_mount(mount_point)

    @rpc_expose(description="Check if mount exists")
    def has_mount(self, mount_point: str) -> bool:
        """Check if a mount exists.

        Args:
            mount_point: Virtual path to check

        Returns:
            True if mount exists
        """
        return self._mount_core_service.has_mount(mount_point)

    # =========================================================================
    # Sync Operations
    # =========================================================================

    @rpc_expose(description="Sync metadata from connector backend")
    def sync_mount(
        self,
        mount_point: str | None = None,
        path: str | None = None,
        recursive: bool = True,
        dry_run: bool = False,
        sync_content: bool = True,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        generate_embeddings: bool = False,
        context: "OperationContext | None" = None,
        progress_callback: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        """Sync metadata and content from connector backend(s).

        Args:
            mount_point: Mount to sync (None = all mounts)
            path: Specific path within mount
            recursive: Sync subdirectories recursively
            dry_run: Report only, no changes
            sync_content: Also sync content to cache
            include_patterns: Glob patterns to include
            exclude_patterns: Glob patterns to exclude
            generate_embeddings: Generate embeddings
            context: Operation context
            progress_callback: Progress callback function

        Returns:
            Dictionary with sync statistics
        """
        from nexus.services.sync_service import SyncContext

        ctx = SyncContext(
            mount_point=mount_point,
            path=path,
            recursive=recursive,
            dry_run=dry_run,
            sync_content=sync_content,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
            generate_embeddings=generate_embeddings,
            context=context,
            progress_callback=progress_callback,
        )

        result = self._sync_service.sync_mount(ctx)
        return result.to_dict()

    # =========================================================================
    # Async Sync Jobs
    # =========================================================================

    @rpc_expose(description="Start async sync job for a mount")
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
        context: "OperationContext | None" = None,
    ) -> dict[str, Any]:
        """Start an async sync job for a mount.

        Args:
            mount_point: Mount to sync
            path: Specific path within mount
            recursive: Sync subdirectories
            dry_run: Report only
            sync_content: Sync content to cache
            include_patterns: Patterns to include
            exclude_patterns: Patterns to exclude
            generate_embeddings: Generate embeddings
            context: Operation context

        Returns:
            Dictionary with job_id and status
        """
        if mount_point is None:
            raise ValueError("mount_point is required for async sync")

        # Get user_id from context
        user_id = None
        if context:
            user_id = getattr(context, "subject_id", None)

        # Build params dict
        params = {
            "path": path,
            "recursive": recursive,
            "dry_run": dry_run,
            "sync_content": sync_content,
            "include_patterns": include_patterns,
            "exclude_patterns": exclude_patterns,
            "generate_embeddings": generate_embeddings,
        }

        job_id = self._sync_job_service.create_job(mount_point, params, user_id)
        self._sync_job_service.start_job(job_id)

        return {
            "job_id": job_id,
            "status": "pending",
            "mount_point": mount_point,
        }

    @rpc_expose(description="Get sync job status and progress")
    def get_sync_job(self, job_id: str) -> dict[str, Any] | None:
        """Get sync job status.

        Args:
            job_id: Job ID to look up

        Returns:
            Job details or None
        """
        return self._sync_job_service.get_job(job_id)

    @rpc_expose(description="Cancel a running sync job")
    def cancel_sync_job(self, job_id: str) -> dict[str, Any]:
        """Cancel a running sync job.

        Args:
            job_id: Job ID to cancel

        Returns:
            Dictionary with success status
        """
        success = self._sync_job_service.cancel_job(job_id)

        if success:
            return {
                "success": True,
                "job_id": job_id,
                "message": "Cancellation requested",
            }
        else:
            job = self._sync_job_service.get_job(job_id)
            if not job:
                return {
                    "success": False,
                    "job_id": job_id,
                    "message": "Job not found",
                }
            else:
                return {
                    "success": False,
                    "job_id": job_id,
                    "message": f"Cannot cancel job with status: {job['status']}",
                }

    @rpc_expose(description="List sync jobs")
    def list_sync_jobs(
        self,
        mount_point: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List sync jobs with optional filters.

        Args:
            mount_point: Filter by mount
            status: Filter by status
            limit: Maximum jobs to return

        Returns:
            List of job dictionaries
        """
        return self._sync_job_service.list_jobs(
            mount_point=mount_point,
            status=status,
            limit=limit,
        )

    # =========================================================================
    # Persistence Operations
    # =========================================================================

    @rpc_expose(description="Save mount configuration to database")
    def save_mount(
        self,
        mount_point: str,
        backend_type: str,
        backend_config: dict[str, Any],
        priority: int = 0,
        readonly: bool = False,
        owner_user_id: str | None = None,
        tenant_id: str | None = None,
        description: str | None = None,
        context: "OperationContext | None" = None,
    ) -> str:
        """Save mount configuration to database.

        Args:
            mount_point: Virtual path
            backend_type: Backend type
            backend_config: Backend configuration
            priority: Mount priority
            readonly: Read-only flag
            owner_user_id: Owner user ID
            tenant_id: Tenant ID
            description: Description
            context: Operation context

        Returns:
            Mount ID
        """
        return self._mount_persist_service.save_mount(
            mount_point=mount_point,
            backend_type=backend_type,
            backend_config=backend_config,
            priority=priority,
            readonly=readonly,
            owner_user_id=owner_user_id,
            tenant_id=tenant_id,
            description=description,
            context=context,
        )

    @rpc_expose(description="List saved mount configurations")
    def list_saved_mounts(
        self,
        owner_user_id: str | None = None,
        tenant_id: str | None = None,
        context: "OperationContext | None" = None,
    ) -> list[dict[str, Any]]:
        """List saved mount configurations.

        Args:
            owner_user_id: Filter by owner
            tenant_id: Filter by tenant
            context: Operation context

        Returns:
            List of saved mount configurations
        """
        return self._mount_persist_service.list_saved_mounts(
            owner_user_id=owner_user_id,
            tenant_id=tenant_id,
            context=context,
        )

    @rpc_expose(description="Load and activate saved mount")
    def load_mount(self, mount_point: str) -> str:
        """Load saved mount configuration and activate it.

        Args:
            mount_point: Virtual path of saved mount

        Returns:
            Mount ID
        """
        return self._mount_persist_service.load_mount(mount_point)

    @rpc_expose(description="Delete saved mount configuration")
    def delete_saved_mount(self, mount_point: str) -> bool:
        """Delete saved mount configuration.

        Args:
            mount_point: Virtual path

        Returns:
            True if deleted
        """
        return self._mount_persist_service.delete_saved_mount(mount_point)

    def load_all_saved_mounts(self, auto_sync: bool = False) -> dict[str, Any]:
        """Load all saved mount configurations.

        Args:
            auto_sync: Auto-sync connector mounts

        Returns:
            Loading results dictionary
        """
        return self._mount_persist_service.load_all_mounts(auto_sync)
