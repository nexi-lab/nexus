// PyO3 #[pymethods] macro generates `.into()` conversions for PyErr that
// clippy flags as useless. This is a known PyO3 + clippy interaction.
#![allow(clippy::useless_conversion)]

//! PyO3 Python bindings for Nexus raft lock-state value types.
//!
//! Exposes lock-info / lock-state / holder-info as readable Python
//! data classes plus a few standalone helpers (`join_cluster` for the
//! K3s-style TLS bootstrap).
//!
//! Python reaches the raft state machine itself through `PyKernel`
//! syscalls (see `RustMetastoreProxy` in `src/nexus/core/metastore.py`).
//! No `MetaStore` / `ZoneManager` / `ZoneHandle` pyclass crosses the
//! PyO3 boundary.

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;

use crate::raft::{
    HolderInfo as RustHolderInfo, LockAcquireResult as RustLockAcquireResult,
    LockInfo as RustLockInfo,
};

// =========================================================================
// Lock-mode string constants (F4 C2) — used by HolderInfo conversions.
// =========================================================================

const LOCK_MODE_EXCLUSIVE: &str = "exclusive";
const LOCK_MODE_SHARED: &str = "shared";

/// Render a `LockMode` back to its string form for the Python side.
fn lock_mode_str(mode: crate::prelude::LockMode) -> &'static str {
    match mode {
        crate::prelude::LockMode::Exclusive => LOCK_MODE_EXCLUSIVE,
        crate::prelude::LockMode::Shared => LOCK_MODE_SHARED,
    }
}

/// Python-compatible holder info.
#[pyclass]
#[derive(Clone)]
pub struct PyHolderInfo {
    #[pyo3(get)]
    pub lock_id: String,
    #[pyo3(get)]
    pub holder_info: String,
    /// Per-holder conflict mode (F4 C2): `"exclusive"` or
    /// `"shared"`. Not to be confused with the lock-level display
    /// label ("mutex"/"semaphore"), which is computed from
    /// `max_holders` on the Python side and never stored.
    #[pyo3(get)]
    pub mode: String,
    #[pyo3(get)]
    pub acquired_at: u64,
    #[pyo3(get)]
    pub expires_at: u64,
}

impl From<RustHolderInfo> for PyHolderInfo {
    fn from(h: RustHolderInfo) -> Self {
        Self {
            lock_id: h.lock_id,
            holder_info: h.holder_info,
            mode: lock_mode_str(h.mode).to_string(),
            acquired_at: h.acquired_at,
            expires_at: h.expires_at,
        }
    }
}

/// Python-compatible lock state result.
#[pyclass]
#[derive(Clone)]
pub struct PyLockState {
    #[pyo3(get)]
    pub acquired: bool,
    #[pyo3(get)]
    pub current_holders: u32,
    #[pyo3(get)]
    pub max_holders: u32,
    #[pyo3(get)]
    pub holders: Vec<PyHolderInfo>,
}

impl From<RustLockAcquireResult> for PyLockState {
    fn from(s: RustLockAcquireResult) -> Self {
        Self {
            acquired: s.acquired,
            current_holders: s.current_holders,
            max_holders: s.max_holders,
            holders: s.holders.into_iter().map(|h| h.into()).collect(),
        }
    }
}

/// Python-compatible lock info.
#[pyclass]
#[derive(Clone)]
pub struct PyLockInfo {
    #[pyo3(get)]
    pub path: String,
    #[pyo3(get)]
    pub max_holders: u32,
    #[pyo3(get)]
    pub holders: Vec<PyHolderInfo>,
}

impl From<RustLockInfo> for PyLockInfo {
    fn from(l: RustLockInfo) -> Self {
        Self {
            path: l.path,
            max_holders: l.max_holders,
            holders: l.holders.into_iter().map(|h| h.into()).collect(),
        }
    }
}

// PyMetaStore (embedded redb metastore pyclass) deleted — no Python
// callers exist.  Python reaches the metastore through PyKernel
// syscalls (see RustMetastoreProxy in src/nexus/core/metastore.py),
// and Rust kernel callers use the in-tree MetaStore trait directly.

// =============================================================================
// R20.18.6: PyZoneManager + PyZoneHandle pyclass shells deleted.
//
// Both wrappers previously existed only to let Python construct /
// interact with raft zones. R20.18.2-R20.18.5 moved federation
// bootstrap into Kernel::init_federation_from_env and cut every
// Python caller over to PyKernel syscalls + zone_* thin shims. The
// pure-Rust `crate::zone_manager::ZoneManager` and
// `crate::zone_handle::ZoneHandle` types remain as kernel-internal
// SSOT; nothing crosses the PyO3 boundary as a ZoneHandle any more.
//
// `parse_consistency` helper was only used by the deleted pymethods;
// it went with them.
// =============================================================================

// =============================================================================
// Standalone join_cluster function (K3s-style pre-provision)
// =============================================================================

