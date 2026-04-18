//! Rust client for federation peer RPCs (R16.5b).
//!
//! Replaces the Python ``_discover_mount`` (VFS sys_stat) and
//! ``_request_membership`` (ZoneApiService.JoinZone) helpers that
//! previously lived in ``src/nexus/raft/federation.py``. Runs every
//! gRPC call through a shared tokio runtime and a per-peer tonic
//! ``Channel`` pool so repeated calls reuse the HTTP/2 connection.
//!
//! mTLS + TOFU: when a [`TlsMaterial`] is attached, each channel
//! negotiates mTLS with the caller-supplied node identity and a CA
//! bundle built from the local CA plus every pinned zone CA in the
//! [`nexus_raft::federation::TofuTrustStore`]. Callers that already
//! have a bundle path can skip the store lookup and pass raw PEM bytes
//! directly.
//!
//! This module lives in ``kernel`` (not ``raft``) because it needs
//! access to BOTH proto families — ``vfs.proto`` is built only here
//! (for the ``ReadBlob`` client used by [`crate::peer_blob_client`]),
//! and ``transport.proto`` ships in the raft rlib that kernel
//! already depends on. No cross-crate proto duplication.

use std::sync::Arc;
use std::time::Duration;

use dashmap::DashMap;
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};
use tonic::transport::{Certificate, Channel, ClientTlsConfig, Endpoint, Identity};

use crate::kernel::vfs_proto;
use nexus_raft::federation::TofuTrustStore;
use nexus_raft::transport::proto::nexus::raft::{
    zone_api_service_client::ZoneApiServiceClient, JoinZoneRequest,
};

/// mTLS material supplied by the caller (Python ``ZoneTlsConfig``).
///
/// PEM bytes are stored directly; ``tonic`` re-parses them on each
/// channel build which is fine for the rare-event federation path.
#[derive(Debug, Clone)]
struct TlsMaterial {
    /// Aggregate CA bundle PEM — local CA plus every TOFU-pinned zone CA.
    ca_bundle_pem: Vec<u8>,
    /// This node's signed leaf cert PEM (client identity for mTLS).
    node_cert_pem: Vec<u8>,
    /// This node's private key PEM (client identity for mTLS).
    node_key_pem: Vec<u8>,
}

/// Per-peer channel cache + shared runtime.
///
/// Kept private to this module — the PyO3 wrapper [`PyFederationClient`]
/// is the only entry point exposed to Python.
struct FederationClient {
    runtime: Arc<tokio::runtime::Runtime>,
    channels: DashMap<String, Channel>,
    tls_material: Option<TlsMaterial>,
    timeout: Duration,
}

impl FederationClient {
    fn new(runtime: Arc<tokio::runtime::Runtime>, tls_material: Option<TlsMaterial>) -> Self {
        Self {
            runtime,
            channels: DashMap::new(),
            tls_material,
            timeout: Duration::from_secs(10),
        }
    }

    /// Fetch or build a tonic channel for ``address``.
    ///
    /// Address forms: ``host:port`` or ``http(s)://host:port``.
    /// ``https://`` is selected automatically when TLS material is
    /// attached — callers shouldn't need to pick the scheme.
    async fn channel_for(&self, address: &str) -> Result<Channel, String> {
        if let Some(ch) = self.channels.get(address) {
            return Ok(ch.clone());
        }
        let scheme = if self.tls_material.is_some() {
            "https"
        } else {
            "http"
        };
        let endpoint_str = if address.starts_with("http://") || address.starts_with("https://") {
            address.to_string()
        } else {
            format!("{scheme}://{address}")
        };

        let mut ep = Endpoint::from_shared(endpoint_str)
            .map_err(|e| format!("invalid endpoint '{address}': {e}"))?
            .connect_timeout(Duration::from_secs(5))
            .timeout(self.timeout);

        if let Some(tls) = &self.tls_material {
            let identity = Identity::from_pem(&tls.node_cert_pem, &tls.node_key_pem);
            let ca = Certificate::from_pem(&tls.ca_bundle_pem);
            let tls_cfg = ClientTlsConfig::new().identity(identity).ca_certificate(ca);
            ep = ep
                .tls_config(tls_cfg)
                .map_err(|e| format!("TLS config for '{address}': {e}"))?;
        }

        let channel = ep
            .connect()
            .await
            .map_err(|e| format!("connect {address}: {e}"))?;
        self.channels
            .entry(address.to_string())
            .or_insert_with(|| channel.clone());
        Ok(channel)
    }

