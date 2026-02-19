"""Storage connector protocol interfaces (Issue #1601, #1703).

Defines the Storage Brick boundary as composable protocols:

- ``ContentStoreProtocol`` — Minimal CAS interface (most consumers need only this)
- ``DirectoryOpsProtocol`` — Directory operations (VFS Router, mount services)
- ``ConnectorProtocol`` — Full connector interface (Storage Brick boundary)
- ``PassthroughProtocol`` — Same-box operations (locking, physical paths)
- ``OAuthCapableProtocol`` — OAuth token management capability
- ``StreamingProtocol`` — Memory-efficient large file I/O (stream/range)
- ``BatchContentProtocol`` — Bulk content read optimization
- ``DirectoryListingProtocol`` — Extended directory listing + file metadata

Design decisions:
    - Protocol for brick interfaces, ABC for internal implementations (§11.3)
    - Protocols in ``core/protocols/``, implementations stay in ``backends/`` (§11.4)
    - Modeled after ``VFSRouterProtocol`` pattern (§5.1)
    - Layered protocols: consumers import only the capability they need (§5.6)

References:
    - docs/design/NEXUS-LEGO-ARCHITECTURE.md §5.1, §5.6, §11.3, §11.4
    - Issue #1601: ConnectorProtocol + Storage Brick Extraction
    - Issue #1703: Make backends implement ConnectorProtocol
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Iterator

    from nexus.backends.backend import FileInfo, HandlerStatusResponse
    from nexus.contracts.types import OperationContext
    from nexus.core.response import HandlerResponse


@runtime_checkable
class ContentStoreProtocol(Protocol):
    """Minimal CAS interface — most consumers need only this.

    Covers content-addressable storage operations: write, read, delete,
    existence check, size, and reference counting.
    """

    @property
    def name(self) -> str: ...

    def write_content(
        self, content: bytes, context: OperationContext | None = None
    ) -> HandlerResponse[str]: ...

    def read_content(
        self, content_hash: str, context: OperationContext | None = None
    ) -> HandlerResponse[bytes]: ...

    def delete_content(
        self, content_hash: str, context: OperationContext | None = None
    ) -> HandlerResponse[None]: ...

    def content_exists(
        self, content_hash: str, context: OperationContext | None = None
    ) -> HandlerResponse[bool]: ...

    def get_content_size(
        self, content_hash: str, context: OperationContext | None = None
    ) -> HandlerResponse[int]: ...

    def get_ref_count(
        self, content_hash: str, context: OperationContext | None = None
    ) -> HandlerResponse[int]: ...


@runtime_checkable
class DirectoryOpsProtocol(Protocol):
    """Directory operations — needed by VFS Router and mount services."""

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: OperationContext | None = None,
    ) -> HandlerResponse[None]: ...

    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: OperationContext | None = None,
    ) -> HandlerResponse[None]: ...

    def is_directory(
        self, path: str, context: OperationContext | None = None
    ) -> HandlerResponse[bool]: ...


@runtime_checkable
class ConnectorProtocol(ContentStoreProtocol, DirectoryOpsProtocol, Protocol):
    """Full connector interface — the Storage Brick boundary.

    Combines CAS content operations, directory operations, and connection
    lifecycle management with capability flags for polymorphic dispatch.
    """

    # --- Connection lifecycle ---

    def connect(self, context: OperationContext | None = None) -> HandlerStatusResponse: ...

    def disconnect(self, context: OperationContext | None = None) -> None: ...

    def check_connection(
        self, context: OperationContext | None = None
    ) -> HandlerStatusResponse: ...

    # --- Capability flags ---

    @property
    def user_scoped(self) -> bool: ...

    @property
    def is_connected(self) -> bool: ...

    @property
    def is_passthrough(self) -> bool: ...

    @property
    def has_root_path(self) -> bool: ...

    @property
    def has_virtual_filesystem(self) -> bool: ...

    @property
    def has_token_manager(self) -> bool: ...


@runtime_checkable
class PassthroughProtocol(Protocol):
    """Same-box operations — locking, physical path access.

    Only PassthroughBackend implements this. Used by events/locking code
    to safely narrow the backend type instead of using ``cast()``.
    """

    @property
    def base_path(self) -> Path: ...

    def get_physical_path(self, virtual_path: str) -> Path: ...

    def lock(self, path: str, timeout: float = 30.0, max_holders: int = 1) -> str | None: ...

    def unlock(self, lock_id: str) -> bool: ...


@runtime_checkable
class OAuthCapableProtocol(Protocol):
    """OAuth token management capability.

    Implemented by connectors that use OAuth credentials (Gmail, GDrive,
    Slack, X, Google Calendar). Used to detect OAuth backends dynamically
    instead of hardcoding backend type lists.
    """

    token_manager: Any
    token_manager_db: str
    user_email: str | None
    provider: str


@runtime_checkable
class StreamingProtocol(Protocol):
    """Memory-efficient large file I/O — streaming reads and writes.

    Used by nexus_fs_core for HTTP range requests and large file operations.
    All Backend subclasses provide default implementations; backends with
    native streaming (e.g., GCS, S3) can override for true streaming.
    """

    def stream_content(
        self,
        content_hash: str,
        chunk_size: int = 8192,
        context: OperationContext | None = None,
    ) -> Any: ...

    def stream_range(
        self,
        content_hash: str,
        start: int,
        end: int,
        chunk_size: int = 8192,
        context: OperationContext | None = None,
    ) -> Iterator[bytes]: ...

    def write_stream(
        self,
        chunks: Iterator[bytes],
        context: OperationContext | None = None,
    ) -> HandlerResponse[str]: ...


@runtime_checkable
class BatchContentProtocol(Protocol):
    """Bulk content read optimization.

    Used by object_store, async_nexus_fs, and memory services to reduce
    round-trips when reading multiple content items.
    """

    def batch_read_content(
        self,
        content_hashes: list[str],
        context: OperationContext | None = None,
    ) -> dict[str, bytes | None]: ...


@runtime_checkable
class DirectoryListingProtocol(Protocol):
    """Extended directory operations — listing and file metadata.

    Used by search_service, sync_service, and write_back_service for
    directory enumeration and delta sync change detection.
    """

    def list_dir(
        self, path: str, context: OperationContext | None = None
    ) -> list[str]: ...

    def get_file_info(
        self, path: str, context: OperationContext | None = None
    ) -> HandlerResponse[FileInfo]: ...
