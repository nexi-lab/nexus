"""Mount manager for persistent mount configuration.

Provides mount persistence across server restarts by storing mount configurations
in the metadata database.

Supports:
- Saving mount configurations to database
- Restoring mounts on startup
- Listing all persisted mounts
- Removing mount configurations
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from nexus.core.router import MountConfig
from nexus.storage.models import MountConfigModel

if TYPE_CHECKING:
    pass


class MountManager:
    """Manager for persistent mount configurations.

    Stores mount configurations in the database so they can be restored
    after server restarts. Useful for dynamic user mounts (e.g., personal
    Google Drive mounts).

    Example:
        >>> from nexus import NexusFS
        >>> from nexus.core.mount_manager import MountManager
        >>>
        >>> nx = NexusFS(...)
        >>> manager = MountManager(nx.metadata.SessionLocal)
        >>>
        >>> # Save a mount to database
        >>> manager.save_mount(
        ...     mount_point="/personal/alice",
        ...     backend_type="google_drive",
        ...     backend_config={"access_token": "...", "user_email": "alice@acme.com"},
        ...     priority=10,
        ...     owner_user_id="alice",
        ... )
        >>>
        >>> # List all persisted mounts
        >>> mounts = manager.list_mounts()
        >>>
        >>> # Remove a mount from database
        >>> manager.remove_mount("/personal/alice")
    """

    def __init__(self, session_factory: Any) -> None:
        """Initialize mount manager.

        Args:
            session_factory: SQLAlchemy sessionmaker instance
        """
        self.SessionLocal = session_factory

    def save_mount(
        self,
        mount_point: str,
        backend_type: str,
        backend_config: dict,
        priority: int = 0,
        readonly: bool = False,
        owner_user_id: str | None = None,
        tenant_id: str | None = None,
        description: str | None = None,
    ) -> str:
        """Save a mount configuration to the database.

        Args:
            mount_point: Virtual path where backend is mounted (e.g., "/personal/alice")
            backend_type: Type of backend (e.g., "google_drive", "gcs", "local")
            backend_config: Backend-specific configuration (dict) - will be JSON-encoded
            priority: Mount priority (higher = preferred)
            readonly: Whether mount is read-only
            owner_user_id: User ID who owns this mount
            tenant_id: Tenant ID this mount belongs to
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
            ...     priority=10,
            ...     owner_user_id="google:alice123",
            ...     tenant_id="acme",
            ...     description="Alice's personal Google Drive"
            ... )
        """
        with self.SessionLocal() as session:
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
                priority=priority,
                readonly=int(bool(readonly)),  # Convert to int for SQLite/PostgreSQL compatibility
                backend_config=json.dumps(backend_config),
                owner_user_id=owner_user_id,
                tenant_id=tenant_id,
                description=description,
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
        priority: int | None = None,
        readonly: bool | None = None,
        description: str | None = None,
    ) -> bool:
        """Update an existing mount configuration.

        Args:
            mount_point: Mount point to update
            backend_config: New backend config (if provided)
            priority: New priority (if provided)
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
        with self.SessionLocal() as session:
            stmt = select(MountConfigModel).where(MountConfigModel.mount_point == mount_point)
            mount_model = session.execute(stmt).scalar_one_or_none()

            if not mount_model:
                return False

            # Update fields if provided
            if backend_config is not None:
                mount_model.backend_config = json.dumps(backend_config)
            if priority is not None:
                mount_model.priority = priority
            if readonly is not None:
                mount_model.readonly = int(
                    bool(readonly)
                )  # Convert to int for SQLite/PostgreSQL compatibility
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
            ...     print(f"Priority: {config['priority']}")
        """
        with self.SessionLocal() as session:
            stmt = select(MountConfigModel).where(MountConfigModel.mount_point == mount_point)
            mount_model = session.execute(stmt).scalar_one_or_none()

            if not mount_model:
                return None

            return {
                "mount_id": mount_model.mount_id,
                "mount_point": mount_model.mount_point,
                "backend_type": mount_model.backend_type,
                "backend_config": json.loads(mount_model.backend_config),
                "priority": mount_model.priority,
                "readonly": bool(mount_model.readonly),
                "owner_user_id": mount_model.owner_user_id,
                "tenant_id": mount_model.tenant_id,
                "description": mount_model.description,
                "created_at": mount_model.created_at,
                "updated_at": mount_model.updated_at,
            }

    def list_mounts(
        self, owner_user_id: str | None = None, tenant_id: str | None = None
    ) -> list[dict]:
        """List all persisted mount configurations.

        Args:
            owner_user_id: Filter by owner user ID (optional)
            tenant_id: Filter by tenant ID (optional)

        Returns:
            List of mount configuration dicts

        Example:
            >>> # List all mounts
            >>> all_mounts = manager.list_mounts()
            >>>
            >>> # List mounts for specific user
            >>> user_mounts = manager.list_mounts(owner_user_id="alice")
            >>>
            >>> # List mounts for specific tenant
            >>> tenant_mounts = manager.list_mounts(tenant_id="acme")
        """
        with self.SessionLocal() as session:
            stmt = select(MountConfigModel)

            # Apply filters
            if owner_user_id:
                stmt = stmt.where(MountConfigModel.owner_user_id == owner_user_id)
            if tenant_id:
                stmt = stmt.where(MountConfigModel.tenant_id == tenant_id)

            # Order by priority (desc) then mount_point
            stmt = stmt.order_by(MountConfigModel.priority.desc(), MountConfigModel.mount_point)

            results = session.execute(stmt).scalars().all()

            return [
                {
                    "mount_id": m.mount_id,
                    "mount_point": m.mount_point,
                    "backend_type": m.backend_type,
                    "backend_config": json.loads(m.backend_config),
                    "priority": m.priority,
                    "readonly": bool(m.readonly),
                    "owner_user_id": m.owner_user_id,
                    "tenant_id": m.tenant_id,
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
        with self.SessionLocal() as session:
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
            ...     router.add_mount(mc.mount_point, mc.backend, mc.priority, mc.readonly)
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
                    priority=mount_data["priority"],
                    readonly=mount_data["readonly"],
                )

                mount_configs.append(mount_config)

            except Exception as e:
                # Log error but continue with other mounts
                print(f"Warning: Failed to restore mount {mount_data['mount_point']}: {e}")
                continue

        return mount_configs
