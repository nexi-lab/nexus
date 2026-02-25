"""Memory API for AI Agent Memory Management (v0.4.0).

High-level API for storing, querying, and searching agent memories
with identity-based relationships and semantic search.

Includes temporal query operators (Issue #1023) for time-based filtering
inspired by SimpleMem (arXiv:2601.02553).
"""

import builtins
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy.orm import Session

from nexus.bricks.memory._temporal import parse_datetime, validate_temporal_params
from nexus.bricks.memory.router import MemoryViewRouter
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.types import OperationContext, Permission

logger = logging.getLogger(__name__)

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
        entity_registry: Any = None,
        llm_provider: Any = None,
        permission_enforcer: Any = None,
        embedding_provider: Any = None,
        graph_store_class: type[Any] | None = None,
    ):
        """Initialize Memory API.

        Args:
            session: Database session.
            backend: Storage backend for content.
            zone_id: Current zone ID.
            user_id: Current user ID.
            agent_id: Current agent ID.
            entity_registry: Entity registry instance (MemoryEntityRegistryProtocol).
            llm_provider: Optional LLM provider for reflection/learning.
            permission_enforcer: Optional permission enforcer (MemoryPermissionProtocol).
            embedding_provider: Optional embedding provider for semantic search (DI).
            graph_store_class: Optional GraphStore class for entity/relationship storage (DI).
        """
        self.session = session
        self.backend = backend
        self.zone_id = zone_id
        self.user_id = user_id
        self.agent_id = agent_id
        self.llm_provider = llm_provider
        self._embedding_provider = embedding_provider
        self._graph_store_class = graph_store_class

        # Initialize components — accept injected deps or fall back
        if entity_registry is not None:
            self.entity_registry = entity_registry
        else:
            import importlib
            from types import SimpleNamespace

            _er_mod = importlib.import_module("nexus.bricks.rebac.entity_registry")
            _EntityRegistry = _er_mod.EntityRegistry

            self.entity_registry = _EntityRegistry(SimpleNamespace(session_factory=lambda: session))

        self.memory_router = MemoryViewRouter(session, self.entity_registry)

        # Permission enforcer — injected or constructed from ReBAC
        if permission_enforcer is not None:
            self.permission_enforcer = permission_enforcer
        else:
            import importlib

            from sqlalchemy import Engine

            _rebac_mod = importlib.import_module("nexus.bricks.rebac.manager")
            _ReBACManager = _rebac_mod.ReBACManager
            _enforcer_mod = importlib.import_module("nexus.bricks.rebac.memory_permission_enforcer")
            _MemoryPermissionEnforcer = _enforcer_mod.MemoryPermissionEnforcer

            bind = session.get_bind()
            assert isinstance(bind, Engine), "Expected Engine, got Connection"
            rebac_manager = _ReBACManager(
                bind,
                is_postgresql=(bind.dialect.name == "postgresql"),
            )
            self.permission_enforcer = _MemoryPermissionEnforcer(
                memory_router=self.memory_router,
                entity_registry=self.entity_registry,
                rebac_manager=rebac_manager,
            )

        # Create operation context
        self.context = OperationContext(
            user_id=agent_id or user_id or "system",
            groups=[],
            is_admin=False,
        )

        # Composed services (#1498)
        from nexus.bricks.memory.ace_facade import AceFacade
        from nexus.bricks.memory.state import MemoryStateManager
        from nexus.bricks.memory.versioning import MemoryVersioning

        self._versioning = MemoryVersioning(
            session_factory=lambda: session,
            memory_router=self.memory_router,
            permission_enforcer=self.permission_enforcer,
            backend=backend,
            context=self.context,
        )
        self._state = MemoryStateManager(
            memory_router=self.memory_router,
            permission_enforcer=self.permission_enforcer,
            context=self.context,
        )
        self._ace = AceFacade(
            session=session,
            backend=backend,
            llm_provider=llm_provider,
            user_id=user_id or "system",
            agent_id=agent_id,
            zone_id=zone_id,
        )

    @staticmethod
    def _get_text_content(content: str | bytes | dict[str, Any]) -> str | None:
        """Extract text from content for enrichment pipeline.

        Args:
            content: Memory content (text, bytes, or dict).

        Returns:
            Text string suitable for NLP processing, or None for binary content.
        """
        import json

        if isinstance(content, dict):
            return json.dumps(content)
        elif isinstance(content, str):
            return content if content.strip() else None
        return None

    def _read_content(self, content_hash: str, *, parse_json: bool = False) -> str | dict[str, Any]:
        """Read and decode content from CAS backend (#1498 DRY helper).

        Centralises the content-read-and-decode pattern used by get(), query(),
        search(), retrieve(), get_history(), get_version(), and diff_versions().

        Args:
            content_hash: CAS content hash to read.
            parse_json: If True, attempt to parse UTF-8 content as JSON first.

        Returns:
            Decoded string, parsed dict (if parse_json), or hex for binary.
            Falls back to a placeholder string on read failure.
        """
        import json as _json

        try:
            content_bytes: bytes = self.backend.read_content(content_hash, context=self.context)
            if parse_json:
                try:
                    parsed: dict[str, Any] = _json.loads(content_bytes.decode("utf-8"))
                    return parsed
                except (_json.JSONDecodeError, UnicodeDecodeError):
                    pass
            try:
                return content_bytes.decode("utf-8")
            except UnicodeDecodeError:
                return content_bytes.hex()
        except Exception as e:
            logger.debug("Failed to read memory content %s: %s", content_hash, e)
            return f"<content not available: {content_hash}>"

    def _batch_operation(
        self,
        memory_ids: list[str],
        operation: Callable[[str], bool],
        success_key: str = "success",
    ) -> dict[str, Any]:
        """Generic batch operation helper (#1498 DRY helper).

        Applies a single-item operation to each ID and collects results.

        Args:
            memory_ids: List of memory IDs to operate on.
            operation: Callable taking a memory_id and returning bool.
            success_key: Name for the success count in the result dict
                (e.g., 'approved', 'deleted', 'deactivated', 'invalidated').

        Returns:
            Dict with success/failure counts and ID lists.
        """
        success_ids: list[str] = []
        failed_ids: list[str] = []

        for memory_id in memory_ids:
            if operation(memory_id):
                success_ids.append(memory_id)
            else:
                failed_ids.append(memory_id)

        return {
            success_key: len(success_ids),
            "failed": len(failed_ids),
            f"{success_key}_ids": success_ids,
            "failed_ids": failed_ids,
        }

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
        classify_stability: bool = True,  # #1191: Auto-classify temporal stability
        detect_evolution: bool = False,  # #1190: Detect memory evolution relationships (opt-in)
    ) -> str:
        """Store a memory.

        Args:
            content: Memory content (text, bytes, or structured dict).
            scope: Memory scope ('agent', 'user', 'zone', 'global').
            memory_type: Type of memory ('fact', 'preference', 'experience'). Optional if using namespace structure.
            importance: Importance score (0.0-1.0).
            namespace: Hierarchical namespace for organization (e.g., "knowledge/geography/facts"). v0.8.0
            path_key: Optional unique key within namespace for upsert mode. v0.8.0
            state: Memory state ('inactive', 'active'). Defaults to 'active'. #368
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

        from nexus.bricks.memory.enrichment import EnrichmentFlags, EnrichmentPipeline

        # Build enrichment flags from store() parameters
        enrichment_flags = EnrichmentFlags(
            generate_embedding=generate_embedding,
            extract_entities=extract_entities,
            extract_temporal=extract_temporal,
            extract_relationships=extract_relationships,
            classify_stability=classify_stability,
            detect_evolution=detect_evolution,
            resolve_coreferences=resolve_coreferences,
            resolve_temporal=resolve_temporal,
            store_to_graph=store_to_graph,
            embedding_provider=embedding_provider,
            coreference_context=coreference_context,
            temporal_reference_time=temporal_reference_time,
            relationship_types=relationship_types,
        )

        pipeline = EnrichmentPipeline(llm_provider=self.llm_provider)

        # #1027: Apply write-time content transformations (coreference + temporal resolution)
        if isinstance(content, str):
            content = pipeline.resolve_content(content, enrichment_flags)

        # Convert content to bytes
        if isinstance(content, dict):
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
        try:
            backend_context = context if context else self.context
            content_hash = self.backend.write_content(
                content_bytes, context=backend_context
            ).content_hash
        except Exception as e:
            raise RuntimeError(f"Failed to store content in backend: {e}") from e

        # Run enrichment pipeline on text content
        text_content = self._get_text_content(content)
        enrichment = pipeline.enrich(text_content, enrichment_flags)

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
            state=state,
            embedding=enrichment.embedding_json,
            embedding_model=enrichment.embedding_model,
            embedding_dim=enrichment.embedding_dim,
            entities_json=enrichment.entities_json,
            entity_types=enrichment.entity_types,
            person_refs=enrichment.person_refs,
            temporal_refs_json=enrichment.temporal_refs_json,
            earliest_date=enrichment.earliest_date,
            latest_date=enrichment.latest_date,
            relationships_json=enrichment.relationships_json,
            relationship_count=enrichment.relationship_count,
            temporal_stability=enrichment.temporal_stability,
            stability_confidence=enrichment.stability_confidence,
            estimated_ttl_days=enrichment.estimated_ttl_days,
            valid_at=valid_at_dt,
            size_bytes=len(content_bytes),
            created_by=user_id or agent_id,
            change_reason=change_reason,
        )

        # #1190: Detect memory evolution relationships (opt-in)
        if detect_evolution and text_content:
            try:
                from nexus.bricks.memory.evolution_detector import (
                    MemoryEvolutionDetector,
                    apply_evolution_results,
                )

                detector = MemoryEvolutionDetector(llm_provider=self.llm_provider)

                # Reuse embedding vector if available
                embedding_vec_for_evolution = None
                if enrichment.embedding_json:
                    embedding_vec_for_evolution = json.loads(enrichment.embedding_json)

                evolution_result = detector.detect(
                    session=self.session,
                    zone_id=zone_id,
                    new_text=text_content,
                    new_entities=enrichment.parsed_entities,
                    person_refs=enrichment.person_refs,
                    entity_types=enrichment.entity_types,
                    embedding_vec=embedding_vec_for_evolution,
                    exclude_memory_id=memory.memory_id,
                )

                if evolution_result and evolution_result.relationships:
                    apply_evolution_results(
                        session=self.session,
                        new_memory_id=memory.memory_id,
                        results=evolution_result,
                    )
            except Exception:
                import logging

                logging.getLogger(__name__).warning(
                    "Evolution detection failed, continuing without it",
                    exc_info=True,
                )

        # #1039: Store extracted entities and relationships to graph tables
        if store_to_graph and (enrichment.entities_json or enrichment.relationships_json):
            try:
                self._store_to_graph(
                    memory_id=memory.memory_id,
                    zone_id=zone_id,
                    entities_json=enrichment.entities_json,
                    relationships_json=enrichment.relationships_json,
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
        import json
        import os

        from nexus.bricks.memory._sync import run_sync
        from nexus.storage.record_store import SQLAlchemyRecordStore

        GraphStore = self._graph_store_class
        if GraphStore is None:
            logger.debug("No graph_store_class injected, skipping graph storage")
            return

        # Get database URL from session's engine
        db_url = os.environ.get("NEXUS_DATABASE_URL", "")
        if not db_url:
            # Try to get from the session's engine bind
            try:
                bind = self.session.get_bind()
                url = getattr(bind, "url", None)
                if url:
                    db_url = str(url)
            except Exception as e:
                logger.debug("Failed to extract database URL from session: %s", e)
                return

        if not db_url:
            return

        # Use default zone if not provided
        effective_zone_id = zone_id or ROOT_ZONE_ID

        async def _do_store() -> None:
            _store = SQLAlchemyRecordStore(db_url=db_url)
            try:
                async with _store.async_session_factory() as session:
                    graph_store = GraphStore(_store, session, zone_id=effective_zone_id)

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
                _store.close()

        run_sync(_do_store())

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
        temporal_stability: str | None = None,  # #1191: Filter by temporal stability
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
        # Fall back to as_of if as_of_event not specified
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
            temporal_stability=temporal_stability,  # #1191: Stability filtering
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

        # Build results with enriched content using Pydantic models (#1498)
        from nexus.bricks.memory.response_models import MemoryQueryResponse

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
        import json

        from nexus.bricks.memory._sync import run_sync

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
            # Use the DI-injected embedding provider if available
            embedding_provider = self._embedding_provider
            if embedding_provider is None:
                # Fall back to keyword search if no provider available
                logger.debug("No embedding provider available, falling back to keyword search")
                return self._keyword_search(query, scope, memory_type, limit, after_dt, before_dt)

        # Generate query embedding
        query_embedding = run_sync(embedding_provider.embed_text(query))

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
                        )
                        content = content_bytes.decode("utf-8")
                        keyword_score = self._compute_keyword_score(query, content)
                    except Exception as e:
                        logger.debug("Failed to read content for keyword scoring: %s", e)

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

            # Build result list with content using Pydantic model (#1498)
            from nexus.bricks.memory.response_models import MemorySearchResponse

            results = []
            for memory, score, semantic_score, keyword_score in scored_memories:
                # Read content via DRY helper (#1498)
                content = self._read_content(memory.content_hash)

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
        except Exception as e:
            # Don't fail the read operation if tracking fails
            logger.debug("Failed to track memory access: %s", e)
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
            from sqlalchemy import select

            memories = list(
                self.session.execute(
                    select(MemoryModel)
                    .where(
                        MemoryModel.zone_id == self.zone_id,
                        MemoryModel.importance > min_importance,
                    )
                    .limit(batch_size)
                )
                .scalars()
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
        """Follow the superseded_by chain to find the current memory (#1188). Delegates to MemoryVersioning."""
        return self._versioning.resolve_to_current(memory_id)

    def resolve_to_current(self, memory_id: str) -> Any:
        """Public wrapper for version resolution (#1193). Delegates to MemoryVersioning."""
        return self._versioning.resolve_to_current(memory_id)

    # ── Path resolution (#2177) ──────────────────────────────────────

    @staticmethod
    def is_memory_path(path: str) -> bool:
        """Check if a path is a memory virtual path."""
        from nexus.bricks.memory.router import MemoryViewRouter

        return MemoryViewRouter.is_memory_path(path)

    def resolve(self, virtual_path: str) -> Any:
        """Resolve virtual path to canonical memory."""
        return self.memory_router.resolve(virtual_path)

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

        # Read content via DRY helper (#1498)
        content = self._read_content(memory.content_hash)

        # Calculate effective importance with decay (Issue #1030)
        effective_importance = get_effective_importance(
            importance_original=memory.importance_original,
            importance_current=memory.importance,
            last_accessed_at=memory.last_accessed_at,
            created_at=memory.created_at,
        )

        from nexus.bricks.memory.response_models import MemoryDetailResponse

        return MemoryDetailResponse.from_memory_model(
            memory,
            content=content,
            importance_effective=effective_importance,
        ).model_dump()

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

        # Read content via DRY helper (#1498), try JSON parse for structured content
        content = self._read_content(memory.content_hash, parse_json=True)

        from nexus.bricks.memory.response_models import MemoryRetrieveResponse

        return MemoryRetrieveResponse.from_memory_model(
            memory,
            content=content,
        ).model_dump()

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
        return self._state.delete(memory_id, context=context)

    def approve(self, memory_id: str) -> bool:
        """Approve a memory (activate it) (#368). Delegates to MemoryStateManager."""
        return self._state.approve(memory_id)

    def deactivate(self, memory_id: str) -> bool:
        """Deactivate a memory (make it inactive) (#368). Delegates to MemoryStateManager."""
        return self._state.deactivate(memory_id)

    def approve_batch(self, memory_ids: list[str]) -> dict[str, Any]:
        """Approve multiple memories at once (#368). Delegates to MemoryStateManager."""
        return self._state.approve_batch(memory_ids)

    def deactivate_batch(self, memory_ids: list[str]) -> dict[str, Any]:
        """Deactivate multiple memories at once (#368). Delegates to MemoryStateManager."""
        return self._state.deactivate_batch(memory_ids)

    def delete_batch(self, memory_ids: list[str]) -> dict[str, Any]:
        """Delete multiple memories at once (#368). Delegates to MemoryStateManager."""
        return self._state.delete_batch(memory_ids)

    def invalidate(
        self,
        memory_id: str,
        invalid_at: datetime | str | None = None,
    ) -> bool:
        """Invalidate a memory (mark as no longer valid) (#1183). Delegates to MemoryStateManager."""
        return self._state.invalidate(memory_id, invalid_at=invalid_at)

    def invalidate_batch(
        self, memory_ids: list[str], invalid_at: datetime | str | None = None
    ) -> dict[str, Any]:
        """Invalidate multiple memories at once (#1183). Delegates to MemoryStateManager."""
        return self._state.invalidate_batch(memory_ids, invalid_at=invalid_at)

    def revalidate(self, memory_id: str) -> bool:
        """Revalidate a memory (#1183). Delegates to MemoryStateManager."""
        return self._state.revalidate(memory_id)

    def get_history(self, memory_id: str) -> list[dict[str, Any]]:
        """Get the complete version history chain for a memory (#1188). Delegates to MemoryVersioning."""
        return self._versioning.get_history(memory_id)

    def gc_old_versions(self, older_than_days: int = 365) -> int:
        """Garbage collect old superseded versions (#1188). Delegates to MemoryVersioning."""
        return self._versioning.gc_old_versions(older_than_days)

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

        from nexus.bricks.memory.response_models import MemoryListResponse

        results = []
        for memory in memories:
            # Check permission
            if not self.permission_enforcer.check_memory(memory, Permission.READ, check_context):
                continue

            results.append(
                MemoryListResponse(
                    memory_id=memory.memory_id,
                    content_hash=memory.content_hash,
                    zone_id=memory.zone_id,
                    user_id=memory.user_id,
                    agent_id=memory.agent_id,
                    scope=memory.scope,
                    visibility=memory.visibility,
                    memory_type=memory.memory_type,
                    importance=memory.importance,
                    state=memory.state,
                    namespace=memory.namespace,
                    path_key=memory.path_key,
                    created_at=MemoryListResponse._iso_or_none(memory.created_at),
                    updated_at=MemoryListResponse._iso_or_none(memory.updated_at),
                ).model_dump()
            )

        return results

    # ========== ACE (Agentic Context Engineering) Delegation (#1498) ==========
    # These methods delegate to ACE services via AceFacade.
    # Callers are encouraged to use ACE services directly.

    @property
    def ace(self) -> Any:
        """Access composed ACE services (trajectory, feedback, playbook, etc.)."""
        return self._ace

    def start_trajectory(self, task_description: str, task_type: str | None = None) -> str:
        """Start tracking a new execution trajectory. Delegates to ACE TrajectoryManager."""
        return self._ace.trajectory.start_trajectory(task_description, task_type)  # type: ignore[no-any-return]

    def log_trajectory_step(
        self, trajectory_id: str, step_type: str, description: str, result: Any = None
    ) -> None:
        """Log a step in the trajectory. Delegates to ACE TrajectoryManager."""
        self._ace.trajectory.log_step(trajectory_id, step_type, description, result)

    def log_step(
        self, trajectory_id: str, step_type: str, description: str, result: Any = None
    ) -> None:
        """Alias for log_trajectory_step(). Delegates to ACE TrajectoryManager."""
        self._ace.trajectory.log_step(trajectory_id, step_type, description, result)

    def complete_trajectory(
        self,
        trajectory_id: str,
        status: str,
        success_score: float | None = None,
        error_message: str | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> str:
        """Complete a trajectory with outcome. Delegates to ACE TrajectoryManager."""
        return self._ace.trajectory.complete_trajectory(  # type: ignore[no-any-return]
            trajectory_id, status, success_score, error_message, metrics
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
        """Add feedback to a completed trajectory. Delegates to ACE FeedbackManager."""
        return self._ace.feedback.add_feedback(  # type: ignore[no-any-return]
            trajectory_id, feedback_type, score, source, message, metrics
        )

    def get_trajectory_feedback(self, trajectory_id: str) -> builtins.list[dict[str, Any]]:
        """Get all feedback for a trajectory. Delegates to ACE FeedbackManager."""
        return self._ace.feedback.get_trajectory_feedback(trajectory_id)  # type: ignore[no-any-return]

    def get_effective_score(
        self,
        trajectory_id: str,
        strategy: Literal["latest", "average", "weighted"] = "latest",
    ) -> float:
        """Get effective score for trajectory. Delegates to ACE FeedbackManager."""
        return self._ace.feedback.get_effective_score(trajectory_id, strategy)  # type: ignore[no-any-return]

    def mark_for_relearning(self, trajectory_id: str, reason: str, priority: int = 5) -> None:
        """Flag trajectory for re-reflection. Delegates to ACE FeedbackManager."""
        self._ace.feedback.mark_for_relearning(trajectory_id, reason, priority)

    def batch_add_feedback(
        self, feedback_items: builtins.list[dict[str, Any]]
    ) -> builtins.list[str]:
        """Add feedback to multiple trajectories. Delegates to ACE FeedbackManager."""
        return self._ace.feedback.batch_add_feedback(feedback_items)  # type: ignore[no-any-return]

    async def reflect_async(self, trajectory_id: str, context: str | None = None) -> dict[str, Any]:
        """Reflect on a single trajectory (async). Delegates to ACE Reflector."""
        return await self._ace.reflector.reflect_async(trajectory_id, context)  # type: ignore[no-any-return]

    def reflect(self, trajectory_id: str, context: str | None = None) -> dict[str, Any]:
        """Reflect on a single trajectory (sync). Delegates to ACE Reflector."""
        from nexus.bricks.memory._sync import run_sync

        return run_sync(self.reflect_async(trajectory_id, context))

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
        import importlib
        from datetime import datetime

        _ace_reflection = importlib.import_module("nexus.services.ace.reflection")
        Reflector = _ace_reflection.Reflector  # noqa: N806
        _ace_trajectory = importlib.import_module("nexus.services.ace.trajectory")
        TrajectoryManager = _ace_trajectory.TrajectoryManager  # noqa: N806
        from nexus.storage.models import TrajectoryModel

        target_agent_id = agent_id or self.agent_id

        # Query trajectories
        from sqlalchemy import select

        stmt = select(TrajectoryModel).filter_by(agent_id=target_agent_id)

        if since:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            stmt = stmt.where(TrajectoryModel.started_at >= since_dt)

        if task_type:
            stmt = stmt.filter_by(task_type=task_type)

        trajectories = list(
            self.session.execute(stmt.order_by(TrajectoryModel.started_at.desc()).limit(100))
            .scalars()
            .all()
        )

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
            except Exception as e:
                logger.debug(
                    "Skipping failed reflection for trajectory %s: %s", traj.trajectory_id, e
                )
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
        """Batch reflection across multiple trajectories (sync)."""
        from nexus.bricks.memory._sync import run_sync

        return run_sync(self.batch_reflect_async(agent_id, since, min_trajectories, task_type))

    def get_playbook(self, playbook_name: str = "default") -> dict[str, Any] | None:
        """Get agent's playbook. Delegates to ACE PlaybookManager."""
        playbooks = self._ace.playbook.query_playbooks(
            agent_id=self.agent_id, name_pattern=playbook_name, limit=1
        )
        if not playbooks:
            return None
        return self._ace.playbook.get_playbook(playbooks[0]["playbook_id"])  # type: ignore[no-any-return]

    def update_playbook(
        self, strategies: builtins.list[dict[str, Any]], playbook_name: str = "default"
    ) -> dict[str, Any]:
        """Update playbook with new strategies. Delegates to ACE PlaybookManager."""
        playbook = self.get_playbook(playbook_name)
        if not playbook:
            playbook_id = self._ace.playbook.create_playbook(
                name=playbook_name,
                description=f"Playbook for {self.agent_id or 'agent'}",
                scope="agent",
            )
        else:
            playbook_id = playbook["playbook_id"]

        ace_strategies = [
            {
                "type": s.get("category", "neutral"),
                "description": s.get("pattern", ""),
                "evidence": s.get("context", ""),
                "confidence": s.get("confidence", 0.5),
            }
            for s in strategies
        ]

        self._ace.playbook.update_playbook(playbook_id, strategies=ace_strategies)
        return {"playbook_id": playbook_id, "strategies_added": len(ace_strategies)}

    def curate_playbook(
        self, reflections: builtins.list[str], playbook_name: str = "default"
    ) -> dict[str, Any]:
        """Auto-curate playbook from reflections. Delegates to ACE Curator."""
        playbook = self.get_playbook(playbook_name)
        if not playbook:
            playbook_id = self._ace.playbook.create_playbook(
                name=playbook_name,
                description=f"Playbook for {self.agent_id or 'agent'}",
                scope="agent",
            )
        else:
            playbook_id = playbook["playbook_id"]
        return self._ace.curator.curate_playbook(playbook_id, reflections)  # type: ignore[no-any-return]

    async def consolidate_async(
        self,
        memory_type: str | None = None,
        scope: str | None = None,
        namespace: str | None = None,
        namespace_prefix: str | None = None,
        preserve_high_importance: bool = True,
        importance_threshold: float = 0.8,
    ) -> dict[str, Any]:
        """Consolidate memories to prevent context collapse (async). Delegates to ACE ConsolidationEngine."""
        max_importance = importance_threshold if preserve_high_importance else 1.0
        results = self._ace.consolidation.consolidate_by_criteria(
            memory_type=memory_type,
            scope=scope,
            namespace=namespace,
            namespace_prefix=namespace_prefix,
            importance_max=max_importance,
            batch_size=10,
            limit=100,
        )
        total_consolidated = sum(r.get("memories_consolidated", 0) for r in results)
        total_created = len(results)
        return {
            "memories_consolidated": total_consolidated,
            "consolidations_created": total_created,
            "space_saved": total_consolidated - total_created,
        }

    def consolidate(
        self,
        memory_type: str | None = None,
        scope: str | None = None,
        namespace: str | None = None,
        namespace_prefix: str | None = None,
        preserve_high_importance: bool = True,
        importance_threshold: float = 0.8,
    ) -> dict[str, Any]:
        """Consolidate memories (sync). Delegates to ACE ConsolidationEngine."""
        from nexus.bricks.memory._sync import run_sync

        return run_sync(
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
        """Execute with automatic learning loop (async). Delegates to ACE LearningLoop."""
        playbook_id = None
        if auto_curate:
            playbook = self.get_playbook(playbook_name)
            if playbook:
                playbook_id = playbook["playbook_id"]

        execution_result = await self._ace.learning_loop.execute_with_learning_async(
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
        """Execute with automatic learning loop (sync). Delegates to ACE LearningLoop."""
        from nexus.bricks.memory._sync import run_sync

        return run_sync(
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
        self, agent_id: str | None = None, status: str | None = None, limit: int = 50
    ) -> builtins.list[dict[str, Any]]:
        """Query execution trajectories. Delegates to ACE TrajectoryManager."""
        target_agent_id = agent_id or self.agent_id
        return self._ace.trajectory.query_trajectories(  # type: ignore[no-any-return]
            agent_id=target_agent_id, status=status, limit=limit
        )

    def query_playbooks(
        self, agent_id: str | None = None, scope: str | None = None, limit: int = 50
    ) -> builtins.list[dict[str, Any]]:
        """Query playbooks. Delegates to ACE PlaybookManager."""
        target_agent_id = agent_id or self.agent_id
        return self._ace.playbook.query_playbooks(  # type: ignore[no-any-return]
            agent_id=target_agent_id, scope=scope, limit=limit
        )

    def process_relearning(self, limit: int = 10) -> builtins.list[dict[str, Any]]:
        """Process trajectories flagged for re-learning. Delegates to ACE LearningLoop."""
        return self._ace.learning_loop.process_relearning_queue(limit)  # type: ignore[no-any-return]

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

        # Try to get embedding provider via DI
        if embedding_provider is None:
            embedding_provider = self._embedding_provider
            if embedding_provider is None:
                raise ValueError(
                    "No embedding provider available for index_memories_async(). "
                    "Pass embedding_provider= or inject via Memory(..., embedding_provider=...)."
                )

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
                        )
                        content = content_bytes.decode("utf-8")

                        # Generate embedding
                        embedding_vec = await embedding_provider.embed_text(content)
                        embedding_json = json.dumps(embedding_vec)

                        # Update memory with embedding
                        memory.embedding = embedding_json
                        memory.embedding_model = getattr(embedding_provider, "model", "unknown")
                        memory.embedding_dim = len(embedding_vec)

                        success_count += 1
                    except Exception as e:
                        # Failed to generate embedding for this memory
                        logger.debug(
                            "Failed to generate embedding for memory %s: %s", memory.memory_id, e
                        )
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
        from nexus.bricks.memory._sync import run_sync

        return run_sync(
            self.index_memories_async(embedding_provider, batch_size, memory_type, scope)
        )

    # ========== Version Tracking Methods (#1184) ==========

    def _get_chain_memory_ids(self, memory_id: str) -> builtins.list[str]:
        """Get all memory IDs in the supersedes chain (#1188). Delegates to MemoryVersioning."""
        return self._versioning.get_chain_memory_ids(memory_id)

    def list_versions(self, memory_id: str) -> builtins.list[dict[str, Any]]:
        """List all versions of a memory. Delegates to MemoryVersioning."""
        return self._versioning.list_versions(memory_id)

    def get_version(
        self,
        memory_id: str,
        version: int,
        context: OperationContext | None = None,
    ) -> dict[str, Any] | None:
        """Retrieve a specific version of a memory. Delegates to MemoryVersioning."""
        return self._versioning.get_version(memory_id, version, context=context)

    def rollback(
        self,
        memory_id: str,
        version: int,
        context: OperationContext | None = None,
    ) -> None:
        """Rollback a memory to a previous version. Delegates to MemoryVersioning."""
        self._versioning.rollback(memory_id, version, context=context)

    def diff_versions(
        self,
        memory_id: str,
        v1: int,
        v2: int,
        mode: Literal["metadata", "content"] = "metadata",
        context: OperationContext | None = None,
    ) -> dict[str, Any] | str:
        """Compare two versions of a memory. Delegates to MemoryVersioning."""
        return self._versioning.diff_versions(memory_id, v1, v2, mode=mode, context=context)
