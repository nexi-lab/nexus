"""Memory brick service implementing MemoryProtocol.

Follows the pay/ brick pattern: zero cross-brick imports, constructor DI,
Protocol-based boundaries.

Related: Issue #2128, NEXUS-LEGO-ARCHITECTURE.md
"""

from __future__ import annotations

import builtins
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal


@dataclass
class RetentionPolicy:
    """Version retention configuration for automatic GC.

    Controls how many versions to keep and for how long before garbage collection.
    """

    keep_last_n: int = 10  # Always keep N most recent versions
    keep_versions_days: int = 90  # Keep versions newer than N days
    gc_interval_hours: int = 24  # Run GC every N hours
    enabled: bool = True  # Enable automatic GC


class MemoryBrick:
    """Memory management brick implementing MemoryProtocol.

    Zero imports from other bricks. All dependencies via constructor DI.
    Follows NEXUS-LEGO-ARCHITECTURE design principles.

    Example:
        >>> from nexus.bricks.memory import MemoryBrick, RetentionPolicy
        >>> brick = MemoryBrick(
        ...     record_store=ctx.record_store,
        ...     permission_enforcer=system.permissions,
        ...     temporal_utils=TemporalUtils(),
        ...     event_log=system.event_log,
        ... )
        >>> memory_id = await brick.store(content="test", scope="user")
    """

    def __init__(
        self,
        memory_router: Any,  # MemoryViewRouter - TODO: Protocol
        permission_enforcer: Any,  # MemoryPermissionEnforcer - TODO: Protocol
        backend: Any,  # Content storage backend (CAS)
        context: Any,  # OperationContext
        session_factory: Any,  # Callable[[], Session]
        event_log: Any | None = None,  # EventLogProtocol
        graph_store: Any | None = None,  # GraphStoreProtocol (optional)
        llm_provider: Any | None = None,  # Optional LLM provider for enrichment
        retention_policy: RetentionPolicy | None = None,
        zone_id: str | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
    ):
        """Initialize Memory brick with dependency injection.

        Args:
            memory_router: Memory view router for accessing memory records.
            permission_enforcer: ReBAC permission enforcer (TODO: Protocol).
            backend: Content storage backend (CAS).
            context: Operation context for permission checks.
            session_factory: Database session factory.
            event_log: Event logging service.
            graph_store: Optional graph store for entity relationships.
            llm_provider: Optional LLM provider for enrichment.
            retention_policy: Version retention configuration.
            zone_id: Current zone ID.
            user_id: Current user ID.
            agent_id: Current agent ID.

        TODO(#2XXX): Refactor to remove MemoryRouter dependency, use record_store directly.
        """
        self._memory_router = memory_router
        self._permissions = permission_enforcer
        self._backend = backend
        self._context = context
        self._session_factory = session_factory
        self._event_log = event_log
        self._graph_store = graph_store
        self._llm_provider = llm_provider
        self._retention_policy = retention_policy or RetentionPolicy()
        self._zone_id = zone_id
        self._user_id = user_id
        self._agent_id = agent_id

        # Internal components (lazy-loaded on first use)
        self._crud: Any = None
        self._query: Any = None
        self._lifecycle: Any = None
        self._versioning: Any = None

    def _ensure_components(self) -> None:
        """Lazy-load internal components on first use."""
        if self._crud is not None:
            return

        from nexus.bricks.memory.crud import MemoryCRUD
        from nexus.bricks.memory.lifecycle import MemoryLifecycle
        from nexus.bricks.memory.query import MemoryQuery
        from nexus.bricks.memory.versioning_ops import MemoryVersioning

        self._crud = MemoryCRUD(
            memory_router=self._memory_router,
            permission_enforcer=self._permissions,
            backend=self._backend,
            context=self._context,
            llm_provider=self._llm_provider,
            zone_id=self._zone_id,
            user_id=self._user_id,
            agent_id=self._agent_id,
        )
        self._query = MemoryQuery(
            memory_router=self._memory_router,
            permission_enforcer=self._permissions,
            backend=self._backend,
            context=self._context,
        )
        self._lifecycle = MemoryLifecycle(
            memory_router=self._memory_router,
            permission_enforcer=self._permissions,
            context=self._context,
        )
        self._versioning = MemoryVersioning(
            session_factory=self._session_factory,
            memory_router=self._memory_router,
            permission_enforcer=self._permissions,
            backend=self._backend,
            context=self._context,
            retention_policy=self._retention_policy,
        )

    # ── MemoryProtocol Implementation ──────────────────────────────────────

    def store(
        self,
        content: str | bytes | dict[str, Any],
        scope: str = "user",
        memory_type: str | None = None,
        importance: float | None = None,
        namespace: str | None = None,
        path_key: str | None = None,
        state: str = "active",
        _metadata: dict[str, Any] | None = None,
        context: Any | None = None,
        generate_embedding: bool = True,
        embedding_provider: Any = None,
        resolve_coreferences: bool = False,
        coreference_context: str | None = None,
        resolve_temporal: bool = False,
        temporal_reference_time: Any = None,
        extract_entities: bool = True,
        extract_temporal: bool = True,
        extract_relationships: bool = False,
        relationship_types: builtins.list[str] | None = None,
        store_to_graph: bool = False,
        valid_at: datetime | str | None = None,
        classify_stability: bool = True,
        detect_evolution: bool = False,
    ) -> str:
        """Store a new memory."""
        self._ensure_components()
        return self._crud.store(
            content=content,
            scope=scope,
            memory_type=memory_type,
            importance=importance,
            namespace=namespace,
            path_key=path_key,
            state=state,
            _metadata=_metadata,
            context=context,
            generate_embedding=generate_embedding,
            embedding_provider=embedding_provider,
            resolve_coreferences=resolve_coreferences,
            coreference_context=coreference_context,
            resolve_temporal=resolve_temporal,
            temporal_reference_time=temporal_reference_time,
            extract_entities=extract_entities,
            extract_temporal=extract_temporal,
            extract_relationships=extract_relationships,
            relationship_types=relationship_types,
            store_to_graph=store_to_graph,
            valid_at=valid_at,
            classify_stability=classify_stability,
            detect_evolution=detect_evolution,
        )

    def get(
        self,
        memory_id: str,
        track_access: bool = True,
        context: Any | None = None,
    ) -> dict[str, Any] | None:
        """Get a memory by ID."""
        self._ensure_components()
        return self._crud.get(memory_id=memory_id, track_access=track_access, context=context)

    def retrieve(
        self,
        namespace: str | None = None,
        path_key: str | None = None,
        path: str | None = None,
    ) -> dict[str, Any] | None:
        """Retrieve a memory by path."""
        self._ensure_components()
        return self._crud.retrieve(namespace=namespace, path_key=path_key, path=path)

    def delete(
        self,
        memory_id: str,
        context: Any | None = None,
    ) -> bool:
        """Delete a memory."""
        self._ensure_components()
        return self._crud.delete(memory_id=memory_id, context=context)

    def list(
        self,
        scope: str | None = None,
        memory_type: str | None = None,
        namespace: str | None = None,
        namespace_prefix: str | None = None,
        state: str | None = "active",
        after: str | datetime | None = None,
        before: str | datetime | None = None,
        during: str | None = None,
        limit: int | None = 100,
        context: Any | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """List memories with filters."""
        self._ensure_components()
        return self._crud.list(
            scope=scope,
            memory_type=memory_type,
            namespace=namespace,
            namespace_prefix=namespace_prefix,
            state=state,
            after=after,
            before=before,
            during=during,
            limit=limit,
            context=context,
        )

    def query(
        self,
        user_id: str | None = None,
        agent_id: str | None = None,
        zone_id: str | None = None,
        scope: str | None = None,
        memory_type: str | None = None,
        namespace: str | None = None,
        namespace_prefix: str | None = None,
        state: str | None = "active",
        after: str | datetime | None = None,
        before: str | datetime | None = None,
        during: str | None = None,
        entity_type: str | None = None,
        person: str | None = None,
        event_after: str | datetime | None = None,
        event_before: str | datetime | None = None,
        include_invalid: bool = False,
        include_superseded: bool = False,
        temporal_stability: str | None = None,
        as_of: str | datetime | None = None,
        as_of_event: str | datetime | None = None,
        as_of_system: str | datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
        context: Any | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """Query memories with advanced filters."""
        self._ensure_components()
        return self._query.query(
            user_id=user_id,
            agent_id=agent_id,
            zone_id=zone_id,
            scope=scope,
            memory_type=memory_type,
            namespace=namespace,
            namespace_prefix=namespace_prefix,
            state=state,
            after=after,
            before=before,
            during=during,
            entity_type=entity_type,
            person=person,
            event_after=event_after,
            event_before=event_before,
            include_invalid=include_invalid,
            include_superseded=include_superseded,
            temporal_stability=temporal_stability,
            as_of=as_of,
            as_of_event=as_of_event,
            as_of_system=as_of_system,
            limit=limit,
            offset=offset,
            context=context,
        )

    def search(
        self,
        query: str,
        scope: str | None = None,
        memory_type: str | None = None,
        limit: int = 10,
        search_mode: str = "hybrid",
        embedding_provider: Any = None,
        after: str | datetime | None = None,
        before: str | datetime | None = None,
        during: str | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """Search memories with semantic/keyword search."""
        self._ensure_components()
        return self._query.search(
            query=query,
            scope=scope,
            memory_type=memory_type,
            limit=limit,
            search_mode=search_mode,
            embedding_provider=embedding_provider,
            after=after,
            before=before,
            during=during,
        )

    # ── State lifecycle ────────────────────────────────────────────────────

    def approve(self, memory_id: str) -> bool:
        """Approve a memory (mark as active)."""
        self._ensure_components()
        return self._lifecycle.approve(memory_id)

    def deactivate(self, memory_id: str) -> bool:
        """Deactivate a memory."""
        self._ensure_components()
        return self._lifecycle.deactivate(memory_id)

    def approve_batch(self, memory_ids: builtins.list[str]) -> dict[str, Any]:
        """Approve multiple memories."""
        self._ensure_components()
        return self._lifecycle.approve_batch(memory_ids)

    def deactivate_batch(self, memory_ids: builtins.list[str]) -> dict[str, Any]:
        """Deactivate multiple memories."""
        self._ensure_components()
        return self._lifecycle.deactivate_batch(memory_ids)

    def delete_batch(self, memory_ids: builtins.list[str]) -> dict[str, Any]:
        """Delete multiple memories."""
        self._ensure_components()
        return self._lifecycle.delete_batch(memory_ids)

    def invalidate(
        self,
        memory_id: str,
        invalid_at: datetime | str | None = None,
    ) -> bool:
        """Invalidate a memory (mark with invalid_at timestamp)."""
        self._ensure_components()
        return self._lifecycle.invalidate(memory_id, invalid_at=invalid_at)

    def invalidate_batch(
        self,
        memory_ids: builtins.list[str],
        invalid_at: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Invalidate multiple memories."""
        self._ensure_components()
        return self._lifecycle.invalidate_batch(memory_ids, invalid_at=invalid_at)

    def revalidate(self, memory_id: str) -> bool:
        """Revalidate a memory (clear invalid_at)."""
        self._ensure_components()
        return self._lifecycle.revalidate(memory_id)

    # ── Versioning ─────────────────────────────────────────────────────────

    def get_history(self, memory_id: str) -> builtins.list[dict[str, Any]]:
        """Get version history for a memory."""
        self._ensure_components()
        return self._versioning.get_history(memory_id)

    def list_versions(self, memory_id: str) -> builtins.list[dict[str, Any]]:
        """List all versions of a memory."""
        self._ensure_components()
        return self._versioning.list_versions(memory_id)

    def get_version(
        self,
        memory_id: str,
        version: int,
        context: Any | None = None,
    ) -> dict[str, Any] | None:
        """Get a specific version of a memory."""
        self._ensure_components()
        return self._versioning.get_version(memory_id, version=version, context=context)

    def rollback(
        self,
        memory_id: str,
        version: int,
        context: Any | None = None,
    ) -> None:
        """Rollback a memory to a previous version."""
        self._ensure_components()
        return self._versioning.rollback(memory_id, version=version, context=context)

    def diff_versions(
        self,
        memory_id: str,
        v1: int,
        v2: int,
        mode: Literal["metadata", "content"] = "metadata",
        context: Any | None = None,
    ) -> dict[str, Any] | str:
        """Compare two versions of a memory."""
        self._ensure_components()
        return self._versioning.diff_versions(memory_id, v1=v1, v2=v2, mode=mode, context=context)

    def gc_old_versions(self, older_than_days: int = 365) -> int:
        """Garbage collect old versions."""
        self._ensure_components()
        return self._versioning.gc_old_versions(older_than_days=older_than_days)

    async def gc_old_versions_by_policy(self, zone_id: str | None = None) -> int:
        """Run GC based on retention policy (scheduled task).

        Returns:
            Number of versions deleted.
        """
        self._ensure_components()
        return await self._versioning.gc_old_versions_by_policy(zone_id=zone_id or self._zone_id)

    def resolve_to_current(self, memory_id: str) -> Any:
        """Resolve a memory ID to the current version."""
        self._ensure_components()
        return self._versioning.resolve_to_current(memory_id)

    # ── Maintenance ────────────────────────────────────────────────────────

    def apply_decay_batch(
        self,
        decay_factor: float = 0.95,
        min_importance: float = 0.1,
        batch_size: int = 1000,
    ) -> dict[str, Any]:
        """Apply importance decay to memories."""
        self._ensure_components()
        return self._lifecycle.apply_decay_batch(
            decay_factor=decay_factor,
            min_importance=min_importance,
            batch_size=batch_size,
        )