    /// Discover a peer zone's DT_MOUNT target via VFS ``sys_stat``.
    ///
    /// Returns ``Some(MountInfo)`` on success, ``None`` if the peer
    /// reports the path is not found or the response is an error
    /// (``is_error == true``). Raises on transport failure or malformed
    /// JSON so the caller can distinguish "peer unreachable" from
    /// "path not a mount".
    async fn discover_mount(
        &self,
        peer_addr: &str,
        path: &str,
    ) -> Result<Option<serde_json::Value>, String> {
        let channel = self.channel_for(peer_addr).await?;
        let mut client = vfs_proto::nexus_vfs_service_client::NexusVfsServiceClient::new(channel);

        let req_json = serde_json::json!({ "path": path });
        let payload =
            serde_json::to_vec(&req_json).map_err(|e| format!("encode sys_stat request: {e}"))?;
        let mut request = tonic::Request::new(vfs_proto::CallRequest {
            method: "sys_stat".to_string(),
            payload,
            auth_token: String::new(),
        });
        request.set_timeout(self.timeout);

        let resp = client
            .call(request)
            .await
            .map_err(|e| format!("sys_stat {peer_addr}: {e}"))?
            .into_inner();

        if resp.is_error {
            // Match the Python helper's "warn then return None" behavior
            // — the caller surfaces a clear ValueError downstream.
            let err_body = String::from_utf8_lossy(&resp.payload);
            tracing::warn!(
                peer = %peer_addr,
                path = %path,
                error = %err_body,
                "sys_stat returned error",
            );
            return Ok(None);
        }

        let decoded: serde_json::Value = serde_json::from_slice(&resp.payload)
            .map_err(|e| format!("decode sys_stat response: {e}"))?;
        Ok(Some(decoded))
    }

    /// Request peer membership for a zone (ZoneApiService.JoinZone).
    ///
    /// Follows one level of leader redirect — if the initial peer is a
    /// follower and returns ``leader_address``, the call recurses against
    /// the leader. Deeper redirects are treated as a cluster bug and
    /// surfaced as an error (mirrors Python's single-level retry).
    async fn request_join_zone(
        &self,
        peer_addr: &str,
        zone_id: &str,
        node_id: u64,
        node_address: &str,
        depth: u8,
    ) -> Result<(), String> {
        if depth > 1 {
            return Err(format!(
                "JoinZone: too many leader redirects (peer={peer_addr}, zone={zone_id})"
            ));
        }
        let channel = self.channel_for(peer_addr).await?;
        let mut client = ZoneApiServiceClient::new(channel);

        let mut request = tonic::Request::new(JoinZoneRequest {
            zone_id: zone_id.to_string(),
            node_id,
            node_address: node_address.to_string(),
        });
        request.set_timeout(self.timeout);

        let resp = client
            .join_zone(request)
            .await
            .map_err(|e| format!("JoinZone {peer_addr}: {e}"))?
            .into_inner();

        if !resp.success {
            if let Some(leader) = resp.leader_address.filter(|s| !s.is_empty()) {
                tracing::info!(
                    peer = %peer_addr,
                    leader = %leader,
                    zone = %zone_id,
                    "JoinZone redirected to leader",
                );
                // Recurse against the leader. Box the future so the
                // recursive async call compiles (async-fn recursion
                // needs explicit indirection in Rust).
                let fut = Box::pin(self.request_join_zone(
                    &leader,
                    zone_id,
                    node_id,
                    node_address,
                    depth + 1,
                ));
                return fut.await;
            }
            return Err(format!(
                "JoinZone failed on {peer_addr}: {}",
                resp.error.unwrap_or_else(|| "unknown error".into())
            ));
        }
        Ok(())
    }
}

