//! TOFU (Trust On First Use) store for peer zone CA certificates.
//!
//! Modelled after SSH ``known_hosts``: on first contact the peer zone's
//! CA fingerprint is pinned; on subsequent connections the fingerprint
//! is verified. If it changes, the caller receives
//! [`TofuError::FingerprintMismatch`] and must explicitly forget the
//! zone (``nexus tls forget-zone``) before reconnecting.
//!
//! # On-disk format
//!
//! JSONL, one [`TrustedZone`] per line:
//!
//! ```text
//! {"zone_id":"shared","ca_fingerprint":"SHA256:abc...","ca_pem":"-----BEGIN...",
//!  "first_seen":"2026-02-27T10:30:00Z","last_verified":"...",
//!  "peer_addresses":["10.0.0.2:2126"]}
//! ```
//!
//! # Cryptographic primitives
//!
//! - Fingerprint = ``SHA256:{base64(sha256(DER))}`` (no ``=`` padding),
//!   SSH-style.
//! - DER extraction goes through the ``pem`` crate so callers can pass
//!   either PEM bytes or raw DER via [`TofuTrustStore::verify_or_trust`].

use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};

use base64::Engine as _;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use time::OffsetDateTime;

#[cfg(feature = "python")]
use pyo3::exceptions::{PyIOError, PyRuntimeError, PyValueError};
#[cfg(feature = "python")]
use pyo3::prelude::*;
#[cfg(feature = "python")]
use std::sync::Mutex;

/// Outcome of [`TofuTrustStore::verify_or_trust`]. Serialized as
/// ``"trusted_new"`` / ``"trusted_known"`` over the PyO3 boundary.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TofuResult {
    /// First contact with this ``zone_id`` — fingerprint was pinned.
    TrustedNew,
    /// Known ``zone_id`` whose fingerprint matched — metadata updated.
    TrustedKnown,
}

impl TofuResult {
    pub fn as_str(self) -> &'static str {
        match self {
            TofuResult::TrustedNew => "trusted_new",
            TofuResult::TrustedKnown => "trusted_known",
        }
    }
}

/// Errors surfaced by [`TofuTrustStore`] operations.
#[derive(Debug)]
pub enum TofuError {
    /// Known zone presented a different CA fingerprint. The operator
    /// must explicitly forget the zone before reconnecting — surfaced
    /// with an SSH-style "ZONE CERTIFICATE CHANGED" banner.
    FingerprintMismatch {
        zone_id: String,
        expected: String,
        got: String,
    },
    /// Failed to parse the supplied PEM / DER certificate bytes.
    InvalidCertificate(String),
    /// Filesystem I/O error during load / save.
    Io(std::io::Error),
    /// JSONL parse / serialize error.
    Serde(serde_json::Error),
}

impl std::fmt::Display for TofuError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            TofuError::FingerprintMismatch {
                zone_id,
                expected,
                got,
            } => write!(
                f,
                "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n\
                 @    WARNING: ZONE CERTIFICATE CHANGED!    @\n\
                 @@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n\
                 Zone '{zone_id}' CA fingerprint changed.\n  \
                 Expected: {expected}\n  \
                 Got:      {got}\n\
                 This could indicate a MITM attack or certificate rotation.\n\
                 If expected, run: nexus tls forget-zone {zone_id}",
            ),
            TofuError::InvalidCertificate(msg) => write!(f, "invalid certificate: {msg}"),
            TofuError::Io(e) => write!(f, "tofu store I/O: {e}"),
            TofuError::Serde(e) => write!(f, "tofu store JSON: {e}"),
        }
    }
}

impl std::error::Error for TofuError {}

impl From<std::io::Error> for TofuError {
    fn from(e: std::io::Error) -> Self {
        TofuError::Io(e)
    }
}

impl From<serde_json::Error> for TofuError {
    fn from(e: serde_json::Error) -> Self {
        TofuError::Serde(e)
    }
}

/// A pinned zone entry. Field names / order are the on-disk JSONL
/// schema — preserve them when adding fields (prepend with
/// ``#[serde(default)]`` so old stores keep loading).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TrustedZone {
    pub zone_id: String,
    pub ca_fingerprint: String,
    pub ca_pem: String,
    pub first_seen: String,
    pub last_verified: String,
    #[serde(default)]
    pub peer_addresses: Vec<String>,
}

