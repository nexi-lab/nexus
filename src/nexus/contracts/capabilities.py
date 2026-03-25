"""Connector capability discovery (Issue #2069).

Provides a unified ``ConnectorCapability`` enum for declaring and querying
backend capabilities.  Backends declare ``_CAPABILITIES`` as a ClassVar;
consumers query via ``has_capability()`` or ``cap in backend.capabilities``.

Canonical home: contracts/ (base tier — available to all layers).
Moved from core/protocols/capabilities.py in #1323.

Design decisions:
    - Hybrid approach: StrEnum for O(1) gating + Protocol classes for type-safe
      method access (A1)
    - Source of truth is in the backend class (A2)
    - Single ``capabilities`` property replaces 10 individual flag delegations (A3)
    - frozenset for gating, isinstance for narrowing (P1)
    - Eager validation at registration time (P4)

References:
    - Issue #2069: ConnectorProtocol Capability Discovery
    - NEXUS-LEGO-ARCHITECTURE.md §5.6 (layered protocols)
"""

from enum import StrEnum


class ConnectorCapability(StrEnum):
    """Enumeration of all capabilities a connector backend can support.

    Each value corresponds to either:
    - An existing boolean property on Backend ABC (capability flags)
    - An existing runtime_checkable Protocol sub-type
    - A new Protocol for previously orphaned hasattr checks
    """

    # --- From existing Backend ABC boolean properties ---

    RENAME = "rename"
    """Backend supports direct file rename/move."""

    EXTERNAL_CONTENT = "external_content"
    """Content is managed by an external source (local FS, API, IPC), not CAS."""

    ROOT_PATH = "root_path"
    """Backend has a local root_path for physical storage."""

    TOKEN_MANAGER = "token_manager"
    """Backend manages OAuth tokens."""

    DATA_DIR = "data_dir"
    """Backend has a data_dir for ancillary data storage."""

    PARALLEL_MMAP = "parallel_mmap"
    """Backend supports Rust-accelerated parallel mmap reads."""

    USER_SCOPED = "user_scoped"
    """Backend requires per-user OAuth credentials."""

    # --- From existing Protocol sub-types ---

    STREAMING = "streaming"
    """Backend supports StreamingProtocol (memory-efficient large file I/O)."""

    BATCH_CONTENT = "batch_content"
    """Backend supports BatchContentProtocol (bulk content reads)."""

    DIRECTORY_LISTING = "directory_listing"
    """Backend supports DirectoryListingProtocol (list_dir + get_file_info)."""

    SEARCHABLE = "searchable"
    """Backend supports SearchableConnector (content/metadata search)."""

    OAUTH = "oauth"
    """Backend supports OAuthCapableProtocol (token management)."""

    # --- New capabilities (replacing orphan hasattr checks) ---

    SIGNED_URL = "signed_url"
    """Backend can generate pre-signed/signed download URLs."""

    PATH_DELETE = "path_delete"
    """Backend supports path-based delete (not just hash-based)."""

    CACHE_BULK_READ = "cache_bulk_read"
    """Backend supports read_bulk_from_cache() for bulk cache reads."""

    CACHE_SYNC = "cache_sync"
    """Backend supports sync_to_cache() / sync_from_cache()."""

    MULTIPART_UPLOAD = "multipart_upload"
    """Backend supports multipart/chunked uploads."""

    CAS = "cas"
    """Backend uses content-addressable storage (CAS) addressing."""

    # --- Knowledge platform capabilities (Issue #2929) ---

    NATIVE_VERSIONING = "native_versioning"
    """Backend supports native object versioning (e.g., S3 versioning, GCS generations)."""

    CHANGE_NOTIFICATIONS = "change_notifications"
    """Backend supports change notifications (e.g., S3 Event Notifications, GCS Pub/Sub)."""

    RESUMABLE_UPLOAD = "resumable_upload"
    """Backend supports resumable uploads (e.g., GCS resumable, S3 multipart with resume)."""

    # --- Connector protocol capabilities (Issue #3148) ---

    SKILL_DOC = "skill_doc"
    """Backend supports SkillDocMixin (auto-generated .skill/ documentation)."""

    SYNC = "sync"
    """Backend implements ConnectorSyncProvider for delta sync."""

    WATCH = "watch"
    """Sync provider supports real-time watch() for push-based updates."""

    WRITE_BACK = "write_back"
    """Backend supports write operations (validated YAML → backend action)."""

    CLI_BACKED = "cli_backed"
    """Backend delegates execution to an external CLI subprocess."""

    SYNC_ELIGIBLE = "sync_eligible"
    """Backend should be periodically synced to the metastore by ConnectorSyncLoop."""


# --- Capability-to-Protocol mapping ---
# Used for registration-time validation: if a backend claims a capability
# that maps to a Protocol, we verify the class has the required methods.


# --- Convenience frozensets ---

CORE_CAPABILITIES: frozenset[ConnectorCapability] = frozenset()
"""Default capabilities for Backend ABC (empty — backends opt in)."""

BLOB_CONNECTOR_CAPABILITIES: frozenset[ConnectorCapability] = frozenset(
    {
        ConnectorCapability.RENAME,
        ConnectorCapability.DIRECTORY_LISTING,
        ConnectorCapability.PATH_DELETE,
        ConnectorCapability.STREAMING,
        ConnectorCapability.BATCH_CONTENT,
    }
)
"""Common capabilities for blob storage connectors (S3, GCS, Azure)."""

OAUTH_CONNECTOR_CAPABILITIES: frozenset[ConnectorCapability] = frozenset(
    {
        ConnectorCapability.USER_SCOPED,
        ConnectorCapability.TOKEN_MANAGER,
        ConnectorCapability.OAUTH,
        ConnectorCapability.SYNC_ELIGIBLE,
    }
)
"""Common capabilities for OAuth-based connectors."""

CLI_CONNECTOR_CAPABILITIES: frozenset[ConnectorCapability] = frozenset(
    {
        ConnectorCapability.CLI_BACKED,
        ConnectorCapability.WRITE_BACK,
        ConnectorCapability.SKILL_DOC,
        ConnectorCapability.SYNC,
        ConnectorCapability.SYNC_ELIGIBLE,
    }
)
"""Common capabilities for CLI-backed connectors (gws, gh)."""