/// Join an existing cluster by provisioning TLS certificates from the leader.
///
/// Called BEFORE ZoneManager is created. Connects to the leader using TLS
/// without certificate verification (TOFU), then verifies the CA fingerprint
/// from the join token after receipt.
///
/// Args:
///     peer_address: Leader's gRPC address (e.g., "10.0.0.1:2126").
///     join_token: K3s-style join token ("K10<password>::server:<ca_fingerprint>").
///     node_id: This node's ID.
///     tls_dir: Directory to write ca.pem, node.pem, node-key.pem.
#[cfg(all(feature = "grpc", has_protos))]
#[pyfunction]
fn join_cluster(
    peer_address: &str,
    join_token: &str,
    hostname: &str,
    tls_dir: &str,
) -> PyResult<()> {
    use crate::transport::call_join_cluster;

    let node_id = crate::transport::hostname_to_node_id(hostname);

    // Parse join token: K10<password>::server:<ca_fingerprint>
    let token_prefix = "K10";
    let separator = "::server:";
    if !join_token.starts_with(token_prefix) {
        return Err(PyRuntimeError::new_err(
            "Invalid join token: must start with 'K10'",
        ));
    }
    let body = &join_token[token_prefix.len()..];
    let sep_pos = body.find(separator).ok_or_else(|| {
        PyRuntimeError::new_err("Invalid join token: missing '::server:' separator")
    })?;
    let password = &body[..sep_pos];
    let expected_fingerprint = &body[sep_pos + separator.len()..];

    if password.is_empty() {
        return Err(PyRuntimeError::new_err(
            "Invalid join token: empty password",
        ));
    }
    if !expected_fingerprint.starts_with("SHA256:") {
        return Err(PyRuntimeError::new_err(
            "Invalid join token: fingerprint must start with 'SHA256:'",
        ));
    }

    // Build endpoint URL
    let endpoint = if peer_address.starts_with("http") {
        peer_address.to_string()
    } else {
        format!("http://{}", peer_address)
    };

    // Create a temporary Tokio runtime for the blocking call
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(|e| PyRuntimeError::new_err(format!("Failed to create runtime: {}", e)))?;

    let result = runtime
        .block_on(call_join_cluster(
            &endpoint, node_id, "", // node_address — not needed for pre-provision
            "root", password, 30, // timeout_secs
        ))
        .map_err(|e| PyRuntimeError::new_err(format!("JoinCluster RPC failed: {}", e)))?;

    // Verify CA fingerprint matches the join token
    let ca_fingerprint = crate::transport::certgen::ca_fingerprint_from_pem(&result.ca_pem)
        .map_err(|e| PyRuntimeError::new_err(format!("Failed to compute CA fingerprint: {}", e)))?;
    if ca_fingerprint != expected_fingerprint {
        return Err(PyRuntimeError::new_err(format!(
            "CA fingerprint mismatch: expected '{}', got '{}'",
            expected_fingerprint, ca_fingerprint
        )));
    }

    // Write certs to disk
    let dir = std::path::Path::new(tls_dir);
    std::fs::create_dir_all(dir)
        .map_err(|e| PyRuntimeError::new_err(format!("Failed to create TLS dir: {}", e)))?;

    std::fs::write(dir.join("ca.pem"), &result.ca_pem)
        .map_err(|e| PyRuntimeError::new_err(format!("Failed to write ca.pem: {}", e)))?;
    std::fs::write(dir.join("node.pem"), &result.node_cert_pem)
        .map_err(|e| PyRuntimeError::new_err(format!("Failed to write node.pem: {}", e)))?;

    // Write private key with restricted permissions
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        let mut opts = std::fs::OpenOptions::new();
        opts.write(true).create(true).truncate(true).mode(0o600);
        use std::io::Write;
        let mut f = opts
            .open(dir.join("node-key.pem"))
            .map_err(|e| PyRuntimeError::new_err(format!("Failed to write node-key.pem: {}", e)))?;
        f.write_all(&result.node_key_pem)
            .map_err(|e| PyRuntimeError::new_err(format!("Failed to write node-key.pem: {}", e)))?;
    }
    #[cfg(not(unix))]
    {
        std::fs::write(dir.join("node-key.pem"), &result.node_key_pem)
            .map_err(|e| PyRuntimeError::new_err(format!("Failed to write node-key.pem: {}", e)))?;
    }

    Ok(())
}

/// Derive a deterministic node ID from a hostname (exposed to Python).
#[cfg(all(feature = "grpc", has_protos))]
#[pyfunction]
fn hostname_to_node_id(hostname: &str) -> u64 {
    crate::transport::hostname_to_node_id(hostname)
}

