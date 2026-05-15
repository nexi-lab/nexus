"""Aspect contracts — extensible metadata for entities (Issue #2929).

Aspects are discrete, typed metadata facets attached to entities via URN.
Inspired by DataHub's entity-aspect model, adapted for Nexus.

Design decisions:
    - Static registry with decorator pattern (Issue #7)
    - AspectEnvelope wraps typed payloads with version + audit info
    - Pydantic models for schema enforcement at write time
    - JSON serialization for storage simplicity

Example:
    >>> from nexus.contracts.aspects import register_aspect, AspectBase
    >>>
    >>> @register_aspect("schema_metadata", max_versions=20)
    ... class SchemaMetadataAspect(AspectBase):
    ...     columns: list[dict[str, str]]
    ...     format: str
    ...     row_count: int | None = None
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar

logger = logging.getLogger(__name__)

# Maximum aspect payload size in bytes (1MB default)
MAX_ASPECT_PAYLOAD_BYTES: int = 1_048_576

# Default maximum versions to retain per aspect
DEFAULT_MAX_VERSIONS: int = 20


@dataclass(frozen=True, slots=True)
class AspectEnvelope:
    """Immutable container for a typed aspect payload with version and audit info.

    Attributes:
        aspect_name: Registered aspect type name.
        version: Version number (0 = current, 1+ = history).
        payload: JSON-serializable aspect data.
        created_by: User/agent who created this version.
        created_at: When this version was created.
    """

    aspect_name: str
    version: int
    payload: dict[str, Any]
    created_by: str = "system"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_json(self) -> str:
        """Serialize payload to JSON string."""
        return json.dumps(self.payload, default=str)

    @classmethod
    def from_json(
        cls,
        aspect_name: str,
        version: int,
        json_str: str,
        created_by: str = "system",
        created_at: datetime | None = None,
    ) -> AspectEnvelope:
        """Deserialize an AspectEnvelope from stored JSON."""
        payload: dict[str, Any] = json.loads(json_str)
        return cls(
            aspect_name=aspect_name,
            version=version,
            payload=payload,
            created_by=created_by,
            created_at=created_at or datetime.now(UTC),
        )


class AspectBase:
    """Base class for aspect Pydantic-style models.

    Subclasses define the schema for a specific aspect type.
    Registration via ``@register_aspect`` is required for storage.
    """

    _aspect_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for storage."""
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AspectBase:
        """Deserialize from stored dict."""
        return cls(**data)


@dataclass(frozen=True, slots=True)
class AspectRegistration:
    """Metadata about a registered aspect type."""

    name: str
    cls: type
    max_versions: int


