"""Memory API for AI Agent Memory Management (v0.4.0).

High-level API for storing, querying, and searching agent memories
with identity-based relationships and semantic search.

Includes temporal query operators (Issue #1023) for time-based filtering
inspired by SimpleMem (arXiv:2601.02553).
"""

from __future__ import annotations

import builtins
import contextlib
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy.orm import Session

from nexus.core.entity_registry import EntityRegistry
from nexus.core.memory_permission_enforcer import MemoryPermissionEnforcer
from nexus.core.memory_router import MemoryViewRouter
from nexus.core.permissions import OperationContext, Permission
from nexus.core.temporal import parse_datetime, validate_temporal_params

# Importance decay configuration (Issue #1030)
DEFAULT_DECAY_FACTOR = 0.95  # 5% decay per day
DEFAULT_MIN_IMPORTANCE = 0.1  # Minimum importance floor


def get_effective_importance(
    importance_original: float | None,
    importance_current: float | None,
    last_accessed_at: datetime | None,
    created_at: datetime | None,
    decay_factor: float = DEFAULT_DECAY_FACTOR,
    min_importance: float = DEFAULT_MIN_IMPORTANCE,
) -> float:
    """Calculate current importance with time-based decay.

    Formula: importance_decayed = importance_original * decay_factor^(days_since_access)

    Args:
        importance_original: Original importance score (preserved)
        importance_current: Current importance (may already be decayed)
        last_accessed_at: Last time memory was accessed
        created_at: Memory creation time (fallback if never accessed)
        decay_factor: Decay multiplier per day (default: 0.95 = 5% decay/day)
        min_importance: Minimum importance floor (default: 0.1)

    Returns:
        Effective importance score after decay (clamped to min_importance)

    Example:
        >>> # Memory with original importance 0.8, not accessed for 10 days
        >>> effective = get_effective_importance(0.8, 0.8, None, created_10_days_ago)
        >>> # effective ≈ 0.8 * 0.95^10 ≈ 0.48
    """

    # Use original importance if available, otherwise current, otherwise default 0.5
    original = importance_original or importance_current or 0.5

    # Calculate days since last access
    now = datetime.now(UTC)
    if last_accessed_at:
        # Ensure timezone aware comparison
        if last_accessed_at.tzinfo is None:
            last_accessed_at = last_accessed_at.replace(tzinfo=UTC)
        days_since = (now - last_accessed_at).days
    elif created_at:
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        days_since = (now - created_at).days
    else:
        days_since = 0

    # Apply exponential decay
    decayed = original * (decay_factor ** max(0, days_since))

    # Clamp to minimum importance
    return max(min_importance, decayed)


