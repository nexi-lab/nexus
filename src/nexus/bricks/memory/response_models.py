"""Pydantic response models for Memory brick using mixin-based composition.

Refactored from services/memory/response_models.py to eliminate 60% field duplication
(168 LOC savings) through mixin composition pattern.

Related: Issue #2128 (Memory brick extraction), Issue #1498 (Response models).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class MemoryCoreMixin(BaseModel):
    """Core memory fields: identity, content, scope, importance, state.

    These fields appear in ALL memory response shapes.
    """

    model_config = ConfigDict(from_attributes=True)

    memory_id: str
    content_hash: str | None = None
    scope: str | None = None
    importance: float | None = None
    state: str | None = None

    @classmethod
    def _iso_or_none(cls, dt: datetime | None) -> str | None:
        """Convert datetime to ISO format string."""
        return dt.isoformat() if dt else None


class MemoryMetadataMixin(BaseModel):
    """Metadata fields: zone, user, agent, namespace, timestamps.

    Used for list(), query(), and detail responses.
    """

    model_config = ConfigDict(from_attributes=True)

    zone_id: str | None = None
    user_id: str | None = None
    agent_id: str | None = None
    visibility: str | None = None
    memory_type: str | None = None
    namespace: str | None = None
    path_key: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class MemoryEnrichmentMixin(BaseModel):
    """Enrichment fields: entities, relationships, temporal data.

    Used for query() and search() responses that include NLP-extracted metadata.
    """

    model_config = ConfigDict(from_attributes=True)

    entity_types: str | None = None
    person_refs: str | None = None
    temporal_refs_json: str | None = None
    earliest_date: str | None = None
    latest_date: str | None = None
    relationships_json: str | None = None
    relationship_count: int | None = None


class MemoryTemporalMixin(BaseModel):
    """Temporal validity fields: valid_at, invalid_at, is_current.

    Used for query() responses that support temporal operators (as_of, during).
    """

    model_config = ConfigDict(from_attributes=True)

    valid_at: str | None = None
    invalid_at: str | None = None
    is_current: bool = True


class MemoryEvolutionMixin(BaseModel):
    """Evolution and stability fields: classification, TTL, supersession.

    Used for detail and query responses that include evolution tracking.
    """

    model_config = ConfigDict(from_attributes=True)

    temporal_stability: str | None = None
    stability_confidence: float | None = None
    estimated_ttl_days: int | None = None
    supersedes_id: str | None = None
    superseded_by_id: str | None = None
    extends_ids: str | None = None
    extended_by_ids: str | None = None
    derived_from_ids: str | None = None


class MemoryDecayMixin(BaseModel):
    """Importance decay fields: original, effective, access tracking.

    Used for detail responses that show importance decay over time.
    """

    model_config = ConfigDict(from_attributes=True)

    importance_original: float | None = None
    importance_effective: float | None = None
    access_count: int | None = None
    last_accessed_at: str | None = None


# ── Composed Response Models ────────────────────────────────────────────────


class MemoryListResponse(MemoryCoreMixin, MemoryMetadataMixin):
    """Lightweight response for list() — no content, no enrichment.

    Fields: Core (5) + Metadata (9) = 14 fields total
    """

    pass


class MemoryRetrieveResponse(MemoryCoreMixin, MemoryMetadataMixin):
    """Response for retrieve() — includes content but no enrichment.

    Fields: Core (5) + Metadata (9) + content (1) = 15 fields total
    """

    content: str | dict[str, Any] | None = None

    @classmethod
    def from_memory_model(
        cls,
        memory: Any,
        content: str | dict[str, Any] | None = None,
    ) -> MemoryRetrieveResponse:
        """Build from a MemoryModel ORM instance."""
        return cls(
            memory_id=memory.memory_id,
            content=content,
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
            created_at=cls._iso_or_none(memory.created_at),
            updated_at=cls._iso_or_none(memory.updated_at),
        )


class MemoryDetailResponse(
    MemoryCoreMixin,
    MemoryMetadataMixin,
    MemoryDecayMixin,
    MemoryTemporalMixin,
    MemoryEvolutionMixin,
):
    """Full response for get() — all fields including decay, temporal, evolution.

    Fields: Core (5) + Metadata (9) + Decay (4) + Temporal (3) + Evolution (9) + content (1) = 31 fields total
    """

    content: str | dict[str, Any] | None = None

    @classmethod
    def from_memory_model(
        cls,
        memory: Any,
        content: str | dict[str, Any] | None = None,
        importance_effective: float | None = None,
        content_hash_override: str | None = None,
    ) -> MemoryDetailResponse:
        """Build from a MemoryModel ORM instance.

        Args:
            memory: MemoryModel (SQLAlchemy ORM object).
            content: Decoded content string or parsed dict.
            importance_effective: Pre-computed effective importance with decay.
            content_hash_override: Override content_hash (for as_of_system queries).
        """
        return cls(
            memory_id=memory.memory_id,
            content=content,
            content_hash=content_hash_override or memory.content_hash,
            zone_id=memory.zone_id,
            user_id=memory.user_id,
            agent_id=memory.agent_id,
            scope=memory.scope,
            visibility=memory.visibility,
            memory_type=memory.memory_type,
            importance=memory.importance,
            importance_original=memory.importance_original,
            importance_effective=importance_effective,
            access_count=memory.access_count,
            last_accessed_at=cls._iso_or_none(memory.last_accessed_at),
            state=memory.state,
            namespace=memory.namespace,
            path_key=memory.path_key,
            created_at=cls._iso_or_none(memory.created_at),
            updated_at=cls._iso_or_none(memory.updated_at),
            valid_at=cls._iso_or_none(memory.valid_at),
            invalid_at=cls._iso_or_none(memory.invalid_at),
            is_current=memory.invalid_at is None,
            temporal_stability=memory.temporal_stability,
            stability_confidence=memory.stability_confidence,
            estimated_ttl_days=memory.estimated_ttl_days,
            supersedes_id=memory.supersedes_id,
            superseded_by_id=memory.superseded_by_id,
            extends_ids=memory.extends_ids,
            extended_by_ids=memory.extended_by_ids,
            derived_from_ids=memory.derived_from_ids,
        )


class MemoryQueryResponse(
    MemoryCoreMixin,
    MemoryMetadataMixin,
    MemoryEnrichmentMixin,
    MemoryTemporalMixin,
    MemoryEvolutionMixin,
):
    """Full response for query() — includes enrichment + temporal fields.

    Fields: Core (5) + Metadata (9) + Enrichment (7) + Temporal (3) + Evolution (9) + content + importance_effective = 35 fields total
    """

    content: str | dict[str, Any] | None = None
    importance_effective: float | None = None

    @classmethod
    def from_memory_model(
        cls,
        memory: Any,
        content: str | dict[str, Any] | None = None,
        importance_effective: float | None = None,
        content_hash_override: str | None = None,
    ) -> MemoryQueryResponse:
        """Build from a MemoryModel ORM instance."""
        return cls(
            memory_id=memory.memory_id,
            content=content,
            content_hash=content_hash_override or memory.content_hash,
            zone_id=memory.zone_id,
            user_id=memory.user_id,
            agent_id=memory.agent_id,
            scope=memory.scope,
            visibility=memory.visibility,
            memory_type=memory.memory_type,
            importance=memory.importance,
            importance_effective=importance_effective,
            state=memory.state,
            namespace=memory.namespace,
            path_key=memory.path_key,
            entity_types=memory.entity_types,
            person_refs=memory.person_refs,
            temporal_refs_json=memory.temporal_refs_json,
            earliest_date=cls._iso_or_none(memory.earliest_date),
            latest_date=cls._iso_or_none(memory.latest_date),
            relationships_json=memory.relationships_json,
            relationship_count=memory.relationship_count,
            temporal_stability=memory.temporal_stability,
            stability_confidence=memory.stability_confidence,
            estimated_ttl_days=memory.estimated_ttl_days,
            extends_ids=memory.extends_ids,
            extended_by_ids=memory.extended_by_ids,
            derived_from_ids=memory.derived_from_ids,
            created_at=cls._iso_or_none(memory.created_at),
            updated_at=cls._iso_or_none(memory.updated_at),
            valid_at=cls._iso_or_none(memory.valid_at),
            invalid_at=cls._iso_or_none(memory.invalid_at),
            is_current=memory.invalid_at is None,
        )


class MemorySearchResponse(MemoryCoreMixin, MemoryMetadataMixin):
    """Response for search() — includes relevance scores.

    Fields: Core (5) + Metadata (9) + content (1) + scores (3) = 18 fields total
    """

    content: str | dict[str, Any] | None = None
    score: float = 0.0
    semantic_score: float | None = None
    keyword_score: float | None = None

    @classmethod
    def from_memory_model(
        cls,
        memory: Any,
        content: str | dict[str, Any] | None = None,
        score: float = 0.0,
        semantic_score: float | None = None,
        keyword_score: float | None = None,
    ) -> MemorySearchResponse:
        """Build from a MemoryModel ORM instance with search scores."""
        return cls(
            memory_id=memory.memory_id,
            content=content,
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
            created_at=cls._iso_or_none(memory.created_at),
            updated_at=cls._iso_or_none(memory.updated_at),
            score=score,
            semantic_score=semantic_score,
            keyword_score=keyword_score,
        )


class BatchOperationResult(BaseModel):
    """Generic result for batch operations (approve_batch, deactivate_batch, delete_batch)."""

    success_count: int
    failed_count: int
    success_ids: list[str]
    failed_ids: list[str]

    def to_dict(self, success_key: str = "success", failed_key: str = "failed") -> dict[str, Any]:
        """Convert to dict format with custom key names.

        Args:
            success_key: Name for the success count field (e.g., 'approved', 'deleted').
            failed_key: Name for the failed count field.
        """
        return {
            success_key: self.success_count,
            failed_key: self.failed_count,
            f"{success_key}_ids": self.success_ids,
            f"{failed_key}_ids": self.failed_ids,
        }