class AspectRegistry:
    """Static registry of known aspect types.

    Aspects are registered at import time via ``@register_aspect``.
    The registry enforces that only known aspect types can be stored,
    and provides schema validation on write.
    """

    _instance: ClassVar[AspectRegistry | None] = None
    _registry: dict[str, AspectRegistration]

    def __init__(self) -> None:
        self._registry = {}

    @classmethod
    def get(cls) -> AspectRegistry:
        """Get the singleton registry instance."""
        if cls._instance is None:
            cls._instance = AspectRegistry()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton (for testing only)."""
        cls._instance = None

    def register(
        self,
        name: str,
        cls: type,
        max_versions: int = DEFAULT_MAX_VERSIONS,
    ) -> None:
        """Register an aspect type.

        Args:
            name: Unique aspect name (e.g., 'schema_metadata').
            cls: The aspect class.
            max_versions: Max history versions to retain.

        Raises:
            ValueError: If name is already registered with a different class.
        """
        existing = self._registry.get(name)
        if existing is not None and existing.cls is not cls:
            raise ValueError(
                f"Aspect '{name}' already registered with {existing.cls.__name__}, "
                f"cannot re-register with {cls.__name__}"
            )
        self._registry[name] = AspectRegistration(
            name=name,
            cls=cls,
            max_versions=max_versions,
        )

    def get_registration(self, name: str) -> AspectRegistration | None:
        """Look up a registered aspect by name."""
        return self._registry.get(name)

    def is_registered(self, name: str) -> bool:
        """Check if an aspect name is registered."""
        return name in self._registry

    def list_aspects(self) -> list[str]:
        """Return all registered aspect names."""
        return list(self._registry.keys())

    def max_versions_for(self, name: str) -> int:
        """Get the max versions retention for an aspect type."""
        reg = self._registry.get(name)
        return reg.max_versions if reg else DEFAULT_MAX_VERSIONS

    def validate_payload(self, name: str, payload: dict[str, Any]) -> None:
        """Validate a payload against the registered aspect schema.

        Args:
            name: Aspect name.
            payload: Data to validate.

        Raises:
            ValueError: If aspect is not registered.
            ValueError: If payload exceeds size limit.
            ValueError: If required fields are missing.
        """
        if not self.is_registered(name):
            raise ValueError(f"Unknown aspect type: {name!r}")

        payload_json = json.dumps(payload, default=str)
        if len(payload_json.encode()) > MAX_ASPECT_PAYLOAD_BYTES:
            raise ValueError(
                f"Aspect payload exceeds {MAX_ASPECT_PAYLOAD_BYTES} bytes "
                f"(got {len(payload_json.encode())} bytes)"
            )

        # Validate required fields by attempting to instantiate the aspect class
        reg = self._registry[name]
        if issubclass(reg.cls, AspectBase):
            try:
                reg.cls.from_dict(payload)
            except TypeError as e:
                raise ValueError(f"Invalid payload for aspect {name!r}: {e}") from e


def register_aspect(
    name: str,
    max_versions: int = DEFAULT_MAX_VERSIONS,
) -> Any:
    """Decorator to register an aspect type with the global registry.

    Usage:
        @register_aspect("schema_metadata", max_versions=20)
        class SchemaMetadataAspect(AspectBase):
            columns: list[dict[str, str]]
            format: str
    """

    def decorator(cls: type[AspectBase]) -> type[AspectBase]:
        AspectRegistry.get().register(name, cls, max_versions)
        cls._aspect_name = name
        return cls

    return decorator


# ============================================================================
# Built-in aspects
# ============================================================================


@register_aspect("path", max_versions=5)
class PathAspect(AspectBase):
    """Tracks the virtual path of a file entity.

    Updated on rename/move. The URN stays stable; only this aspect changes.
    """

    def __init__(self, virtual_path: str, backend_id: str = "") -> None:
        self.virtual_path = virtual_path
        self.backend_id = backend_id


@register_aspect("schema_metadata", max_versions=20)
class SchemaMetadataAspect(AspectBase):
    """Schema information extracted from structured data files.

    Stored by the catalog brick's schema extractors.
    """

    def __init__(
        self,
        columns: list[dict[str, str]] | None = None,
        format: str = "unknown",
        row_count: int | None = None,
        confidence: float = 1.0,
        warnings: list[str] | None = None,
    ) -> None:
        self.columns = columns or []
        self.format = format
        self.row_count = row_count
        self.confidence = confidence
        self.warnings = warnings or []


@register_aspect("file_metadata", max_versions=10)
class FileMetadataAspect(AspectBase):
    """Full file metadata snapshot emitted by MCLRecorder on writes/deletes.

    Stores the metadata dict from FileMetadata.to_dict(). Fields are
    intentionally loose (**kwargs) because the exact shape depends on the
    backend and may evolve — the aspect store treats it as an opaque blob.
    """

    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AspectBase:
        return cls(**data)


@register_aspect("ownership", max_versions=5)
class OwnershipAspect(AspectBase):
    """Tracks entity ownership for access control and audit."""

    def __init__(
        self,
        owner_id: str,
        owner_type: str = "user",
    ) -> None:
        self.owner_id = owner_id
        self.owner_type = owner_type


@register_aspect("governance.classification", max_versions=10)
class GovernanceClassificationAspect(AspectBase):
    """Governance classification for data sensitivity and access control."""

    def __init__(
        self,
        level: str = "internal",
        owner: str = "",
        reason: str = "",
        review_date: str = "",
    ) -> None:
        self.level = level
        self.owner = owner
        self.reason = reason
        self.review_date = review_date


@register_aspect("lineage", max_versions=5)
class LineageAspect(AspectBase):
    """Agent lineage tracking — records which files an agent read to produce an output.

    Enables impact analysis ("if I change X, what outputs are stale?"),
    provenance auditing, and staleness detection. Inspired by DataHub's
    UpstreamLineage aspect, adapted for agent-native workflows.

    Issue #3417.

    Attributes:
        upstream: List of upstream dependencies with version info.
        agent_id: Which agent produced this output.
        agent_generation: Session generation counter (optional).
        operation: Type of write operation (write, write_batch, copy).
        duration_ms: How long the agent worked on this (optional).
    """

    # Maximum upstream entries per lineage aspect to prevent unbounded growth
    MAX_UPSTREAM_ENTRIES: int = 500

    def __init__(
        self,
        upstream: list[dict[str, Any]] | None = None,
        agent_id: str = "",
        agent_generation: int | None = None,
        operation: str = "write",
        duration_ms: int | None = None,
        truncated: bool = False,
    ) -> None:
        self.upstream = upstream or []
        self.agent_id = agent_id
        self.agent_generation = agent_generation
        self.operation = operation
        self.duration_ms = duration_ms
        self.truncated = truncated

    @classmethod
    def from_session_reads(
        cls,
        reads: list[dict[str, Any]],
        agent_id: str,
        agent_generation: int | None = None,
        operation: str = "write",
        duration_ms: int | None = None,
    ) -> "LineageAspect":
        """Build a LineageAspect from accumulated session reads.

        Each read entry should have: path, version, content_id, access_type.
        Caps at MAX_UPSTREAM_ENTRIES with a warning.

        Args:
            reads: List of read dicts from the session accumulator.
            agent_id: Agent that produced this output.
            agent_generation: Session generation counter.
            operation: Write operation type.
            duration_ms: Processing duration.

        Returns:
            LineageAspect with upstream entries populated.
        """
        truncated = len(reads) > cls.MAX_UPSTREAM_ENTRIES
        if truncated:
            logger.warning(
                "Lineage truncated: %d reads exceed max %d for agent %s",
                len(reads),
                cls.MAX_UPSTREAM_ENTRIES,
                agent_id,
            )
        upstream = [
            {
                "path": r["path"],
                "version": r.get("version", 0),
                "content_id": r.get("content_id", ""),
                "access_type": r.get("access_type", "content"),
            }
            for r in reads[: cls.MAX_UPSTREAM_ENTRIES]
        ]
        return cls(
            upstream=upstream,
            agent_id=agent_id,
            agent_generation=agent_generation,
            operation=operation,
            duration_ms=duration_ms,
            truncated=truncated,
        )

    @classmethod
    def from_explicit_declaration(
        cls,
        upstream: list[dict[str, Any]],
        agent_id: str,
        agent_generation: int | None = None,
    ) -> "LineageAspect":
        """Build a LineageAspect from an explicit upstream declaration.

        Used when agents declare their inputs via the REST API.

        Args:
            upstream: List of upstream dicts (path, version, content_id required).
            agent_id: Agent declaring the lineage.
            agent_generation: Session generation counter.

        Returns:
            LineageAspect with upstream entries populated.
        """
        truncated = len(upstream) > cls.MAX_UPSTREAM_ENTRIES
        capped = upstream[: cls.MAX_UPSTREAM_ENTRIES]
        return cls(
            upstream=capped,
            agent_id=agent_id,
            agent_generation=agent_generation,
            operation="explicit",
            truncated=truncated,
        )


@register_aspect("document_structure", max_versions=10)
class DocumentStructureAspect(AspectBase):
    """Structure metadata for non-tabular documents (Markdown, PDF, etc.).

    Unlike schema_metadata (columns/types for tabular data), this captures
    document-level structure: headings, front matter, code blocks, etc.
    Issue #2978.
    """

    def __init__(
        self,
        title: str | None = None,
        headings: list[dict[str, Any]] | None = None,
        front_matter: dict[str, Any] | None = None,
        word_count: int = 0,
        link_count: int = 0,
        code_languages: list[str] | None = None,
        format: str = "unknown",
        confidence: float = 1.0,
    ) -> None:
        self.title = title
        self.headings = headings or []
        self.front_matter = front_matter
        self.word_count = word_count
        self.link_count = link_count
        self.code_languages = code_languages or []
        self.format = format
        self.confidence = confidence
