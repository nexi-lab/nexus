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
/// ``ZoneMetastore``). The literal ``"/"`` is still fine for
/// unambiguous string-splitting / delimiter uses where readers
/// aren't asked to disambiguate "which root?".
pub const VFS_ROOT: &str = "/";

/// BLAKE3 hash of the empty byte string — used as the canonical ETag
/// for zero-content inodes (DT_DIR, empty files). Mirrors the Python
/// ``nexus.core.hash_utils.BLAKE3_EMPTY`` constant.
pub const BLAKE3_EMPTY: &str = "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262";

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
    /// it for cluster peering; R20.18.7 reuses it for the co-located
    /// `ReadBlob` RPC on the raft port. Follows the etcd
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
