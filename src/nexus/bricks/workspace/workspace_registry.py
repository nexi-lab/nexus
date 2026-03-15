"""Workspace Registry.

This module provides explicit registration for workspace directories.
Unlike the old path-based system, users explicitly declare which directories
should have workspace capabilities.

Key Concepts:
- Workspaces: Directories that support snapshots, versioning, rollback
- Registration: Explicit declaration (no path magic)
- Permissions: Handled separately via ReBAC
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class WorkspaceConfig:
    """Configuration for a workspace directory.

    A workspace is a directory that supports:
    - Snapshots (point-in-time captures)
    - Versioning (rollback)
    - Workspace logs

    Attributes:
        path: Absolute path to workspace (e.g., "/my-workspace")
        name: Optional friendly name
        description: Human-readable description
        created_at: When workspace was registered
        created_by: Who registered it (for audit)
        metadata: User-defined metadata dict
    """

    path: str
    name: str | None = None
    description: str = ""
    created_at: datetime | None = None
    created_by: str | None = None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Convert to dictionary for API responses."""
        return {
            "path": self.path,
            "name": self.name,
            "description": self.description,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "created_by": self.created_by,
            "metadata": self.metadata,
        }


class WorkspaceRegistry:
    """Registry for workspace directories.

    Tracks which directories have snapshot/restore/versioning functionality.

    Storage:
    - In-memory cache for fast lookups
    - Database persistence for durability

    Example:
        >>> registry = WorkspaceRegistry(metadata_store)
        >>> registry.register_workspace("/my-workspace", name="main")
        >>> config = registry.get_workspace("/my-workspace")
        >>> print(config.name)  # "main"
    """

    def __init__(
        self,
        metadata: Any,
        rebac_manager: Any | None = None,  # v0.5.0: For auto-granting ownership
        record_store: Any | None = None,
    ):
        """Initialize workspace registry.

        Args:
            metadata: Metadata store for database persistence
            rebac_manager: ReBAC manager for auto-granting ownership (v0.5.0)
            record_store: RecordStoreABC instance providing session_factory
        """
        self.metadata = metadata
        self.rebac_manager = rebac_manager  # v0.5.0
        if record_store is None:
            raise ValueError("record_store is required — use factory.py for DI wiring")
        self.metadata_session_factory = record_store.session_factory
        self._workspaces: dict[str, WorkspaceConfig] = {}
        self._load_from_db()

    def _load_from_db(self) -> None:
        """Load workspace configs from database."""
        from nexus.storage.models import WorkspaceConfigModel

        with self.metadata_session_factory() as session:
            from sqlalchemy import select

            workspaces = session.execute(select(WorkspaceConfigModel)).scalars().all()
            for ws in workspaces:
                metadata_dict = json.loads(ws.extra_metadata) if ws.extra_metadata else {}
                self._workspaces[ws.path] = WorkspaceConfig(
                    path=ws.path,
                    name=ws.name,
                    description=ws.description or "",
                    created_at=ws.created_at,
                    created_by=ws.created_by,
                    metadata=metadata_dict,
                )

    # === Workspace Management ===

    def register_workspace(
        self,
        path: str,
        name: str | None = None,
        description: str = "",
        created_by: str | None = None,
        metadata: dict | None = None,
        context: Any | None = None,  # v0.5.0: OperationContext
        session_id: str | None = None,  # v0.5.0: If provided, workspace is session-scoped
        ttl: Any | None = None,  # v0.5.0: timedelta for auto-expiry
        overlay: bool = False,  # Issue #1264: Enable overlay for this workspace
        base_snapshot_hash: str | None = None,  # Issue #1264: Base manifest CAS hash
    ) -> WorkspaceConfig:
        """Register a directory as a workspace.

        Args:
            path: Absolute path to workspace directory
            name: Optional friendly name
            description: Human-readable description
            created_by: User/agent who created it (prefer context)
            metadata: Additional metadata
            context: OperationContext with user_id and agent_id (v0.5.0)
            session_id: If provided, workspace is session-scoped (temporary). If None, persistent. (v0.5.0)
            ttl: Time-to-live as timedelta (v0.5.0)

        Returns:
            WorkspaceConfig object

        Raises:
            ValueError: If path already registered
            PermissionError: If agent doesn't belong to user

        Examples:
            >>> # Persistent workspace (traditional)
            >>> registry.register_workspace(
            ...     "/my-workspace",
            ...     name="main-workspace",
            ...     created_by="alice"
            ... )

            >>> # v0.5.0: Session-scoped workspace (temporary)
            >>> from nexus.contracts.types import OperationContext
            >>> from datetime import timedelta
            >>> ctx = OperationContext(user_id="alice", groups=[])
            >>> registry.register_workspace(
            ...     "/tmp/notebook",
            ...     context=ctx,
            ...     session_id=session.session_id,  # session_id = session-scoped
            ...     ttl=timedelta(hours=8)
            ... )
        """
        if path in self._workspaces:
            raise ValueError(f"Workspace already registered: {path}")

        # v0.5.0: Derive scope from session_id
        scope = "session" if session_id else "persistent"

        # v0.5.0: Extract identity from context if provided
        user_id = None
        agent_id = None

        # v0.5.0: Extract zone_id from context (for auto-grant)
        zone_id = None

        if context:
            # Handle both dict (from RPC) and OperationContext (direct calls)
            logger.warning(
                f"[CONTEXT-DEBUG] register_workspace: context type={type(context)}, context={context}"
            )
            if isinstance(context, dict):
                user_id = context.get("user_id") or context.get("user_id")
                agent_id = context.get("agent_id")
                zone_id = context.get("zone_id") or context.get("zone")
                logger.warning(
                    f"[CONTEXT-DEBUG] Extracted from dict: user_id={user_id}, agent_id={agent_id}, zone_id={zone_id}"
                )
            else:
                user_id = getattr(context, "user_id", None)
                agent_id = getattr(context, "agent_id", None)
                zone_id = getattr(context, "zone_id", None)
                logger.warning(
                    f"[CONTEXT-DEBUG] Extracted from object: user_id={user_id}, agent_id={agent_id}, zone_id={zone_id}"
                )

            # Validate agent ownership
            if agent_id and user_id:
                _agent_reg = getattr(self, "_agent_registry", None)
                if _agent_reg is not None and not _agent_reg.validate_ownership(agent_id, user_id):
                    raise PermissionError(f"Agent {agent_id} not owned by {user_id}")

        # Calculate expiry
        expires_at = None
        if ttl:
            expires_at = datetime.now(UTC) + ttl

        # Create config
        ws_metadata = metadata or {}

        # Issue #1264: Store overlay config in workspace metadata
        if overlay and base_snapshot_hash:
            ws_metadata["overlay_config"] = {
                "enabled": True,
                "base_manifest_hash": base_snapshot_hash,
                "agent_id": agent_id,
            }

        config = WorkspaceConfig(
            path=path,
            name=name,
            description=description,
            created_at=datetime.now(UTC),
            created_by=created_by or user_id,
            metadata=ws_metadata,
        )

        # Save to cache
        self._workspaces[path] = config

        # Persist to database (with v0.5.0 fields)
        self._save_workspace_to_db(config, user_id, agent_id, scope, session_id, expires_at)

        # v0.5.0: Auto-grant ownership to registering user
        # Workspaces are just directories, so we grant permission on the FILE object
        # The workspace manager will check FILE permissions (using the file namespace)
        logger.warning(
            f"[AUTO-GRANT] workspace_registry: path={path}, user_id={user_id}, rebac_manager={self.rebac_manager is not None}"
        )
        if self.rebac_manager and user_id:
            try:
                # Grant permission on FILE object (for both file operations and workspace operations)
                logger.warning(f"[AUTO-GRANT] Creating tuple: user:{user_id} → owner → file:{path}")
                self.rebac_manager.rebac_write(
                    subject=("user", user_id),
                    relation="direct_owner",  # Use concrete relation, not computed union
                    object=("file", path),
                    zone_id=zone_id,  # v0.5.0: Pass zone_id from context
                )
                logger.warning(f"[AUTO-GRANT] ✓ SUCCESS: user:{user_id} → owner → file:{path}")
            except Exception as e:
                # Don't fail registration if permission grant fails
                logger.error(f"[AUTO-GRANT] ✗ FAILED: {e}", exc_info=True)
        else:
            logger.warning(
                f"[AUTO-GRANT] SKIPPED: rebac={self.rebac_manager is not None}, user={user_id}"
            )

        return config

    def update_workspace(
        self,
        path: str,
        name: str | None = None,
        description: str | None = None,
        metadata: dict | None = None,
    ) -> WorkspaceConfig:
        """Update an existing workspace configuration.

        Args:
            path: Absolute path to workspace directory
            name: Optional new friendly name (pass None to keep existing)
            description: Optional new description (pass None to keep existing)
            metadata: Optional new metadata (pass None to keep existing)

        Returns:
            Updated WorkspaceConfig object

        Raises:
            ValueError: If workspace not found
        """
        # Check if workspace exists
        if path not in self._workspaces:
            raise ValueError(f"Workspace not found: {path}")

        # Get existing config
        existing_config = self._workspaces[path]

        # Update fields (only if provided)
        if name is not None:
            existing_config.name = name
        if description is not None:
            existing_config.description = description
        if metadata is not None:
            existing_config.metadata = metadata

        # Update in cache
        self._workspaces[path] = existing_config

        # Update in database
        from nexus.storage.models import WorkspaceConfigModel

        with self.metadata_session_factory() as session:
            from sqlalchemy import select

            ws_model = (
                session.execute(select(WorkspaceConfigModel).filter_by(path=path)).scalars().first()
            )
            if ws_model:
                if name is not None:
                    ws_model.name = name
                if description is not None:
                    ws_model.description = description
                if metadata is not None:
                    ws_model.extra_metadata = json.dumps(metadata)
                session.commit()

        return existing_config

    def unregister_workspace(self, path: str) -> bool:
        """Unregister a workspace (does NOT delete files).

        Args:
            path: Workspace path

        Returns:
            True if unregistered, False if not found
        """
        if path not in self._workspaces:
            return False

        # Remove from cache
        del self._workspaces[path]

        # Remove from database
        self._delete_workspace_from_db(path)

        return True

    def get_workspace(self, path: str) -> WorkspaceConfig | None:
        """Get workspace config by exact path.

        Args:
            path: Exact workspace path

        Returns:
            WorkspaceConfig or None if not found
        """
        return self._workspaces.get(path)

    def find_workspace_for_path(self, path: str) -> WorkspaceConfig | None:
        """Find workspace containing this path.

        Checks if path is inside a registered workspace.

        Args:
            path: Path to check (can be file inside workspace)

        Returns:
            WorkspaceConfig if found, None otherwise

        Example:
            >>> registry.register_workspace("/my-workspace")
            >>> config = registry.find_workspace_for_path("/my-workspace/subdir/file.txt")
            >>> print(config.path)  # "/my-workspace"
        """
        # Check exact match first
        if path in self._workspaces:
            return self._workspaces[path]

        # Check if path is inside a workspace
        for ws_path, config in self._workspaces.items():
            if path.startswith(ws_path + "/"):
                return config

        return None

    def list_workspaces(self) -> list[WorkspaceConfig]:
        """List all registered workspaces.

        Returns:
            List of WorkspaceConfig objects
        """
        # Reload from database to ensure we have the latest workspaces
        # This handles the case where workspaces are registered after the server starts
        self._load_from_db()
        return list(self._workspaces.values())

    # === Database Persistence ===

    def _save_workspace_to_db(
        self,
        config: WorkspaceConfig,
        user_id: str | None = None,  # v0.5.0
        agent_id: str | None = None,  # v0.5.0
        scope: str = "persistent",  # v0.5.0
        session_id: str | None = None,  # v0.5.0
        expires_at: Any | None = None,  # v0.5.0
    ) -> None:
        """Persist workspace config to database."""
        from nexus.storage.models import WorkspaceConfigModel

        with self.metadata_session_factory() as session:
            model = WorkspaceConfigModel(
                path=config.path,
                name=config.name,
                description=config.description,
                created_at=config.created_at or datetime.now(UTC),
                created_by=config.created_by,
                user_id=user_id,  # v0.5.0
                agent_id=agent_id,  # v0.5.0
                scope=scope,  # v0.5.0
                session_id=session_id,  # v0.5.0
                expires_at=expires_at,  # v0.5.0
                extra_metadata=json.dumps(config.metadata) if config.metadata else None,
            )
            session.add(model)
            session.commit()

    def _delete_workspace_from_db(self, path: str) -> None:
        """Delete workspace config from database."""
        from nexus.storage.models import WorkspaceConfigModel

        with self.metadata_session_factory() as session:
            from sqlalchemy import select

            workspace = (
                session.execute(select(WorkspaceConfigModel).filter_by(path=path)).scalars().first()
            )
            if workspace:
                session.delete(workspace)
                session.commit()
