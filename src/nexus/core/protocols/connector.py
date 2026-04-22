"""Storage connector protocol interfaces (Issue #1601, #1703, #2367).

Defines the Storage Brick boundary as composable protocols:

- ``ContentStoreProtocol`` — Minimal CAS interface (most consumers need only this)
- ``DirectoryOpsProtocol`` — Directory operations (VFS Router, mount services)
- ``ConnectorProtocol`` — Full connector interface (Storage Brick boundary)
- ``OAuthCapableProtocol`` — OAuth token management capability
- ``StreamingProtocol`` — Memory-efficient large file I/O (stream/range)
- ``BatchContentProtocol`` — Bulk content read optimization
- ``DirectoryListingProtocol`` — Extended directory listing + file metadata
- ``SearchableConnector`` — Thin search capability for searchable connectors

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

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Iterator

    from nexus.backends.base.backend import FileInfo, HandlerStatusResponse
    from nexus.contracts.backend_features import BackendFeature
    from nexus.contracts.types import OperationContext
    from nexus.core.object_store import WriteResult

# ---------------------------------------------------------------------------
# SearchableConnector (Issue #2367)
# ---------------------------------------------------------------------------

@runtime_checkable
class SearchableConnector(Protocol):
    """Thin search capability for connectors that support content/metadata search.

    Heavy search logic stays in the Search brick. This protocol just
    advertises "I support search" at the connector level, enabling the
    Search brick to discover searchable connectors via isinstance().

    References:
        - NEXUS-LEGO-ARCHITECTURE.md §2.3, §4.3
        - Issue #2367: Extract SearchableConnector sub-protocol
    """

    def search(
        self,
        query: str,
        *,
        filters: dict[str, Any] | None = None,
        limit: int = 10,
        context: "OperationContext | None" = None,
    ) -> list[dict[str, Any]]: ...

    def index(
        self,
        key: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        context: "OperationContext | None" = None,
    ) -> None: ...

    def remove_from_index(
        self,
        key: str,
        context: "OperationContext | None" = None,
    ) -> None: ...

# ---------------------------------------------------------------------------
# ContentStoreProtocol (Issue #1601)
# ---------------------------------------------------------------------------

@runtime_checkable
class ContentStoreProtocol(Protocol):
    """Minimal content store interface — most consumers need only this.

    Covers content operations: write, read, delete, existence check,
    size, and reference counting.  Uses opaque ``content_id`` (hash for
    CAS backends, path for PAS backends).
    """

    @property
    def name(self) -> str: ...

    def write_content(
        self, content: bytes, content_id: str = "", *, offset: int = 0, context: "OperationContext | None" = None
    ) -> "WriteResult": ...

    def read_content(
        self, content_id: str, context: "OperationContext | None" = None
    ) -> bytes: ...

    def delete_content(
        self, content_id: str, context: "OperationContext | None" = None
    ) -> None: ...

    def content_exists(
        self, content_id: str, context: "OperationContext | None" = None
    ) -> bool: ...

    def get_content_size(
        self, content_id: str, context: "OperationContext | None" = None
    ) -> int: ...

@runtime_checkable
class DirectoryOpsProtocol(Protocol):
    """Directory operations — needed by VFS Router and mount services."""

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: "OperationContext | None" = None,
    ) -> None: ...

    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: "OperationContext | None" = None,
    ) -> None: ...

    def is_directory(
        self, path: str, context: "OperationContext | None" = None
    ) -> bool: ...

@runtime_checkable
class CapabilityAwareProtocol(Protocol):
    """Protocol for backends that declare capabilities (Issue #2069).

    Implemented by Backend ABC and all wrappers. Consumers use this to
    query backend capabilities uniformly instead of hasattr/isinstance.
    """

    @property
    def backend_features(self) -> "frozenset[BackendFeature]": ...

    def has_feature(self, cap: "BackendFeature") -> bool: ...

@runtime_checkable
class ConnectorProtocol(
    ContentStoreProtocol, DirectoryOpsProtocol, CapabilityAwareProtocol, Protocol
):
    """Full connector interface — the Storage Brick boundary.

    Combines CAS content operations, directory operations, and connection
    lifecycle management with capability flags for polymorphic dispatch.
    """

    # --- Connection lifecycle ---

    def check_connection(
        self, context: "OperationContext | None" = None
    ) -> "HandlerStatusResponse": ...

    # --- Capability flags ---

    @property
    def is_connected(self) -> bool: ...

    @property
    def has_root_path(self) -> bool: ...

@runtime_checkable
class OAuthCapableProtocol(Protocol):
    """OAuth token management capability.

    Implemented by connectors that use OAuth credentials (Gmail, GDrive,
    Slack, X, Google Calendar). Used to detect OAuth backends dynamically
    instead of hardcoding backend type lists.

    ``user_scoped`` and ``has_token_manager`` moved here from
    ConnectorProtocol (Issue #1824) — non-OAuth connectors no longer
    need to declare these.
    """

    token_manager: Any
    token_manager_db: str
    user_email: str | None
    provider: str

    @property
    def user_scoped(self) -> bool: ...

    @property
    def has_token_manager(self) -> bool: ...

@runtime_checkable
class StreamingProtocol(Protocol):
    """Memory-efficient large file I/O — streaming reads and writes.

    Used by NexusFS for HTTP range requests and large file operations.
    All Backend subclasses provide default implementations; backends with
    native streaming (e.g., GCS, S3) can override for true streaming.
    """

    def stream_content(
        self,
        content_id: str,
        chunk_size: int = 8192,
        context: "OperationContext | None" = None,
    ) -> "Iterator[bytes]": ...

    def stream_range(
        self,
        content_id: str,
        start: int,
        end: int,
        chunk_size: int = 8192,
        context: "OperationContext | None" = None,
    ) -> "Iterator[bytes]": ...

    def write_stream(
        self,
        chunks: "Iterator[bytes]",
        content_id: str = "",
        *,
        context: "OperationContext | None" = None,
    ) -> "WriteResult": ...

@runtime_checkable
class BatchContentProtocol(Protocol):
    """Bulk content read optimization.

    Used by object_store and memory services to reduce
    round-trips when reading multiple content items.
    """

    def batch_read_content(
        self,
        content_ids: list[str],
        context: "OperationContext | None" = None,
    ) -> dict[str, bytes | None]: ...

@runtime_checkable
class DirectoryListingProtocol(Protocol):
    """Extended directory operations — listing and file metadata.

    Used by search_service and connector sync loop for
    directory enumeration and delta sync change detection.
    """

    def list_dir(
        self, path: str, context: "OperationContext | None" = None
    ) -> list[str]: ...

    def get_file_info(
        self, path: str, context: "OperationContext | None" = None
    ) -> "FileInfo": ...

@runtime_checkable
class SignedUrlProtocol(Protocol):
    """Backend can generate pre-signed/signed download URLs (Issue #2069).

    Replaces orphan ``hasattr(backend, 'generate_presigned_url')`` and
    ``hasattr(backend, 'generate_signed_url')`` checks in filesystem.py.
    """

    def generate_signed_download_url(
        self,
        backend_path: str,
        expires_in: int = 3600,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any]: ...

@runtime_checkable
class PathDeleteProtocol(Protocol):
    """Backend supports path-based delete (Issue #2069).

    Backend supports path-based delete operations.
    """

    def delete(
        self,
        path: str,
        context: "OperationContext | None" = None,
    ) -> None: ...