/// File-backed TOFU trust store.
///
/// Safe for shared use behind a [`Mutex`] — the struct itself is
/// `!Sync` via the owned [`HashMap`], and the PyO3 wrapper
/// [`PyTofuTrustStore`] serializes access through `Mutex`.
pub struct TofuTrustStore {
    path: PathBuf,
    entries: HashMap<String, TrustedZone>,
}

impl TofuTrustStore {
    /// Open an on-disk trust store; creates nothing on disk until the
    /// first mutation. Missing files are treated as an empty store.
    pub fn open(path: impl Into<PathBuf>) -> Result<Self, TofuError> {
        let path = path.into();
        let entries = Self::load(&path)?;
        Ok(Self { path, entries })
    }

    fn load(path: &Path) -> Result<HashMap<String, TrustedZone>, TofuError> {
        if !path.exists() {
            return Ok(HashMap::new());
        }
        let content = fs::read_to_string(path)?;
        let mut entries: HashMap<String, TrustedZone> = HashMap::new();
        for line in content.lines() {
            let line = line.trim();
            if line.is_empty() {
                continue;
            }
            match serde_json::from_str::<TrustedZone>(line) {
                Ok(entry) => {
                    entries.insert(entry.zone_id.clone(), entry);
                }
                Err(e) => {
                    // Corrupt lines must not wedge startup — log and skip.
                    tracing::warn!(error = %e, "skipping malformed trust store entry");
                }
            }
        }
        Ok(entries)
    }

    fn save(&self) -> Result<(), TofuError> {
        if let Some(parent) = self.path.parent() {
            fs::create_dir_all(parent)?;
        }
        let mut buf = String::new();
        for entry in self.entries.values() {
            buf.push_str(&serde_json::to_string(entry)?);
            buf.push('\n');
        }
        fs::write(&self.path, buf)?;
        Ok(())
    }

    /// Compute the SSH-style ``SHA256:{base64}`` fingerprint for a
    /// PEM-encoded certificate via ``pem::parse`` → DER → SHA-256.
    pub fn fingerprint_pem(ca_pem: &[u8]) -> Result<String, TofuError> {
        let parsed = pem::parse(ca_pem)
            .map_err(|e| TofuError::InvalidCertificate(format!("PEM parse: {e}")))?;
        Ok(Self::fingerprint_der(parsed.contents()))
    }

    /// Fingerprint a DER-encoded certificate (used internally and by
    /// callers who already have DER bytes).
    pub fn fingerprint_der(der: &[u8]) -> String {
        let digest = Sha256::digest(der);
        let b64 = base64::engine::general_purpose::STANDARD_NO_PAD.encode(digest);
        format!("SHA256:{b64}")
    }

    /// Pin a new zone's fingerprint on first contact; verify on
    /// subsequent contact. Peer addresses accumulate without dupes.
    ///
    /// ``ca_pem`` must be the PEM-encoded zone CA certificate.
    pub fn verify_or_trust(
        &mut self,
        zone_id: &str,
        ca_pem: &[u8],
        peer_address: &str,
    ) -> Result<TofuResult, TofuError> {
        let fp = Self::fingerprint_pem(ca_pem)?;
        let now = OffsetDateTime::now_utc()
            .format(&time::format_description::well_known::Rfc3339)
            .map_err(|e| TofuError::InvalidCertificate(format!("timestamp format: {e}")))?;

        if let Some(existing) = self.entries.get_mut(zone_id) {
            if existing.ca_fingerprint != fp {
                return Err(TofuError::FingerprintMismatch {
                    zone_id: zone_id.to_string(),
                    expected: existing.ca_fingerprint.clone(),
                    got: fp,
                });
            }
            existing.last_verified = now;
            if !existing.peer_addresses.iter().any(|a| a == peer_address) {
                existing.peer_addresses.push(peer_address.to_string());
            }
            self.save()?;
            return Ok(TofuResult::TrustedKnown);
        }

        // First contact — pin. PEM is ASCII by definition (RFC 7468);
        // a non-ASCII body here means the caller handed us binary.
        let ca_pem_str = std::str::from_utf8(ca_pem)
            .map_err(|e| TofuError::InvalidCertificate(format!("PEM not valid ASCII: {e}")))?
            .to_string();
        let entry = TrustedZone {
            zone_id: zone_id.to_string(),
            ca_fingerprint: fp.clone(),
            ca_pem: ca_pem_str,
            first_seen: now.clone(),
            last_verified: now,
            peer_addresses: vec![peer_address.to_string()],
        };
        self.entries.insert(zone_id.to_string(), entry);
        self.save()?;
        tracing::info!(
            zone = %zone_id,
            fingerprint = %fp,
            peer = %peer_address,
            "TOFU: pinned zone CA fingerprint",
        );
        Ok(TofuResult::TrustedNew)
    }