/// Register raft's PyO3 classes on the calling crate's Python module.
///
/// F2 C8 (Option A): raft is an rlib inside the ``nexus_kernel`` cdylib
/// now — the old ``#[pymodule] fn _nexus_raft`` is gone. Kernel's own
/// ``#[pymodule]`` calls this function to expose ``MetaStore`` /
/// ``ZoneManager`` / ``ZoneHandle`` from the single ``nexus_kernel``
/// Python module. Kept ``pub`` so ``kernel::lib::nexus_kernel`` can
/// reach it via the ``nexus_raft_lib::register_python_classes`` path.
pub fn register_python_classes(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyLockState>()?;
    m.add_class::<PyLockInfo>()?;
    m.add_class::<PyHolderInfo>()?;
    // R20.18.6: PyZoneManager + PyZoneHandle pyclass registrations removed
    // with the pyclasses themselves. Python drives zones via kernel's
    // zone_* PyO3 methods + federation_rpc shim.
    #[cfg(all(feature = "grpc", has_protos))]
    m.add_function(wrap_pyfunction!(join_cluster, m)?)?;
    #[cfg(all(feature = "grpc", has_protos))]
    m.add_function(wrap_pyfunction!(hostname_to_node_id, m)?)?;

    #[cfg(feature = "grpc")]
    {
        use crate::federation::tofu::{PyTofuTrustStore, PyTrustedZone};
        m.add_class::<PyTofuTrustStore>()?;
        m.add_class::<PyTrustedZone>()?;
    }
    Ok(())
}

// =============================================================================
// Unit tests: federation mount helpers (R16.1b)
// =============================================================================
//
// End-to-end mount success / idempotent / auto-create paths need a full
// ZoneConsensus + tokio runtime; those are exercised by the federation
// E2E suite (docker) gated at R12. Here we cover the pure helper
// surface that backs those flows — encoder, decoder, and field fidelity.

#[cfg(all(test, feature = "grpc", has_protos))]
mod mount_helpers_tests {
    use crate::zone_manager::{
        decode_file_metadata, encode_file_metadata, path_matches_prefix, DT_DIR, DT_MOUNT,
        I_LINKS_COUNT_KEY,
    };

    /// Mount + dir entries round-trip through encode/decode with the
    /// expected field fidelity: DT_MOUNT keeps ``target_zone_id``,
    /// DT_DIR carries empty ``target_zone_id``, and at a shared path
    /// the two only differ in ``entry_type`` / ``target_zone_id``
    /// (the identifying pair after the schema cleanup that dropped
    /// ``backend_name``).
    #[test]
    fn encode_file_metadata_roundtrip_fidelity() {
        let mount_bytes = encode_file_metadata("/x", DT_MOUNT, "zone-a", "zone-b");
        let dir_bytes = encode_file_metadata("/x", DT_DIR, "zone-a", "");

        let m = decode_file_metadata(&mount_bytes).unwrap();
        assert_eq!(m.path, "/x");
        assert_eq!(m.entry_type, DT_MOUNT);
        assert_eq!(m.zone_id, "zone-a");
        assert_eq!(m.target_zone_id, "zone-b");

        let d = decode_file_metadata(&dir_bytes).unwrap();
        assert_eq!(d.entry_type, DT_DIR);
        assert_eq!(d.target_zone_id, "");

        // Mount + dir at the same path differ only in entry_type / target_zone_id.
        assert_eq!(m.path, d.path);
        assert_eq!(m.zone_id, d.zone_id);
        assert_ne!(m.entry_type, d.entry_type);
        assert_ne!(m.target_zone_id, d.target_zone_id);
    }

    /// R16.3 boundary: accepts self + descendants separated by ``/``,
    /// rejects siblings with shared stems and non-descendants, matches
    /// everything when the normalized prefix is empty (share-the-whole-
    /// zone path). Covered in one table-driven test.
    #[test]
    fn path_matches_prefix_matrix() {
        let cases: &[(&str, &str, bool)] = &[
            // (path, prefix, expected)
            ("/usr/alice", "/usr/alice", true),         // self
            ("/usr/alice/", "/usr/alice", true),        // trailing slash
            ("/usr/alice/foo", "/usr/alice", true),     // direct child
            ("/usr/alice/foo/bar", "/usr/alice", true), // grandchild
            ("/usr/alicebob", "/usr/alice", false),     // sibling — shared stem
            ("/usr/alice-temp", "/usr/alice", false),   // sibling — shared stem
            ("/usr", "/usr/alice", false),              // ancestor, not descendant
            ("/etc/passwd", "/usr/alice", false),       // unrelated
            ("/", "", true),                            // empty prefix ≡ whole zone
            ("/a", "", true),
            ("/foo/bar", "", true),
        ];
        for (path, prefix, expected) in cases {
            assert_eq!(
                path_matches_prefix(path, prefix),
                *expected,
                "path_matches_prefix({path:?}, {prefix:?})",
            );
        }
    }

    #[test]
    fn i_links_count_key_matches_python_constant() {
        // Guard rail: the Rust constant must match
        // ``RaftMetadataStore._KEY_LINKS_COUNT`` in
        // ``src/nexus/storage/raft_metadata_store.py`` — a mismatch here
        // means Rust-side AdjustCounter writes to a different raft-log key
        // than the Python reader expects, and federation nlink tracking
        // silently diverges.
        assert_eq!(I_LINKS_COUNT_KEY, "__i_links_count__");
    }
}
