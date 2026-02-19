"""Query operations for Memory brick (query, search, semantic/keyword search).

Migrated from memory_api.py Memory class methods.
Part of Issue #2128 Memory brick extraction.

Related: #406 (embedding), #1023 (temporal), #1025 (entities), #1028 (events),
        #1183 (temporal validity), #1185 (bi-temporal), #1498 (refactoring)
"""

from __future__ import annotations

import builtins
import json
import logging
import math
from datetime import datetime
from typing import Any

# TODO(#2XXX): Replace with Protocol imports when dependencies are extracted
from nexus.core.permissions import OperationContext, Permission
from nexus.core.temporal import parse_datetime, validate_temporal_params
from nexus.rebac.memory_permission_enforcer import MemoryPermissionEnforcer
from nexus.services.memory.memory_router import MemoryViewRouter

logger = logging.getLogger(__name__)

# Importance decay configuration (reused from crud.py)
DEFAULT_DECAY_FACTOR = 0.95
DEFAULT_MIN_IMPORTANCE = 0.1


def get_effective_importance(
    importance_original: float | None,
    importance_current: float | None,
    last_accessed_at: datetime | None,
    created_at: datetime | None,
    decay_factor: float = DEFAULT_DECAY_FACTOR,
    min_importance: float = DEFAULT_MIN_IMPORTANCE,
) -> float:
    """Calculate current importance with time-based decay."""
    from datetime import UTC

    original = importance_original or importance_current or 0.5
    now = datetime.now(UTC)

    if last_accessed_at:
        if last_accessed_at.tzinfo is None:
            last_accessed_at = last_accessed_at.replace(tzinfo=UTC)
        days_since = (now - last_accessed_at).days
    elif created_at:
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        days_since = (now - created_at).days
    else:
        days_since = 0

    decayed = original * (decay_factor ** max(0, days_since))
    return max(min_importance, decayed)