    /// Retrieve the trusted CA PEM for a zone (``None`` if unknown).
    pub fn ca_pem(&self, zone_id: &str) -> Option<&str> {
        self.entries.get(zone_id).map(|e| e.ca_pem.as_str())
    }

    /// Drop a zone from the store. Returns ``true`` if the entry existed.
    pub fn remove(&mut self, zone_id: &str) -> Result<bool, TofuError> {
        let existed = self.entries.remove(zone_id).is_some();
        if existed {
            self.save()?;
            tracing::info!(zone = %zone_id, "TOFU: removed zone from trust store");
        }
        Ok(existed)
    }

    /// Snapshot of every trusted zone (cheap clone of owned strings).
    pub fn list_trusted(&self) -> Vec<TrustedZone> {
        self.entries.values().cloned().collect()
    }

    /// Write a combined CA bundle (local CA first, followed by every
    /// trusted zone CA) to ``{store_dir}/ca-bundle.pem``. Returns the
    /// path written. Callers pass this path to gRPC channels that
    /// need to trust multiple CAs simultaneously.
    pub fn build_ca_bundle(&self, local_ca_path: &Path) -> Result<PathBuf, TofuError> {
        let bundle_path = self
            .path
            .parent()
            .map(|p| p.join("ca-bundle.pem"))
            .unwrap_or_else(|| PathBuf::from("ca-bundle.pem"));
        if let Some(parent) = bundle_path.parent() {
            fs::create_dir_all(parent)?;
        }
        let mut parts: Vec<String> = Vec::new();
        if local_ca_path.exists() {
            parts.push(fs::read_to_string(local_ca_path)?.trim().to_string());
        }
        for entry in self.entries.values() {
            parts.push(entry.ca_pem.trim().to_string());
        }
        let joined = if parts.is_empty() {
            String::new()
        } else {
            let mut s = parts.join("\n");
            s.push('\n');
            s
        };
        fs::write(&bundle_path, joined)?;
        Ok(bundle_path)
    }

    /// Filesystem path this store persists to (for debug / error messages).
    pub fn path(&self) -> &Path {
        &self.path
    }
}

// ─────────────────────────────────────────────────────────────────────
// PyO3 surface — only compiled when the ``python`` feature is on.
// Server-only builds (e.g. ``nexus-witness`` / ``nexus-federation-server``)
// skip this block and link against the pure-Rust ``TofuTrustStore``.
// ─────────────────────────────────────────────────────────────────────

#[cfg(feature = "python")]
/// Read-only view of a trusted zone entry exposed to Python.
#[pyclass(name = "TrustedZone", get_all)]
#[derive(Debug, Clone)]
pub struct PyTrustedZone {
    pub zone_id: String,
    pub ca_fingerprint: String,
    pub ca_pem: String,
    pub first_seen: String,
    pub last_verified: String,
    pub peer_addresses: Vec<String>,
}

#[cfg(feature = "python")]
impl From<TrustedZone> for PyTrustedZone {
    fn from(t: TrustedZone) -> Self {
        Self {
            zone_id: t.zone_id,
            ca_fingerprint: t.ca_fingerprint,
            ca_pem: t.ca_pem,
            first_seen: t.first_seen,
            last_verified: t.last_verified,
            peer_addresses: t.peer_addresses,
        }
    }
}

/// PyO3 wrapper for the file-backed trust store. Serializes concurrent
/// writes through an internal ``Mutex`` so callers can hand the same
/// instance to multiple threads without guarding it themselves.
#[cfg(feature = "python")]
#[pyclass(name = "TofuTrustStore")]
pub struct PyTofuTrustStore {
    inner: Mutex<TofuTrustStore>,
}

#[cfg(feature = "python")]
#[pymethods]
impl PyTofuTrustStore {
    /// Open (or create-on-first-write) a trust store at ``path``.
    #[new]
    pub fn py_new(path: &str) -> PyResult<Self> {
        let inner = TofuTrustStore::open(path).map_err(tofu_error_to_py)?;
        Ok(Self {
            inner: Mutex::new(inner),
        })
    }

