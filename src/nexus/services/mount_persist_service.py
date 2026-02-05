"""Mount Persist Service - Mount configuration persistence.

This service handles saving, loading, and managing mount configurations
in the database for persistence across server restarts.

Phase 2: Mount Mixin Refactoring
Extracted from: nexus_fs_mounts.py (persistence methods)

All methods are synchronous. FastAPI auto-wraps with to_thread.

Example:
    ```python
    persist_service = MountPersistService(mount_manager, mount_service, sync_service)

    # Save mount config
    mount_id = persist_service.save_mount(
        mount_point="/mnt/gcs",
        backend_type="gcs_connector",
        backend_config={"bucket": "my-bucket"},
    )

    # Load on startup
    result = persist_service.load_all_mounts()
    print(f"Loaded {result['loaded']} mounts")
    ```
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from nexus.core.context_utils import get_user_identity, get_zone_id

if TYPE_CHECKING:
    from nexus.core.mount_manager import MountManager
    from nexus.core.permissions import OperationContext
    from nexus.services.mount_core_service import MountCoreService
    from nexus.services.sync_service import SyncService

logger = logging.getLogger(__name__)


class MountPersistService:
    """Handles mount configuration persistence (SYNC).

    All methods are synchronous. FastAPI auto-wraps with to_thread.
    """

    def __init__(
        self,
        mount_manager: MountManager | None,
        mount_service: MountCoreService,
        sync_service: SyncService | None = None,
    ):
        """Initialize persist service.

        Args:
            mount_manager: MountManager for database operations
            mount_service: MountCoreService for activating mounts
            sync_service: Optional SyncService for auto-sync
        """
        self._manager = mount_manager
        self._mounts = mount_service
        self._sync = sync_service

    def _check_manager(self) -> None:
        """Check that mount manager is available.

        Raises:
            RuntimeError: If mount manager is not available
        """
        if not self._manager:
            raise RuntimeError(
                "Mount manager not available. Ensure NexusFS is initialized with a database."
            )

    def save_mount(
        self,
        mount_point: str,
        backend_type: str,
        backend_config: dict[str, Any],
        priority: int = 0,
        readonly: bool = False,
        owner_user_id: str | None = None,
        zone_id: str | None = None,
        description: str | None = None,
        context: OperationContext | None = None,
    ) -> str:
        """Save mount configuration to database.

        Args:
            mount_point: Virtual path where backend is mounted
            backend_type: Backend type identifier
            backend_config: Backend-specific configuration
            priority: Mount priority (default: 0)
            readonly: Read-only flag (default: False)
            owner_user_id: Owner user ID (auto-populated from context)
            zone_id: Zone ID (auto-populated from context)
            description: Human-readable description
            context: Operation context

        Returns:
            Mount ID (UUID string)
        """
        self._check_manager()

        # Auto-populate from context if not provided
        if owner_user_id is None and context:
            subject_type, subject_id = get_user_identity(context)
            if subject_id:
                owner_user_id = f"{subject_type}:{subject_id}"
                logger.info(f"[SAVE_MOUNT] Auto-populated owner_user_id: {owner_user_id}")

        if zone_id is None and context:
            zone_id = get_zone_id(context)
            if zone_id:
                logger.info(f"[SAVE_MOUNT] Auto-populated zone_id: {zone_id}")

        assert self._manager is not None
        mount_id = self._manager.save_mount(
            mount_point=mount_point,
            backend_type=backend_type,
            backend_config=backend_config,
            priority=priority,
            readonly=readonly,
            owner_user_id=owner_user_id,
            zone_id=zone_id,
            description=description,
        )

        # Also activate the mount via MountCoreService
        try:
            self._mounts.add_mount(
                mount_point=mount_point,
                backend_type=backend_type,
                backend_config=backend_config,
                priority=priority,
                readonly=readonly,
                context=context,
            )
        except Exception as e:
            logger.warning(f"[SAVE_MOUNT] Mount saved but activation failed: {e}")

        return mount_id

    def load_mount(
        self,
        mount_point: str,
        context: OperationContext | None = None,
    ) -> str:
        """Load saved mount configuration and activate it.

        Args:
            mount_point: Virtual path of saved mount
            context: Operation context

        Returns:
            Mount ID if successfully loaded

        Raises:
            ValueError: If mount not found in database
        """
        self._check_manager()

        # Check if mount is already active
        if self._mounts.has_mount(mount_point):
            logger.info(f"[LOAD_MOUNT] Mount already active: {mount_point}")
            # Return the mount_id from database
            assert self._manager is not None
            config = self._manager.get_mount(mount_point)
            return str(config["mount_id"]) if config else mount_point

        assert self._manager is not None
        config = self._manager.get_mount(mount_point)
        if not config:
            raise ValueError(f"Mount not found in database: {mount_point}")

        # Parse backend config from JSON if needed
        backend_config = config["backend_config"]
        if isinstance(backend_config, str):
            backend_config = json.loads(backend_config)

        return self._mounts.add_mount(
            mount_point=config["mount_point"],
            backend_type=config["backend_type"],
            backend_config=backend_config,
            priority=config["priority"],
            readonly=bool(config["readonly"]),
            context=context,
        )

    def load_all_mounts(self, auto_sync: bool = False) -> dict[str, Any]:
        """Load all saved mount configurations.

        Args:
            auto_sync: If True, sync connector mounts after loading

        Returns:
            Dictionary with loading results:
            - loaded: Number of mounts loaded
            - synced: Number of mounts synced
            - failed: Number of failures
            - errors: List of error messages
        """
        if not self._manager:
            logger.warning("Mount manager not available, skipping mount restoration")
            return {"loaded": 0, "synced": 0, "failed": 0, "errors": []}

        saved_mounts = self._manager.list_mounts()

        if not saved_mounts:
            logger.info("No saved mounts found in database")
            return {"loaded": 0, "synced": 0, "failed": 0, "errors": []}

        logger.info(f"Found {len(saved_mounts)} saved mount(s) to load")

        loaded = 0
        failed = 0
        synced = 0
        errors: list[str] = []

        for mount in saved_mounts:
            mount_point = mount["mount_point"]
            try:
                logger.info(f"Loading mount: {mount_point} ({mount['backend_type']})")

                # Parse backend config
                backend_config = mount["backend_config"]
                if isinstance(backend_config, str):
                    backend_config = json.loads(backend_config)

                # Activate the mount
                self._mounts.add_mount(
                    mount_point=mount_point,
                    backend_type=mount["backend_type"],
                    backend_config=backend_config,
                    priority=mount["priority"],
                    readonly=bool(mount["readonly"]),
                )

                loaded += 1
                logger.info(f"Successfully loaded mount: {mount_point}")

                # Auto-sync if requested
                if auto_sync and self._sync:
                    backend_type = mount["backend_type"]
                    is_connector = "connector" in backend_type.lower() or backend_type.lower() in [
                        "gcs",
                        "s3",
                    ]

                    if is_connector:
                        try:
                            logger.info(f"Auto-syncing connector mount: {mount_point}")
                            from nexus.services.sync_service import SyncContext

                            # Build context from mount owner if available
                            sync_context = self._build_sync_context(mount)

                            ctx = SyncContext(
                                mount_point=mount_point,
                                recursive=True,
                                dry_run=False,
                                context=sync_context,
                            )
                            result = self._sync.sync_mount(ctx)
                            synced += 1
                            logger.info(
                                f"Synced {mount_point}: "
                                f"{result.files_scanned} scanned, "
                                f"{result.files_created} created"
                            )
                        except Exception as sync_e:
                            logger.warning(f"Failed to sync {mount_point}: {sync_e}")
                    else:
                        logger.info(f"Skipping auto-sync for {mount_point} (not a connector)")

            except Exception as e:
                failed += 1
                error_msg = f"Failed to load mount {mount_point}: {e}"
                errors.append(error_msg)
                logger.error(error_msg)

        logger.info(f"Mount loading complete: {loaded} loaded, {synced} synced, {failed} failed")

        return {"loaded": loaded, "synced": synced, "failed": failed, "errors": errors}

    def list_saved_mounts(
        self,
        owner_user_id: str | None = None,
        zone_id: str | None = None,
        context: OperationContext | None = None,
    ) -> list[dict[str, Any]]:
        """List saved mount configurations.

        Args:
            owner_user_id: Filter by owner (auto-populated from context)
            zone_id: Filter by zone (auto-populated from context)
            context: Operation context

        Returns:
            List of mount configuration dictionaries
        """
        self._check_manager()

        # Auto-populate filters from context
        if owner_user_id is None and context:
            subject_type, subject_id = get_user_identity(context)
            if subject_id:
                owner_user_id = f"{subject_type}:{subject_id}"
                logger.info(f"[LIST_SAVED_MOUNTS] Auto-filtering by owner: {owner_user_id}")

        if zone_id is None and context:
            zone_id = get_zone_id(context)
            if zone_id:
                logger.info(f"[LIST_SAVED_MOUNTS] Auto-filtering by zone: {zone_id}")

        assert self._manager is not None
        return self._manager.list_mounts(owner_user_id=owner_user_id, zone_id=zone_id)

    def delete_saved_mount(self, mount_point: str) -> bool:
        """Delete saved mount configuration.

        Note: This does NOT deactivate the mount if currently active.

        Args:
            mount_point: Virtual path of mount to delete

        Returns:
            True if deleted, False if not found
        """
        self._check_manager()

        assert self._manager is not None
        return self._manager.remove_mount(mount_point)

    def _build_sync_context(self, mount: dict[str, Any]) -> Any:
        """Build OperationContext from mount owner info.

        Args:
            mount: Mount configuration dictionary

        Returns:
            OperationContext or None
        """
        if not mount.get("owner_user_id"):
            return None

        try:
            from nexus.core.permissions import OperationContext

            owner_parts = mount["owner_user_id"].split(":", 1)
            if len(owner_parts) == 2:
                subject_type, subject_id = owner_parts
            else:
                subject_type, subject_id = "user", owner_parts[0]

            return OperationContext(
                user=subject_id,
                groups=[],
                zone_id=mount.get("zone_id", "default"),
                subject_type=subject_type,
                subject_id=subject_id,
            )
        except Exception as e:
            logger.warning(f"Failed to build sync context: {e}")
            return None
