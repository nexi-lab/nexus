"""Mount management operations for NexusFS.

This module contains mount management operations:
- add_mount: Add dynamic backend mount
- remove_mount: Remove backend mount
- list_mounts: List all active mounts
- get_mount: Get mount details
- save_mount: Persist mount to database
- load_mounts: Load persisted mounts from database
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nexus.backends.backend import Backend
from nexus.core.rpc_decorator import rpc_expose

if TYPE_CHECKING:
    from nexus.core.mount_manager import MountManager
    from nexus.core.router import PathRouter


class NexusFSMountsMixin:
    """Mixin providing mount management operations for NexusFS."""

    # Type hints for attributes that will be provided by NexusFS parent class
    if TYPE_CHECKING:
        router: PathRouter
        mount_manager: MountManager | None

    @rpc_expose(description="Add dynamic backend mount")
    def add_mount(
        self,
        mount_point: str,
        backend_type: str,
        backend_config: dict[str, Any],
        priority: int = 0,
        readonly: bool = False,
    ) -> str:
        """Add a dynamic backend mount to the filesystem.

        This adds a backend mount at runtime without requiring server restart.
        Useful for user-specific storage, temporary backends, or multi-tenant scenarios.

        Args:
            mount_point: Virtual path where backend is mounted (e.g., "/personal/alice")
            backend_type: Backend type - "local", "gcs", "google_drive", etc.
            backend_config: Backend-specific configuration dict
            priority: Mount priority - higher values take precedence (default: 0)
            readonly: Whether mount is read-only (default: False)

        Returns:
            Mount ID (unique identifier for this mount)

        Raises:
            ValueError: If mount_point already exists or configuration is invalid
            RuntimeError: If backend type is not supported

        Examples:
            >>> # Add personal GCS mount
            >>> mount_id = nx.add_mount(
            ...     mount_point="/personal/alice",
            ...     backend_type="gcs",
            ...     backend_config={
            ...         "bucket": "alice-personal-bucket",
            ...         "project_id": "my-project"
            ...     },
            ...     priority=10
            ... )

            >>> # Add local shared mount
            >>> mount_id = nx.add_mount(
            ...     mount_point="/shared/team",
            ...     backend_type="local",
            ...     backend_config={"data_dir": "/mnt/shared"},
            ...     readonly=True
            ... )
        """
        # Import backend classes dynamically
        backend: Backend
        if backend_type == "local":
            from nexus.backends.local import LocalBackend

            backend = LocalBackend(root_path=backend_config["data_dir"])
        elif backend_type == "gcs":
            from nexus.backends.gcs import GCSBackend

            backend = GCSBackend(
                bucket_name=backend_config["bucket"],
                project_id=backend_config.get("project_id"),
                credentials_path=backend_config.get("credentials_path"),
            )
        else:
            raise RuntimeError(f"Unsupported backend type: {backend_type}")

        # Add mount to router
        self.router.add_mount(
            mount_point=mount_point, backend=backend, priority=priority, readonly=readonly
        )
        return mount_point  # Return mount_point as the mount ID

    @rpc_expose(description="Remove backend mount")
    def remove_mount(self, mount_point: str) -> bool:
        """Remove a backend mount from the filesystem.

        Args:
            mount_point: Virtual path of mount to remove (e.g., "/personal/alice")

        Returns:
            True if mount was removed, False if mount not found

        Examples:
            >>> # Remove user's personal mount
            >>> if nx.remove_mount("/personal/alice"):
            ...     print("Mount removed successfully")
        """
        return self.router.remove_mount(mount_point)

    @rpc_expose(description="List all backend mounts")
    def list_mounts(self) -> list[dict[str, Any]]:
        """List all active backend mounts.

        Returns:
            List of mount info dictionaries, each containing:
                - mount_point: Virtual path (str)
                - priority: Mount priority (int)
                - readonly: Read-only flag (bool)
                - backend_type: Backend type name (str)

        Examples:
            >>> # List all mounts
            >>> for mount in nx.list_mounts():
            ...     print(f"{mount['mount_point']} (priority={mount['priority']})")
        """
        mounts = []
        for mount_info in self.router.list_mounts():
            mounts.append(
                {
                    "mount_point": mount_info.mount_point,
                    "priority": mount_info.priority,
                    "readonly": mount_info.readonly,
                    "backend_type": type(mount_info.backend).__name__,
                }
            )
        return mounts

    @rpc_expose(description="Get mount details")
    def get_mount(self, mount_point: str) -> dict[str, Any] | None:
        """Get details about a specific mount.

        Args:
            mount_point: Virtual path of mount (e.g., "/personal/alice")

        Returns:
            Mount info dict if found, None otherwise. Dict contains:
                - mount_point: Virtual path (str)
                - priority: Mount priority (int)
                - readonly: Read-only flag (bool)
                - backend_type: Backend type name (str)

        Examples:
            >>> mount = nx.get_mount("/personal/alice")
            >>> if mount:
            ...     print(f"Priority: {mount['priority']}")
        """
        mount_info = self.router.get_mount(mount_point)
        if mount_info:
            return {
                "mount_point": mount_info.mount_point,
                "priority": mount_info.priority,
                "readonly": mount_info.readonly,
                "backend_type": type(mount_info.backend).__name__,
            }
        return None

    @rpc_expose(description="Check if mount exists")
    def has_mount(self, mount_point: str) -> bool:
        """Check if a mount exists at the given path.

        Args:
            mount_point: Virtual path to check (e.g., "/personal/alice")

        Returns:
            True if mount exists, False otherwise

        Examples:
            >>> if nx.has_mount("/personal/alice"):
            ...     print("Alice's mount is active")
        """
        return self.router.has_mount(mount_point)

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
    ) -> str:
        """Save a mount configuration to the database for persistence.

        This allows mounts to survive server restarts. The mount must still be
        activated using add_mount() - this only stores the configuration.

        Args:
            mount_point: Virtual path where backend is mounted
            backend_type: Backend type - "local", "gcs", etc.
            backend_config: Backend-specific configuration dict
            priority: Mount priority (default: 0)
            readonly: Whether mount is read-only (default: False)
            owner_user_id: User who owns this mount (optional)
            tenant_id: Tenant ID for multi-tenant isolation (optional)
            description: Human-readable description (optional)

        Returns:
            Mount ID (UUID string)

        Raises:
            ValueError: If mount already exists at mount_point
            RuntimeError: If mount manager is not available

        Examples:
            >>> # Save personal Google Drive mount configuration
            >>> mount_id = nx.save_mount(
            ...     mount_point="/personal/alice",
            ...     backend_type="google_drive",
            ...     backend_config={"access_token": "ya29.xxx"},
            ...     owner_user_id="google:alice123",
            ...     tenant_id="acme",
            ...     description="Alice's personal Google Drive"
            ... )
        """
        if not hasattr(self, "mount_manager") or self.mount_manager is None:
            raise RuntimeError(
                "Mount manager not available. Ensure NexusFS is initialized with a database."
            )

        return self.mount_manager.save_mount(
            mount_point=mount_point,
            backend_type=backend_type,
            backend_config=backend_config,
            priority=priority,
            readonly=readonly,
            owner_user_id=owner_user_id,
            tenant_id=tenant_id,
            description=description,
        )

    @rpc_expose(description="List saved mount configurations")
    def list_saved_mounts(
        self, owner_user_id: str | None = None, tenant_id: str | None = None
    ) -> list[dict[str, Any]]:
        """List mount configurations saved in the database.

        Args:
            owner_user_id: Filter by owner user ID (optional)
            tenant_id: Filter by tenant ID (optional)

        Returns:
            List of saved mount configurations

        Raises:
            RuntimeError: If mount manager is not available

        Examples:
            >>> # List all saved mounts
            >>> mounts = nx.list_saved_mounts()

            >>> # List mounts for specific user
            >>> alice_mounts = nx.list_saved_mounts(owner_user_id="google:alice123")
        """
        if not hasattr(self, "mount_manager") or self.mount_manager is None:
            raise RuntimeError(
                "Mount manager not available. Ensure NexusFS is initialized with a database."
            )

        return self.mount_manager.list_mounts(owner_user_id=owner_user_id, tenant_id=tenant_id)

    @rpc_expose(description="Load and activate saved mount")
    def load_mount(self, mount_point: str) -> str:
        """Load a saved mount configuration and activate it.

        This retrieves the mount configuration from the database and activates it
        by calling add_mount() internally.

        Args:
            mount_point: Virtual path of saved mount to load

        Returns:
            Mount ID if successfully loaded and activated

        Raises:
            ValueError: If mount not found in database
            RuntimeError: If mount manager is not available

        Examples:
            >>> # Load Alice's saved mount
            >>> nx.load_mount("/personal/alice")
        """
        if not hasattr(self, "mount_manager") or self.mount_manager is None:
            raise RuntimeError(
                "Mount manager not available. Ensure NexusFS is initialized with a database."
            )

        # Get mount config from database
        mount_config = self.mount_manager.get_mount(mount_point)
        if not mount_config:
            raise ValueError(f"Mount not found in database: {mount_point}")

        # Parse backend config from JSON (if it's a string)
        import json

        backend_config = mount_config["backend_config"]
        if isinstance(backend_config, str):
            backend_config = json.loads(backend_config)

        # Activate the mount
        return self.add_mount(
            mount_point=mount_config["mount_point"],
            backend_type=mount_config["backend_type"],
            backend_config=backend_config,
            priority=mount_config["priority"],
            readonly=bool(mount_config["readonly"]),
        )

    @rpc_expose(description="Delete saved mount configuration")
    def delete_saved_mount(self, mount_point: str) -> bool:
        """Delete a saved mount configuration from the database.

        Note: This does NOT deactivate the mount if it's currently active.
        Use remove_mount() to deactivate an active mount.

        Args:
            mount_point: Virtual path of mount to delete

        Returns:
            True if deleted, False if not found

        Raises:
            RuntimeError: If mount manager is not available

        Examples:
            >>> # Remove from database
            >>> nx.delete_saved_mount("/personal/alice")
            >>> # Also deactivate if currently mounted
            >>> nx.remove_mount("/personal/alice")
        """
        if not hasattr(self, "mount_manager") or self.mount_manager is None:
            raise RuntimeError(
                "Mount manager not available. Ensure NexusFS is initialized with a database."
            )

        return self.mount_manager.remove_mount(mount_point)
