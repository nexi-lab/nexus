"""Data models for Dify enterprise permission integration.

Defines request/response schemas for the Dify External Knowledge API,
sync configuration, and permission metadata mapping.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class DifySyncMode(StrEnum):
    """How Nexus syncs permission data to Dify."""

    METADATA = "metadata"
    EXTERNAL_KNOWLEDGE = "external_knowledge"
    HYBRID = "hybrid"


class DifyMetadataField(StrEnum):
    """Standard metadata fields injected into Dify documents."""

    OWNER_ID = "owner_id"
    DEPARTMENT = "department"
    ACCESS_LEVEL = "access_level"
    ZONE_ID = "zone_id"
    FILE_PATH = "file_path"
    ALLOWED_USERS = "allowed_users"
    ALLOWED_GROUPS = "allowed_groups"


@dataclass(frozen=True, slots=True)
class DifyConfig:
    """Configuration for the Dify integration brick.

    Attributes:
        base_url: Dify instance base URL (e.g. https://dify.example.com).
        api_key: Dify API key for knowledge base management.
        retrieval_api_key: Shared secret for Dify → Nexus retrieval calls.
        sync_mode: How to integrate (metadata, external_knowledge, hybrid).
        default_knowledge_id: Default Dify knowledge base ID for syncing.
        score_threshold: Minimum relevance score for retrieval results.
        top_k: Default number of results to return.
        sync_batch_size: Number of documents to sync in each batch.
        metadata_fields: Which permission metadata fields to include.
    """

    base_url: str
    api_key: str
    retrieval_api_key: str
    sync_mode: DifySyncMode = DifySyncMode.EXTERNAL_KNOWLEDGE
    default_knowledge_id: str | None = None
    score_threshold: float = 0.5
    top_k: int = 5
    sync_batch_size: int = 50
    metadata_fields: tuple[str, ...] = (
        DifyMetadataField.OWNER_ID,
        DifyMetadataField.ZONE_ID,
        DifyMetadataField.DEPARTMENT,
        DifyMetadataField.ACCESS_LEVEL,
    )


# ---------------------------------------------------------------------------
# Dify External Knowledge API schemas (Dify → Nexus direction)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DifyRetrievalSetting:
    """Retrieval parameters from Dify's External Knowledge API request."""

    top_k: int = 5
    score_threshold: float = 0.5


@dataclass(frozen=True, slots=True)
class DifyRetrievalRequest:
    """Incoming retrieval request from Dify.

    Matches Dify External Knowledge API spec::

        POST /retrieval
        {
            "knowledge_id": "...",
            "query": "...",
            "retrieval_setting": {"top_k": 5, "score_threshold": 0.5}
        }

    Extended with ``user_id`` and ``zone_id`` for permission filtering.
    """

    knowledge_id: str
    query: str
    retrieval_setting: DifyRetrievalSetting = field(
        default_factory=DifyRetrievalSetting
    )
    user_id: str | None = None
    zone_id: str | None = None


@dataclass(frozen=True, slots=True)
class DifyRetrievalRecord:
    """Single retrieval result returned to Dify."""

    content: str
    score: float
    title: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DifyRetrievalResponse:
    """Response to Dify's retrieval request."""

    records: list[DifyRetrievalRecord] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Nexus → Dify sync schemas
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DifyDocumentMetadata:
    """Permission metadata attached to a document in Dify's knowledge base."""

    file_path: str
    owner_id: str
    zone_id: str
    department: str = ""
    access_level: int = 0
    allowed_users: str = ""
    allowed_groups: str = ""
    last_synced_at: str = ""


@dataclass(frozen=True, slots=True)
class DifySyncRecord:
    """Tracks sync state for a single document."""

    file_path: str
    dify_document_id: str | None = None
    knowledge_id: str = ""
    last_synced_at: datetime | None = None
    content_hash: str = ""
    metadata_hash: str = ""
    sync_error: str | None = None


@dataclass(frozen=True, slots=True)
class DifySyncResult:
    """Summary of a sync operation."""

    total_files: int = 0
    synced: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
