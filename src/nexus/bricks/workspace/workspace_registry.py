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

    # === Shared Helpers (DRY — Issue #2987) ===

    def _extract_context(self, context: Any | None) -> tuple[str | None, str | None, str | None]:
        """Extract user_id, agent_id, zone_id from an OperationContext or dict.

        Returns:
            Tuple of (user_id, agent_id, zone_id).
        """
        if context is None:
            return None, None, None

        if isinstance(context, dict):
            user_id = context.get("user_id")
            agent_id = context.get("agent_id")
            zone_id = context.get("zone_id") or context.get("zone")
        else:
            user_id = getattr(context, "user_id", None)
            agent_id = getattr(context, "agent_id", None)
            zone_id = getattr(context, "zone_id", None)

        logger.debug(
            "Extracted context: user_id=%s, agent_id=%s, zone_id=%s",
            user_id,
            agent_id,
            zone_id,
        )
        return user_id, agent_id, zone_id

    def _validate_agent_ownership(self, agent_id: str | None, user_id: str | None) -> None:
        """Validate that agent belongs to user, if both are provided."""
        if agent_id and user_id:
            _agent_reg = getattr(self, "_agent_registry", None)
            if _agent_reg is not None and not _agent_reg.validate_ownership(agent_id, user_id):
                raise PermissionError(f"Agent {agent_id} not owned by {user_id}")

    def _compute_expiry(self, ttl: Any | None) -> datetime | None:
        """Compute expiration datetime from a timedelta TTL."""
        if ttl is None:
            return None
        result: datetime = datetime.now(UTC) + ttl
        return result

    def _auto_grant_ownership(self, path: str, user_id: str | None, zone_id: str | None) -> None:
        """Auto-grant direct_owner permission via ReBAC. Non-fatal."""
        logger.debug(
            "Auto-grant check: path=%s, user_id=%s, rebac_manager=%s",
            path,
            user_id,
            self.rebac_manager is not None,
        )
        if self.rebac_manager and user_id:
            try:
                self.rebac_manager.rebac_write(
                    subject=("user", user_id),
                    relation="direct_owner",
                    object=("file", path),
                    zone_id=zone_id,
                )
                logger.debug("Auto-grant succeeded: user:%s → owner → file:%s", user_id, path)
            except Exception as e:
                logger.error("Auto-grant failed for %s: %s", path, e, exc_info=True)

    def _load_from_db(self) -> None:
        """Load workspace configs from database."""
        from nexus.storage.models import PathRegistrationModel

        with self.metadata_session_factory() as session:
            from sqlalchemy import select

            workspaces = (
                session.execute(select(PathRegistrationModel).filter_by(type="workspace"))
                .scalars()
                .all()
            )
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
        context: Any | None = None,
        session_id: str | None = None,
        ttl: Any | None = None,
        overlay: bool = False,
        base_snapshot_hash: str | None = None,
    ) -> WorkspaceConfig:
        """Register a directory as a workspace."""
        if path in self._workspaces:
            raise ValueError(f"Workspace already registered: {path}")

        scope = "session" if session_id else "persistent"
        user_id, agent_id, zone_id = self._extract_context(context)
        self._validate_agent_ownership(agent_id, user_id)
        expires_at = self._compute_expiry(ttl)

        ws_metadata = metadata or {}
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

        self._workspaces[path] = config
        self._save_workspace_to_db(config, user_id, agent_id, scope, session_id, expires_at)
        self._auto_grant_ownership(path, user_id, zone_id)

        return config

    def update_workspace(
        self,
        path: str,
        name: str | None = None,
        description: str | None = None,
        metadata: dict | None = None,
    ) -> WorkspaceConfig:
        """Update an existing workspace configuration. DB is source of truth."""
        from nexus.storage.models import PathRegistrationModel

        with self.metadata_session_factory() as session:
            from sqlalchemy import select

            ws_model = (
                session.execute(select(PathRegistrationModel).filter_by(path=path))
                .scalars()
                .first()
            )
            if ws_model is None:
                raise ValueError(f"Workspace not found: {path}")

            if name is not None:
                ws_model.name = name
            if description is not None:
                ws_model.description = description
            if metadata is not None:
                ws_model.extra_metadata = json.dumps(metadata)
            session.commit()

            metadata_dict = json.loads(ws_model.extra_metadata) if ws_model.extra_metadata else {}
            config = WorkspaceConfig(
                path=ws_model.path,
                name=ws_model.name,
                description=ws_model.description or "",
                created_at=ws_model.created_at,
                created_by=ws_model.created_by,
                metadata=metadata_dict,
            )
            self._workspaces[path] = config

        return config

    def unregister_workspace(self, path: str) -> bool:
        """Unregister a workspace (does NOT delete files). DB is source of truth."""
        self._workspaces.pop(path, None)
        return self._delete_workspace_from_db(path)

    def get_workspace(self, path: str) -> WorkspaceConfig | None:
        """Get workspace config by exact path."""
        return self._workspaces.get(path)

    def find_workspace_for_path(self, path: str) -> WorkspaceConfig | None:
        """Find workspace containing this path."""
        if path in self._workspaces:
            return self._workspaces[path]
        for ws_path, config in self._workspaces.items():
            if path.startswith(ws_path + "/"):
                return config
        return None

    def list_workspaces(self) -> list[WorkspaceConfig]:
        """List all registered workspaces."""
        return list(self._workspaces.values())

    def refresh(self) -> None:
        """Explicitly reload all configs from database."""
        self._load_from_db()

    # === Database Persistence ===

    def _save_workspace_to_db(
        self,
        config: WorkspaceConfig,
        user_id: str | None = None,
        agent_id: str | None = None,
        scope: str = "persistent",
        session_id: str | None = None,
        expires_at: Any | None = None,
    ) -> None:
        """Persist workspace config to database."""
        from nexus.storage.models import PathRegistrationModel

        with self.metadata_session_factory() as session:
            model = PathRegistrationModel(
                path=config.path,
                type="workspace",
                name=config.name,
                description=config.description,
                created_at=config.created_at or datetime.now(UTC),
                created_by=config.created_by,
                user_id=user_id,
                agent_id=agent_id,
                scope=scope,
                session_id=session_id,
                expires_at=expires_at,
                extra_metadata=json.dumps(config.metadata) if config.metadata else None,
            )
            session.add(model)
            session.commit()

    def _delete_workspace_from_db(self, path: str) -> bool:
        """Delete workspace config from database. Returns True if found and deleted."""
        from nexus.storage.models import PathRegistrationModel

        with self.metadata_session_factory() as session:
            from sqlalchemy import select

            workspace = (
                session.execute(select(PathRegistrationModel).filter_by(path=path))
                .scalars()
                .first()
            )
            if workspace:
                session.delete(workspace)
                session.commit()
                return True
            return False
