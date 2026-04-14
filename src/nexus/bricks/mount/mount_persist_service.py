"""Mount Persist Service - Mount configuration persistence.

This service handles saving, loading, and managing mount configurations
in the database for persistence across server restarts.

Phase 2: Mount Mixin Refactoring
Extracted from: nexus_fs_mounts.py (persistence methods)

Mount activation methods (save_mount, load_mount, load_all_mounts) are async
because MountCoreService.add_mount is async. Database-only methods remain sync.

Example:
    ```python
    persist_service = MountPersistService(mount_manager, mount_service)

    # Save mount config
    mount_id = await persist_service.save_mount(
        mount_point="/mnt/gcs",
        backend_type="path_gcs",
        backend_config={"bucket": "my-bucket"},
    )

    # Load on startup
    result = await persist_service.load_all_mounts()
    print(f"Loaded {result['loaded']} mounts")
    ```
"""

import json
import logging
from typing import TYPE_CHECKING, Any

from nexus.lib.context_utils import get_user_identity, get_zone_id

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext

    from .mount_manager import MountManager
    from .mount_service import MountService

logger = logging.getLogger(__name__)


class MountPersistService:
    """Handles mount configuration persistence.

    Mount activation methods (save_mount, load_mount, load_all_mounts)
    are async because MountCoreService.add_mount is async.
    Database-only methods remain sync.
    """

    def __init__(
        self,
        mount_manager: "MountManager | None",
        mount_service: "MountService | None",
    ):
        """Initialize persist service.

        Args:
            mount_manager: MountManager for database operations
            mount_service: MountService for activating mounts
        """
        self._manager = mount_manager
        self._mounts_ref: "MountService | None" = mount_service

    @property
    def _mounts(self) -> "MountService":
        """MountService accessor — raises if not wired yet."""
        if self._mounts_ref is None:
            raise RuntimeError("MountService not wired into MountPersistService")
        return self._mounts_ref

    def _check_manager(self) -> None:
        """Check that mount manager is available.

        Raises:
            RuntimeError: If mount manager is not available
        """
        if not self._manager:
            raise RuntimeError(
                "Mount manager not available. Ensure NexusFS is initialized with a database."
            )

    async def save_mount(
        self,
        mount_point: str,
        backend_type: str,
        backend_config: dict[str, Any],
        owner_user_id: str | None = None,
        zone_id: str | None = None,
        description: str | None = None,
        context: "OperationContext | None" = None,
    ) -> str:
        """Save mount configuration to database.

        Args:
            mount_point: Virtual path where backend is mounted
            backend_type: Backend type identifier
            backend_config: Backend-specific configuration
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
            owner_user_id=owner_user_id,
            zone_id=zone_id,
            description=description,
        )

        # Also activate the mount via MountService

        try:
            self._mounts.add_mount_sync(
                mount_point=mount_point,
                backend_type=backend_type,
                backend_config=backend_config,
                context=context,
            )
        except Exception as e:
            logger.warning(f"[SAVE_MOUNT] Mount saved but activation failed: {e}")

        return mount_id

    async def load_mount(
        self,
        mount_point: str,
        context: "OperationContext | None" = None,
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

        if self._mounts.has_mount_sync(mount_point):
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

        return self._mounts.add_mount_sync(
            mount_point=config["mount_point"],
            backend_type=config["backend_type"],
            backend_config=backend_config,
            context=context,
        )

    async def load_all_mounts(self) -> dict[str, Any]:
        """Load all saved mount configurations.

        Returns:
            Dictionary with loading results:
            - loaded: Number of mounts loaded
            - failed: Number of failures
            - errors: List of error messages
        """
        if not self._manager:
            logger.debug("Mount manager not available, skipping mount restoration")
            return {"loaded": 0, "failed": 0, "errors": []}

        saved_mounts = self._manager.list_mounts()

        if not saved_mounts:
            logger.info("No saved mounts found in database")
            return {"loaded": 0, "failed": 0, "errors": []}

        logger.info(f"Found {len(saved_mounts)} saved mount(s) to load")

        loaded = 0
        failed = 0
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
                self._mounts.add_mount_sync(
                    mount_point=mount_point,
                    backend_type=mount["backend_type"],
                    backend_config=backend_config,
                )

                loaded += 1
                logger.info(f"Successfully loaded mount: {mount_point}")

            except Exception as e:
                failed += 1
                error_msg = f"Failed to load mount {mount_point}: {e}"
                errors.append(error_msg)
                logger.error(error_msg)

        logger.info(f"Mount loading complete: {loaded} loaded, {failed} failed")

        return {"loaded": loaded, "failed": failed, "errors": errors}

    def list_saved_mounts(
        self,
        owner_user_id: str | None = None,
        zone_id: str | None = None,
        context: "OperationContext | None" = None,
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