    /// Verify or pin a peer zone's CA. ``ca_pem`` must be the
    /// PEM-encoded zone CA certificate bytes.
    ///
    /// Returns ``"trusted_new"`` (first contact, pinned) or
    /// ``"trusted_known"`` (fingerprint matched). Raises ``RuntimeError``
    /// with the SSH-style "@@@@ ZONE CERTIFICATE CHANGED @@@@" banner
    /// on fingerprint mismatch.
    pub fn verify_or_trust(
        &self,
        zone_id: &str,
        ca_pem: &[u8],
        peer_address: &str,
    ) -> PyResult<String> {
        let mut guard = self.lock_inner()?;
        let result = guard
            .verify_or_trust(zone_id, ca_pem, peer_address)
            .map_err(tofu_error_to_py)?;
        Ok(result.as_str().to_string())
    }

    /// Drop a zone from the store. Returns ``True`` if it existed.
    pub fn remove(&self, zone_id: &str) -> PyResult<bool> {
        let mut guard = self.lock_inner()?;
        guard.remove(zone_id).map_err(tofu_error_to_py)
    }

    /// Look up a zone's trusted CA PEM, as ``bytes``. Returns
    /// ``None`` when the zone is not in the store.
    pub fn get_ca_pem(&self, zone_id: &str) -> PyResult<Option<Vec<u8>>> {
        let guard = self.lock_inner()?;
        Ok(guard.ca_pem(zone_id).map(|s| s.as_bytes().to_vec()))
    }

    /// Snapshot every trusted zone (preserves insertion order via
    /// ``HashMap`` values — callers that need deterministic ordering
    /// should sort by ``zone_id``).
    pub fn list_trusted(&self) -> PyResult<Vec<PyTrustedZone>> {
        let guard = self.lock_inner()?;
        Ok(guard
            .list_trusted()
            .into_iter()
            .map(PyTrustedZone::from)
            .collect())
    }

    /// Write ``ca-bundle.pem`` alongside the store file containing
    /// the local CA and every trusted zone CA. Returns the path
    /// written, as a string.
    pub fn build_ca_bundle(&self, local_ca_path: &str) -> PyResult<String> {
        let guard = self.lock_inner()?;
        let path = guard
            .build_ca_bundle(Path::new(local_ca_path))
            .map_err(tofu_error_to_py)?;
        Ok(path.to_string_lossy().into_owned())
    }

    /// Path this store persists to (for debugging / error messages).
    pub fn path(&self) -> PyResult<String> {
        let guard = self.lock_inner()?;
        Ok(guard.path().to_string_lossy().into_owned())
    }
}

#[cfg(feature = "python")]
impl PyTofuTrustStore {
    fn lock_inner(&self) -> PyResult<std::sync::MutexGuard<'_, TofuTrustStore>> {
        self.inner
            .lock()
            .map_err(|_| PyRuntimeError::new_err("TofuTrustStore mutex poisoned"))
    }
}

#[cfg(feature = "python")]
fn tofu_error_to_py(e: TofuError) -> PyErr {
    match e {
        TofuError::FingerprintMismatch { .. } => PyRuntimeError::new_err(e.to_string()),
        TofuError::InvalidCertificate(_) => PyValueError::new_err(e.to_string()),
        TofuError::Io(_) => PyIOError::new_err(e.to_string()),
        TofuError::Serde(_) => PyRuntimeError::new_err(e.to_string()),
    }
}