class MemoryQuery:
    """Query and search operations for memories.

    Handles complex queries with temporal filters, semantic search, and keyword search.

    Note: Temporarily depends on MemoryViewRouter and MemoryPermissionEnforcer.
    These will be replaced with Protocol-based dependencies when those components
    are extracted as bricks (Q2 2026).
    """

    def __init__(
        self,
        memory_router: MemoryViewRouter,
        permission_enforcer: MemoryPermissionEnforcer,
        backend: Any,
        context: OperationContext,
        llm_provider: Any | None = None,
        zone_id: str | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
    ):
        """Initialize query operations.

        Args:
            memory_router: Memory view router for accessing memory records.
            permission_enforcer: ReBAC permission enforcer.
            backend: Content storage backend (CAS).
            context: Operation context for permission checks.
            llm_provider: Optional LLM provider for embeddings.
            zone_id: Current zone ID.
            user_id: Current user ID.
            agent_id: Current agent ID.

        TODO(#2XXX): Replace MemoryViewRouter with MemoryRouterProtocol.
        TODO(#2XXX): Replace MemoryPermissionEnforcer with PermissionEnforcerProtocol.
        """
        self._memory_router = memory_router
        self._permission_enforcer = permission_enforcer
        self._backend = backend
        self._context = context
        self._llm_provider = llm_provider
        self._zone_id = zone_id
        self._user_id = user_id
        self._agent_id = agent_id

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
        context: OperationContext | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """Query memories by relationships and metadata.

        Args:
            user_id: Filter by user ID (defaults to current user).
            agent_id: Filter by agent ID.
            zone_id: Filter by zone ID (defaults to current zone).
            scope: Filter by scope.
            memory_type: Filter by memory type.
            namespace: Filter by exact namespace match.
            namespace_prefix: Filter by namespace prefix (hierarchical).
            state: Filter by state ('inactive', 'active', 'all'). Defaults to 'active'.
            after: Return memories created after this time (ISO-8601 or datetime).
            before: Return memories created before this time (ISO-8601 or datetime).
            during: Return memories during this period (partial date: "2025", "2025-01").
            entity_type: Filter by entity type (e.g., "PERSON", "ORG", "DATE").
            person: Filter by person name reference.
            event_after: Filter by event earliest_date >= value.
            event_before: Filter by event latest_date <= value.
            include_invalid: Include invalidated memories.
            include_superseded: Include superseded memories.
            temporal_stability: Filter by temporal stability.
            as_of: Point-in-time query (deprecated, use as_of_event).
            as_of_event: What was TRUE at time X? Filters by valid_at/invalid_at.
            as_of_system: What did SYSTEM KNOW at time X? Filters by created_at.
            limit: Maximum number of results.
            offset: Number of results to skip (for pagination).
            context: Optional operation context to override identity.

        Returns:
            List of memory dictionaries with metadata.

        Examples:
            >>> memories = query(scope="user", memory_type="preference")
            >>> memories = query(during="2025-01")  # Temporal query
            >>> memories = query(person="John Smith")  # Entity query
        """
        # Use context identity if provided, otherwise fall back to instance identity
        if user_id is None:
            user_id = context.user_id if context else self._user_id
        if zone_id is None:
            zone_id = context.zone_id if context else self._zone_id

        # Validate and normalize temporal parameters
        after_dt, before_dt = validate_temporal_params(after, before, during)

        # Parse event date parameters if strings
        event_after_dt = None
        event_before_dt = None
        if event_after:
            if isinstance(event_after, str):
                event_after_dt = datetime.fromisoformat(event_after.replace("Z", "+00:00"))
            else:
                event_after_dt = event_after
        if event_before:
            if isinstance(event_before, str):
                event_before_dt = datetime.fromisoformat(event_before.replace("Z", "+00:00"))
            else:
                event_before_dt = event_before

        # Parse as_of_event and as_of_system for bi-temporal queries
        effective_as_of_event = as_of_event or as_of
        valid_at_point = (
            (
                parse_datetime(effective_as_of_event)
                if isinstance(effective_as_of_event, str)
                else effective_as_of_event
            )
            if effective_as_of_event is not None
            else None
        )
        system_at_point = (
            (parse_datetime(as_of_system) if isinstance(as_of_system, str) else as_of_system)
            if as_of_system is not None
            else None
        )

        # Query memories
        memories = self._memory_router.query_memories(
            zone_id=zone_id,
            user_id=user_id,
            agent_id=agent_id,
            scope=scope,
            memory_type=memory_type,
            namespace=namespace,
            namespace_prefix=namespace_prefix,
            state=state,
            after=after_dt,
            before=before_dt,
            entity_type=entity_type,
            person=person,
            event_after=event_after_dt,
            event_before=event_before_dt,
            include_invalid=include_invalid,
            include_superseded=include_superseded,
            temporal_stability=temporal_stability,
            valid_at_point=valid_at_point,
            system_at_point=system_at_point,
            limit=limit,
        )

        # Filter by permissions first (before fetching content)
        check_context = context or self._context
        accessible_memories = []
        for memory in memories:
            if self._permission_enforcer.check_memory(memory, Permission.READ, check_context):
                accessible_memories.append(memory)

        # Apply offset/limit AFTER permission filtering for correct pagination
        if offset:
            accessible_memories = accessible_memories[offset:]
        if limit:
            accessible_memories = accessible_memories[:limit]

        # For as_of_system queries, resolve historical content hashes
        historical_content_hashes: dict[str, str] = {}
        if system_at_point is not None:
            from sqlalchemy import select

            from nexus.storage.models import VersionHistoryModel

            system_at_naive = (
                system_at_point.replace(tzinfo=None) if system_at_point.tzinfo else system_at_point
            )

            session = self._memory_router._session
            for memory in accessible_memories:
                updated_at_naive = (
                    memory.updated_at.replace(tzinfo=None)
                    if memory.updated_at and memory.updated_at.tzinfo
                    else memory.updated_at
                )

                if updated_at_naive and updated_at_naive > system_at_naive:
                    stmt = (
                        select(VersionHistoryModel)
                        .where(
                            VersionHistoryModel.resource_type == "memory",
                            VersionHistoryModel.resource_id == memory.memory_id,
                            VersionHistoryModel.created_at <= system_at_point,
                        )
                        .order_by(VersionHistoryModel.version_number.desc())
                        .limit(1)
                    )
                    version = session.execute(stmt).scalar_one_or_none()
                    if version:
                        historical_content_hashes[memory.memory_id] = version.content_hash

        # Batch read all content hashes
        content_hashes = [
            historical_content_hashes.get(memory.memory_id, memory.content_hash)
            for memory in accessible_memories
        ]
        content_map = self._backend.batch_read_content(content_hashes)

        # Build results with enriched content using Pydantic models
        from nexus.bricks.memory.response_models import MemoryQueryResponse

        results = []
        for memory in accessible_memories:
            effective_content_hash = historical_content_hashes.get(
                memory.memory_id, memory.content_hash
            )
            content_bytes = content_map.get(effective_content_hash)

            if content_bytes is not None:
                try:
                    content = content_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    content = content_bytes.hex()
            else:
                content = f"<content not available: {memory.content_hash}>"

            # Calculate effective importance with decay
            effective_importance = get_effective_importance(
                importance_original=memory.importance_original,
                importance_current=memory.importance,
                last_accessed_at=memory.last_accessed_at,
                created_at=memory.created_at,
            )

            results.append(
                MemoryQueryResponse.from_memory_model(
                    memory,
                    content=content,
                    importance_effective=effective_importance,
                    content_hash_override=effective_content_hash,
                ).model_dump()
            )

        return results

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
        """Semantic search over memories.

        Args:
            query: Search query text.
            scope: Filter by scope.
            memory_type: Filter by memory type.
            limit: Maximum number of results.
            search_mode: Search mode - "semantic", "keyword", or "hybrid".
            embedding_provider: Optional embedding provider.
            after: Return memories created after this time.
            before: Return memories created before this time.
            during: Return memories during this period (partial date).

        Returns:
            List of memory dictionaries with relevance scores.

        Examples:
            >>> results = search("Python programming preferences")
            >>> results = search("project updates", during="2025-01")
        """
        from nexus.core.sync_bridge import run_sync

        # Validate and normalize temporal parameters
        after_dt, before_dt = validate_temporal_params(after, before, during)

        # Fallback to keyword if no embeddings available
        if (
            search_mode in ("semantic", "hybrid")
            and embedding_provider is None
            and self._llm_provider is None
        ):
            search_mode = "keyword"

        if search_mode == "keyword":
            return self._keyword_search(query, scope, memory_type, limit, after_dt, before_dt)

        # For semantic/hybrid search, need embeddings
        if embedding_provider is None:
            try:
                from nexus.search.embeddings import create_embedding_provider

                try:
                    embedding_provider = create_embedding_provider(provider="openrouter")
                except Exception as e:
                    logger.debug(
                        "Failed to create embedding provider, falling back to keyword search: %s", e
                    )
                    return self._keyword_search(query, scope, memory_type, limit, after_dt, before_dt)
            except ImportError:
                return self._keyword_search(query, scope, memory_type, limit, after_dt, before_dt)

        # Generate query embedding
        query_embedding = run_sync(embedding_provider.embed_text(query))

        # Query memories from database
        from sqlalchemy import select

        from nexus.storage.models import MemoryModel

        session = self._memory_router._session
        stmt = select(MemoryModel).where(MemoryModel.embedding.isnot(None))

        if scope:
            stmt = stmt.where(MemoryModel.scope == scope)
        if memory_type:
            stmt = stmt.where(MemoryModel.memory_type == memory_type)

        # Filter by zone/user/agent
        if self._zone_id:
            stmt = stmt.where(MemoryModel.zone_id == self._zone_id)
        if self._user_id:
            stmt = stmt.where(MemoryModel.user_id == self._user_id)
        if self._agent_id:
            stmt = stmt.where(MemoryModel.agent_id == self._agent_id)

        # Temporal filtering
        if after_dt:
            stmt = stmt.where(MemoryModel.created_at >= after_dt)
        if before_dt:
            stmt = stmt.where(MemoryModel.created_at <= before_dt)

        result = session.execute(stmt)
        memories_with_embeddings = result.scalars().all()

        # Compute similarity scores
        scored_memories = []
        for memory in memories_with_embeddings:
            if not self._permission_enforcer.check_memory(memory, Permission.READ, self._context):
                continue

            if not memory.embedding:
                continue

            try:
                memory_embedding = json.loads(memory.embedding)
            except (json.JSONDecodeError, TypeError):
                continue

            # Compute cosine similarity
            similarity = self._cosine_similarity(query_embedding, memory_embedding)

            # For hybrid mode, also compute keyword score
            keyword_score = 0.0
            if search_mode == "hybrid":
                try:
                    content_bytes = self._backend.read_content(
                        memory.content_hash, context=self._context
                    ).unwrap()
                    content = content_bytes.decode("utf-8")
                    keyword_score = self._compute_keyword_score(query, content)
                except Exception as e:
                    logger.debug("Failed to read content for keyword scoring: %s", e)

            # Combined score for hybrid mode
            if search_mode == "hybrid":
                score = 0.7 * similarity + 0.3 * keyword_score  # 70% semantic, 30% keyword
            else:
                score = similarity

            scored_memories.append((memory, score, similarity, keyword_score))

        # Sort by score
        scored_memories.sort(key=lambda x: x[1], reverse=True)
        scored_memories = scored_memories[:limit]

        # Build result list with content
        from nexus.bricks.memory.response_models import MemorySearchResponse

        results = []
        for memory, score, semantic_score, keyword_score in scored_memories:
            try:
                content_bytes = self._backend.read_content(
                    memory.content_hash, context=self._context
                ).unwrap()
                try:
                    content = content_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    content = content_bytes.hex()
            except Exception:
                content = f"<content not available: {memory.content_hash}>"

            results.append(
                MemorySearchResponse.from_memory_model(
                    memory,
                    content=content,
                    score=score,
                    semantic_score=semantic_score if search_mode == "hybrid" else None,
                    keyword_score=keyword_score if search_mode == "hybrid" else None,
                ).model_dump()
            )

        return results

    def _keyword_search(
        self,
        query: str,
        scope: str | None,
        memory_type: str | None,
        limit: int,
        after_dt: datetime | None = None,
        before_dt: datetime | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """Keyword-only search fallback."""
        # Get all memories matching filters
        memories = self.query(
            scope=scope,
            memory_type=memory_type,
            after=after_dt,
            before=before_dt,
            limit=limit * 3,  # Get more to filter
        )

        # Simple text matching
        scored_results = []
        for memory in memories:
            content = memory.get("content", "")
            if not content or isinstance(content, bytes):
                continue

            score = self._compute_keyword_score(query, content)
            if score > 0:
                memory["score"] = score
                scored_results.append(memory)

        # Sort by score and limit
        scored_results.sort(key=lambda x: x["score"], reverse=True)
        return scored_results[:limit]

    def _compute_keyword_score(self, query: str, content: str) -> float:
        """Compute keyword match score."""
        query_lower = query.lower()
        content_lower = content.lower()

        # Simple relevance scoring
        if query_lower in content_lower:
            return 1.0
        else:
            # Count word matches
            query_words = query_lower.split()
            matches = sum(1 for word in query_words if word in content_lower)
            return matches / len(query_words) if query_words else 0.0

    def _cosine_similarity(self, vec1: builtins.list[float], vec2: builtins.list[float]) -> float:
        """Compute cosine similarity between two vectors."""
        # Compute dot product
        dot_product = sum(a * b for a, b in zip(vec1, vec2, strict=False))

        # Compute magnitudes
        mag1 = math.sqrt(sum(a * a for a in vec1))
        mag2 = math.sqrt(sum(b * b for b in vec2))

        # Avoid division by zero
        if mag1 == 0 or mag2 == 0:
            return 0.0

        return dot_product / (mag1 * mag2)
