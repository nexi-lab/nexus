"""Sync provider protocol and data types for connector delta sync.

Defines the contract between connector sync providers and the MountService
sync orchestrator. Providers implement list/fetch; the orchestrator handles
state persistence, pagination, mutex, and idempotent writes.

Design decisions (Issue #3148):
    - State lifecycle centralized in orchestrator, not per-connector (3A)
    - SyncPage includes deleted_ids for deletion tracking (Decision #7)
    - FetchResult supports bytes | AsyncIterator[bytes] for streaming (14A+C)
    - Expired/invalid state_token triggers full re-sync
    - Partial failure is safe: CAS idempotency handles re-writes
    - Concurrent sync protection via mount-level mutex
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class SyncStatus(StrEnum):
    """Status of a sync state token."""

    VALID = "valid"
    EXPIRED = "expired"
    CORRUPTED = "corrupted"
    INITIAL = "initial"


@dataclass(frozen=True)
class RemoteItem:
    """A single item returned by a sync provider's list operation.

    Contains metadata only — content is fetched separately via fetch_item().
    """

    item_id: str
    relative_path: str
    size: int | None = None
    modified_time: str | None = None  # ISO 8601
    content_hash: str | None = None  # Provider-specific hash for dedup
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SyncPage:
    """A page of sync results from a connector.

    Attributes:
        items: New or updated items in this page.
        deleted_ids: IDs of items deleted since last sync.
        next_page_token: Token for next page, or None if this is the last page.
        state_token: Opaque token representing sync state after this page.
            Persisted by the orchestrator for delta sync on next run.
    """

    items: list[RemoteItem]
    deleted_ids: list[str] = field(default_factory=list)
    next_page_token: str | None = None
    state_token: str | None = None


@dataclass
class FetchResult:
    """Result of fetching a single item's content.

    Supports both in-memory bytes and streaming for large files.
    The streaming path (async_chunks) is protocol-level only — implementation
    deferred to Phase 3 when Drive connector ships.

    Attributes:
        relative_path: Where to write this item in the mount.
        content: Full content as bytes (for small items).
        async_chunks: Async iterator of chunks (for large items, Phase 3).
        size: Content size in bytes (known upfront for streaming).
    """

    relative_path: str
    content: bytes | None = None
    async_chunks: AsyncIterator[bytes] | None = None
    size: int | None = None

    def is_streaming(self) -> bool:
        """Whether this result uses streaming (large file path)."""
        return self.async_chunks is not None


@dataclass
class MountSyncState:
    """Persisted sync state for a mount point.

    Stored in metastore keyed by (mount_point, provider_type).
    The orchestrator manages lifecycle: create, update, invalidate.

    Attributes:
        mount_point: The mount this state belongs to.
        provider_type: Connector type identifier.
        state_token: Opaque token from the provider's last sync page.
        status: Current status of the state token.
        last_sync_time: ISO 8601 timestamp of last successful sync.
        items_synced: Total items synced in last run.
        pages_processed: Pages processed in last run.
    """

    mount_point: str
    provider_type: str
    state_token: str | None = None
    status: SyncStatus = SyncStatus.INITIAL
    last_sync_time: str | None = None
    items_synced: int = 0
    pages_processed: int = 0

    def invalidate(self) -> None:
        """Mark state as requiring full re-sync."""
        self.state_token = None
        self.status = SyncStatus.INITIAL
        self.items_synced = 0
        self.pages_processed = 0


@runtime_checkable
class ConnectorSyncProvider(Protocol):
    """Protocol for connectors that support delta sync.

    Implementations provide list and fetch operations. The MountService
    sync orchestrator handles:
    - State persistence (metastore)
    - Pagination (iterating pages via next_page_token)
    - Expired token recovery (full re-sync)
    - Concurrent sync mutex (per mount point)
    - Idempotent writes (CAS dedup)

    Implementations should:
    - Return SyncPage with items, deleted_ids, and state_token
    - Raise ValueError with "token expired" or "token invalid" on bad state
    - Support since=None for full sync (no delta)
    """

    async def list_remote_items(
        self,
        path: str,
        *,
        since: str | None = None,
        page_token: str | None = None,
        page_size: int = 100,
    ) -> SyncPage:
        """List remote items, optionally since a previous state token.

        Args:
            path: Mount-relative path to list.
            since: State token from previous sync (None = full sync).
            page_token: Pagination token for continuing a multi-page list.
            page_size: Maximum items per page.

        Returns:
            SyncPage with items and pagination/state tokens.

        Raises:
            ValueError: If state token is expired or invalid (triggers re-sync).
        """
        ...

    async def fetch_item(
        self,
        item_id: str,
    ) -> FetchResult:
        """Fetch content for a single remote item.

        Args:
            item_id: Provider-specific item identifier.

        Returns:
            FetchResult with content bytes or streaming iterator.
        """
        ...
