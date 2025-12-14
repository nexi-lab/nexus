"""Workspace and Memory Registry.

This module provides explicit registration for workspace and memory directories.
Unlike the old path-based system, users explicitly declare which directories
should have workspace/memory capabilities.

Key Concepts:
- Workspaces: Directories that support snapshots, versioning, rollback
- Memories: Directories that support consolidation, search, versioning
- Registration: Explicit declaration (no path magic)
- Permissions: Handled separately via ReBAC
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from nexus.storage.metadata_store import SQLAlchemyMetadataStore


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


@dataclass
class MemoryConfig:
    """Configuration for a memory directory.

    A memory is a directory that supports:
    - Memory consolidation
    - Semantic search
    - Memory versioning

    Note: No owner or scope needed - permissions handled by ReBAC.

    Attributes:
        path: Absolute path to memory (e.g., "/my-memory")
        name: Optional friendly name
        description: Human-readable description
        created_at: When memory was registered
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
    """Registry for workspace and memory directories.

    Tracks which directories have special functionality:
    - Workspaces: snapshot/restore/versioning
    - Memories: consolidation/search

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
        metadata: SQLAlchemyMetadataStore,
        rebac_manager: Any | None = None,  # v0.5.0: For auto-granting ownership
    ):
        """Initialize workspace registry.

        Args:
            metadata: Metadata store for database persistence
            rebac_manager: ReBAC manager for auto-granting ownership (v0.5.0)
        """
        self.metadata = metadata
        self.rebac_manager = rebac_manager  # v0.5.0
        self._workspaces: dict[str, WorkspaceConfig] = {}
        self._memories: dict[str, MemoryConfig] = {}
        self._load_from_db()

    def _load_from_db(self) -> None:
        """Load workspace/memory configs from database."""
        from nexus.storage.models import MemoryConfigModel, WorkspaceConfigModel

        with self.metadata.SessionLocal() as session:
            # Load workspaces - merge with existing cache (don't clear it)
            workspaces = session.query(WorkspaceConfigModel).all()
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

            # Load memories - merge with existing cache (don't clear it)
            memories = session.query(MemoryConfigModel).all()
            for mem in memories:
                if mem.path is None:
                    continue  # Skip invalid entries
                metadata_dict = json.loads(mem.extra_metadata) if mem.extra_metadata else {}
                self._memories[mem.path] = MemoryConfig(
                    path=mem.path,
                    name=mem.name,
                    description=mem.description or "",
                    created_at=mem.created_at,
                    created_by=mem.created_by,
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
    ) -> WorkspaceConfig:
        """Register a directory as a workspace.

        Args:
            path: Absolute path to workspace directory
            name: Optional friendly name
            description: Human-readable description
            created_by: User/agent who created it (legacy, prefer context)
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
            >>> from nexus.core.permissions import OperationContext
            >>> from datetime import timedelta
            >>> ctx = OperationContext(user="alice", groups=[])
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

        # v0.5.0: Extract tenant_id from context (for auto-grant)
        tenant_id = None

        if context:
            # Handle both dict (from RPC) and OperationContext (direct calls)
            logger.warning(
                f"[CONTEXT-DEBUG] register_workspace: context type={type(context)}, context={context}"
            )
            if isinstance(context, dict):
                user_id = context.get("user_id") or context.get("user")
                agent_id = context.get("agent_id")
                tenant_id = context.get("tenant_id") or context.get("tenant")
                logger.warning(
                    f"[CONTEXT-DEBUG] Extracted from dict: user_id={user_id}, agent_id={agent_id}, tenant_id={tenant_id}"
                )
            else:
                user_id = getattr(context, "user_id", None) or getattr(context, "user", None)
                agent_id = getattr(context, "agent_id", None)
                tenant_id = getattr(context, "tenant_id", None)
                logger.warning(
                    f"[CONTEXT-DEBUG] Extracted from object: user_id={user_id}, agent_id={agent_id}, tenant_id={tenant_id}"
                )

            # Validate agent ownership
            if agent_id and user_id and hasattr(self, "entity_registry") and self.entity_registry:
                from nexus.core.agents import validate_agent_ownership

                if not validate_agent_ownership(agent_id, user_id, self.entity_registry):
                    raise PermissionError(f"Agent {agent_id} not owned by {user_id}")

        # Calculate expiry
        expires_at = None
        if ttl:
            from datetime import UTC

            expires_at = datetime.now(UTC) + ttl

        # Create config
        config = WorkspaceConfig(
            path=path,
            name=name,
            description=description,
            created_at=datetime.now(),
            created_by=created_by or user_id,
            metadata=metadata or {},
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
                    tenant_id=tenant_id,  # v0.5.0: Pass tenant_id from context
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

    # === Memory Management ===

    def register_memory(
        self,
        path: str,
        name: str | None = None,
        description: str = "",
        created_by: str | None = None,
        metadata: dict | None = None,
        context: Any | None = None,  # v0.5.0: OperationContext
        session_id: str | None = None,  # v0.5.0: If provided, memory is session-scoped
        ttl: Any | None = None,  # v0.5.0: timedelta for auto-expiry
    ) -> MemoryConfig:
        """Register a directory as a memory.

        Args:
            path: Absolute path to memory directory
            name: Optional friendly name
            description: Human-readable description
            created_by: User/agent who created it (legacy, prefer context)
            metadata: Additional metadata
            context: OperationContext with user_id and agent_id (v0.5.0)
            session_id: If provided, memory is session-scoped (temporary). If None, persistent. (v0.5.0)
            ttl: Time-to-live as timedelta (v0.5.0)

        Returns:
            MemoryConfig object

        Raises:
            ValueError: If path already registered
            PermissionError: If agent doesn't belong to user

        Examples:
            >>> # Traditional persistent memory
            >>> registry.register_memory(
            ...     "/my-memory",
            ...     name="personal-kb",
            ...     created_by="alice"
            ... )

            >>> # v0.5.0: Session-scoped memory (temporary)
            >>> from nexus.core.permissions import OperationContext
            >>> from datetime import timedelta
            >>> ctx = OperationContext(user="alice", groups=[])
            >>> registry.register_memory(
            ...     "/tmp/memory",
            ...     context=ctx,
            ...     session_id=session.session_id,  # session_id = session-scoped
            ...     ttl=timedelta(hours=8)
            ... )
        """
        if path in self._memories:
            raise ValueError(f"Memory already registered: {path}")

        # v0.5.0: Derive scope from session_id
        scope = "session" if session_id else "persistent"

        # v0.5.0: Extract identity from context
        user_id = None
        agent_id = None
        tenant_id = None  # v0.5.0: For auto-grant

        if context:
            # Handle both dict (from RPC) and OperationContext (direct calls)
            if isinstance(context, dict):
                user_id = context.get("user_id") or context.get("user")
                agent_id = context.get("agent_id")
                tenant_id = context.get("tenant_id") or context.get("tenant")
            else:
                user_id = getattr(context, "user_id", None) or getattr(context, "user", None)
                agent_id = getattr(context, "agent_id", None)
                tenant_id = getattr(context, "tenant_id", None)

            # Validate agent ownership
            if agent_id and user_id and hasattr(self, "entity_registry") and self.entity_registry:
                from nexus.core.agents import validate_agent_ownership

                if not validate_agent_ownership(agent_id, user_id, self.entity_registry):
                    raise PermissionError(f"Agent {agent_id} not owned by {user_id}")

        # Calculate expiry
        expires_at = None
        if ttl:
            from datetime import UTC

            expires_at = datetime.now(UTC) + ttl

        # Create config
        config = MemoryConfig(
            path=path,
            name=name,
            description=description,
            created_at=datetime.now(),
            created_by=created_by or user_id,
            metadata=metadata or {},
        )

        # Save to cache
        self._memories[path] = config

        # Persist to database (with v0.5.0 fields)
        self._save_memory_to_db(config, user_id, agent_id, scope, session_id, expires_at)

        # v0.5.0: Auto-grant ownership to registering user
        logger.warning(
            f"[AUTO-GRANT] workspace_registry: path={path}, user_id={user_id}, rebac_manager={self.rebac_manager is not None}"
        )
        if self.rebac_manager and user_id:
            try:
                logger.warning(f"[AUTO-GRANT] Creating tuple: user:{user_id} → owner → file:{path}")
                self.rebac_manager.rebac_write(
                    subject=("user", user_id),
                    relation="direct_owner",  # Use concrete relation, not computed union
                    object=("file", path),
                    tenant_id=tenant_id,  # v0.5.0: Pass tenant_id from context
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

    def unregister_memory(self, path: str) -> bool:
        """Unregister a memory (does NOT delete files).

        Args:
            path: Memory path

        Returns:
            True if unregistered, False if not found
        """
        if path not in self._memories:
            return False

        # Remove from cache
        del self._memories[path]

        # Remove from database
        self._delete_memory_from_db(path)

        return True

    def get_memory(self, path: str) -> MemoryConfig | None:
        """Get memory config by exact path.

        Args:
            path: Exact memory path

        Returns:
            MemoryConfig or None if not found
        """
        return self._memories.get(path)

    def find_memory_for_path(self, path: str) -> MemoryConfig | None:
        """Find memory containing this path.

        Args:
            path: Path to check

        Returns:
            MemoryConfig if found, None otherwise
        """
        # Check exact match first
        if path in self._memories:
            return self._memories[path]

        # Check if path is inside a memory
        for mem_path, config in self._memories.items():
            if path.startswith(mem_path + "/"):
                return config

        return None

    def list_memories(self) -> list[MemoryConfig]:
        """List all registered memories.

        Returns:
            List of MemoryConfig objects
        """
        return list(self._memories.values())

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

        with self.metadata.SessionLocal() as session:
            model = WorkspaceConfigModel(
                path=config.path,
                name=config.name,
                description=config.description,
                created_at=config.created_at or datetime.now(),
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

        with self.metadata.SessionLocal() as session:
            workspace = session.query(WorkspaceConfigModel).filter_by(path=path).first()
            if workspace:
                session.delete(workspace)
                session.commit()

    def _save_memory_to_db(
        self,
        config: MemoryConfig,
        user_id: str | None = None,  # v0.5.0
        agent_id: str | None = None,  # v0.5.0
        scope: str = "persistent",  # v0.5.0
        session_id: str | None = None,  # v0.5.0
        expires_at: Any | None = None,  # v0.5.0
    ) -> None:
        """Persist memory config to database."""
        from nexus.storage.models import MemoryConfigModel

        with self.metadata.SessionLocal() as session:
            model = MemoryConfigModel(
                path=config.path,
                name=config.name,
                description=config.description,
                created_at=config.created_at or datetime.now(),
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

    def _delete_memory_from_db(self, path: str) -> None:
        """Delete memory config from database."""
        from nexus.storage.models import MemoryConfigModel

        with self.metadata.SessionLocal() as session:
            memory = session.query(MemoryConfigModel).filter_by(path=path).first()
            if memory:
                session.delete(memory)
                session.commit()
