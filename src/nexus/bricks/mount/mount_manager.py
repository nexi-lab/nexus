"""Mount manager for persistent mount configuration.

Provides mount persistence across server restarts by storing mount configurations
in the metadata database.

Supports:
- Saving mount configurations to database
- Restoring mounts on startup
- Listing all persisted mounts
- Removing mount configurations
"""

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from nexus.storage.models import MountConfigModel

logger = logging.getLogger(__name__)


@dataclass
class MountConfig:
    """Mount configuration for restore_mounts() return type.

    This is a service-layer data transfer object. The kernel PathRouter no
    longer exposes MountConfig (it uses _MountEntry internally). This DTO
    carries the fields needed by callers of ``restore_mounts()`` to call
    ``router.add_mount()``.
    """

    mount_point: str
    backend: Any
    readonly: bool = False
    io_profile: str = "balanced"


if TYPE_CHECKING:
    from nexus.storage.record_store import RecordStoreABC


class MountManager:
    """Manager for persistent mount configurations.

    Stores mount configurations in the database so they can be restored
    after server restarts. Useful for dynamic user mounts (e.g., personal
    Google Drive mounts).

    Example:
        >>> from nexus import NexusFS
        >>> from nexus.bricks.mount.mount_manager import MountManager
        >>>
        >>> nx = NexusFS(...)
        >>> manager = MountManager(nx._record_store)
        >>>
        >>> # Save a mount to database
        >>> manager.save_mount(
        ...     mount_point="/personal/alice",
        ...     backend_type="google_drive",
        ...     backend_config={"access_token": "...", "user_email": "alice@acme.com"},
        ...     owner_user_id="alice",
        ... )
        >>>
        >>> # List all persisted mounts
        >>> mounts = manager.list_mounts()
        >>>
        >>> # Remove a mount from database
        >>> manager.remove_mount("/personal/alice")
    """

    def __init__(self, record_store: "RecordStoreABC") -> None:
        """Initialize mount manager.

        Args:
            record_store: A RecordStoreABC providing session_factory for database access.
        """
        self._session_factory = record_store.session_factory

    def save_mount(
        self,
        mount_point: str,
        backend_type: str,
        backend_config: dict,
        readonly: bool = False,
        io_profile: str = "balanced",
        owner_user_id: str | None = None,
        zone_id: str | None = None,
        description: str | None = None,
    ) -> str:
        """Save a mount configuration to the database.

        Args:
            mount_point: Virtual path where backend is mounted (e.g., "/personal/alice")
            backend_type: Type of backend (e.g., "google_drive", "gcs", "local")
            backend_config: Backend-specific configuration (dict) - will be JSON-encoded
            readonly: Whether mount is read-only
            io_profile: I/O tuning profile (Issue #1413)
            owner_user_id: User ID who owns this mount
            zone_id: Zone ID this mount belongs to
            description: Optional description of the mount

        Returns:
            mount_id: Unique ID of the saved mount configuration

        Raises:
            ValueError: If mount_point already exists

        Example:
            >>> manager.save_mount(
            ...     mount_point="/personal/alice",
            ...     backend_type="google_drive",
            ...     backend_config={
            ...         "access_token": "ya29.xxx",
            ...         "refresh_token": "1//xxx",
            ...         "user_email": "alice@acme.com"
            ...     },
            ...     owner_user_id="google:alice123",
            ...     zone_id="acme",
            ...     description="Alice's personal Google Drive"
            ... )
        """
        with self._session_factory() as session:
            # Check if mount already exists
            stmt = select(MountConfigModel).where(MountConfigModel.mount_point == mount_point)
            existing = session.execute(stmt).scalar_one_or_none()

            if existing:
                raise ValueError(f"Mount already exists at {mount_point}")

            # Create new mount config
            mount_model = MountConfigModel(
                mount_id=str(uuid.uuid4()),
                mount_point=mount_point,
                backend_type=backend_type,
                readonly=int(bool(readonly)),  # Convert to int for SQLite/PostgreSQL compatibility
                backend_config=json.dumps(backend_config),
                owner_user_id=owner_user_id,
                zone_id=zone_id,
                description=description,
                io_profile=io_profile,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )

            # Validate before saving
            mount_model.validate()

            # Save to database
            session.add(mount_model)
            session.commit()

            return mount_model.mount_id

    def update_mount(
        self,
        mount_point: str,
        backend_config: dict | None = None,
        readonly: bool | None = None,
        description: str | None = None,
    ) -> bool:
        """Update an existing mount configuration.

        Args:
            mount_point: Mount point to update
            backend_config: New backend config (if provided)
            readonly: New readonly status (if provided)
            description: New description (if provided)

        Returns:
            True if mount was updated, False if not found

        Example:
            >>> # Update access token for existing mount
            >>> manager.update_mount(
            ...     mount_point="/personal/alice",
            ...     backend_config={"access_token": "new_token", "user_email": "alice@acme.com"}
            ... )
        """
        with self._session_factory() as session:
            stmt = select(MountConfigModel).where(MountConfigModel.mount_point == mount_point)
            mount_model = session.execute(stmt).scalar_one_or_none()

            if not mount_model:
                return False

            # Update fields if provided
            if backend_config is not None:
                mount_model.backend_config = json.dumps(backend_config)
            if readonly is not None:
                mount_model.readonly = bool(readonly)
            if description is not None:
                mount_model.description = description

            mount_model.updated_at = datetime.now(UTC)

            # Validate and save
            mount_model.validate()
            session.commit()

            return True

    def get_mount(self, mount_point: str) -> dict | None:
        """Get a mount configuration from database.

        Args:
            mount_point: Mount point to retrieve

        Returns:
            Mount configuration dict or None if not found

        Example:
            >>> config = manager.get_mount("/personal/alice")
            >>> if config:
            ...     print(f"Backend: {config['backend_type']}")
        """
        with self._session_factory() as session:
            stmt = select(MountConfigModel).where(MountConfigModel.mount_point == mount_point)
            mount_model = session.execute(stmt).scalar_one_or_none()

            if not mount_model:
                return None

            return {
                "mount_id": mount_model.mount_id,
                "mount_point": mount_model.mount_point,
                "backend_type": mount_model.backend_type,
                "backend_config": json.loads(mount_model.backend_config),
                "readonly": bool(mount_model.readonly),
                "io_profile": mount_model.io_profile,
                "owner_user_id": mount_model.owner_user_id,
                "zone_id": mount_model.zone_id,
                "description": mount_model.description,
                "created_at": mount_model.created_at,
                "updated_at": mount_model.updated_at,
            }

    def list_mounts(
        self, owner_user_id: str | None = None, zone_id: str | None = None
    ) -> list[dict]:
        """List all persisted mount configurations.

        Args:
            owner_user_id: Filter by owner user ID (optional)
            zone_id: Filter by zone ID (optional)

        Returns:
            List of mount configuration dicts

        Example:
            >>> # List all mounts
            >>> all_mounts = manager.list_mounts()
            >>>
            >>> # List mounts for specific user
            >>> user_mounts = manager.list_mounts(owner_user_id="alice")
            >>>
            >>> # List mounts for specific zone
            >>> zone_mounts = manager.list_mounts(zone_id="acme")
        """
        with self._session_factory() as session:
            stmt = select(MountConfigModel)

            # Apply filters
            if owner_user_id:
                stmt = stmt.where(MountConfigModel.owner_user_id == owner_user_id)
            if zone_id:
                stmt = stmt.where(MountConfigModel.zone_id == zone_id)

            # Order by mount_point
            stmt = stmt.order_by(MountConfigModel.mount_point)

            results = session.execute(stmt).scalars().all()

            return [
                {
                    "mount_id": m.mount_id,
                    "mount_point": m.mount_point,
                    "backend_type": m.backend_type,
                    "backend_config": json.loads(m.backend_config),
                    "readonly": bool(m.readonly),
                    "io_profile": m.io_profile,
                    "owner_user_id": m.owner_user_id,
                    "zone_id": m.zone_id,
                    "description": m.description,
                    "created_at": m.created_at,
                    "updated_at": m.updated_at,
                }
                for m in results
            ]

    def remove_mount(self, mount_point: str) -> bool:
        """Remove a mount configuration from database.

        Args:
            mount_point: Mount point to remove

        Returns:
            True if mount was removed, False if not found

        Example:
            >>> manager.remove_mount("/personal/alice")
            True
        """
        with self._session_factory() as session:
            stmt = select(MountConfigModel).where(MountConfigModel.mount_point == mount_point)
            mount_model = session.execute(stmt).scalar_one_or_none()

            if not mount_model:
                return False

            session.delete(mount_model)
            session.commit()

            return True

    def restore_mounts(self, backend_factory: Any) -> list[MountConfig]:
        """Restore all persisted mounts using a backend factory function.

        Args:
            backend_factory: Function that takes (backend_type, backend_config) and returns a Backend instance

        Returns:
            List of MountConfig objects ready to be added to router

        Example:
            >>> def create_backend(backend_type: str, config: dict) -> Backend:
            ...     if backend_type == "google_drive":
            ...         return GoogleDriveBackend(**config)
            ...     elif backend_type == "gcs":
            ...         return GCSBackend(**config)
            ...     else:
            ...         raise ValueError(f"Unknown backend type: {backend_type}")
            >>>
            >>> # Restore all mounts
            >>> mount_configs = manager.restore_mounts(create_backend)
            >>>
            >>> # Add to router
            >>> for mc in mount_configs:
            ...     router.add_mount(mc.mount_point, mc.backend, mc.readonly)
        """
        mounts_data = self.list_mounts()
        mount_configs = []

        for mount_data in mounts_data:
            try:
                # Create backend instance
                backend = backend_factory(mount_data["backend_type"], mount_data["backend_config"])

                # Create MountConfig
                mount_config = MountConfig(
                    mount_point=mount_data["mount_point"],
                    backend=backend,
                    readonly=mount_data["readonly"],
                )

                mount_configs.append(mount_config)

            except Exception as e:
                # Log error but continue with other mounts
                logger.warning("Failed to restore mount %s: %s", mount_data["mount_point"], e)
                continue

        return mount_configs