/// Build the aggregate CA bundle PEM from the local CA file plus every
/// TOFU-pinned zone CA. If ``tofu_store_path`` is ``None`` the bundle
/// collapses to just the local CA. Matches the Python ``_build_channel``
/// CA-bundle behavior exactly so peer certs keep validating.
fn build_ca_bundle_pem(
    local_ca_pem: &[u8],
    tofu_store_path: Option<&str>,
) -> Result<Vec<u8>, String> {
    let mut bundle = Vec::with_capacity(local_ca_pem.len());
    bundle.extend_from_slice(local_ca_pem.trim_ascii());
    if !bundle.is_empty() && !bundle.ends_with(b"\n") {
        bundle.push(b'\n');
    }

    if let Some(path) = tofu_store_path {
        let store = TofuTrustStore::open(path).map_err(|e| format!("open TOFU store: {e}"))?;
        for entry in store.list_trusted() {
            let pem = entry.ca_pem;
            bundle.extend_from_slice(pem.trim().as_bytes());
            if !bundle.ends_with(b"\n") {
                bundle.push(b'\n');
            }
        }
    }
    Ok(bundle)
}

// ─────────────────────────────────────────────────────────────────────
// PyO3 surface
// ─────────────────────────────────────────────────────────────────────

/// Python-facing federation client.
///
/// One instance per ``NexusFederation`` — holds a shared tokio runtime
/// plus the TLS identity. Discover / join RPCs block on the runtime so
/// Python callers can use plain sync ``asyncio.to_thread`` if they
/// need to await alongside other async work.
#[pyclass(name = "FederationClient")]
pub struct PyFederationClient {
    inner: Arc<FederationClient>,
}

