//! Cross-tier constants — mirror of ``src/nexus/contracts/constants.py``.
//!
//! Single source of truth for magic values referenced by more than one
//! crate (``kernel``, ``raft``, ``transport``, …). Add new primitives
//! sparingly — the bar is "used by two or more crates/tiers".

/// Canonical root zone identifier.
///
/// Every path routed by the kernel carries an implicit zone; the
/// default is this value. Mirrors
/// ``nexus.contracts.constants.ROOT_ZONE_ID``.
pub const ROOT_ZONE_ID: &str = "root";

/// Canonical VFS root path.
///
/// Appears both as (a) the global filesystem root a user sees
/// (``sys_stat("/")``) and as (b) the zone-relative root key a
/// metastore stores the zone's own root-inode under — these happen
/// to be the same literal because every metastore namespace starts
/// at ``"/"``.
///
/// Use this constant at semantic sites (mount-point comparisons,
/// zone-key root detection, translation boundary in
/// ``ZoneMetaStore``). The literal ``"/"`` is still fine for
/// unambiguous string-splitting / delimiter uses where readers
/// aren't asked to disambiguate "which root?".
pub const VFS_ROOT: &str = "/";

/// BLAKE3 hash of the empty byte string — used as the canonical ETag
/// for zero-content inodes (DT_DIR, empty files). Mirrors the Python
/// ``nexus.core.hash_utils.BLAKE3_EMPTY`` constant.
pub const BLAKE3_EMPTY: &str = "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262";

/// Kernel-reserved path prefix for internal system entries
/// (mount table, ReBAC namespace store, ReBAC version store, zone
/// revisions, …).
///
/// Mirrors `nexus.contracts.constants.SYSTEM_PATH_PREFIX` on the
/// Python side. Stored in MetastoreABC like any other entry, but
/// filtered from user-visible operations and **must be skipped by
/// every dispatch hook**. Without that hook self-exclusion,
/// `sys_read` on a hook's own `/__sys__/...` config (e.g. ReBAC
/// namespace reload) re-enters the same hook, which re-reads the
/// config, which re-enters … unbounded recursion (PR #3890 CI
/// hang). Use [`is_system_path`] at every `on_pre` / `on_post`
/// entry that does not specifically target the system namespace.
pub const SYSTEM_PATH_PREFIX: &str = "/__sys__/";

/// `true` when `path` falls under the kernel-internal system
/// namespace [`SYSTEM_PATH_PREFIX`]. Hook implementations call this
/// at the top of their `on_pre` / `on_post` and short-circuit
/// (`Pass` / no-op) so kernel-internal sys_read/sys_write inside
/// hook bodies cannot recurse.
#[inline]
pub fn is_system_path(path: &str) -> bool {
    path.starts_with(SYSTEM_PATH_PREFIX)
}

/// Kernel-reserved virtual path prefix for advisory lock enumeration.
/// `readdir("/__sys__/locks")` enumerates active advisory locks via
/// `LockManager::list_locks` — admin-only, analogous to `/proc/locks`.
pub const LOCKS_PATH_PREFIX: &str = "/__sys__/locks";

/// Path prefix used in the root zone's state machine to hold the
/// federation share registry (SSOT for `origin_path → zone_id`).
///
/// `federation_share` writes one `FileMetadata` entry under this
/// prefix per shared subtree; `federation_join` looks it up to
/// discover the zone id advertised by a peer.  Because the registry
/// lives in root-zone raft state, every cluster member already has
/// the up-to-date mapping — no separate peer-discovery RPC needed.
///
/// Double-underscore convention matches the existing `/__sys__/`
/// procfs-style reserved prefix.
pub const SHARE_REGISTRY_PREFIX: &str = "/__shares__";

/// Environment variable names — SSOT for env lookups crossing crate
/// boundaries. Anything referenced by two or more crates goes here;
/// crate-local env vars can stay inlined.
///
/// Aligned with Python: `src/nexus/cli/utils.py` mirrors the same
/// names. Rename = Python-side mirror update in the same PR.
pub mod env {
    /// Peer-reachable address this node publishes (host:port).
    ///
    /// SSOT for "where can other nodes reach me?". Raft transport uses
    /// it for cluster peering and the co-located `ReadBlob` RPC on the
    /// raft port. Follows the etcd
    /// `--initial-advertise-peer-urls` / CockroachDB `--advertise-addr`
    /// convention: inter-node services share one advertised address.
    pub const ADVERTISE_ADDR: &str = "NEXUS_ADVERTISE_ADDR";

