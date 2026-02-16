"""Pydantic response models for Memory service (#1498).

Domain-level response models that replace hand-built dicts in Memory API methods.
These models provide schema validation, consistent field sets, and a single source
of truth for memory response shapes.

Usage:
    model = MemoryDetailResponse.from_memory_model(memory, content="...")
    return model.model_dump()  # backward-compatible dict
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class MemoryBaseResponse(BaseModel):
    """Base fields shared by all memory response shapes."""

    model_config = ConfigDict(from_attributes=True)

    memory_id: str
    content_hash: str | None = None
    zone_id: str | None = None
    user_id: str | None = None
    agent_id: str | None = None
    scope: str | None = None
    visibility: str | None = None
    memory_type: str | None = None
    importance: float | None = None
    state: str | None = None
    namespace: str | None = None
    path_key: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def _iso_or_none(cls, dt: datetime | None) -> str | None:
        return dt.isoformat() if dt else None


class MemoryListResponse(MemoryBaseResponse):
    """Lightweight response for list() — no content, no enrichment fields."""

    pass


class MemoryDetailResponse(MemoryBaseResponse):
    """Full response for get() — includes content, enrichment, decay, evolution."""

    content: str | dict[str, Any] | None = None
    importance_original: float | None = None
    importance_effective: float | None = None
    access_count: int | None = None
    last_accessed_at: str | None = None
    valid_at: str | None = None
    invalid_at: str | None = None
    is_current: bool = True
    temporal_stability: str | None = None
    stability_confidence: float | None = None
    estimated_ttl_days: int | None = None
    supersedes_id: str | None = None
    superseded_by_id: str | None = None
    extends_ids: str | None = None
    extended_by_ids: str | None = None
    derived_from_ids: str | None = None

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


class MemoryQueryResponse(MemoryBaseResponse):
    """Full response for query() — includes enrichment metadata + temporal fields."""

    content: str | dict[str, Any] | None = None
    importance_effective: float | None = None
    entity_types: str | None = None
    person_refs: str | None = None
    temporal_refs_json: str | None = None
    earliest_date: str | None = None
    latest_date: str | None = None
    relationships_json: str | None = None
    relationship_count: int | None = None
    temporal_stability: str | None = None
    stability_confidence: float | None = None
    estimated_ttl_days: int | None = None
    extends_ids: str | None = None
    extended_by_ids: str | None = None
    derived_from_ids: str | None = None
    valid_at: str | None = None
    invalid_at: str | None = None
    is_current: bool = True

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


class MemorySearchResponse(MemoryBaseResponse):
    """Response for search() — includes relevance scores."""

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


class MemoryRetrieveResponse(MemoryBaseResponse):
    """Response for retrieve() — includes parsed content."""

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


class BatchOperationResult(BaseModel):
    """Generic result for batch operations."""

    success_count: int
    failed_count: int
    success_ids: list[str]
    failed_ids: list[str]

    def to_dict(self, success_key: str = "success", failed_key: str = "failed") -> dict[str, Any]:
        """Convert to legacy dict format with custom key names.

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
