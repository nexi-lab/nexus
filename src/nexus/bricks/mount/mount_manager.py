"""Mount manager for persistent mount configuration.

Provides mount persistence across server restarts by storing mount configurations
in the Metastore (redb).

Supports:
- Saving mount configurations to metastore
- Restoring mounts on startup
- Listing all persisted mounts
- Removing mount configurations

Issue #192: Migrated from SQLAlchemy ORM (MountConfigModel) to MetastoreABC.
"""

import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.bricks.mount.metastore_mount_store import MetastoreMountStore

logger = logging.getLogger(__name__)


@dataclass
class MountConfig:
    """Mount configuration for restore_mounts() return type.

    This is a service-layer data transfer object. The kernel PathRouter no
    longer exposes MountConfig (it uses _MountEntry internally). This DTO
    carries the fields needed by callers of ``restore_mounts()`` to call
    ``mount_table.add()``.
    """

    mount_point: str
    backend: Any


class MountManager:
    """Manager for persistent mount configurations.

    Stores mount configurations in the metastore so they can be restored
    after server restarts. Useful for dynamic user mounts (e.g., personal
    Google Drive mounts).

    Example:
        >>> from nexus.bricks.mount.mount_manager import MountManager
        >>> from nexus.bricks.mount.metastore_mount_store import MetastoreMountStore
        >>>
        >>> store = MetastoreMountStore(metastore)
        >>> manager = MountManager(store)
        >>>
        >>> # Save a mount
        >>> manager.save_mount(
        ...     mount_point="/personal/alice",
        ...     backend_type="google_drive",
        ...     backend_config={"access_token": "...", "user_email": "alice@acme.com"},
        ...     owner_user_id="alice",
        ... )
    """

    def __init__(self, mount_store: "MetastoreMountStore") -> None:
        """Initialize mount manager.

        Args:
            mount_store: A MetastoreMountStore for metastore-backed persistence.
        """
        self._store = mount_store

    def save_mount(
        self,
        mount_point: str,
        backend_type: str,
        backend_config: dict[str, Any],
        owner_user_id: str | None = None,
        zone_id: str | None = None,
        description: str | None = None,
    ) -> str:
        """Save a mount configuration to the metastore.

        Args:
            mount_point: Virtual path where backend is mounted (e.g., "/personal/alice")
            backend_type: Type of backend (e.g., "google_drive", "gcs", "cas_local")
            backend_config: Backend-specific configuration (dict)
            owner_user_id: User ID who owns this mount
            zone_id: Zone ID this mount belongs to
            description: Optional description of the mount

        Returns:
            mount_id: Unique ID of the saved mount configuration

        Raises:
            ValueError: If mount_point already exists or validation fails
        """
        mount_id = str(uuid.uuid4())
        return self._store.save(
            mount_id=mount_id,
            mount_point=mount_point,
            backend_type=backend_type,
            backend_config=backend_config,
            owner_user_id=owner_user_id,
            zone_id=zone_id,
            description=description,
        )

    def update_mount(
        self,
        mount_point: str,
        backend_config: dict[str, Any] | None = None,
        description: str | None = None,
    ) -> bool:
        """Update an existing mount configuration.

        Args:
            mount_point: Mount point to update
            backend_config: New backend config (if provided)
            description: New description (if provided)

        Returns:
            True if mount was updated, False if not found
        """
        return self._store.update(
            mount_point=mount_point,
            backend_config=backend_config,
            description=description,
        )

    def get_mount(self, mount_point: str) -> dict[str, Any] | None:
        """Get a mount configuration.

        Args:
            mount_point: Mount point to retrieve

        Returns:
            Mount configuration dict or None if not found
        """
        return self._store.get(mount_point)

    def list_mounts(
        self, owner_user_id: str | None = None, zone_id: str | None = None
    ) -> list[dict[str, Any]]:
        """List all persisted mount configurations.

        Args:
            owner_user_id: Filter by owner user ID (optional)
            zone_id: Filter by zone ID (optional)

        Returns:
            List of mount configuration dicts
        """
        return self._store.list_all(owner_user_id=owner_user_id, zone_id=zone_id)

    def remove_mount(self, mount_point: str) -> bool:
        """Remove a mount configuration.

        Args:
            mount_point: Mount point to remove

        Returns:
            True if mount was removed, False if not found
        """
        return self._store.remove(mount_point)

    def restore_mounts(self, backend_factory: Any) -> list[MountConfig]:
        """Restore all persisted mounts using a backend factory function.

        Args:
            backend_factory: Function that takes (backend_type, backend_config) and returns a Backend instance

        Returns:
            List of MountConfig objects ready to be added to router
        """
        mounts_data = self.list_mounts()
        mount_configs: list[MountConfig] = []

        for mount_data in mounts_data:
            try:
                # Create backend instance
                backend = backend_factory(mount_data["backend_type"], mount_data["backend_config"])

                # Create MountConfig
                mount_config = MountConfig(
                    mount_point=mount_data["mount_point"],
                    backend=backend,
                )

                mount_configs.append(mount_config)

            except Exception as e:
                # Log error but continue with other mounts
                logger.warning("Failed to restore mount %s: %s", mount_data["mount_point"], e)
                continue

        return mount_configs
