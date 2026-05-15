"""Tier-neutral constants shared across all layers.

Single source of truth for magic values referenced by more than one
layer (kernel, services, bricks, server, CLI).  Prevents cross-tier
imports that violate the LEGO architecture.

See: NEXUS-LEGO-ARCHITECTURE.md §3.3, §5.4
"""

from enum import IntEnum


class PriorityTier(IntEnum):
    """Fixed priority tiers (lower value = higher priority).

    Strict ordering: CRITICAL tasks always run before HIGH,
    HIGH before NORMAL, etc.

    Originally in ``nexus.services.scheduler.constants``; moved to contracts
    because both the scheduler and pay bricks depend on it.
    """

    CRITICAL = 0  # System health, security
    HIGH = 1  # User-facing, urgent
    NORMAL = 2  # Standard (default)
    LOW = 3  # Background jobs
    BEST_EFFORT = 4  # Only when idle


# String aliases for API convenience — used by pay, scheduler, and server routers.
TIER_ALIASES: dict[str, PriorityTier] = {
    "critical": PriorityTier.CRITICAL,
    "high": PriorityTier.HIGH,
    "normal": PriorityTier.NORMAL,
    "low": PriorityTier.LOW,
    "best_effort": PriorityTier.BEST_EFFORT,
}

# Kernel-reserved path prefix for internal system entries (zone revisions, etc.).
# These entries are stored in MetastoreABC but filtered from user-visible operations.
# Moved to contracts because both
# core and services depend on it.
SYSTEM_PATH_PREFIX = "/__sys__/"

# =============================================================================
# Server Defaults
# =============================================================================

DEFAULT_NEXUS_URL = "http://localhost:2026"
"""Default Nexus API server URL. Override via NEXUS_URL env var."""

DEFAULT_NEXUS_PORT = 2026
"""Default Nexus API server port."""

DEFAULT_GRPC_BIND_ADDR = "0.0.0.0:2126"
"""Default Raft gRPC bind address. Override via NEXUS_BIND_ADDR env var."""

MAX_GRPC_MESSAGE_BYTES = 64 * 1024 * 1024  # 64 MiB
"""Maximum gRPC message size (bytes) for the unified VFS service.

Applies to every client/server that talks to ``NexusVFSServiceStub``:

- Python server (``grpc.aio.server(options=...)`` in ``nexus.grpc.server``)
- Python client (``nexus.grpc.defaults.build_channel_options`` used by
  ``RPCTransport``, ``RaftClient``, and federation e2e tests)
- Rust client (``peer_blob_client``'s tonic
  ``max_decoding/encoding_message_size``)

Rust mirror: ``contracts::MAX_GRPC_MESSAGE_BYTES`` in
``rust/contracts/src/constants.rs``. Raising this value requires
bumping both in lockstep.

Chosen as 64 MiB to accommodate large file reads (> 16 MiB CDC chunk
threshold) and unbounded list_metadata responses. Issue #2938."""

DEFAULT_LANGGRAPH_URL = "http://localhost:2024"
"""Default LangGraph server URL. Override via LANGGRAPH_SERVER_URL env var."""

# =============================================================================
# OAuth Defaults
# =============================================================================

DEFAULT_OAUTH_REDIRECT_URI = "http://localhost:3000/oauth/callback"
"""Default OAuth redirect URI for local development."""

DEFAULT_GOOGLE_REDIRECT_URI = "http://localhost:5173/oauth/callback"
"""Default Google OAuth redirect URI (frontend dev server)."""

# =============================================================================
# Observability Defaults
# =============================================================================

DEFAULT_OTEL_ENDPOINT = "http://localhost:4317"
"""Default OpenTelemetry OTLP endpoint. Override via OTEL_EXPORTER_OTLP_ENDPOINT env var."""

# =============================================================================
# Search Defaults
# =============================================================================

DEFAULT_ZOEKT_URL = "http://localhost:6070"
"""Default Zoekt code search server URL. Override via ZOEKT_URL env var."""

# =============================================================================
# Event Bus Defaults
# =============================================================================

DEFAULT_NATS_URL = "nats://localhost:4222"
"""Default NATS JetStream server URL. Override via NEXUS_NATS_URL env var."""

# =============================================================================
# Zone Defaults
# =============================================================================

# =============================================================================
# File Size Limits
# =============================================================================

NEXUS_FS_MAX_INMEMORY_SIZE = 1 * 1024 * 1024 * 1024  # 1 GB
"""Maximum file size for in-memory buffering (read-copy, cat, write buffer).

Shared across the kernel (sys_copy cross-backend fallback), the slim facade,
and the fsspec compatibility layer.  Change this one constant to adjust all
in-memory size guards.
"""

IMMUTABLE_VERSION = "immutable"
"""Backend version marker for content that never changes (e.g., Gmail emails).

Connectors returning this version skip change-detection and re-fetch logic.
"""

ROOT_ZONE_ID = "root"
"""Default zone ID for standalone (non-federated) deployments.

Every NexusFS instance has a zone_id. In standalone mode it defaults to
``"root"``. In federated mode each zone has a unique ID assigned by
the Raft consensus layer.
"""

VFS_ROOT = "/"
"""Canonical VFS root path.

Appears both as (a) the global filesystem root a user sees
(``sys_stat("/")``) and as (b) the zone-relative root key a
metastore stores the zone's own root-inode under — these happen to be
the same literal because every metastore namespace starts at ``"/"``.

Rust mirror: ``contracts::VFS_ROOT``
(``rust/contracts/src/constants.rs``).
"""