    /// Socket this node binds its raft gRPC server on. Defaults to
    /// `0.0.0.0:2126`. Parsed to derive the default raft port when
    /// `ADVERTISE_ADDR` is unset.
    pub const BIND_ADDR: &str = "NEXUS_BIND_ADDR";
}

/// Maximum gRPC message size (bytes) for the unified VFS service.
///
/// Applies to every client/server that talks to `NexusVFSService`:
/// Python server (`grpc.aio.server(options=...)`), Python client
/// (`nexus.grpc.defaults.build_channel_options`), and the Rust peer-
/// blob client (`tonic` `max_decoding/encoding_message_size`).
///
/// 64 MiB accommodates files above the 16 MiB CDC chunk threshold —
/// both single-blob content reads and scatter-gather `ReadBlob`
/// responses. Raising this value requires bumping both the Python
/// mirror (`nexus.contracts.constants.MAX_GRPC_MESSAGE_BYTES`) and
/// this constant in lockstep.
pub const MAX_GRPC_MESSAGE_BYTES: usize = 64 * 1024 * 1024;

// ── gRPC server hardening defaults ─────────────────────────────────
//
// Conservative production defaults shared by every tonic gRPC server
// nexus runs (raft transport, witness, VFS). Sized for federation
// traffic patterns: dozens of concurrent peer streams + intermittent
// VFS client RPCs. Tune via runtime config when a workload demands
// otherwise; the defaults stay here to keep ad-hoc Server::builder
// sites from drifting apart.

/// Maximum concurrent HTTP/2 streams the server will accept per
/// connection. 1024 is comfortably above any expected per-peer
/// concurrency (raft heartbeat / append / snapshot share one
/// connection per peer); cap exists to fail closed under runaway
/// stream creation rather than exhausting the server allocator.
pub const GRPC_MAX_CONCURRENT_STREAMS: u32 = 1024;

/// HTTP/2 keepalive ping interval. Servers send a PING every
/// `GRPC_HTTP2_KEEPALIVE_INTERVAL_SECS` so an idle TCP connection
/// dropped by an intermediate NAT / load balancer is detected
/// quickly instead of hanging until the next RPC. 30s matches the
/// gRPC default-server recommendation.
pub const GRPC_HTTP2_KEEPALIVE_INTERVAL_SECS: u64 = 30;

/// HTTP/2 keepalive PING ack deadline. If no PONG arrives within
/// `GRPC_HTTP2_KEEPALIVE_TIMEOUT_SECS` of the PING, the connection
/// is considered dead and torn down. 10s gives a slow peer one
/// full retry window without holding a half-dead connection open
/// indefinitely.
pub const GRPC_HTTP2_KEEPALIVE_TIMEOUT_SECS: u64 = 10;

/// TCP-level keepalive on each accepted connection (sent at the
/// kernel socket layer). Catches half-open connections the HTTP/2
/// layer can't reach (e.g. when the peer process disappears
/// silently without RST). 60s aligns with common Linux defaults.
pub const GRPC_TCP_KEEPALIVE_SECS: u64 = 60;

// ── tokio runtime sizing ───────────────────────────────────────────

/// Floor on worker threads for a runtime that hosts a tonic gRPC
/// server alongside the raft transport loops.
///
/// The federation server multiplexes latency-sensitive work (the
/// accept loop + per-connection HTTP/2 handshakes) with blocking work
/// (each zone's `transport_loop` performs synchronous redb disk I/O in
/// `advance`). If the worker pool is too small, a burst of blocking
/// loop work — or a flood of inbound connection attempts — can starve
/// the accept/handshake path: new connections complete the TCP
/// handshake (kernel backlog) but never receive an HTTP/2 SETTINGS
/// frame, so clients time out while existing connections limp along.
/// Four workers keep the accept path live even on a 2-core host
/// running multiple zones.
pub const MIN_SERVER_RUNTIME_WORKERS: usize = 4;

/// Worker-thread count for a multi-threaded tokio runtime, sized to the
/// host's available parallelism with a floor of `min_workers`.
///
/// SSOT for "how many workers should this runtime get?" — every
/// `Builder::new_multi_thread()` site shares it so the daemon's outer
/// runtime and the federation server's inner runtime scale identically
/// instead of drifting to ad-hoc hardcoded counts. IO-bound gRPC + raft
/// work wants roughly one worker per logical core (cgroup / affinity
/// constrained), which `available_parallelism` reports.
#[must_use]
pub fn recommended_worker_threads(min_workers: usize) -> usize {
    std::thread::available_parallelism()
        .map(std::num::NonZeroUsize::get)
        .unwrap_or(min_workers)
        .max(min_workers)
}