// ─────────────────────────────────────────────────────────────────────
// Tests
// ─────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    /// Generate a self-signed CA for a given ``zone_id`` and return
    /// its PEM bytes. Shared by every test below.
    fn make_ca_pem(zone_id: &str) -> Vec<u8> {
        use rcgen::{CertificateParams, DistinguishedName, DnType, KeyPair};
        let mut params = CertificateParams::new(vec![]).expect("params");
        let mut dn = DistinguishedName::new();
        dn.push(DnType::OrganizationName, "Nexus");
        dn.push(DnType::CommonName, format!("nexus-zone-{zone_id}-ca"));
        params.distinguished_name = dn;
        let key = KeyPair::generate().expect("keypair");
        let cert = params.self_signed(&key).expect("sign");
        cert.pem().into_bytes()
    }

    /// Operator-scale integration flow: first contact pins two zones
    /// → fingerprint format sanity-check → re-verify accumulates peer
    /// addresses without duplicates → lookup + list surface the
    /// pinned entries → a rotated cert is rejected with the SSH-style
    /// banner → forget-zone + re-pin cycles the entry back to
    /// TrustedNew → build_ca_bundle stitches local CA + every pinned
    /// zone CA into one PEM file. Covers every public method of
    /// TofuTrustStore against real rcgen-generated certs.
    #[test]
    fn operator_trust_lifecycle() {
        let dir = tempfile::tempdir().unwrap();
        let mut store = TofuTrustStore::open(dir.path().join("known")).unwrap();
        let ca_a = make_ca_pem("zone-a");
        let ca_b = make_ca_pem("zone-b");

        // First contact — TrustedNew, SSH-style fingerprint.
        assert_eq!(
            store
                .verify_or_trust("zone-a", &ca_a, "10.0.0.1:2126")
                .unwrap(),
            TofuResult::TrustedNew,
        );
        let fp_a = store.list_trusted()[0].ca_fingerprint.clone();
        assert!(fp_a.starts_with("SHA256:"));
        assert_eq!(fp_a.len(), "SHA256:".len() + 43); // 32-byte hash → 43 base64 no-pad chars
        assert!(!fp_a.contains('='));

        // Second zone pins cleanly too.
        store
            .verify_or_trust("zone-b", &ca_b, "10.0.0.2:2126")
            .unwrap();

        // Re-verify from a new peer → TrustedKnown + accumulation.
        // Duplicate peer address is de-duped.
        assert_eq!(
            store
                .verify_or_trust("zone-a", &ca_a, "10.0.0.3:2126")
                .unwrap(),
            TofuResult::TrustedKnown,
        );
        store
            .verify_or_trust("zone-a", &ca_a, "10.0.0.1:2126")
            .unwrap();

        // Lookup + list.
        assert!(store
            .ca_pem("zone-a")
            .unwrap()
            .contains("BEGIN CERTIFICATE"));
        assert!(store.ca_pem("nope").is_none());
        let ids: Vec<_> = store
            .list_trusted()
            .into_iter()
            .map(|t| t.zone_id)
            .collect();
        assert!(ids.contains(&"zone-a".into()) && ids.contains(&"zone-b".into()));
        let a = store
            .list_trusted()
            .into_iter()
            .find(|t| t.zone_id == "zone-a")
            .unwrap();
        assert_eq!(a.peer_addresses.len(), 2);

        // Rotated cert → explicit mismatch with SSH banner.
        let rotated = make_ca_pem("zone-a");
        let err = store
            .verify_or_trust("zone-a", &rotated, "10.0.0.1:2126")
            .unwrap_err();
        assert!(matches!(err, TofuError::FingerprintMismatch { .. }));
        assert!(err.to_string().contains("ZONE CERTIFICATE CHANGED"));

        // forget-zone + re-pin cycles back to TrustedNew.
        assert!(store.remove("zone-a").unwrap());
        assert!(!store.remove("zone-a").unwrap()); // second call: false
        assert_eq!(
            store
                .verify_or_trust("zone-a", &ca_a, "10.0.0.1:2126")
                .unwrap(),
            TofuResult::TrustedNew,
        );

        // Build CA bundle: local CA + zone-a + zone-b = 3 blocks.
        let local_ca = dir.path().join("ca.pem");
        std::fs::write(&local_ca, make_ca_pem("local")).unwrap();
        let bundle_path = store.build_ca_bundle(&local_ca).unwrap();
        let bundle = std::fs::read_to_string(&bundle_path).unwrap();
        assert_eq!(bundle.matches("BEGIN CERTIFICATE").count(), 3);
    }

    /// Cross-process lifecycle: a store opened, pinned, then dropped
    /// must re-hydrate from JSONL so a second open sees the prior
    /// fingerprint as TrustedKnown. Separate from the lifecycle test
    /// above because this one explicitly closes/reopens the store to
    /// exercise the disk serialization path.
    #[test]
    fn persistence_across_process_restart() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("known");
        let ca = make_ca_pem("persist-zone");

        TofuTrustStore::open(&path)
            .unwrap()
            .verify_or_trust("persist-zone", &ca, "10.0.0.1:2126")
            .unwrap();
        let r = TofuTrustStore::open(&path)
            .unwrap()
            .verify_or_trust("persist-zone", &ca, "10.0.0.2:2126")
            .unwrap();
        assert_eq!(r, TofuResult::TrustedKnown);
    }
}