#[pymethods]
impl PyFederationClient {
    /// Construct a new federation client.
    ///
    /// All PEM inputs are caller-supplied ``bytes``. Pass ``None`` for
    /// the whole TLS set (``local_ca_pem`` / ``node_cert_pem`` /
    /// ``node_key_pem``) to run plaintext — matches the insecure
    /// channel path that Python's ``_build_channel`` took when
    /// ``_tls_config`` was ``None``.
    ///
    /// ``tofu_store_path`` is optional: when set, its trusted zone CAs
    /// are appended to the ``local_ca_pem`` into a single CA bundle.
    #[new]
    #[pyo3(signature = (
        local_ca_pem=None,
        node_cert_pem=None,
        node_key_pem=None,
        tofu_store_path=None,
    ))]
    pub fn py_new(
        local_ca_pem: Option<&[u8]>,
        node_cert_pem: Option<&[u8]>,
        node_key_pem: Option<&[u8]>,
        tofu_store_path: Option<&str>,
    ) -> PyResult<Self> {
        let tls_material =
            match (local_ca_pem, node_cert_pem, node_key_pem) {
                (Some(ca), Some(cert), Some(key)) => {
                    let bundle = build_ca_bundle_pem(ca, tofu_store_path)
                        .map_err(PyRuntimeError::new_err)?;
                    Some(TlsMaterial {
                        ca_bundle_pem: bundle,
                        node_cert_pem: cert.to_vec(),
                        node_key_pem: key.to_vec(),
                    })
                }
                (None, None, None) => None,
                _ => return Err(PyValueError::new_err(
                    "TLS requires all three: local_ca_pem, node_cert_pem, node_key_pem (or none)",
                )),
            };

        let runtime = Arc::new(
            tokio::runtime::Builder::new_multi_thread()
                .worker_threads(1)
                .thread_name("nexus-federation-client")
                .enable_all()
                .build()
                .map_err(|e| PyRuntimeError::new_err(format!("tokio runtime: {e}")))?,
        );

        Ok(Self {
            inner: Arc::new(FederationClient::new(runtime, tls_material)),
        })
    }

    /// Discover a peer zone via VFS ``sys_stat``. Returns a dict with
    /// keys ``is_mount``, ``entry_type``, ``target_zone_id``, ... on
    /// success, or ``None`` if the peer reports the path is missing /
    /// the call errors gracefully.
    pub fn discover_mount<'py>(
        &self,
        py: Python<'py>,
        peer_addr: &str,
        path: &str,
    ) -> PyResult<Option<Bound<'py, PyDict>>> {
        let peer_addr = peer_addr.to_string();
        let path_owned = path.to_string();
        let inner = Arc::clone(&self.inner);

        let value: Option<serde_json::Value> = py.detach(|| {
            inner
                .runtime
                .block_on(inner.discover_mount(&peer_addr, &path_owned))
                .map_err(PyRuntimeError::new_err)
        })?;

        let Some(value) = value else {
            return Ok(None);
        };
        let obj = value
            .as_object()
            .ok_or_else(|| PyRuntimeError::new_err("sys_stat response is not a JSON object"))?;

        let dict = PyDict::new(py);
        for (k, v) in obj {
            let item = json_value_to_py(py, v)?;
            dict.set_item(k, item)?;
        }
        Ok(Some(dict))
    }

    /// Request peer membership for a zone. Raises on RPC / leader-
    /// redirect / cluster errors; returns cleanly on success.
    pub fn request_join_zone(
        &self,
        py: Python<'_>,
        peer_addr: &str,
        zone_id: &str,
        node_id: u64,
        node_address: &str,
    ) -> PyResult<()> {
        let peer_addr = peer_addr.to_string();
        let zone_id = zone_id.to_string();
        let node_address = node_address.to_string();
        let inner = Arc::clone(&self.inner);

        py.detach(|| {
            inner
                .runtime
                .block_on(inner.request_join_zone(&peer_addr, &zone_id, node_id, &node_address, 0))
                .map_err(PyRuntimeError::new_err)
        })
    }
}

/// Convert a ``serde_json::Value`` to a Python object. Federation
/// responses are plain JSON (strings, numbers, bools, nested dicts /
/// lists), so the mapping stays narrow — no ``__type__`` escape-hatch
/// is needed.
fn json_value_to_py<'py>(py: Python<'py>, v: &serde_json::Value) -> PyResult<Bound<'py, PyAny>> {
    use pyo3::types::{PyBool, PyFloat, PyList, PyString};
    use pyo3::IntoPyObjectExt;

    match v {
        serde_json::Value::Null => Ok(py.None().into_bound(py)),
        serde_json::Value::Bool(b) => Ok(PyBool::new(py, *b).to_owned().into_any()),
        serde_json::Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                Ok(i.into_bound_py_any(py)?)
            } else if let Some(u) = n.as_u64() {
                Ok(u.into_bound_py_any(py)?)
            } else if let Some(f) = n.as_f64() {
                Ok(PyFloat::new(py, f).into_any())
            } else {
                Err(PyRuntimeError::new_err(format!(
                    "JSON number not representable: {n}"
                )))
            }
        }
        serde_json::Value::String(s) => Ok(PyString::new(py, s).into_any()),
        serde_json::Value::Array(items) => {
            let list = PyList::empty(py);
            for item in items {
                list.append(json_value_to_py(py, item)?)?;
            }
            Ok(list.into_any())
        }
        serde_json::Value::Object(map) => {
            let dict = PyDict::new(py);
            for (k, v) in map {
                dict.set_item(k, json_value_to_py(py, v)?)?;
            }
            Ok(dict.into_any())
        }
    }
}