class Memory:
    """High-level Memory API for AI agents.

    Provides simple methods for storing, querying, and searching memories
    with automatic permission checks and identity management.
    """

    def __init__(
        self,
        session: Session,
        backend: Any,
        zone_id: str | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
        entity_registry: EntityRegistry | None = None,
        llm_provider: Any = None,
    ):
        """Initialize Memory API.

        Args:
            session: Database session.
            backend: Storage backend for content.
            zone_id: Current zone ID.
            user_id: Current user ID.
            agent_id: Current agent ID.
            entity_registry: Entity registry instance.
            llm_provider: Optional LLM provider for reflection/learning.
        """
        self.session = session
        self.backend = backend
        self.zone_id = zone_id
        self.user_id = user_id
        self.agent_id = agent_id
        self.llm_provider = llm_provider

        # Initialize components
        self.entity_registry = entity_registry or EntityRegistry(session)
        self.memory_router = MemoryViewRouter(session, self.entity_registry)

        # Initialize ReBAC manager for permission checks
        from sqlalchemy import Engine

        from nexus.core.rebac_manager_enhanced import EnhancedReBACManager

        bind = session.get_bind()
        assert isinstance(bind, Engine), "Expected Engine, got Connection"
        self.rebac_manager = EnhancedReBACManager(bind)

        self.permission_enforcer = MemoryPermissionEnforcer(
            memory_router=self.memory_router,
            entity_registry=self.entity_registry,
            rebac_manager=self.rebac_manager,
        )

        # Create operation context
        self.context = OperationContext(
            user=agent_id or user_id or "system",
            groups=[],
            is_admin=False,
        )

    def store(
        self,
        content: str | bytes | dict[str, Any],
        scope: str = "user",
        memory_type: str | None = None,
        importance: float | None = None,
        namespace: str | None = None,  # v0.8.0: Hierarchical namespace
        path_key: str | None = None,  # v0.8.0: Optional key for upsert mode
        state: str = "active",  # #368: Memory state ('inactive', 'active')
        _metadata: dict[str, Any] | None = None,
        context: OperationContext | None = None,
        generate_embedding: bool = True,  # #406: Generate embedding for semantic search
        embedding_provider: Any = None,  # #406: Optional embedding provider
        resolve_coreferences: bool = False,  # #1027: Resolve pronouns to entity names
        coreference_context: str | None = None,  # #1027: Prior conversation context
        resolve_temporal: bool = False,  # #1027: Resolve temporal expressions to absolute dates
        temporal_reference_time: Any = None,  # #1027: Reference time for temporal resolution
        extract_entities: bool = True,  # #1025: Extract named entities
        extract_temporal: bool = True,  # #1028: Extract temporal metadata for date queries
        extract_relationships: bool = False,  # #1038: Extract relationships (triplets)
        relationship_types: list[str] | None = None,  # #1038: Custom relationship types
        store_to_graph: bool = False,  # #1039: Store entities/relationships to graph tables
        valid_at: datetime | str | None = None,  # #1183: When fact became valid in real world
    ) -> str:
        """Store a memory.

        Args:
            content: Memory content (text, bytes, or structured dict).
            scope: Memory scope ('agent', 'user', 'zone', 'global').
            memory_type: Type of memory ('fact', 'preference', 'experience'). Optional if using namespace structure.
            importance: Importance score (0.0-1.0).
            namespace: Hierarchical namespace for organization (e.g., "knowledge/geography/facts"). v0.8.0
            path_key: Optional unique key within namespace for upsert mode. v0.8.0
            state: Memory state ('inactive', 'active'). Defaults to 'active' for backward compatibility. #368
            _metadata: Additional metadata (deprecated, use structured content dict instead).
            context: Optional operation context to override identity (v0.7.1+).
            resolve_coreferences: Resolve pronouns to entity names for context-independence. #1027
            coreference_context: Prior conversation context for pronoun resolution. #1027
            resolve_temporal: Resolve temporal expressions to absolute dates. #1027
            temporal_reference_time: Reference time for temporal resolution (datetime or ISO string). #1027
            extract_entities: Extract named entities for symbolic filtering. Defaults to True. #1025
            extract_temporal: Extract temporal metadata for date-based queries. Defaults to True. #1028
            valid_at: When fact became valid in real world. Accepts datetime or ISO-8601 string. #1183

        Returns:
            memory_id: The created or updated memory ID.

        Examples:
            >>> # Append mode (no path_key)
            >>> memory_id = memory.store(
            ...     content={"fact": "Paris is capital of France"},
            ...     namespace="knowledge/geography/facts"
            ... )

            >>> # Upsert mode (with path_key)
            >>> memory_id = memory.store(
            ...     content={"theme": "dark", "font_size": 14"},
            ...     namespace="user/preferences/ui",
            ...     path_key="settings"  # Will update if exists
            ... )

            >>> # Create inactive memory for manual approval (#368)
            >>> memory_id = memory.store(
            ...     content="Unverified information",
            ...     state="inactive"  # Won't appear in queries until approved
            ... )
            >>> memory.approve(memory_id)  # Activate it later

            >>> # Resolve coreferences for context-independent storage (#1027)
            >>> memory_id = memory.store(
            ...     content="He went to the store.",
            ...     resolve_coreferences=True,
            ...     coreference_context="John Smith was hungry."
            ... )
            >>> # Stored as: "John Smith went to the store."

            >>> # Resolve temporal expressions for context-independent storage (#1027)
            >>> memory_id = memory.store(
            ...     content="Meeting scheduled for tomorrow at 2pm.",
            ...     resolve_temporal=True,
            ...     temporal_reference_time="2025-01-10T12:00:00"
            ... )
            >>> # Stored as: "Meeting scheduled for on 2025-01-11 at 14:00."
        """
        import json

        # #1027: Apply coreference resolution before storing (write-time disambiguation)
        # This transforms "He went to the store" -> "John Smith went to the store"
        # making memories self-contained and context-independent
        if resolve_coreferences and isinstance(content, str):
            from nexus.core.coref_resolver import resolve_coreferences as resolve_coref

            content = resolve_coref(
                text=content,
                context=coreference_context,
                llm_provider=self.llm_provider,
            )

        # #1027: Apply temporal resolution (Φtime from SimpleMem pipeline)
        # This transforms "Meeting tomorrow" -> "Meeting on 2025-01-11"
        # making memories time-independent and self-contained
        if resolve_temporal and isinstance(content, str):
            from nexus.core.temporal_resolver import resolve_temporal as resolve_temp

            content = resolve_temp(
                text=content,
                reference_time=temporal_reference_time,
                llm_provider=self.llm_provider,
            )

        # Convert content to bytes
        if isinstance(content, dict):
            # Structured content - serialize as JSON
            content_bytes = json.dumps(content, indent=2).encode("utf-8")
        elif isinstance(content, str):
            content_bytes = content.encode("utf-8")
        else:
            content_bytes = content

        # v0.7.1: Use context identity if provided, otherwise fall back to instance identity
        zone_id = context.zone_id if context else self.zone_id
        user_id = context.user_id if context else self.user_id
        agent_id = context.agent_id if context else self.agent_id
        # Store content in backend (CAS)
        # LocalBackend.write_content() handles hashing and storage
        try:
            backend_context = context if context else self.context
            content_hash = self.backend.write_content(
                content_bytes, context=backend_context
            ).unwrap()
        except Exception as e:
            # If backend write fails, we can't proceed
            raise RuntimeError(f"Failed to store content in backend: {e}") from e

        # Generate embedding if requested (#406)
        embedding_json = None
        embedding_model_name = None
        embedding_dim = None

        if generate_embedding:
            # Get text content for embedding
            if isinstance(content, dict):
                # For structured content, embed the JSON string
                text_content = json.dumps(content)
            elif isinstance(content, str):
                text_content = content
            else:
                # For binary content, skip embedding
                text_content = None

            if text_content and len(text_content.strip()) > 0:
                # Try to get embedding provider
                if embedding_provider is None:
                    try:
                        from nexus.search.embeddings import create_embedding_provider

                        # Try to create default provider (OpenRouter)
                        with contextlib.suppress(Exception):
                            embedding_provider = create_embedding_provider(provider="openrouter")
                    except ImportError:
                        pass

                # Generate embedding
                if embedding_provider:
                    import asyncio

                    try:
                        embedding_vec = asyncio.run(embedding_provider.embed_text(text_content))
                        embedding_json = json.dumps(embedding_vec)
                        embedding_model_name = getattr(embedding_provider, "model", "unknown")
                        embedding_dim = len(embedding_vec)
                    except Exception:
                        # Failed to generate embedding, continue without it
                        pass

        # #1025: Extract named entities for symbolic filtering
        entities_json_str = None
        entity_types_str = None
        person_refs_str = None

        if extract_entities:
            # Get text content for entity extraction
            if isinstance(content, dict):
                text_for_entities = json.dumps(content)
            elif isinstance(content, str):
                text_for_entities = content
            else:
                text_for_entities = None

            if text_for_entities and len(text_for_entities.strip()) > 0:
                from nexus.core.entity_extractor import EntityExtractor

                extractor = EntityExtractor(use_spacy=False)
                entities = extractor.extract(text_for_entities)

                if entities:
                    entities_json_str = json.dumps([e.to_dict() for e in entities])
                    entity_types_str = extractor.get_entity_types_string(text_for_entities)
                    person_refs_str = extractor.get_person_refs_string(text_for_entities)

        # #1028: Extract temporal metadata for date-based queries
        temporal_refs_json_str = None
        earliest_date = None
        latest_date = None

        if extract_temporal:
            # Get text content for temporal extraction
            if isinstance(content, dict):
                text_for_temporal = json.dumps(content)
            elif isinstance(content, str):
                text_for_temporal = content
            else:
                text_for_temporal = None

            if text_for_temporal and len(text_for_temporal.strip()) > 0:
                from nexus.core.temporal_resolver import extract_temporal_metadata

                temporal_meta = extract_temporal_metadata(
                    text_for_temporal,
                    reference_time=temporal_reference_time,
                )

                if temporal_meta["temporal_refs"]:
                    temporal_refs_json_str = json.dumps(temporal_meta["temporal_refs"])
                    earliest_date = temporal_meta["earliest_date"]
                    latest_date = temporal_meta["latest_date"]

        # #1038: Extract relationships for graph-based retrieval
        relationships_json_str = None
        relationship_count_val = None

        if extract_relationships:
            # Get text content for relationship extraction
            if isinstance(content, dict):
                text_for_relationships = json.dumps(content)
            elif isinstance(content, str):
                text_for_relationships = content
            else:
                text_for_relationships = None

            if text_for_relationships and len(text_for_relationships.strip()) > 0:
                from nexus.core.relationship_extractor import LLMRelationshipExtractor

                rel_extractor = LLMRelationshipExtractor(
                    llm_provider=self.llm_provider,
                    confidence_threshold=0.5,
                )

                # Get entities as hints for relationship extraction
                entities_for_rel = None
                if entities_json_str:
                    entities_for_rel = json.loads(entities_json_str)

                rel_result = rel_extractor.extract(
                    text_for_relationships,
                    entities=entities_for_rel,
                    relationship_types=relationship_types,
                )

                if rel_result.relationships:
                    relationships_json_str = json.dumps(rel_result.to_dicts())
                    relationship_count_val = len(rel_result.relationships)

        # #1183: Parse valid_at if provided as string
        valid_at_dt = (
            (parse_datetime(valid_at) if isinstance(valid_at, str) else valid_at)
            if valid_at is not None
            else None
        )

        # #1188: Extract change_reason from metadata for append-only pattern
        change_reason = None
        if _metadata:
            if _metadata.get("correction"):
                change_reason = "correction"
            elif _metadata.get("change_reason"):
                change_reason = _metadata["change_reason"]

        # Create memory record (upserts if namespace+path_key exists)
        memory = self.memory_router.create_memory(
            content_hash=content_hash,
            zone_id=zone_id,
            user_id=user_id,
            agent_id=agent_id,
            scope=scope,
            memory_type=memory_type,
            importance=importance,
            namespace=namespace,
            path_key=path_key,
            state=state,  # #368: Pass state parameter
            embedding=embedding_json,  # #406: Store embedding
            embedding_model=embedding_model_name,  # #406: Store model name
            embedding_dim=embedding_dim,  # #406: Store dimension
            entities_json=entities_json_str,  # #1025: Store entities
            entity_types=entity_types_str,  # #1025: Store entity types
            person_refs=person_refs_str,  # #1025: Store person references
            temporal_refs_json=temporal_refs_json_str,  # #1028: Store temporal refs
            earliest_date=earliest_date,  # #1028: Store earliest date
            latest_date=latest_date,  # #1028: Store latest date
            relationships_json=relationships_json_str,  # #1038: Store relationships
            relationship_count=relationship_count_val,  # #1038: Store relationship count
            valid_at=valid_at_dt,  # #1183: When fact became valid
            size_bytes=len(content_bytes),  # #1184: Content size for versioning
            created_by=user_id or agent_id,  # #1184: Who created this version
            change_reason=change_reason,  # #1188: For correction mode
        )

        # #1039: Store extracted entities and relationships to graph tables
        if store_to_graph and (entities_json_str or relationships_json_str):
            try:
                self._store_to_graph(
                    memory_id=memory.memory_id,
                    zone_id=zone_id,
                    entities_json=entities_json_str,
                    relationships_json=relationships_json_str,
                )
            except Exception as e:
                # Log warning but don't fail the memory store
                import logging

                logging.getLogger(__name__).warning(f"Failed to store to graph: {e}")

        return memory.memory_id

    def _store_to_graph(
        self,
        memory_id: str,
        zone_id: str | None,
        entities_json: str | None,
        relationships_json: str | None,
    ) -> None:
        """Store extracted entities and relationships in graph tables (#1039).

        This stores entities in the `entities` table and relationships in the
        `relationships` table, enabling GraphRAG-style retrieval.

        Args:
            memory_id: Source memory ID for provenance tracking
            zone_id: Zone ID for multi-zone isolation
            entities_json: JSON string of extracted entities
            relationships_json: JSON string of extracted relationships
        """
        import asyncio
        import json
        import os

        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

        from nexus.search.graph_store import GraphStore

        # Get database URL from session's engine
        sync_url = os.environ.get("NEXUS_DATABASE_URL", "")
        if not sync_url:
            # Try to get from the session's engine bind
            try:
                bind = self.session.get_bind()
                url = getattr(bind, "url", None)
                if url:
                    sync_url = str(url)
            except Exception:
                return

        if not sync_url:
            return

        # Convert to async URL
        if sync_url.startswith("postgresql://"):
            async_url = sync_url.replace("postgresql://", "postgresql+asyncpg://")
        elif sync_url.startswith("sqlite:///"):
            async_url = sync_url.replace("sqlite:///", "sqlite+aiosqlite:///")
        else:
            async_url = sync_url

        # Use default zone if not provided
        effective_zone_id = zone_id or "default"

        async def _do_store() -> None:
            engine = create_async_engine(async_url)
            async_session_factory = async_sessionmaker(
                engine, class_=AsyncSession, expire_on_commit=False
            )
            try:
                async with async_session_factory() as session:
                    graph_store = GraphStore(session, zone_id=effective_zone_id)

                    # Store entities
                    entity_id_map: dict[str, str] = {}  # name -> entity_id
                    if entities_json:
                        entities = json.loads(entities_json)
                        for entity in entities:
                            entity_id, _ = await graph_store.add_entity(
                                name=entity.get("text", entity.get("name", "Unknown")),
                                entity_type=entity.get("label", entity.get("type", "CONCEPT")),
                                metadata=entity.get("metadata"),
                            )
                            entity_id_map[entity.get("text", entity.get("name", ""))] = entity_id

                            # Add mention linking back to source memory
                            await graph_store.add_mention(
                                entity_id=entity_id,
                                memory_id=memory_id,
                                confidence=entity.get("confidence", 0.9),
                                mention_text=entity.get("text", entity.get("name", "")),
                            )

                    # Store relationships
                    if relationships_json:
                        relationships = json.loads(relationships_json)
                        for rel in relationships:
                            # Get or create source and target entities
                            source_name = rel.get("subject") or rel.get("source", "")
                            target_name = rel.get("object") or rel.get("target", "")

                            source_id = entity_id_map.get(source_name)
                            target_id = entity_id_map.get(target_name)

                            # Create entities if not already in map
                            if not source_id and source_name:
                                source_id, _ = await graph_store.add_entity(
                                    name=source_name,
                                    entity_type="CONCEPT",
                                )
                                entity_id_map[source_name] = source_id

                            if not target_id and target_name:
                                target_id, _ = await graph_store.add_entity(
                                    name=target_name,
                                    entity_type="CONCEPT",
                                )
                                entity_id_map[target_name] = target_id

                            # Add relationship
                            if source_id and target_id:
                                rel_type = rel.get("predicate") or rel.get("type", "RELATED_TO")
                                await graph_store.add_relationship(
                                    source_entity_id=source_id,
                                    target_entity_id=target_id,
                                    relationship_type=rel_type.upper().replace(" ", "_"),
                                    confidence=rel.get("confidence", 0.8),
                                )

                    await session.commit()
            finally:
                await engine.dispose()

        asyncio.run(_do_store())

    def query(
        self,
        user_id: str | None = None,
        agent_id: str | None = None,
        zone_id: str | None = None,
        scope: str | None = None,
        memory_type: str | None = None,
        namespace: str | None = None,  # v0.8.0: Exact namespace match
        namespace_prefix: str | None = None,  # v0.8.0: Prefix match for hierarchical queries
        state: str | None = "active",  # #368: Default to active memories only
        after: str | datetime | None = None,  # #1023: Temporal filter
        before: str | datetime | None = None,  # #1023: Temporal filter
        during: str | None = None,  # #1023: Temporal range (partial date)
        entity_type: str | None = None,  # #1025: Filter by entity type
        person: str | None = None,  # #1025: Filter by person reference
        event_after: str | datetime | None = None,  # #1028: Filter by event date >= value
        event_before: str | datetime | None = None,  # #1028: Filter by event date <= value
        include_invalid: bool = False,  # #1183: Include invalidated memories
        include_superseded: bool = False,  # #1188: Include superseded memories
        as_of: str
        | datetime
        | None = None,  # #1183: Point-in-time query (deprecated, use as_of_event)
        as_of_event: str
        | datetime
        | None = None,  # #1185: What was TRUE at time X? (valid_at/invalid_at)
        as_of_system: str
        | datetime
        | None = None,  # #1185: What did SYSTEM KNOW at time X? (created_at)
        limit: int | None = None,
        offset: int = 0,
        context: OperationContext | None = None,
    ) -> list[dict[str, Any]]:
        """Query memories by relationships and metadata.

        Args:
            user_id: Filter by user ID (defaults to current user).
            agent_id: Filter by agent ID.
            zone_id: Filter by zone ID (defaults to current zone).
            scope: Filter by scope.
            memory_type: Filter by memory type.
            namespace: Filter by exact namespace match. v0.8.0
            namespace_prefix: Filter by namespace prefix (hierarchical). v0.8.0
            state: Filter by state ('inactive', 'active', 'all'). Defaults to 'active'. #368
            after: Return memories created after this time (ISO-8601 or datetime). #1023
            before: Return memories created before this time (ISO-8601 or datetime). #1023
            during: Return memories during this period (partial date: "2025", "2025-01"). #1023
            entity_type: Filter by entity type (e.g., "PERSON", "ORG", "DATE"). #1025
            person: Filter by person name reference. #1025
            event_after: Filter by event earliest_date >= value (ISO-8601 or datetime). #1028
            event_before: Filter by event latest_date <= value (ISO-8601 or datetime). #1028
            include_invalid: Include invalidated memories. Default False (current facts only). #1183
            as_of: Point-in-time query (deprecated, use as_of_event). #1183
            as_of_event: What was TRUE at time X? Filters by valid_at/invalid_at. #1185
            as_of_system: What did SYSTEM KNOW at time X? Filters by created_at, returns historical content. #1185
            limit: Maximum number of results.
            offset: Number of results to skip (for pagination).
            context: Optional operation context to override identity (v0.7.1+).

        Returns:
            List of memory dictionaries with metadata.

        Examples:
            >>> memories = memory.query(scope="user", memory_type="preference")
            >>> for mem in memories:
            ...     print(f"{mem['memory_id']}: {mem['content']}")

            >>> # Query memories from January 2025 (#1023)
            >>> memories = memory.query(during="2025-01")

            >>> # Query memories after a specific date (#1023)
            >>> memories = memory.query(after="2025-01-01T00:00:00Z")

            >>> # Query memories containing a person (#1025)
            >>> memories = memory.query(person="John Smith")

            >>> # Query memories with organization entities (#1025)
            >>> memories = memory.query(entity_type="ORG")

            >>> # Query memories about events after a date (#1028)
            >>> memories = memory.query(event_after="2025-01-01")

            >>> # Query memories about events in a date range (#1028)
            >>> memories = memory.query(event_after="2025-01-01", event_before="2025-01-31")
        """
        # v0.7.1: Use context identity if provided, otherwise fall back to instance identity or explicit params
        if user_id is None:
            user_id = context.user_id if context else self.user_id
        if zone_id is None:
            zone_id = context.zone_id if context else self.zone_id

        # #1023: Validate and normalize temporal parameters
        after_dt, before_dt = validate_temporal_params(after, before, during)

        # #1028: Parse event date parameters if strings
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

        # #1185: Parse as_of_event and as_of_system for bi-temporal queries
        # Fall back to as_of for backward compatibility
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
        memories = self.memory_router.query_memories(
            zone_id=zone_id,
            user_id=user_id,
            agent_id=agent_id,
            scope=scope,
            memory_type=memory_type,
            namespace=namespace,  # v0.8.0: Namespace filtering
            namespace_prefix=namespace_prefix,  # v0.8.0: Namespace prefix filtering
            state=state,
            after=after_dt,
            before=before_dt,
            entity_type=entity_type,  # #1025: Entity filtering
            person=person,  # #1025: Person filtering
            event_after=event_after_dt,  # #1028: Event date filtering
            event_before=event_before_dt,  # #1028: Event date filtering
            include_invalid=include_invalid,  # #1183: Include invalidated memories
            include_superseded=include_superseded,  # #1188: Include superseded memories
            valid_at_point=valid_at_point,  # #1185: Point-in-time query (as_of_event)
            system_at_point=system_at_point,  # #1185: System-time query (as_of_system)
            limit=limit,
        )

        # Filter by permissions first (before fetching content)
        # Use provided context or fall back to instance context
        check_context = context or self.context
        accessible_memories = []
        for memory in memories:
            # Check read permission
            if self.permission_enforcer.check_memory(memory, Permission.READ, check_context):
                accessible_memories.append(memory)

        # Apply offset/limit AFTER permission filtering for correct pagination.
        # Applying offset in SQL would skip rows before permission checks,
        # causing pages to have fewer results than expected.
        if offset:
            accessible_memories = accessible_memories[offset:]
        if limit:
            accessible_memories = accessible_memories[:limit]

        # #1185: For as_of_system queries, resolve historical content hashes
        # If a memory was updated after the system_at_point, get the version that was current at that time
        historical_content_hashes: dict[str, str] = {}  # memory_id -> historical content_hash
        if system_at_point is not None:
            from sqlalchemy import select

            from nexus.storage.models import VersionHistoryModel

            # Normalize system_at_point for comparison (SQLite stores without timezone)
            system_at_naive = (
                system_at_point.replace(tzinfo=None) if system_at_point.tzinfo else system_at_point
            )

            for memory in accessible_memories:
                # Check if memory was updated after system_at_point
                updated_at_naive = (
                    memory.updated_at.replace(tzinfo=None)
                    if memory.updated_at and memory.updated_at.tzinfo
                    else memory.updated_at
                )

                if updated_at_naive and updated_at_naive > system_at_naive:
                    # Memory was updated after system_at_point, need to find historical version
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
                    version = self.session.execute(stmt).scalar_one_or_none()
                    if version:
                        historical_content_hashes[memory.memory_id] = version.content_hash

        # Batch read all content hashes (including historical ones)
        content_hashes = [
            historical_content_hashes.get(memory.memory_id, memory.content_hash)
            for memory in accessible_memories
        ]
        content_map = self.backend.batch_read_content(content_hashes)

        # Build results with enriched content
        results = []
        for memory in accessible_memories:
            # #1185: Get content hash (use historical version if as_of_system was specified)
            effective_content_hash = historical_content_hashes.get(
                memory.memory_id, memory.content_hash
            )
            # Get content from batch read result
            content_bytes = content_map.get(effective_content_hash)

            if content_bytes is not None:
                try:
                    content = content_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    content = content_bytes.hex()  # Binary content
            else:
                content = f"<content not available: {memory.content_hash}>"

            # Calculate effective importance with decay (Issue #1030)
            effective_importance = get_effective_importance(
                importance_original=memory.importance_original,
                importance_current=memory.importance,
                last_accessed_at=memory.last_accessed_at,
                created_at=memory.created_at,
            )

            results.append(
                {
                    "memory_id": memory.memory_id,
                    "content": content,
                    "content_hash": effective_content_hash,  # #1185: Use historical hash for as_of_system
                    "zone_id": memory.zone_id,
                    "user_id": memory.user_id,
                    "agent_id": memory.agent_id,
                    "scope": memory.scope,
                    "visibility": memory.visibility,
                    "memory_type": memory.memory_type,
                    "importance": memory.importance,
                    "importance_effective": effective_importance,  # #1030
                    "state": memory.state,  # #368
                    "namespace": memory.namespace,  # v0.8.0
                    "path_key": memory.path_key,  # v0.8.0
                    "entity_types": memory.entity_types,  # #1025
                    "person_refs": memory.person_refs,  # #1025
                    "temporal_refs_json": memory.temporal_refs_json,  # #1028
                    "earliest_date": memory.earliest_date.isoformat()
                    if memory.earliest_date
                    else None,  # #1028
                    "latest_date": memory.latest_date.isoformat()
                    if memory.latest_date
                    else None,  # #1028
                    "relationships_json": memory.relationships_json,  # #1038
                    "relationship_count": memory.relationship_count,  # #1038
                    "created_at": memory.created_at.isoformat() if memory.created_at else None,
                    "updated_at": memory.updated_at.isoformat() if memory.updated_at else None,
                    "valid_at": memory.valid_at.isoformat() if memory.valid_at else None,  # #1183
                    "invalid_at": memory.invalid_at.isoformat()
                    if memory.invalid_at
                    else None,  # #1183
                    "is_current": memory.invalid_at is None,  # #1183: True if not invalidated
                }
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
        after: str | datetime | None = None,  # #1023: Temporal filter
        before: str | datetime | None = None,  # #1023: Temporal filter
        during: str | None = None,  # #1023: Temporal range (partial date)
    ) -> list[dict[str, Any]]:
        """Semantic search over memories.

        Args:
            query: Search query text.
            scope: Filter by scope.
            memory_type: Filter by memory type.
            limit: Maximum number of results.
            search_mode: Search mode - "semantic", "keyword", or "hybrid" (default: "hybrid")
            embedding_provider: Optional embedding provider for semantic/hybrid search
            after: Return memories created after this time (ISO-8601 or datetime). #1023
            before: Return memories created before this time (ISO-8601 or datetime). #1023
            during: Return memories during this period (partial date: "2025", "2025-01"). #1023

        Returns:
            List of memory dictionaries with relevance scores.

        Examples:
            >>> results = memory.search("Python programming preferences")
            >>> for mem in results:
            ...     print(f"Score: {mem['score']:.2f} - {mem['content']}")

            >>> # Search memories from January 2025 (#1023)
            >>> results = memory.search("project updates", during="2025-01")

            >>> # Search recent memories only (#1023)
            >>> results = memory.search("API design", after="2025-01-01")

        Note:
            Semantic search requires vector embeddings. If not available,
            falls back to simple text matching.
        """
        import asyncio
        import json

        # #1023: Validate and normalize temporal parameters
        after_dt, before_dt = validate_temporal_params(after, before, during)

        # If semantic or hybrid mode is requested but no embeddings available, fall back to keyword
        if (
            search_mode in ("semantic", "hybrid")
            and embedding_provider is None
            and self.llm_provider is None
        ):
            # No embedding provider available, fall back to keyword search
            search_mode = "keyword"

        if search_mode == "keyword":
            # Keyword-only search using text matching
            return self._keyword_search(query, scope, memory_type, limit, after_dt, before_dt)

        # For semantic/hybrid search, we need embeddings
        if embedding_provider is None:
            # Try to use a default embedding provider if available
            try:
                from nexus.search.embeddings import create_embedding_provider

                # Try to create an embedding provider (checks for API keys in env)
                try:
                    embedding_provider = create_embedding_provider(provider="openrouter")
                except Exception:
                    # Fall back to keyword search if no provider available
                    return self._keyword_search(
                        query, scope, memory_type, limit, after_dt, before_dt
                    )
            except ImportError:
                # Fall back to keyword search
                return self._keyword_search(query, scope, memory_type, limit, after_dt, before_dt)

        # Generate query embedding
        query_embedding = asyncio.run(embedding_provider.embed_text(query))

        # Query memories from database
        from sqlalchemy import select

        from nexus.storage.models import MemoryModel

        with self.session as session:
            stmt = select(MemoryModel).where(MemoryModel.embedding.isnot(None))

            if scope:
                stmt = stmt.where(MemoryModel.scope == scope)
            if memory_type:
                stmt = stmt.where(MemoryModel.memory_type == memory_type)

            # Filter by zone/user/agent
            if self.zone_id:
                stmt = stmt.where(MemoryModel.zone_id == self.zone_id)
            if self.user_id:
                stmt = stmt.where(MemoryModel.user_id == self.user_id)
            if self.agent_id:
                stmt = stmt.where(MemoryModel.agent_id == self.agent_id)

            # #1023: Temporal filtering
            if after_dt:
                stmt = stmt.where(MemoryModel.created_at >= after_dt)
            if before_dt:
                stmt = stmt.where(MemoryModel.created_at <= before_dt)

            # Get memories with embeddings
            result = session.execute(stmt)
            memories_with_embeddings = result.scalars().all()

            # Compute similarity scores
            scored_memories = []
            for memory in memories_with_embeddings:
                # Check permission
                if not self.permission_enforcer.check_memory(memory, Permission.READ, self.context):
                    continue

                # Parse embedding from JSON
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
                    # Read content and compute keyword match
                    try:
                        content_bytes = self.backend.read_content(
                            memory.content_hash, context=self.context
                        ).unwrap()
                        content = content_bytes.decode("utf-8")
                        keyword_score = self._compute_keyword_score(query, content)
                    except Exception:
                        pass

                # Combined score for hybrid mode
                if search_mode == "hybrid":
                    # Weight: 70% semantic, 30% keyword
                    score = 0.7 * similarity + 0.3 * keyword_score
                else:
                    score = similarity

                scored_memories.append((memory, score, similarity, keyword_score))

            # Sort by score
            scored_memories.sort(key=lambda x: x[1], reverse=True)

            # Limit results
            scored_memories = scored_memories[:limit]

            # Build result list with content
            results = []
            for memory, score, semantic_score, keyword_score in scored_memories:
                # Read content
                try:
                    content_bytes = self.backend.read_content(
                        memory.content_hash, context=self.context
                    ).unwrap()
                    content = content_bytes.decode("utf-8")
                except Exception:
                    content = f"<content not available: {memory.content_hash}>"

                results.append(
                    {
                        "memory_id": memory.memory_id,
                        "content": content,
                        "content_hash": memory.content_hash,
                        "zone_id": memory.zone_id,
                        "user_id": memory.user_id,
                        "agent_id": memory.agent_id,
                        "scope": memory.scope,
                        "visibility": memory.visibility,
                        "memory_type": memory.memory_type,
                        "importance": memory.importance,
                        "state": memory.state,
                        "namespace": memory.namespace,
                        "path_key": memory.path_key,
                        "created_at": memory.created_at.isoformat() if memory.created_at else None,
                        "updated_at": memory.updated_at.isoformat() if memory.updated_at else None,
                        "score": score,
                        "semantic_score": semantic_score if search_mode == "hybrid" else None,
                        "keyword_score": keyword_score if search_mode == "hybrid" else None,
                    }
                )

            return results

    def _keyword_search(
        self,
        query: str,
        scope: str | None,
        memory_type: str | None,
        limit: int,
        after_dt: datetime | None = None,  # #1023: Temporal filter
        before_dt: datetime | None = None,  # #1023: Temporal filter
    ) -> list[dict[str, Any]]:
        """Keyword-only search fallback."""
        # Get all memories matching filters
        memories = self.query(
            scope=scope,
            memory_type=memory_type,
            after=after_dt,  # #1023: Pass temporal filters
            before=before_dt,  # #1023: Pass temporal filters
            limit=limit * 3,  # Get more to filter
        )

        # Simple text matching
        scored_results = []

        for memory in memories:
            content = memory.get("content", "")
            if not content or isinstance(content, bytes):
                continue

            # Simple relevance scoring
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

    def _cosine_similarity(self, vec1: list[float], vec2: list[float]) -> float:
        """Compute cosine similarity between two vectors."""
        import math

        # Compute dot product
        dot_product = sum(a * b for a, b in zip(vec1, vec2, strict=False))

        # Compute magnitudes
        mag1 = math.sqrt(sum(a * a for a in vec1))
        mag2 = math.sqrt(sum(b * b for b in vec2))

        # Avoid division by zero
        if mag1 == 0 or mag2 == 0:
            return 0.0

        return dot_product / (mag1 * mag2)

    def _track_memory_access(self, memory: Any) -> None:
        """Update access tracking when memory is retrieved (Issue #1030).

        Args:
            memory: MemoryModel instance to update.
        """

        try:
            # Update access count and timestamp
            memory.last_accessed_at = datetime.now(UTC)
            memory.access_count = (memory.access_count or 0) + 1

            # Preserve original importance on first access
            if memory.importance_original is None and memory.importance is not None:
                memory.importance_original = memory.importance

            self.session.commit()
        except Exception:
            # Don't fail the read operation if tracking fails
            self.session.rollback()

    def apply_decay_batch(
        self,
        decay_factor: float = DEFAULT_DECAY_FACTOR,
        min_importance: float = DEFAULT_MIN_IMPORTANCE,
        batch_size: int = 1000,
    ) -> dict[str, Any]:
        """Batch update importance scores with decay (Issue #1030).

        Run periodically (e.g., daily cron) to apply decay to all memories.
        This updates the stored importance values based on time since last access.

        Args:
            decay_factor: Decay multiplier per day (default: 0.95)
            min_importance: Minimum importance floor (default: 0.1)
            batch_size: Number of memories to process per batch

        Returns:
            Summary of the batch operation.

        Example:
            >>> # Run as daily maintenance job
            >>> result = memory.apply_decay_batch()
            >>> print(f"Updated {result['updated']} memories")
        """
        from nexus.storage.models import MemoryModel

        updated_count = 0
        skipped_count = 0
        total_processed = 0

        try:
            # Query memories that haven't hit minimum importance yet
            memories = (
                self.session.query(MemoryModel)
                .filter(
                    MemoryModel.zone_id == self.zone_id,
                    MemoryModel.importance > min_importance,
                )
                .limit(batch_size)
                .all()
            )

            for memory in memories:
                total_processed += 1

                # Calculate effective importance
                effective = get_effective_importance(
                    importance_original=memory.importance_original,
                    importance_current=memory.importance,
                    last_accessed_at=memory.last_accessed_at,
                    created_at=memory.created_at,
                    decay_factor=decay_factor,
                    min_importance=min_importance,
                )

                # Only update if decay has occurred
                if memory.importance is not None and effective < memory.importance:
                    # Preserve original importance if not already set
                    if memory.importance_original is None:
                        memory.importance_original = memory.importance
                    memory.importance = effective
                    updated_count += 1
                else:
                    skipped_count += 1

            self.session.commit()

        except Exception as e:
            self.session.rollback()
            return {
                "success": False,
                "error": str(e),
                "updated": 0,
                "skipped": 0,
                "processed": 0,
            }

        return {
            "success": True,
            "updated": updated_count,
            "skipped": skipped_count,
            "processed": total_processed,
            "decay_factor": decay_factor,
            "min_importance": min_importance,
        }

    def _resolve_to_current(self, memory_id: str) -> Any:
        """Follow the superseded_by chain to find the current memory (#1188).

        If the given memory_id has been superseded, follows the chain forward
        to find the latest (current) version.

        Uses _get_memory_by_id_raw to traverse through soft-deleted nodes
        in the chain. The caller (get()) handles filtering deleted memories.

        Returns:
            The current MemoryModel, or None if not found.
        """
        memory = self.memory_router._get_memory_by_id_raw(memory_id)
        if not memory:
            return None

        # Follow superseded_by_id chain to current version
        visited = {memory.memory_id}
        while memory.superseded_by_id:
            successor = self.memory_router._get_memory_by_id_raw(memory.superseded_by_id)
            if successor is None or successor.memory_id in visited:
                break
            visited.add(successor.memory_id)
            memory = successor

        return memory

    def resolve_to_current(self, memory_id: str) -> Any:
        """Public wrapper for _resolve_to_current (#1193).

        Follow the superseded_by chain to find the current memory.
        Returns the current MemoryModel, or None if not found.
        """
        return self._resolve_to_current(memory_id)

    def ensure_upsert_key(self, memory_id: str, existing: dict[str, Any]) -> str:
        """Ensure memory has a path_key for upsert operations (#1193).

        If the existing memory has no path_key, assigns memory_id as its
        path_key and commits the change. Returns the effective path_key.

        This avoids direct model mutation from the router layer.
        """
        upsert_path_key = existing.get("path_key")
        if not upsert_path_key:
            memory_model = self.memory_router.get_memory_by_id(memory_id)
            if memory_model:
                memory_model.path_key = memory_id
                self.memory_router.session.commit()
            upsert_path_key = memory_id
        return upsert_path_key

    def get(
        self,
        memory_id: str,
        track_access: bool = True,
        context: OperationContext | None = None,
    ) -> dict[str, Any] | None:
        """Get a specific memory by ID.

        Args:
            memory_id: Memory ID.
            track_access: Whether to update access tracking (default: True).
                Set to False for internal lookups that shouldn't affect decay.
            context: Optional operation context to override identity (v0.7.1+).

        Returns:
            Memory dictionary or None if not found or no permission.

        Example:
            >>> mem = memory.get("mem_123")
            >>> print(mem['content'])
        """
        memory = self.memory_router.get_memory_by_id(memory_id)
        if not memory:
            return None

        # #1188: Filter out soft-deleted memories
        if memory.state == "deleted":
            return None

        # #1188: Follow superseded chain to current version
        if memory.superseded_by_id:
            memory = self._resolve_to_current(memory_id)
            if not memory or memory.state == "deleted":
                return None

        # Use provided context or fall back to instance context
        check_context = context or self.context

        # Check permission
        if not self.permission_enforcer.check_memory(memory, Permission.READ, check_context):
            return None

        # Track access (Issue #1030)
        if track_access:
            self._track_memory_access(memory)

        # Read content
        content = None
        try:
            content_bytes = self.backend.read_content(
                memory.content_hash, context=self.context
            ).unwrap()
            try:
                content = content_bytes.decode("utf-8")
            except UnicodeDecodeError:
                content = content_bytes.hex()
        except Exception:
            content = f"<content not available: {memory.content_hash}>"

        # Calculate effective importance with decay (Issue #1030)
        effective_importance = get_effective_importance(
            importance_original=memory.importance_original,
            importance_current=memory.importance,
            last_accessed_at=memory.last_accessed_at,
            created_at=memory.created_at,
        )

        return {
            "memory_id": memory.memory_id,
            "content": content,
            "content_hash": memory.content_hash,
            "zone_id": memory.zone_id,
            "user_id": memory.user_id,
            "agent_id": memory.agent_id,
            "scope": memory.scope,
            "visibility": memory.visibility,
            "memory_type": memory.memory_type,
            "importance": memory.importance,
            "importance_original": memory.importance_original,  # #1030
            "importance_effective": effective_importance,  # #1030
            "access_count": memory.access_count,  # #1030
            "last_accessed_at": (
                memory.last_accessed_at.isoformat() if memory.last_accessed_at else None
            ),  # #1030
            "state": memory.state,  # #368
            "namespace": memory.namespace,
            "path_key": memory.path_key,
            "created_at": memory.created_at.isoformat() if memory.created_at else None,
            "updated_at": memory.updated_at.isoformat() if memory.updated_at else None,
            "valid_at": memory.valid_at.isoformat() if memory.valid_at else None,  # #1183
            "invalid_at": memory.invalid_at.isoformat() if memory.invalid_at else None,  # #1183
            "is_current": memory.invalid_at is None,  # #1183: True if not invalidated
        }

    def retrieve(
        self,
        namespace: str | None = None,
        path_key: str | None = None,
        path: str | None = None,
    ) -> dict[str, Any] | None:
        """Retrieve a memory by namespace and path_key.

        Args:
            namespace: Memory namespace.
            path_key: Path key within namespace.
            path: Combined path (alternative to namespace+path_key). Format: "namespace/path_key"

        Returns:
            Memory dictionary or None if not found or no permission.

        Examples:
            >>> # Retrieve by namespace + path_key
            >>> mem = memory.retrieve(namespace="user/preferences/ui", path_key="settings")

            >>> # Retrieve by combined path (sugar syntax)
            >>> mem = memory.retrieve(path="user/preferences/ui/settings")

        Note:
            Only works for memories stored with path_key. Use get(memory_id) for append-mode memories.
        """
        # Parse combined path if provided
        if path:
            parts = path.rsplit("/", 1)
            if len(parts) == 2:
                namespace, path_key = parts
            else:
                # Path doesn't contain /, treat as path_key only
                path_key = parts[0]

        if not namespace or not path_key:
            raise ValueError("Both namespace and path_key are required")

        # Query by namespace + path_key
        from sqlalchemy import select

        from nexus.storage.models import MemoryModel

        stmt = select(MemoryModel).where(
            MemoryModel.namespace == namespace, MemoryModel.path_key == path_key
        )
        memory = self.session.execute(stmt).scalar_one_or_none()

        if not memory:
            return None

        # Check permission
        if not self.permission_enforcer.check_memory(memory, Permission.READ, self.context):
            return None

        # Read content
        content = None
        import json

        try:
            content_bytes = self.backend.read_content(
                memory.content_hash, context=self.context
            ).unwrap()
            try:
                # Try to parse as JSON (structured content)
                content = json.loads(content_bytes.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                # Fall back to text or hex
                try:
                    content = content_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    content = content_bytes.hex()
        except Exception:
            content = f"<content not available: {memory.content_hash}>"

        return {
            "memory_id": memory.memory_id,
            "content": content,
            "content_hash": memory.content_hash,
            "zone_id": memory.zone_id,
            "user_id": memory.user_id,
            "agent_id": memory.agent_id,
            "scope": memory.scope,
            "visibility": memory.visibility,
            "memory_type": memory.memory_type,
            "importance": memory.importance,
            "state": memory.state,  # #368
            "namespace": memory.namespace,
            "path_key": memory.path_key,
            "created_at": memory.created_at.isoformat() if memory.created_at else None,
            "updated_at": memory.updated_at.isoformat() if memory.updated_at else None,
        }

    def delete(
        self,
        memory_id: str,
        context: OperationContext | None = None,
    ) -> bool:
        """Delete a memory (#1188: soft-delete, preserves row).

        Args:
            memory_id: Memory ID to delete.
            context: Optional operation context to override identity.

        Returns:
            True if deleted, False if not found or no permission.

        Example:
            >>> memory.delete("mem_123")
            True
        """
        memory = self.memory_router.get_memory_by_id(memory_id)
        if not memory:
            return False

        # Check permission
        check_context = context or self.context
        if not self.permission_enforcer.check_memory(memory, Permission.WRITE, check_context):
            return False

        return self.memory_router.delete_memory(memory_id)

    def approve(self, memory_id: str) -> bool:
        """Approve a memory (activate it) (#368).

        Args:
            memory_id: Memory ID to approve.

        Returns:
            True if approved, False if not found or no permission.

        Example:
            >>> memory.approve("mem_123")
            True
        """
        memory = self.memory_router.get_memory_by_id(memory_id)
        if not memory:
            return False

        # Check permission
        if not self.permission_enforcer.check_memory(memory, Permission.WRITE, self.context):
            return False

        result = self.memory_router.approve_memory(memory_id)
        return result is not None

    def deactivate(self, memory_id: str) -> bool:
        """Deactivate a memory (make it inactive) (#368).

        Args:
            memory_id: Memory ID to deactivate.

        Returns:
            True if deactivated, False if not found or no permission.

        Example:
            >>> memory.deactivate("mem_123")
            True
        """
        memory = self.memory_router.get_memory_by_id(memory_id)
        if not memory:
            return False

        # Check permission
        if not self.permission_enforcer.check_memory(memory, Permission.WRITE, self.context):
            return False

        result = self.memory_router.deactivate_memory(memory_id)
        return result is not None

    def approve_batch(self, memory_ids: list[str]) -> dict[str, Any]:
        """Approve multiple memories at once (#368).

        Args:
            memory_ids: List of memory IDs to approve.

        Returns:
            Dictionary with success/failure counts and details.

        Example:
            >>> result = memory.approve_batch(["mem_1", "mem_2", "mem_3"])
            >>> print(f"Approved {result['approved']} memories")
        """
        approved = []
        failed = []

        for memory_id in memory_ids:
            if self.approve(memory_id):
                approved.append(memory_id)
            else:
                failed.append(memory_id)

        return {
            "approved": len(approved),
            "failed": len(failed),
            "approved_ids": approved,
            "failed_ids": failed,
        }

    def deactivate_batch(self, memory_ids: list[str]) -> dict[str, Any]:
        """Deactivate multiple memories at once (#368).

        Args:
            memory_ids: List of memory IDs to deactivate.

        Returns:
            Dictionary with success/failure counts and details.

        Example:
            >>> result = memory.deactivate_batch(["mem_1", "mem_2", "mem_3"])
            >>> print(f"Deactivated {result['deactivated']} memories")
        """
        deactivated = []
        failed = []

        for memory_id in memory_ids:
            if self.deactivate(memory_id):
                deactivated.append(memory_id)
            else:
                failed.append(memory_id)

        return {
            "deactivated": len(deactivated),
            "failed": len(failed),
            "deactivated_ids": deactivated,
            "failed_ids": failed,
        }

    def delete_batch(self, memory_ids: list[str]) -> dict[str, Any]:
        """Delete multiple memories at once (#368).

        Args:
            memory_ids: List of memory IDs to delete.

        Returns:
            Dictionary with success/failure counts and details.

        Example:
            >>> result = memory.delete_batch(["mem_1", "mem_2", "mem_3"])
            >>> print(f"Deleted {result['deleted']} memories")
        """
        deleted = []
        failed = []

        for memory_id in memory_ids:
            if self.delete(memory_id):
                deleted.append(memory_id)
            else:
                failed.append(memory_id)

        return {
            "deleted": len(deleted),
            "failed": len(failed),
            "deleted_ids": deleted,
            "failed_ids": failed,
        }

    def invalidate(
        self,
        memory_id: str,
        invalid_at: datetime | str | None = None,
    ) -> bool:
        """Invalidate a memory (mark as no longer valid) (#1183).

        This is a temporal soft-delete that marks when a fact became false,
        without removing the historical record. The memory remains queryable
        for historical analysis but is excluded from "current facts" queries.

        Args:
            memory_id: Memory ID to invalidate.
            invalid_at: When the fact became invalid. Defaults to now().

        Returns:
            True if invalidated, False if not found or no permission.

        Example:
            >>> # Fact is no longer true as of today
            >>> memory.invalidate("mem_123")
            True

            >>> # Fact became false on a specific date
            >>> memory.invalidate("mem_123", invalid_at="2026-01-15")
            True

            >>> # Query only current facts (excludes invalidated)
            >>> current_facts = memory.query(include_invalid=False)

        Note:
            Unlike delete(), invalidate() preserves the memory for historical
            queries. Use delete() to permanently remove data.
        """
        memory = self.memory_router.get_memory_by_id(memory_id)
        if not memory:
            return False

        # Check permission
        if not self.permission_enforcer.check_memory(memory, Permission.WRITE, self.context):
            return False

        # Parse invalid_at
        invalid_at_dt: datetime = datetime.now(UTC)
        if invalid_at is not None:
            if isinstance(invalid_at, str):
                parsed = parse_datetime(invalid_at)
                if parsed is not None:
                    invalid_at_dt = parsed
            else:
                invalid_at_dt = invalid_at

        # Update the memory
        result = self.memory_router.invalidate_memory(memory_id, invalid_at_dt)
        return result is not None

    def invalidate_batch(
        self, memory_ids: list[str], invalid_at: datetime | str | None = None
    ) -> dict[str, Any]:
        """Invalidate multiple memories at once (#1183).

        Args:
            memory_ids: List of memory IDs to invalidate.
            invalid_at: When facts became invalid. Defaults to now().

        Returns:
            Dictionary with success/failure counts and details.

        Example:
            >>> result = memory.invalidate_batch(["mem_1", "mem_2", "mem_3"])
            >>> print(f"Invalidated {result['invalidated']} memories")
        """
        invalidated = []
        failed = []

        for memory_id in memory_ids:
            if self.invalidate(memory_id, invalid_at=invalid_at):
                invalidated.append(memory_id)
            else:
                failed.append(memory_id)

        return {
            "invalidated": len(invalidated),
            "failed": len(failed),
            "invalidated_ids": invalidated,
            "failed_ids": failed,
        }

    def revalidate(self, memory_id: str) -> bool:
        """Revalidate a memory (clear invalid_at timestamp) (#1183).

        Use when a previously invalidated fact becomes true again.

        Args:
            memory_id: Memory ID to revalidate.

        Returns:
            True if revalidated, False if not found or no permission.

        Example:
            >>> # Fact is true again
            >>> memory.revalidate("mem_123")
            True
        """
        memory = self.memory_router.get_memory_by_id(memory_id)
        if not memory:
            return False

        # Check permission
        if not self.permission_enforcer.check_memory(memory, Permission.WRITE, self.context):
            return False

        result = self.memory_router.revalidate_memory(memory_id)
        return result is not None

    def get_history(self, memory_id: str) -> list[dict[str, Any]]:
        """Get the complete version history chain for a memory (#1188).

        Traverses the supersedes chain to return all versions of a memory,
        from oldest to newest.

        Args:
            memory_id: Any memory ID in the chain (can be current or old).

        Returns:
            List of memory dicts in chronological order (oldest first).
            Empty list if memory not found.
        """
        import json

        memory = self.memory_router.get_memory_by_id(memory_id)
        if not memory:
            return []

        # Walk backward to find the oldest ancestor
        current = memory
        while current.supersedes_id:
            ancestor = self.memory_router.get_memory_by_id(current.supersedes_id)
            if ancestor is None:
                break
            current = ancestor

        # Now walk forward from oldest to newest
        chain = []
        visited = set()
        node = current
        while node and node.memory_id not in visited:
            visited.add(node.memory_id)

            # Read content for this memory
            content = None
            try:
                content_bytes = self.backend.read_content(
                    node.content_hash, context=self.context
                ).unwrap()
                try:
                    content = json.loads(content_bytes.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    try:
                        content = content_bytes.decode("utf-8")
                    except UnicodeDecodeError:
                        content = content_bytes.hex()
            except Exception:
                content = f"<content not available: {node.content_hash}>"

            chain.append(
                {
                    "memory_id": node.memory_id,
                    "content": content,
                    "content_hash": node.content_hash,
                    "version": node.current_version,
                    "supersedes_id": node.supersedes_id,
                    "superseded_by_id": node.superseded_by_id,
                    "valid_at": node.valid_at.isoformat() if node.valid_at else None,
                    "invalid_at": node.invalid_at.isoformat() if node.invalid_at else None,
                    "created_at": node.created_at.isoformat() if node.created_at else None,
                }
            )

            # Move to next version
            if node.superseded_by_id:
                next_node = self.memory_router.get_memory_by_id(node.superseded_by_id)
                if next_node is None:
                    break
                node = next_node
            else:
                break

        return chain

    def gc_old_versions(self, older_than_days: int = 365) -> int:
        """Garbage collect old superseded versions (#1188).

        Removes superseded memories older than the threshold.
        Never removes current (non-superseded) memories.

        Args:
            older_than_days: Only remove versions older than this many days.

        Returns:
            Number of versions removed.
        """
        from datetime import timedelta

        from sqlalchemy import select

        from nexus.storage.models import MemoryModel

        now = datetime.now(UTC)
        threshold = now - timedelta(days=older_than_days)

        # Find superseded memories older than threshold
        stmt = select(MemoryModel).where(
            MemoryModel.superseded_by_id.isnot(None),  # Is superseded
            MemoryModel.invalid_at.isnot(None),  # Has been invalidated
            MemoryModel.invalid_at <= threshold,  # Older than threshold
        )
        old_memories = list(self.session.execute(stmt).scalars().all())

        removed = 0
        for memory in old_memories:
            self.session.delete(memory)
            removed += 1

        if removed > 0:
            self.session.commit()

        return removed

    def list(
        self,
        scope: str | None = None,
        memory_type: str | None = None,
        namespace: str | None = None,  # v0.8.0: Exact namespace match
        namespace_prefix: str | None = None,  # v0.8.0: Prefix match for hierarchical queries
        state: str | None = "active",  # #368: Default to active memories only
        after: str | datetime | None = None,  # #1023: Temporal filter
        before: str | datetime | None = None,  # #1023: Temporal filter
        during: str | None = None,  # #1023: Temporal range (partial date)
        limit: int | None = 100,
        context: OperationContext | None = None,
    ) -> list[dict[str, Any]]:
        """List memories for current user/agent.

        Args:
            scope: Filter by scope.
            memory_type: Filter by memory type.
            namespace: Filter by exact namespace match. v0.8.0
            namespace_prefix: Filter by namespace prefix for hierarchical queries. v0.8.0
            state: Filter by state ('inactive', 'active', 'all'). Defaults to 'active'. #368
            after: Return memories created after this time (ISO-8601 or datetime). #1023
            before: Return memories created before this time (ISO-8601 or datetime). #1023
            during: Return memories during this period (partial date: "2025", "2025-01"). #1023
            limit: Maximum number of results.
            context: Optional operation context to override identity (v0.7.1+).

        Returns:
            List of memory dictionaries (without full content for efficiency).

        Examples:
            >>> # List all memories
            >>> memories = memory.list()

            >>> # List memories in specific namespace
            >>> prefs = memory.list(namespace="user/preferences/ui")

            >>> # List all geography knowledge
            >>> geo = memory.list(namespace_prefix="knowledge/geography/")

            >>> # List all facts across all domains
            >>> facts = memory.list(namespace_prefix="*/facts")

            >>> # List inactive memories (pending review)
            >>> pending = memory.list(state="inactive")

            >>> # List memories from last week (#1023)
            >>> recent = memory.list(after="2025-01-01")

            >>> # List memories from January 2025 (#1023)
            >>> jan = memory.list(during="2025-01")
        """
        # v0.7.1: Use context identity if provided, otherwise fall back to instance identity
        zone_id = context.zone_id if context else self.zone_id
        user_id = context.user_id if context else self.user_id
        agent_id = context.agent_id if context else self.agent_id

        # #1023: Validate and normalize temporal parameters
        after_dt, before_dt = validate_temporal_params(after, before, during)

        memories = self.memory_router.query_memories(
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
            limit=limit,
        )

        # Use provided context or fall back to instance context
        check_context = context or self.context

        results = []
        for memory in memories:
            # Check permission
            if not self.permission_enforcer.check_memory(memory, Permission.READ, check_context):
                continue

            results.append(
                {
                    "memory_id": memory.memory_id,
                    "content_hash": memory.content_hash,
                    "zone_id": memory.zone_id,
                    "user_id": memory.user_id,
                    "agent_id": memory.agent_id,
                    "scope": memory.scope,
                    "visibility": memory.visibility,
                    "memory_type": memory.memory_type,
                    "importance": memory.importance,
                    "state": memory.state,  # #368
                    "namespace": memory.namespace,
                    "path_key": memory.path_key,
                    "created_at": memory.created_at.isoformat() if memory.created_at else None,
                    "updated_at": memory.updated_at.isoformat() if memory.updated_at else None,
                }
            )

        return results

    # ========== ACE (Agentic Context Engineering) Integration (v0.5.0) ==========

    def start_trajectory(
        self,
        task_description: str,
        task_type: str | None = None,
    ) -> str:
        """Start tracking a new execution trajectory.

        Args:
            task_description: Description of the task
            task_type: Optional task type

        Returns:
            trajectory_id: ID of the created trajectory

        Example:
            >>> traj_id = memory.start_trajectory("Deploy caching strategy")
            >>> # ... execute task ...
            >>> memory.complete_trajectory(traj_id, "success", success_score=0.95)
        """
        from nexus.core.ace.trajectory import TrajectoryManager

        traj_mgr = TrajectoryManager(
            self.session,
            self.backend,
            self.user_id or "system",
            self.agent_id,
            self.zone_id,
        )
        return traj_mgr.start_trajectory(task_description, task_type)

    def log_trajectory_step(
        self,
        trajectory_id: str,
        step_type: str,
        description: str,
        result: Any = None,
    ) -> None:
        """Log a step in the trajectory.

        Args:
            trajectory_id: Trajectory ID
            step_type: Type of step ('action', 'decision', 'observation')
            description: Step description
            result: Optional result data
        """
        from nexus.core.ace.trajectory import TrajectoryManager

        traj_mgr = TrajectoryManager(
            self.session,
            self.backend,
            self.user_id or "system",
            self.agent_id,
            self.zone_id,
        )
        traj_mgr.log_step(trajectory_id, step_type, description, result)

    def log_step(
        self,
        trajectory_id: str,
        step_type: str,
        description: str,
        result: Any = None,
    ) -> None:
        """Alias for log_trajectory_step() to match #303 spec.

        Args:
            trajectory_id: Trajectory ID
            step_type: Type of step ('action', 'decision', 'observation')
            description: Step description
            result: Optional result data

        Example:
            >>> memory.log_step(traj_id, "decision", "Checking data format")
        """
        self.log_trajectory_step(trajectory_id, step_type, description, result)

    def complete_trajectory(
        self,
        trajectory_id: str,
        status: str,
        success_score: float | None = None,
        error_message: str | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> str:
        """Complete a trajectory with outcome.

        Args:
            trajectory_id: Trajectory ID
            status: Status ('success', 'failure', 'partial')
            success_score: Success score (0.0-1.0)
            error_message: Error message if failed
            metrics: Performance metrics (rows_processed, duration_ms, etc.)

        Returns:
            trajectory_id: The completed trajectory ID

        Example:
            >>> memory.complete_trajectory(
            ...     traj_id,
            ...     status="success",
            ...     success_score=0.95,
            ...     metrics={"rows_processed": 1000, "duration_ms": 2500}
            ... )
        """
        from nexus.core.ace.trajectory import TrajectoryManager

        traj_mgr = TrajectoryManager(
            self.session,
            self.backend,
            self.user_id or "system",
            self.agent_id,
            self.zone_id,
        )
        return traj_mgr.complete_trajectory(
            trajectory_id,
            status,
            success_score,
            error_message,
            metrics,
        )

    def add_feedback(
        self,
        trajectory_id: str,
        feedback_type: str,
        score: float | None = None,
        source: str | None = None,
        message: str | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> str:
        """Add feedback to a completed trajectory.

        Args:
            trajectory_id: Trajectory to add feedback to
            feedback_type: Category of feedback
            score: Revised success score (0.0-1.0)
            source: Identifier of feedback source
            message: Human-readable explanation
            metrics: Additional metrics

        Returns:
            feedback_id: ID of the feedback entry

        Example:
            >>> memory.add_feedback(
            ...     traj_id,
            ...     feedback_type="monitoring_alert",
            ...     score=0.3,
            ...     source="datadog",
            ...     message="Error rate spiked to 15%",
            ... )
        """
        from nexus.core.ace.feedback import FeedbackManager

        feedback_mgr = FeedbackManager(self.session)
        return feedback_mgr.add_feedback(
            trajectory_id,
            feedback_type,
            score,
            source,
            message,
            metrics,
        )

    def get_trajectory_feedback(
        self,
        trajectory_id: str,
    ) -> builtins.list[dict[str, Any]]:
        """Get all feedback for a trajectory.

        Returns feedback in chronological order:
        - Initial completion score
        - All subsequent feedback entries

        Args:
            trajectory_id: Trajectory ID

        Returns:
            List of feedback dicts with score, type, source, timestamp

        Example:
            >>> feedback_list = memory.get_trajectory_feedback(traj_id)
            >>> for f in feedback_list:
            ...     print(f"{f['created_at']}: {f['message']}")
        """
        from nexus.core.ace.feedback import FeedbackManager

        feedback_mgr = FeedbackManager(self.session)
        return feedback_mgr.get_trajectory_feedback(trajectory_id)

    def get_effective_score(
        self,
        trajectory_id: str,
        strategy: Literal["latest", "average", "weighted"] = "latest",
    ) -> float:
        """Get current effective score for trajectory.

        Strategies:
        - 'latest': Most recent feedback score
        - 'average': Mean of all feedback scores
        - 'weighted': Time-weighted (recent = higher weight)

        Args:
            trajectory_id: Trajectory to score
            strategy: Scoring strategy

        Returns:
            Effective score (0.0-1.0)

        Example:
            >>> score = memory.get_effective_score(traj_id, strategy="weighted")
            >>> print(f"Effective score: {score:.2f}")
        """
        from nexus.core.ace.feedback import FeedbackManager

        feedback_mgr = FeedbackManager(self.session)
        return feedback_mgr.get_effective_score(trajectory_id, strategy)

    def mark_for_relearning(
        self,
        trajectory_id: str,
        reason: str,
        priority: int = 5,
    ) -> None:
        """Flag trajectory for re-reflection.

        Used when new feedback significantly changes outcome:
        - Production failure detected
        - Human feedback indicates error
        - A/B test shows different results

        Args:
            trajectory_id: Trajectory to re-learn from
            reason: Why re-learning is needed
            priority: Urgency (1=low, 10=critical)

        Example:
            >>> memory.mark_for_relearning(
            ...     traj_id,
            ...     reason="production_failure",
            ...     priority=9
            ... )
        """
        from nexus.core.ace.feedback import FeedbackManager

        feedback_mgr = FeedbackManager(self.session)
        feedback_mgr.mark_for_relearning(trajectory_id, reason, priority)

    def batch_add_feedback(
        self,
        feedback_items: builtins.list[dict[str, Any]],
    ) -> builtins.list[str]:
        """Add feedback to multiple trajectories at once.

        Useful for:
        - Batch processing monitoring alerts
        - Bulk human feedback collection
        - A/B test result imports

        Args:
            feedback_items: List of dicts with trajectory_id, feedback_type, score, etc.

        Returns:
            List of feedback_ids

        Example:
            >>> feedback_items = [
            ...     {
            ...         "trajectory_id": "traj_1",
            ...         "feedback_type": "ab_test_result",
            ...         "score": 0.7,
            ...         "source": "ab_testing_framework",
            ...         "metrics": {"user_sat": 3.2}
            ...     },
            ...     {
            ...         "trajectory_id": "traj_2",
            ...         "feedback_type": "ab_test_result",
            ...         "score": 0.95,
            ...         "source": "ab_testing_framework",
            ...         "metrics": {"user_sat": 4.5}
            ...     }
            ... ]
            >>> feedback_ids = memory.batch_add_feedback(feedback_items)
        """
        from nexus.core.ace.feedback import FeedbackManager

        feedback_mgr = FeedbackManager(self.session)
        return feedback_mgr.batch_add_feedback(feedback_items)

    async def reflect_async(
        self,
        trajectory_id: str,
        context: str | None = None,
    ) -> dict[str, Any]:
        """Reflect on a single trajectory (async).

        Args:
            trajectory_id: Trajectory ID to reflect on
            context: Optional additional context

        Returns:
            Dictionary with reflection results:
                - helpful_strategies: Successful patterns
                - harmful_patterns: Failure patterns
                - observations: Neutral observations
                - memory_id: ID of reflection memory

        Example:
            >>> reflection = await memory.reflect_async(traj_id)
            >>> for strategy in reflection['helpful_strategies']:
            ...     print(f"✓ {strategy['description']}")
        """
        from nexus.core.ace.reflection import Reflector
        from nexus.core.ace.trajectory import TrajectoryManager

        traj_mgr = TrajectoryManager(
            self.session,
            self.backend,
            self.user_id or "system",
            self.agent_id,
            self.zone_id,
        )

        reflector = Reflector(
            self.session,
            self.backend,
            self.llm_provider,
            traj_mgr,
            self.user_id or "system",
            self.agent_id,
            self.zone_id,
        )

        return await reflector.reflect_async(trajectory_id, context)

    def reflect(
        self,
        trajectory_id: str,
        context: str | None = None,
    ) -> dict[str, Any]:
        """Reflect on a single trajectory (sync).

        Args:
            trajectory_id: Trajectory ID to reflect on
            context: Optional additional context

        Returns:
            Reflection results

        Example:
            >>> reflection = memory.reflect(traj_id)
            >>> print(reflection['helpful_strategies'])
        """
        import asyncio

        return asyncio.run(self.reflect_async(trajectory_id, context))

    async def batch_reflect_async(
        self,
        agent_id: str | None = None,
        since: str | None = None,
        min_trajectories: int = 10,
        task_type: str | None = None,
    ) -> dict[str, Any]:
        """Batch reflection across multiple trajectories (async).

        Args:
            agent_id: Filter by agent ID (defaults to current agent)
            since: ISO timestamp to filter trajectories (e.g., "2025-10-01T00:00:00Z")
            min_trajectories: Minimum trajectories needed for batch reflection
            task_type: Filter by task type

        Returns:
            Dictionary with batch reflection results:
                - trajectories_analyzed: Count
                - common_patterns: List of common successful patterns
                - common_failures: List of common failure patterns
                - reflection_ids: List of reflection memory IDs

        Example:
            >>> patterns = await memory.batch_reflect_async(
            ...     since="2025-10-01T00:00:00Z",
            ...     min_trajectories=10
            ... )
            >>> print(f"Analyzed {patterns['trajectories_analyzed']} trajectories")
        """
        from datetime import datetime

        from nexus.core.ace.reflection import Reflector
        from nexus.core.ace.trajectory import TrajectoryManager
        from nexus.storage.models import TrajectoryModel

        target_agent_id = agent_id or self.agent_id

        # Query trajectories
        query = self.session.query(TrajectoryModel).filter_by(agent_id=target_agent_id)

        if since:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            query = query.filter(TrajectoryModel.started_at >= since_dt)

        if task_type:
            query = query.filter_by(task_type=task_type)

        trajectories = query.order_by(TrajectoryModel.started_at.desc()).limit(100).all()

        if len(trajectories) < min_trajectories:
            return {
                "trajectories_analyzed": len(trajectories),
                "error": f"Need at least {min_trajectories} trajectories, found {len(trajectories)}",
                "common_patterns": [],
                "common_failures": [],
                "reflection_ids": [],
            }

        # Create managers
        traj_mgr = TrajectoryManager(
            self.session,
            self.backend,
            self.user_id or "system",
            target_agent_id,
            self.zone_id,
        )

        reflector = Reflector(
            self.session,
            self.backend,
            self.llm_provider,
            traj_mgr,
            self.user_id or "system",
            target_agent_id,
            self.zone_id,
        )

        # Reflect on each trajectory
        all_helpful = []
        all_harmful = []
        reflection_ids = []

        for traj in trajectories:
            try:
                # If no LLM provider, use fallback reflection
                if self.llm_provider is None:
                    # Use direct fallback instead of async call
                    trajectory_data = traj_mgr.get_trajectory(traj.trajectory_id)
                    if trajectory_data:
                        reflection_data = reflector._create_fallback_reflection(trajectory_data)
                        memory_id = reflector._store_reflection(traj.trajectory_id, reflection_data)
                        reflection = {
                            "memory_id": memory_id,
                            "helpful_strategies": reflection_data.get("helpful_strategies", []),
                            "harmful_patterns": reflection_data.get("harmful_patterns", []),
                        }
                    else:
                        continue
                else:
                    reflection = await reflector.reflect_async(traj.trajectory_id)

                all_helpful.extend(reflection.get("helpful_strategies", []))
                all_harmful.extend(reflection.get("harmful_patterns", []))
                reflection_ids.append(reflection.get("memory_id"))
            except Exception:
                # Skip failed reflections
                continue

        # Aggregate common patterns (simple frequency analysis)
        pattern_freq: dict[str, int] = {}
        for strategy in all_helpful:
            desc = strategy.get("description", "")
            pattern_freq[desc] = pattern_freq.get(desc, 0) + 1

        failure_freq: dict[str, int] = {}
        for pattern in all_harmful:
            desc = pattern.get("description", "")
            failure_freq[desc] = failure_freq.get(desc, 0) + 1

        # Get top patterns (appearing in 20%+ of trajectories)
        threshold = len(trajectories) * 0.2
        common_patterns = [
            {"description": desc, "frequency": count}
            for desc, count in pattern_freq.items()
            if count >= threshold
        ]
        common_failures = [
            {"description": desc, "frequency": count}
            for desc, count in failure_freq.items()
            if count >= threshold
        ]

        return {
            "trajectories_analyzed": len(trajectories),
            "common_patterns": sorted(common_patterns, key=lambda x: x["frequency"], reverse=True),
            "common_failures": sorted(common_failures, key=lambda x: x["frequency"], reverse=True),
            "reflection_ids": [rid for rid in reflection_ids if rid],
        }

    def batch_reflect(
        self,
        agent_id: str | None = None,
        since: str | None = None,
        min_trajectories: int = 10,
        task_type: str | None = None,
    ) -> dict[str, Any]:
        """Batch reflection across multiple trajectories (sync).

        Args:
            agent_id: Filter by agent ID
            since: ISO timestamp to filter trajectories
            min_trajectories: Minimum trajectories needed
            task_type: Filter by task type

        Returns:
            Batch reflection results
        """
        import asyncio

        return asyncio.run(self.batch_reflect_async(agent_id, since, min_trajectories, task_type))

    def get_playbook(self, playbook_name: str = "default") -> dict[str, Any] | None:
        """Get agent's playbook.

        Args:
            playbook_name: Playbook name (default: "default")

        Returns:
            Playbook dict with strategies, or None if not found

        Example:
            >>> playbook = memory.get_playbook("default")
            >>> if playbook:
            ...     print(f"Version: {playbook['version']}")
            ...     for strategy in playbook['content']['strategies']:
            ...         print(f"  {strategy['type']}: {strategy['description']}")
        """
        from nexus.core.ace.playbook import PlaybookManager

        playbook_mgr = PlaybookManager(
            self.session,
            self.backend,
            self.user_id or "system",
            self.agent_id,
            self.zone_id,
        )

        # Query by name and agent_id
        playbooks = playbook_mgr.query_playbooks(
            agent_id=self.agent_id,
            name_pattern=playbook_name,
            limit=1,
        )

        if not playbooks:
            return None

        # Get full playbook with content
        return playbook_mgr.get_playbook(playbooks[0]["playbook_id"])

    def update_playbook(
        self,
        strategies: builtins.list[dict[str, Any]],
        playbook_name: str = "default",
    ) -> dict[str, Any]:
        """Update playbook with new strategies.

        Args:
            strategies: List of strategy dicts with:
                - category: 'helpful', 'harmful', or 'neutral'
                - pattern: Strategy description
                - context: Context where it applies
                - confidence: Confidence score (0.0-1.0)
            playbook_name: Playbook name (default: "default")

        Returns:
            Update result with playbook_id and strategies_added

        Example:
            >>> memory.update_playbook([
            ...     {
            ...         'category': 'helpful',
            ...         'pattern': 'Always validate input before processing',
            ...         'context': 'Data processing tasks',
            ...         'confidence': 0.9
            ...     }
            ... ])
        """
        from nexus.core.ace.playbook import PlaybookManager

        playbook_mgr = PlaybookManager(
            self.session,
            self.backend,
            self.user_id or "system",
            self.agent_id,
            self.zone_id,
        )

        # Get or create playbook
        playbook = self.get_playbook(playbook_name)
        if not playbook:
            # Create new playbook
            playbook_id = playbook_mgr.create_playbook(
                name=playbook_name,
                description=f"Playbook for {self.agent_id or 'agent'}",
                scope="agent",
            )
        else:
            playbook_id = playbook["playbook_id"]

        # Convert strategies to ACE format
        ace_strategies = []
        for s in strategies:
            ace_strategies.append(
                {
                    "type": s.get("category", "neutral"),  # helpful/harmful/neutral
                    "description": s.get("pattern", ""),
                    "evidence": s.get("context", ""),
                    "confidence": s.get("confidence", 0.5),
                }
            )

        # Update playbook
        playbook_mgr.update_playbook(playbook_id, strategies=ace_strategies)

        return {
            "playbook_id": playbook_id,
            "strategies_added": len(ace_strategies),
        }

    def curate_playbook(
        self,
        reflections: builtins.list[str],
        playbook_name: str = "default",
    ) -> dict[str, Any]:
        """Auto-curate playbook from reflection memories.

        Args:
            reflections: List of reflection memory IDs
            playbook_name: Playbook name (default: "default")

        Returns:
            Curation result with strategies_added and strategies_merged

        Example:
            >>> result = memory.curate_playbook(
            ...     reflections=["mem_123", "mem_456"],
            ...     playbook_name="default"
            ... )
            >>> print(f"Added {result['strategies_added']} new strategies")
        """
        from nexus.core.ace.curation import Curator
        from nexus.core.ace.playbook import PlaybookManager

        playbook_mgr = PlaybookManager(
            self.session,
            self.backend,
            self.user_id or "system",
            self.agent_id,
            self.zone_id,
        )

        curator = Curator(self.session, self.backend, playbook_mgr)

        # Get or create playbook
        playbook = self.get_playbook(playbook_name)
        if not playbook:
            playbook_id = playbook_mgr.create_playbook(
                name=playbook_name,
                description=f"Playbook for {self.agent_id or 'agent'}",
                scope="agent",
            )
        else:
            playbook_id = playbook["playbook_id"]

        # Curate
        return curator.curate_playbook(playbook_id, reflections)

    async def consolidate_async(
        self,
        memory_type: str | None = None,
        scope: str | None = None,
        namespace: str | None = None,  # v0.8.0: Exact namespace
        namespace_prefix: str | None = None,  # v0.8.0: Namespace prefix
        preserve_high_importance: bool = True,
        importance_threshold: float = 0.8,
    ) -> dict[str, Any]:
        """Consolidate memories to prevent context collapse (async).

        Args:
            memory_type: Filter by memory type (e.g., 'experience', 'reflection')
            scope: Filter by scope (e.g., 'agent', 'user')
            namespace: Filter by exact namespace match. v0.8.0
            namespace_prefix: Filter by namespace prefix. v0.8.0
            preserve_high_importance: Keep high-importance memories unconsolidated
            importance_threshold: Threshold for high importance (0.0-1.0)

        Returns:
            Consolidation report with:
                - memories_consolidated: Count
                - consolidations_created: Count
                - space_saved: Approximate reduction

        Examples:
            >>> # Consolidate by namespace
            >>> report = await memory.consolidate_async(
            ...     namespace="knowledge/geography/facts",
            ...     importance_threshold=0.8
            ... )

            >>> # Consolidate all under prefix
            >>> report = await memory.consolidate_async(
            ...     namespace_prefix="knowledge/",
            ...     importance_threshold=0.5
            ... )
        """
        from nexus.core.ace.consolidation import ConsolidationEngine

        consolidation_engine = ConsolidationEngine(
            self.session,
            self.backend,
            self.llm_provider,
            self.user_id or "system",
            self.agent_id,
            self.zone_id,
        )

        # Determine max importance for consolidation
        max_importance = importance_threshold if preserve_high_importance else 1.0

        # Consolidate
        results = consolidation_engine.consolidate_by_criteria(
            memory_type=memory_type,
            scope=scope,
            namespace=namespace,
            namespace_prefix=namespace_prefix,
            importance_max=max_importance,
            batch_size=10,
            limit=100,
        )

        # Calculate stats
        total_consolidated = sum(r.get("memories_consolidated", 0) for r in results)
        total_created = len(results)

        return {
            "memories_consolidated": total_consolidated,
            "consolidations_created": total_created,
            "space_saved": total_consolidated - total_created,  # Approximate
        }

    def consolidate(
        self,
        memory_type: str | None = None,
        scope: str | None = None,
        namespace: str | None = None,  # v0.8.0: Exact namespace
        namespace_prefix: str | None = None,  # v0.8.0: Namespace prefix
        preserve_high_importance: bool = True,
        importance_threshold: float = 0.8,
    ) -> dict[str, Any]:
        """Consolidate memories to prevent context collapse (sync).

        Args:
            memory_type: Filter by memory type
            scope: Filter by scope
            namespace: Filter by exact namespace match. v0.8.0
            namespace_prefix: Filter by namespace prefix. v0.8.0
            preserve_high_importance: Keep high-importance memories
            importance_threshold: Threshold for high importance

        Returns:
            Consolidation report
        """
        import asyncio

        return asyncio.run(
            self.consolidate_async(
                memory_type,
                scope,
                namespace,
                namespace_prefix,
                preserve_high_importance,
                importance_threshold,
            )
        )

    async def execute_with_learning_async(
        self,
        task_fn: Any,
        task_description: str,
        task_type: str | None = None,
        auto_reflect: bool = True,
        auto_curate: bool = True,
        playbook_name: str = "default",
        **task_kwargs: Any,
    ) -> tuple[Any, str]:
        """Execute with automatic trajectory tracking + reflection + curation (async).

        Args:
            task_fn: Async function to execute
            task_description: Description of the task
            task_type: Optional task type
            auto_reflect: Automatically reflect on outcome (default True)
            auto_curate: Automatically curate playbook (default True)
            playbook_name: Playbook to curate (default "default")
            **task_kwargs: Arguments to pass to task_fn

        Returns:
            Tuple of (task_result, trajectory_id)

        Example:
            >>> async def process_data(filename):
            ...     # Process the data
            ...     return {"rows": 1000}
            >>>
            >>> result, traj_id = await memory.execute_with_learning_async(
            ...     process_data,
            ...     "Process customer orders",
            ...     auto_reflect=True,
            ...     auto_curate=True,
            ...     filename="orders.csv"
            ... )
        """
        from nexus.core.ace.learning_loop import LearningLoop

        learning_loop = LearningLoop(
            self.session,
            self.backend,
            self.llm_provider,
            self.user_id or "system",
            self.agent_id,
            self.zone_id,
        )

        # Get or create playbook for curation
        playbook_id = None
        if auto_curate:
            playbook = self.get_playbook(playbook_name)
            if playbook:
                playbook_id = playbook["playbook_id"]

        # Execute with learning
        execution_result = await learning_loop.execute_with_learning_async(
            task_description=task_description,
            task_fn=task_fn,
            task_type=task_type,
            playbook_id=playbook_id,
            enable_reflection=auto_reflect,
            enable_curation=auto_curate,
            **task_kwargs,
        )

        return (execution_result["result"], execution_result["trajectory_id"])

    def execute_with_learning(
        self,
        task_fn: Any,
        task_description: str,
        task_type: str | None = None,
        auto_reflect: bool = True,
        auto_curate: bool = True,
        playbook_name: str = "default",
        **task_kwargs: Any,
    ) -> tuple[Any, str]:
        """Execute with automatic learning (sync).

        Args:
            task_fn: Function to execute (can be sync or async)
            task_description: Description of the task
            task_type: Optional task type
            auto_reflect: Automatically reflect
            auto_curate: Automatically curate playbook
            playbook_name: Playbook to curate
            **task_kwargs: Arguments to pass to task_fn

        Returns:
            Tuple of (task_result, trajectory_id)
        """
        import asyncio

        return asyncio.run(
            self.execute_with_learning_async(
                task_fn,
                task_description,
                task_type,
                auto_reflect,
                auto_curate,
                playbook_name,
                **task_kwargs,
            )
        )

    def query_trajectories(
        self,
        agent_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> builtins.list[dict[str, Any]]:
        """Query execution trajectories.

        Args:
            agent_id: Filter by agent ID (defaults to current agent)
            status: Filter by status (e.g., 'success', 'failure', 'partial')
            limit: Maximum number of results

        Returns:
            List of trajectory dictionaries

        Example:
            >>> trajectories = memory.query_trajectories(status="success", limit=10)
            >>> for traj in trajectories:
            ...     print(f"{traj['trajectory_id']}: {traj['task_description']}")
        """
        from nexus.core.ace.trajectory import TrajectoryManager

        target_agent_id = agent_id or self.agent_id

        traj_mgr = TrajectoryManager(
            self.session,
            self.backend,
            self.user_id or "system",
            target_agent_id,
            self.zone_id,
        )

        return traj_mgr.query_trajectories(
            agent_id=target_agent_id,
            status=status,
            limit=limit,
        )

    def query_playbooks(
        self,
        agent_id: str | None = None,
        scope: str | None = None,
        limit: int = 50,
    ) -> builtins.list[dict[str, Any]]:
        """Query playbooks.

        Args:
            agent_id: Filter by agent ID (defaults to current agent)
            scope: Filter by scope (e.g., 'agent', 'user', 'global')
            limit: Maximum number of results

        Returns:
            List of playbook dictionaries

        Example:
            >>> playbooks = memory.query_playbooks(scope="agent", limit=10)
            >>> for pb in playbooks:
            ...     print(f"{pb['name']}: v{pb['version']}")
        """
        from nexus.core.ace.playbook import PlaybookManager

        target_agent_id = agent_id or self.agent_id

        playbook_mgr = PlaybookManager(
            self.session,
            self.backend,
            self.user_id or "system",
            target_agent_id,
            self.zone_id,
        )

        return playbook_mgr.query_playbooks(
            agent_id=target_agent_id,
            scope=scope,
            limit=limit,
        )

    def process_relearning(
        self,
        limit: int = 10,
    ) -> builtins.list[dict[str, Any]]:
        """Process trajectories flagged for re-learning.

        This processes trajectories that have received feedback after completion,
        re-reflecting on them with updated scores to improve agent learning.

        Args:
            limit: Maximum number of trajectories to process

        Returns:
            List of re-learning results with trajectory_id, success, and reflection_id/error

        Example:
            >>> results = memory.process_relearning(limit=5)
            >>> for result in results:
            ...     if result['success']:
            ...         print(f"Re-learned {result['trajectory_id']}")
        """
        from nexus.core.ace.learning_loop import LearningLoop

        # Initialize learning loop
        learning_loop = LearningLoop(
            session=self.session,
            backend=self.backend,
            user_id=self.user_id or "system",
            agent_id=self.agent_id,
            zone_id=self.zone_id,
            llm_provider=self.llm_provider,
        )

        # Process relearning queue
        return learning_loop.process_relearning_queue(limit)

    async def index_memories_async(
        self,
        embedding_provider: Any | None = None,
        batch_size: int = 10,
        memory_type: str | None = None,
        scope: str | None = None,
    ) -> dict[str, Any]:
        """Generate embeddings for existing memories that don't have them (#406).

        Args:
            embedding_provider: Optional embedding provider (uses OpenRouter by default)
            batch_size: Number of memories to process in each batch
            memory_type: Filter by memory type
            scope: Filter by scope

        Returns:
            Dictionary with indexing results:
                - total_processed: Total memories processed
                - success_count: Number successfully indexed
                - error_count: Number of errors
                - skipped_count: Number skipped (already have embeddings)

        Example:
            >>> result = await memory.index_memories_async()
            >>> print(f"Indexed {result['success_count']} memories")
        """
        import json

        from sqlalchemy import select

        from nexus.storage.models import MemoryModel

        # Try to get embedding provider
        if embedding_provider is None:
            try:
                from nexus.search.embeddings import create_embedding_provider

                embedding_provider = create_embedding_provider(provider="openrouter")
            except Exception as e:
                raise ValueError(
                    f"Failed to create embedding provider: {e}. "
                    "Please provide an embedding provider or set OPENROUTER_API_KEY env var."
                ) from e

        # Query memories without embeddings
        stmt = select(MemoryModel).where(MemoryModel.embedding.is_(None))

        if memory_type:
            stmt = stmt.where(MemoryModel.memory_type == memory_type)
        if scope:
            stmt = stmt.where(MemoryModel.scope == scope)

        # Filter by zone/user/agent
        if self.zone_id:
            stmt = stmt.where(MemoryModel.zone_id == self.zone_id)
        if self.user_id:
            stmt = stmt.where(MemoryModel.user_id == self.user_id)
        if self.agent_id:
            stmt = stmt.where(MemoryModel.agent_id == self.agent_id)

        with self.session as session:
            result = session.execute(stmt)
            memories_to_index = result.scalars().all()

            total_processed = 0
            success_count = 0
            error_count = 0
            skipped_count = 0

            # Process in batches
            for i in range(0, len(memories_to_index), batch_size):
                batch = memories_to_index[i : i + batch_size]

                for memory in batch:
                    total_processed += 1

                    # Skip if already has embedding
                    if memory.embedding:
                        skipped_count += 1
                        continue

                    try:
                        # Read content
                        content_bytes = self.backend.read_content(
                            memory.content_hash, context=self.context
                        ).unwrap()
                        content = content_bytes.decode("utf-8")

                        # Generate embedding
                        embedding_vec = await embedding_provider.embed_text(content)
                        embedding_json = json.dumps(embedding_vec)

                        # Update memory with embedding
                        memory.embedding = embedding_json
                        memory.embedding_model = getattr(embedding_provider, "model", "unknown")
                        memory.embedding_dim = len(embedding_vec)

                        success_count += 1
                    except Exception:
                        # Failed to generate embedding for this memory
                        error_count += 1
                        continue

                # Commit batch
                session.commit()

            return {
                "total_processed": total_processed,
                "success_count": success_count,
                "error_count": error_count,
                "skipped_count": skipped_count,
            }

    def index_memories(
        self,
        embedding_provider: Any | None = None,
        batch_size: int = 10,
        memory_type: str | None = None,
        scope: str | None = None,
    ) -> dict[str, Any]:
        """Generate embeddings for existing memories (sync version).

        Args:
            embedding_provider: Optional embedding provider
            batch_size: Number of memories to process in each batch
            memory_type: Filter by memory type
            scope: Filter by scope

        Returns:
            Dictionary with indexing results

        Example:
            >>> result = memory.index_memories()
            >>> print(f"Indexed {result['success_count']} memories")
        """
        import asyncio

        return asyncio.run(
            self.index_memories_async(embedding_provider, batch_size, memory_type, scope)
        )

    # ========== Version Tracking Methods (#1184) ==========

    def _get_chain_memory_ids(self, memory_id: str) -> builtins.list[str]:
        """Get all memory IDs in the supersedes chain (#1188).

        Walks backward to find the oldest ancestor, then forward to collect all IDs.

        Args:
            memory_id: Any memory ID in the chain.

        Returns:
            List of all memory IDs in the chain (oldest to newest).
        """
        # Use _get_memory_by_id_raw to include soft-deleted memories in the chain,
        # since version history must persist for audit trail purposes (#1188).
        memory = self.memory_router._get_memory_by_id_raw(memory_id)
        if not memory:
            return [memory_id]

        # Walk backward to oldest ancestor
        current = memory
        while current.supersedes_id:
            ancestor = self.memory_router._get_memory_by_id_raw(current.supersedes_id)
            if ancestor is None:
                break
            current = ancestor

        # Walk forward collecting all IDs
        chain_ids = []
        visited = set()
        node = current
        while node and node.memory_id not in visited:
            visited.add(node.memory_id)
            chain_ids.append(node.memory_id)
            if node.superseded_by_id:
                next_node = self.memory_router._get_memory_by_id_raw(node.superseded_by_id)
                if next_node is None:
                    break
                node = next_node
            else:
                break

        return chain_ids

    def list_versions(self, memory_id: str) -> builtins.list[dict[str, Any]]:
        """List all versions of a memory.

        Returns version history with metadata for each version, ordered by
        version number (newest first). Follows the supersedes chain (#1188)
        to collect versions across all memory rows.

        Args:
            memory_id: The memory ID to get versions for.

        Returns:
            List of version info dicts with keys:
            - version: Version number
            - content_hash: SHA-256 hash for CAS retrieval
            - size: Content size in bytes
            - created_at: Timestamp when version was created
            - created_by: User/agent who created it
            - source_type: How version was created ('original', 'update', 'rollback')
            - change_reason: Why this version was created
            - parent_version_id: ID of the previous version

        Example:
            >>> versions = memory.list_versions("mem_123")
            >>> for v in versions:
            ...     print(f"v{v['version']}: {v['size']} bytes")
        """
        from sqlalchemy import select

        from nexus.storage.models import VersionHistoryModel

        # #1188: Collect all memory IDs in the supersedes chain
        chain_ids = self._get_chain_memory_ids(memory_id)

        # Query all versions across the entire chain
        stmt = (
            select(VersionHistoryModel)
            .where(
                VersionHistoryModel.resource_type == "memory",
                VersionHistoryModel.resource_id.in_(chain_ids),
            )
            .order_by(VersionHistoryModel.version_number.desc())
        )

        versions = []
        for v in self.session.scalars(stmt):
            versions.append(
                {
                    "version": v.version_number,
                    "content_hash": v.content_hash,
                    "size": v.size_bytes,
                    "mime_type": v.mime_type,
                    "created_at": v.created_at.isoformat() if v.created_at else None,
                    "created_by": v.created_by,
                    "change_reason": v.change_reason,
                    "source_type": v.source_type,
                    "parent_version_id": v.parent_version_id,
                }
            )

        return versions

    def get_version(
        self,
        memory_id: str,
        version: int,
        context: OperationContext | None = None,
    ) -> dict[str, Any] | None:
        """Retrieve a specific version of a memory.

        Fetches the content and metadata for a specific historical version
        of a memory using CAS storage.

        Args:
            memory_id: The memory ID.
            version: Version number to retrieve (1-indexed).
            context: Optional operation context for permission checks.

        Returns:
            Memory dictionary with content at specified version, or None
            if memory or version not found.

        Example:
            >>> # Get version 1 of a memory
            >>> v1 = memory.get_version("mem_123", version=1)
            >>> print(v1['content'])
        """
        from sqlalchemy import select

        from nexus.storage.models import VersionHistoryModel

        # Check if memory exists and we have permission
        memory = self.memory_router.get_memory_by_id(memory_id)
        if not memory:
            return None

        check_context = context or self.context
        if not self.permission_enforcer.check_memory(memory, Permission.READ, check_context):
            return None

        # #1188: Search across the supersedes chain for the version
        chain_ids = self._get_chain_memory_ids(memory_id)

        # Get the specific version entry from any memory in the chain
        stmt = select(VersionHistoryModel).where(
            VersionHistoryModel.resource_type == "memory",
            VersionHistoryModel.resource_id.in_(chain_ids),
            VersionHistoryModel.version_number == version,
        )
        version_entry = self.session.scalar(stmt)

        if not version_entry:
            return None

        # Read content from CAS using version's content_hash
        content = None
        try:
            content_bytes = self.backend.read_content(
                version_entry.content_hash, context=self.context
            ).unwrap()
            try:
                content = content_bytes.decode("utf-8")
            except UnicodeDecodeError:
                content = content_bytes.hex()
        except Exception:
            content = f"<content not available: {version_entry.content_hash}>"

        return {
            "memory_id": memory_id,
            "version": version_entry.version_number,
            "content": content,
            "content_hash": version_entry.content_hash,
            "size": version_entry.size_bytes,
            "mime_type": version_entry.mime_type,
            "created_at": version_entry.created_at.isoformat()
            if version_entry.created_at
            else None,
            "created_by": version_entry.created_by,
            "source_type": version_entry.source_type,
            "change_reason": version_entry.change_reason,
        }

    def rollback(
        self,
        memory_id: str,
        version: int,
        context: OperationContext | None = None,
    ) -> None:
        """Rollback a memory to a previous version.

        Restores the memory content to a specific historical version.
        Creates a new version entry with source_type='rollback' to maintain
        audit trail.

        Args:
            memory_id: The memory ID to rollback.
            version: Version number to rollback to.
            context: Optional operation context for permission checks.

        Raises:
            ValueError: If memory or version not found, or no permission.

        Example:
            >>> # Rollback to version 1
            >>> memory.rollback("mem_123", version=1)
        """
        from sqlalchemy import select, update

        from nexus.storage.models import MemoryModel, VersionHistoryModel

        # Check if memory exists and we have write permission
        memory = self.memory_router.get_memory_by_id(memory_id)
        if not memory:
            raise ValueError(f"Memory not found: {memory_id}")

        check_context = context or self.context
        if not self.permission_enforcer.check_memory(memory, Permission.WRITE, check_context):
            raise ValueError(f"No permission to rollback memory: {memory_id}")

        # #1188: Search across the supersedes chain for the version
        chain_ids = self._get_chain_memory_ids(memory_id)

        # #1188: Find the latest (current) memory in the chain for rollback
        latest_memory = self.memory_router.get_memory_by_id(chain_ids[-1])
        if latest_memory is None:
            latest_memory = memory
        latest_memory_id = latest_memory.memory_id

        target_stmt = select(VersionHistoryModel).where(
            VersionHistoryModel.resource_type == "memory",
            VersionHistoryModel.resource_id.in_(chain_ids),
            VersionHistoryModel.version_number == version,
        )
        target_version = self.session.scalar(target_stmt)

        if not target_version:
            raise ValueError(f"Version {version} not found for memory {memory_id}")

        # Get current version entry for lineage
        current_stmt = select(VersionHistoryModel).where(
            VersionHistoryModel.resource_type == "memory",
            VersionHistoryModel.resource_id.in_(chain_ids),
            VersionHistoryModel.version_number == latest_memory.current_version,
        )
        current_version_entry = self.session.scalar(current_stmt)
        parent_version_id = current_version_entry.version_id if current_version_entry else None

        # Update the latest memory to target version's content
        latest_memory.content_hash = target_version.content_hash
        latest_memory.updated_at = datetime.now(UTC)

        # Atomically increment version at database level on the latest memory
        self.session.execute(
            update(MemoryModel)
            .where(MemoryModel.memory_id == latest_memory_id)
            .values(current_version=MemoryModel.current_version + 1)
        )
        self.session.refresh(latest_memory)

        # Create version history entry for the rollback on the latest memory
        self.memory_router._create_version_entry(
            memory_id=latest_memory_id,
            content_hash=target_version.content_hash,
            size_bytes=target_version.size_bytes,
            version_number=latest_memory.current_version,
            source_type="rollback",
            parent_version_id=parent_version_id,
            change_reason=f"Rollback to version {version}",
            created_by=check_context.user if check_context else None,
        )

        self.session.commit()

    def diff_versions(
        self,
        memory_id: str,
        v1: int,
        v2: int,
        mode: Literal["metadata", "content"] = "metadata",
        context: OperationContext | None = None,
    ) -> dict[str, Any] | str:
        """Compare two versions of a memory.

        Args:
            memory_id: The memory ID.
            v1: First version number.
            v2: Second version number.
            mode: Diff mode - "metadata" returns size/hash comparison,
                  "content" returns unified diff format.
            context: Optional operation context for permission checks.

        Returns:
            For mode="metadata": Dict with version comparison info.
            For mode="content": String in unified diff format.

        Raises:
            ValueError: If memory or versions not found, or no permission.

        Example:
            >>> # Compare metadata between versions
            >>> diff = memory.diff_versions("mem_123", v1=1, v2=2)
            >>> print(f"Content changed: {diff['content_changed']}")

            >>> # Get content diff
            >>> diff_text = memory.diff_versions("mem_123", v1=1, v2=2, mode="content")
            >>> print(diff_text)
        """
        import difflib

        from sqlalchemy import select

        from nexus.storage.models import VersionHistoryModel

        # Check if memory exists and we have permission
        memory = self.memory_router.get_memory_by_id(memory_id)
        if not memory:
            raise ValueError(f"Memory not found: {memory_id}")

        check_context = context or self.context
        if not self.permission_enforcer.check_memory(memory, Permission.READ, check_context):
            raise ValueError(f"No permission to diff memory: {memory_id}")

        # #1188: Search across the supersedes chain for versions
        chain_ids = self._get_chain_memory_ids(memory_id)

        stmt = select(VersionHistoryModel).where(
            VersionHistoryModel.resource_type == "memory",
            VersionHistoryModel.resource_id.in_(chain_ids),
            VersionHistoryModel.version_number.in_([v1, v2]),
        )

        versions_dict = {v.version_number: v for v in self.session.scalars(stmt)}

        if v1 not in versions_dict:
            raise ValueError(f"Version {v1} not found for memory {memory_id}")
        if v2 not in versions_dict:
            raise ValueError(f"Version {v2} not found for memory {memory_id}")

        version1 = versions_dict[v1]
        version2 = versions_dict[v2]

        if mode == "metadata":
            return {
                "memory_id": memory_id,
                "v1": v1,
                "v2": v2,
                "content_hash_v1": version1.content_hash,
                "content_hash_v2": version2.content_hash,
                "content_changed": version1.content_hash != version2.content_hash,
                "size_v1": version1.size_bytes,
                "size_v2": version2.size_bytes,
                "size_delta": version2.size_bytes - version1.size_bytes,
                "created_at_v1": version1.created_at.isoformat() if version1.created_at else None,
                "created_at_v2": version2.created_at.isoformat() if version2.created_at else None,
            }
        else:
            # Content diff mode - read both versions and compute unified diff
            try:
                content1_bytes = self.backend.read_content(
                    version1.content_hash, context=self.context
                ).unwrap()
                content1 = content1_bytes.decode("utf-8")
            except Exception:
                content1 = f"<content not available: {version1.content_hash}>"

            try:
                content2_bytes = self.backend.read_content(
                    version2.content_hash, context=self.context
                ).unwrap()
                content2 = content2_bytes.decode("utf-8")
            except Exception:
                content2 = f"<content not available: {version2.content_hash}>"

            # Generate unified diff
            diff_lines = difflib.unified_diff(
                content1.splitlines(keepends=True),
                content2.splitlines(keepends=True),
                fromfile=f"version {v1}",
                tofile=f"version {v2}",
            )
            return "".join(diff_lines)
