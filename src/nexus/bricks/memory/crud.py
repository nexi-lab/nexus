"""CRUD operations for Memory brick (store, get, retrieve, delete, list).

Migrated from memory_api.py Memory class methods.
Part of Issue #2128 Memory brick extraction.

Related: #406 (embedding), #1027 (resolution), #1183 (temporal validity),
        #1188 (soft-delete), #1190 (evolution), #1498 (refactoring)
"""

from __future__ import annotations

import builtins
import json
import logging
from datetime import UTC, datetime
from typing import Any

# TODO(#2XXX): Replace with Protocol imports when dependencies are extracted
from nexus.core.permissions import OperationContext, Permission
from nexus.core.temporal import parse_datetime
from nexus.rebac.memory_permission_enforcer import MemoryPermissionEnforcer
from nexus.services.memory.memory_router import MemoryViewRouter

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


class MemoryCRUD:
    """CRUD operations for memory records.

    Handles basic create, read, update, delete operations with permission enforcement.

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
        """Initialize CRUD operations.

        Args:
            memory_router: Memory view router for accessing memory records.
            permission_enforcer: ReBAC permission enforcer.
            backend: Content storage backend (CAS).
            context: Operation context for permission checks.
            llm_provider: Optional LLM provider for enrichment.
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

    @staticmethod
    def _get_text_content(content: str | bytes | dict[str, Any]) -> str | None:
        """Extract text from content for enrichment pipeline.

        Args:
            content: Memory content (text, bytes, or dict).

        Returns:
            Text string suitable for NLP processing, or None for binary content.
        """
        if isinstance(content, dict):
            return json.dumps(content)
        elif isinstance(content, str):
            return content if content.strip() else None
        return None

    def _read_content(self, content_hash: str, *, parse_json: bool = False) -> str | dict[str, Any]:
        """Read and decode content from CAS backend.

        Args:
            content_hash: CAS content hash to read.
            parse_json: If True, attempt to parse UTF-8 content as JSON first.

        Returns:
            Decoded string, parsed dict (if parse_json), or hex for binary.
        """
        try:
            content_bytes: bytes = self._backend.read_content(
                content_hash, context=self._context
            ).unwrap()
            if parse_json:
                try:
                    parsed: dict[str, Any] = json.loads(content_bytes.decode("utf-8"))
                    return parsed
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass
            try:
                return content_bytes.decode("utf-8")
            except UnicodeDecodeError:
                return content_bytes.hex()
        except Exception as e:
            logger.debug("Failed to read memory content %s: %s", content_hash, e)
            return f"<content not available: {content_hash}>"

    def _track_memory_access(self, memory: Any) -> None:
        """Track memory access for importance decay (Issue #1030).

        Updates access_count and last_accessed_at.
        """
        memory.access_count = (memory.access_count or 0) + 1
        memory.last_accessed_at = datetime.now(UTC)

        try:
            self._memory_router.session.commit()
        except Exception:
            logger.warning("Failed to track memory access", exc_info=True)
            self._memory_router.session.rollback()

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
        context: OperationContext | None = None,
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
        """Store a memory.

        Args:
            content: Memory content (text, bytes, or structured dict).
            scope: Memory scope ('agent', 'user', 'zone', 'global').
            memory_type: Type of memory ('fact', 'preference', 'experience').
            importance: Importance score (0.0-1.0).
            namespace: Hierarchical namespace (e.g., "knowledge/geography/facts").
            path_key: Optional unique key within namespace for upsert mode.
            state: Memory state ('inactive', 'active'). Defaults to 'active'.
            _metadata: Additional metadata.
            context: Optional operation context to override identity.
            generate_embedding: Generate embedding for semantic search.
            embedding_provider: Optional embedding provider.
            resolve_coreferences: Resolve pronouns to entity names.
            coreference_context: Prior conversation context for pronoun resolution.
            resolve_temporal: Resolve temporal expressions to absolute dates.
            temporal_reference_time: Reference time for temporal resolution.
            extract_entities: Extract named entities for symbolic filtering.
            extract_temporal: Extract temporal metadata for date-based queries.
            extract_relationships: Extract relationships (triplets).
            relationship_types: Custom relationship types.
            store_to_graph: Store entities/relationships to graph tables.
            valid_at: When fact became valid in real world (datetime or ISO-8601).
            classify_stability: Auto-classify temporal stability.
            detect_evolution: Detect memory evolution relationships (opt-in).

        Returns:
            memory_id: The created or updated memory ID.

        Examples:
            >>> # Append mode (no path_key)
            >>> memory_id = crud.store(
            ...     content={"fact": "Paris is capital of France"},
            ...     namespace="knowledge/geography/facts"
            ... )

            >>> # Upsert mode (with path_key)
            >>> memory_id = crud.store(
            ...     content={"theme": "dark", "font_size": 14},
            ...     namespace="user/preferences/ui",
            ...     path_key="settings"  # Will update if exists
            ... )
        """
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

        pipeline = EnrichmentPipeline(llm_provider=self._llm_provider)

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
        zone_id = context.zone_id if context else self._zone_id
        user_id = context.user_id if context else self._user_id
        agent_id = context.agent_id if context else self._agent_id

        # Store content in backend (CAS)
        try:
            backend_context = context if context else self._context
            content_hash = self._backend.write_content(
                content_bytes, context=backend_context
            ).unwrap()
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
        memory = self._memory_router.create_memory(
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
                from nexus.services.memory.evolution_detector import (
                    MemoryEvolutionDetector,
                    apply_evolution_results,
                )

                detector = MemoryEvolutionDetector(llm_provider=self._llm_provider)

                # Reuse embedding vector if available
                embedding_vec_for_evolution = None
                if enrichment.embedding_json:
                    embedding_vec_for_evolution = json.loads(enrichment.embedding_json)

                session = self._memory_router.session
                evolution_result = detector.detect(
                    session=session,
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
                        session=session,
                        new_memory_id=memory.memory_id,
                        results=evolution_result,
                    )
            except Exception:
                logger.warning(
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
                logger.warning(f"Failed to store to graph: {e}")

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
        import os

        from nexus.core.sync_bridge import run_sync
        from nexus.search.graph_store import GraphStore
        from nexus.storage.record_store import SQLAlchemyRecordStore

        # Get database URL from session's engine
        db_url = os.environ.get("NEXUS_DATABASE_URL", "")
        if not db_url:
            # Try to get from the session's engine bind
            try:
                session = self._memory_router.session
                bind = session.get_bind()
                url = getattr(bind, "url", None)
                if url:
                    db_url = str(url)
            except Exception as e:
                logger.debug("Failed to extract database URL from session: %s", e)
                return

        if not db_url:
            return

        # Use default zone if not provided
        effective_zone_id = zone_id or "root"

        async def _do_store() -> None:
            _store = SQLAlchemyRecordStore(db_url=db_url)
            try:
                async with _store.async_session_factory() as session:
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
                _store.close()

        run_sync(_do_store())

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
            context: Optional operation context to override identity.

        Returns:
            Memory dictionary or None if not found or no permission.
        """
        memory = self._memory_router.get_memory_by_id(memory_id)
        if not memory:
            return None

        # #1188: Filter out soft-deleted memories
        if memory.state == "deleted":
            return None

        # #1188: Follow superseded chain to current version
        if memory.superseded_by_id:
            # TODO: Use versioning service for this
            from nexus.services.memory.versioning import MemoryVersioning

            versioning = MemoryVersioning(
                session_factory=lambda: self._memory_router.session,
                memory_router=self._memory_router,
                permission_enforcer=self._permission_enforcer,
                backend=self._backend,
                context=self._context,
            )
            memory = versioning.resolve_to_current(memory_id)
            if not memory or memory.state == "deleted":
                return None

        # Use provided context or fall back to instance context
        check_context = context or self._context

        # Check permission
        if not self._permission_enforcer.check_memory(memory, Permission.READ, check_context):
            return None

        # Track access (Issue #1030)
        if track_access:
            self._track_memory_access(memory)

        # Read content
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
            path: Combined path (alternative to namespace+path_key).

        Returns:
            Memory dictionary or None if not found or no permission.
        """
        # Parse combined path if provided
        if path:
            parts = path.rsplit("/", 1)
            if len(parts) == 2:
                namespace, path_key = parts
            else:
                path_key = parts[0]

        if not namespace or not path_key:
            raise ValueError("Both namespace and path_key are required")

        # Query by namespace + path_key
        from sqlalchemy import select

        from nexus.storage.models import MemoryModel

        stmt = select(MemoryModel).where(
            MemoryModel.namespace == namespace, MemoryModel.path_key == path_key
        )
        memory = self._memory_router.session.execute(stmt).scalar_one_or_none()

        if not memory:
            return None

        # Check permission
        if not self._permission_enforcer.check_memory(memory, Permission.READ, self._context):
            return None

        # Read content, try JSON parse for structured content
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
        """Delete a memory (soft-delete, preserves row).

        Args:
            memory_id: Memory ID to delete.
            context: Optional operation context to override identity.

        Returns:
            True if deleted, False if not found or no permission.
        """
        # Delegate to lifecycle/state manager
        memory = self._memory_router.get_memory_by_id(memory_id)
        if not memory:
            return False

        check_context = context or self._context
        if not self._permission_enforcer.check_memory(memory, Permission.WRITE, check_context):
            return False

        return self._memory_router.delete_memory(memory_id)

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
        context: OperationContext | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """List memories with filters.

        TODO: Migrate full implementation from memory_api.py Memory.list()
        This is a ~100-line method with temporal query logic.
        """
        raise NotImplementedError("list() migration in progress")