// Silence unused-import warnings when features change. `PyInt` /
// `PyBytes` are kept reachable so downstream growth (e.g. surfacing
// raw bytes payloads from Call responses) is friction-free.
#[allow(dead_code)]
fn _keep_imports_live(py: Python<'_>) {
    let _ = PyBytes::new(py, b"");
}

// ─────────────────────────────────────────────────────────────────────
// Tests
// ─────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    fn make_ca_pem(cn: &str) -> String {
        use rcgen::{CertificateParams, KeyPair};
        let mut params = CertificateParams::new(vec![]).expect("params");
        params
            .distinguished_name
            .push(rcgen::DnType::CommonName, cn);
        let key = KeyPair::generate().expect("key");
        params.self_signed(&key).expect("sign").pem()
    }

    /// Walks every `build_ca_bundle_pem` branch in one go: no-store,
    /// empty-store, populated-store. Coverage matches three prior
    /// unit tests; consolidating them keeps the test count sane for a
    /// trivial helper.
    #[test]
    fn build_ca_bundle_pem_covers_all_store_states() {
        let local = b"-----BEGIN CERTIFICATE-----\nLOCAL\n-----END CERTIFICATE-----";
        let count_certs = |b: &[u8]| -> usize {
            std::str::from_utf8(b)
                .unwrap()
                .matches("BEGIN CERTIFICATE")
                .count()
        };

        // No store attached → bundle is just the local CA.
        let bundle = build_ca_bundle_pem(local, None).unwrap();
        assert!(std::str::from_utf8(&bundle).unwrap().contains("LOCAL"));
        assert_eq!(count_certs(&bundle), 1);

        // Empty store file (exists, no entries) → still just local CA.
        let dir = tempfile::tempdir().unwrap();
        let empty_store = dir.path().join("empty");
        std::fs::write(&empty_store, "").unwrap();
        let bundle = build_ca_bundle_pem(local, Some(empty_store.to_str().unwrap())).unwrap();
        assert_eq!(count_certs(&bundle), 1);

        // Populated store → every pinned zone CA appended after local.
        let populated = dir.path().join("populated");
        let mut store = TofuTrustStore::open(&populated).unwrap();
        store
            .verify_or_trust("zone-a", make_ca_pem("zone-a-ca").as_bytes(), "a:2126")
            .unwrap();
        store
            .verify_or_trust("zone-b", make_ca_pem("zone-b-ca").as_bytes(), "b:2126")
            .unwrap();
        let bundle = build_ca_bundle_pem(local, Some(populated.to_str().unwrap())).unwrap();
        let text = std::str::from_utf8(&bundle).unwrap();
        assert!(text.contains("LOCAL"));
        assert_eq!(count_certs(&bundle), 3);
    }

    /// Smoke-construct the client so the plumbing (runtime build, TLS
    /// material parse, channel pool init) doesn't regress. End-to-end
    /// discover / join flows are exercised by the federation E2E
    /// suite gated at R12 — they need a running peer.
    #[test]
    fn client_constructs_with_and_without_tls() {
        let rt = Arc::new(
            tokio::runtime::Builder::new_multi_thread()
                .worker_threads(1)
                .enable_all()
                .build()
                .unwrap(),
        );
        // Plaintext variant.
        let plain = FederationClient::new(Arc::clone(&rt), None);
        assert!(plain.tls_material.is_none());

        // TLS variant — the material isn't parsed until the first
        // `channel_for` call, so we just check it was stored.
        let tls = FederationClient::new(
            rt,
            Some(TlsMaterial {
                ca_bundle_pem: make_ca_pem("ca").into_bytes(),
                node_cert_pem: make_ca_pem("node").into_bytes(),
                node_key_pem: b"-----BEGIN PRIVATE KEY-----\n-----END PRIVATE KEY-----".to_vec(),
            }),
        );
        assert!(tls.tls_material.is_some());
    }
}
