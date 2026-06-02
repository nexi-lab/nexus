//! `DefaultObjectStoreProvider` — concrete `ObjectStoreProvider` impl.
//!
//! Dispatches `args.backend_type` to the appropriate `ObjectStore`
//! constructor.  Registered by the host binary at startup via
//! `kernel::hal::object_store_provider::set_provider`.
//!
//! Dispatch arms are `#[cfg]`-gated on the same per-driver Cargo
//! features the rest of the crate uses, so a slim binary (e.g.
//! `nexus-cluster`, which compiles only `driver-path-local` +
//! `driver-remote`) gets only the arms it can actually serve; every
//! other `backend_type` falls through to the `unknown backend_type`
//! error.

use std::collections::HashMap;
use std::path::Path;
use std::sync::Arc;
use std::time::Duration;

use kernel::hal::object_store_provider::{
    ObjectStoreBuildResult, ObjectStoreProvider, ObjectStoreProviderArgs,
};

/// Concrete factory registered by the host binary.
///
/// Implements the backend-type construction switch that mount setup
/// needs. Each arm is `#[cfg]`-gated on the corresponding Cargo
/// feature — compile-time inclusion is the gating mechanism.
/// App-layer params are parsed from `args.backend_params` per arm.
pub struct DefaultObjectStoreProvider;

/// Get a non-empty string param from the map, coercing empty to `None`.
fn param<'a>(params: &'a HashMap<String, String>, key: &str) -> Option<&'a str> {
    params.get(key).map(|s| s.as_str()).filter(|s| !s.is_empty())
}

/// Get a required param or return an error.
fn required_param<'a>(
    params: &'a HashMap<String, String>,
    key: &str,
    backend: &str,
) -> Result<&'a str, String> {
    param(params, key).ok_or_else(|| format!("{backend} requires {key}"))
}

/// Parse a bool param, defaulting to `false`.
fn bool_param(params: &HashMap<String, String>, key: &str) -> bool {
    params
        .get(key)
        .map(|v| matches!(v.as_str(), "true" | "1" | "yes"))
        .unwrap_or(false)
}

impl ObjectStoreProvider for DefaultObjectStoreProvider {
    fn build(&self, args: &ObjectStoreProviderArgs<'_>) -> Result<ObjectStoreBuildResult, String> {
        let p = args.backend_params;
        match args.backend_type {
            #[cfg(feature = "driver-path-local")]
            "path_local" => {
                let root = required_param(p, "local_root", "path_local")?;
                let fsync = bool_param(p, "fsync");
                let backend =
                    crate::storage::path_local::PathLocalBackend::new(Path::new(root), fsync)
                        .map_err(|e| format!("path_local init at '{root}': {e}"))?;
                Ok(backend_only(Arc::new(backend)))
            }

            #[cfg(feature = "driver-cas-local")]
            "cas-local" => {
                let root = required_param(p, "local_root", "cas-local")?;
                let fsync = bool_param(p, "fsync");
                let fetcher = Arc::new(kernel::cas_remote::GrpcChunkFetcher::new(
                    Arc::clone(args.peer_client),
                    args.self_address.map(String::from),
                ));
                let backend = crate::storage::cas_local::CasLocalBackend::new_with_fetcher(
                    Path::new(root),
                    fsync,
                    fetcher,
                )
                .map_err(|e| format!("cas-local init at '{root}': {e}"))?;
                Ok(backend_only(Arc::new(backend)))
            }

            #[cfg(feature = "driver-local-connector")]
            "local_connector" => {
                let root = required_param(p, "local_root", "local_connector")?;
                let follow_symlinks = bool_param(p, "follow_symlinks");
                let fsync = bool_param(p, "fsync");
                let backend = crate::storage::local_connector::LocalConnectorBackend::new(
                    Path::new(root),
                    follow_symlinks,
                    fsync,
                )
                .map_err(|e| format!("local_connector init at '{root}': {e}"))?;
                Ok(backend_only(Arc::new(backend)))
            }

            #[cfg(feature = "driver-remote")]
            "remote" => {
                let raw_address = required_param(p, "server_address", "remote")?;
                if let Some(mp) = args.mount_path {
                    if !mp.trim_end_matches('/').is_empty() {
                        return Err(format!(
                            "remote sub-path mount '{mp}' not yet supported \
                             (RemoteBackend path reconstruction — Issue #4273); \
                             mount remote zones at \"/\" for now"
                        ));
                    }
                }
                let auth_token = param(p, "remote_auth_token").unwrap_or("");
                let tls = build_tls_config(p);
                let address = resolve_remote_address(raw_address, tls.is_some())?;
                let remote_timeout: f64 = param(p, "remote_timeout")
                    .and_then(|s| s.parse().ok())
                    .unwrap_or(0.0);
                let timeout = Duration::try_from_secs_f64(remote_timeout)
                    .ok()
                    .filter(|d| !d.is_zero())
                    .unwrap_or(Duration::from_secs(30));
                let transport = Arc::new(
                    kernel::rpc_transport::RpcTransport::new(
                        Arc::clone(args.runtime),
                        address,
                        auth_token,
                        tls.as_ref(),
                        timeout,
                    )
                    .map_err(|e| format!("remote transport to '{address}': {e}"))?,
                );
                let meta_store: Arc<dyn kernel::meta_store::MetaStore> = Arc::new(
                    kernel::meta_store::remote::RemoteMetaStore::new(Arc::clone(&transport)),
                );
                let backend: Arc<dyn kernel::abc::object_store::ObjectStore> =
                    Arc::new(crate::storage::remote::RemoteBackend::new(transport));
                Ok(ObjectStoreBuildResult {
                    backend: Some(backend),
                    pending_remote_meta_store: Some(meta_store),
                })
            }

            #[cfg(feature = "driver-s3")]
            "s3" => {
                let bucket = required_param(p, "s3_bucket", "s3")?;
                let region = required_param(p, "aws_region", "s3")?;
                let access_key = required_param(p, "aws_access_key", "s3")?;
                let secret_key = required_param(p, "aws_secret_key", "s3")?;
                let prefix = param(p, "s3_prefix").unwrap_or("");
                let endpoint = param(p, "s3_endpoint");
                let backend = crate::transports::blob::s3::S3Transport::new(
                    args.backend_name,
                    bucket,
                    prefix,
                    region,
                    access_key,
                    secret_key,
                    endpoint,
                )
                .map_err(|e| format!("s3 init: {e}"))?;
                Ok(backend_only(Arc::new(backend)))
            }

            #[cfg(feature = "driver-gcs")]
            "gcs" => {
                let bucket = required_param(p, "gcs_bucket", "gcs")?;
                let prefix = param(p, "gcs_prefix").unwrap_or("");
                let access_token = required_param(p, "access_token", "gcs")?;
                let backend = crate::transports::blob::gcs::GcsBackend::new(
                    args.backend_name,
                    bucket,
                    prefix,
                    access_token,
                )
                .map_err(|e| format!("gcs init: {e}"))?;
                Ok(backend_only(Arc::new(backend)))
            }

            other => Err(format!("unknown backend_type: '{other}'")),
        }
    }
}

/// Shorthand for the common case: a backend with no pending remote
/// metastore.
fn backend_only(
    backend: Arc<dyn kernel::abc::object_store::ObjectStore>,
) -> ObjectStoreBuildResult {
    ObjectStoreBuildResult {
        backend: Some(backend),
        pending_remote_meta_store: None,
    }
}

/// Validate a remote `server_address` against the transport-security
/// policy and return the bare `host:port` (scheme stripped) to hand to
/// `RpcTransport`. Mirrors `src/nexus/remote/rpc_transport.py`:
///
///   * a `grpcs://` scheme requires TLS material (CA) — otherwise the
///     connection would silently downgrade to plaintext while still
///     carrying `remote_auth_token`;
///   * a plaintext (no-TLS) connection to a non-loopback host is refused
///     unless `NEXUS_GRPC_ALLOW_INSECURE` is set (escape hatch for
///     trusted private networks — docker-compose, k8s pod-local).
///
/// A leading `grpc://` is accepted and stripped (plaintext by scheme).
#[cfg(feature = "driver-remote")]
fn resolve_remote_address(address: &str, has_tls: bool) -> Result<&str, String> {
    let (addr, scheme_requires_tls) = if let Some(rest) = address.strip_prefix("grpcs://") {
        (rest, true)
    } else if let Some(rest) = address.strip_prefix("grpc://") {
        (rest, false)
    } else {
        (address, false)
    };
    if scheme_requires_tls && !has_tls {
        return Err(format!(
            "remote: grpcs:// scheme requires TLS (got '{address}' with no remote_ca_pem); \
             pass TLS material or use grpc:// for plaintext"
        ));
    }
    if !has_tls && !is_loopback_host(addr) && !insecure_grpc_allowed() {
        return Err(format!(
            "remote: insecure (plaintext) gRPC refused for non-loopback address '{addr}'; \
             configure TLS for remote connections, or set NEXUS_GRPC_ALLOW_INSECURE=true \
             for trusted private networks (docker-compose, k8s pod-local)"
        ));
    }
    Ok(addr)
}

/// Whether `host:port` (or bare `host`) targets the loopback interface.
/// Strips an optional IPv6 `[..]` wrapper and the trailing `:port`.
#[cfg(feature = "driver-remote")]
fn is_loopback_host(addr: &str) -> bool {
    let host = addr.rsplit_once(':').map_or(addr, |(h, _)| h);
    let host = host.trim_start_matches('[').trim_end_matches(']');
    host == "localhost"
        || host
            .parse::<std::net::IpAddr>()
            .map(|ip| ip.is_loopback())
            .unwrap_or(false)
}

/// Escape hatch: allow plaintext gRPC to non-loopback hosts on trusted
/// private networks. Off by default (fail-closed).
#[cfg(feature = "driver-remote")]
fn insecure_grpc_allowed() -> bool {
    std::env::var("NEXUS_GRPC_ALLOW_INSECURE")
        .map(|v| matches!(v.to_ascii_lowercase().as_str(), "1" | "true" | "yes"))
        .unwrap_or(false)
}

/// Assemble a `TlsConfig` from the remote-* PEM params in the map.
/// Returns `None` when no CA is supplied (plaintext gRPC); PEM values
/// are stored as UTF-8 strings in the map and converted to bytes.
#[cfg(feature = "driver-remote")]
fn build_tls_config(
    p: &HashMap<String, String>,
) -> Option<kernel::rpc_transport::TlsConfig> {
    let ca = param(p, "remote_ca_pem")?;
    Some(kernel::rpc_transport::TlsConfig {
        ca_pem: ca.as_bytes().to_vec(),
        cert_pem: param(p, "remote_cert_pem").map(|s| s.as_bytes().to_vec()),
        key_pem: param(p, "remote_key_pem").map(|s| s.as_bytes().to_vec()),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use kernel::hal::object_store_provider::{get_provider, set_provider};
    use std::sync::Arc;

    fn noop_peer_client() -> Arc<dyn kernel::hal::peer::PeerBlobClient> {
        kernel::hal::peer::NoopPeerBlobClient::arc()
    }

    fn noop_runtime() -> Arc<tokio::runtime::Runtime> {
        Arc::new(
            tokio::runtime::Builder::new_current_thread()
                .build()
                .unwrap(),
        )
    }

    /// Build a minimal args struct; tests supply `backend_params` explicitly.
    fn mk_args<'a>(
        backend_type: &'a str,
        params: &'a HashMap<String, String>,
        peer_client: &'a Arc<dyn kernel::hal::peer::PeerBlobClient>,
        runtime: &'a Arc<tokio::runtime::Runtime>,
    ) -> ObjectStoreProviderArgs<'a> {
        ObjectStoreProviderArgs {
            backend_type,
            backend_name: "test",
            mount_path: None,
            backend_params: params,
            peer_client,
            self_address: None,
            runtime,
        }
    }

    fn expect_err(r: Result<ObjectStoreBuildResult, String>) -> String {
        match r {
            Ok(_) => panic!("expected Err, got Ok"),
            Err(e) => e,
        }
    }

    fn params(kvs: &[(&str, &str)]) -> HashMap<String, String> {
        kvs.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect()
    }

    #[cfg(feature = "driver-path-local")]
    #[test]
    fn builds_path_local() {
        let dir = tempfile::tempdir().unwrap();
        let p = params(&[("local_root", dir.path().to_str().unwrap())]);
        let pc = noop_peer_client();
        let rt = noop_runtime();
        let args = mk_args("path_local", &p, &pc, &rt);
        let r = DefaultObjectStoreProvider.build(&args).expect("should succeed");
        assert!(r.backend.is_some());
        assert!(r.pending_remote_meta_store.is_none());
    }

    #[cfg(feature = "driver-path-local")]
    #[test]
    fn path_local_missing_root_errors() {
        let p = HashMap::new();
        let pc = noop_peer_client();
        let rt = noop_runtime();
        let args = mk_args("path_local", &p, &pc, &rt);
        let err = expect_err(DefaultObjectStoreProvider.build(&args));
        assert!(err.contains("local_root"), "err was: {err}");
    }

    #[cfg(feature = "driver-cas-local")]
    #[test]
    fn builds_cas_local() {
        let dir = tempfile::tempdir().unwrap();
        let p = params(&[("local_root", dir.path().to_str().unwrap())]);
        let pc = noop_peer_client();
        let rt = noop_runtime();
        let mut args = mk_args("cas-local", &p, &pc, &rt);
        args.self_address = Some("nexus-self:2126");
        let r = DefaultObjectStoreProvider.build(&args).expect("should succeed");
        assert!(r.backend.is_some());
    }

    #[cfg(feature = "driver-local-connector")]
    #[test]
    fn builds_local_connector() {
        let dir = tempfile::tempdir().unwrap();
        let p = params(&[("local_root", dir.path().to_str().unwrap())]);
        let pc = noop_peer_client();
        let rt = noop_runtime();
        let args = mk_args("local_connector", &p, &pc, &rt);
        let r = DefaultObjectStoreProvider.build(&args).expect("should succeed");
        assert!(r.backend.is_some());
    }

    #[cfg(feature = "driver-s3")]
    #[test]
    fn builds_s3() {
        let p = params(&[
            ("s3_bucket", "my-bucket"),
            ("s3_prefix", "prefix"),
            ("aws_region", "us-east-1"),
            ("aws_access_key", "AKIAIOSFODNN7EXAMPLE"),
            ("aws_secret_key", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"),
        ]);
        let pc = noop_peer_client();
        let rt = noop_runtime();
        let args = mk_args("s3", &p, &pc, &rt);
        let r = DefaultObjectStoreProvider.build(&args).expect("should succeed");
        assert!(r.backend.is_some());
    }

    #[cfg(feature = "driver-s3")]
    #[test]
    fn s3_missing_bucket_errors() {
        let p = params(&[
            ("aws_region", "us-east-1"),
            ("aws_access_key", "key"),
            ("aws_secret_key", "secret"),
        ]);
        let pc = noop_peer_client();
        let rt = noop_runtime();
        let args = mk_args("s3", &p, &pc, &rt);
        let err = expect_err(DefaultObjectStoreProvider.build(&args));
        assert!(err.contains("s3_bucket"), "err was: {err}");
    }

    #[cfg(feature = "driver-gcs")]
    #[test]
    fn builds_gcs() {
        let p = params(&[
            ("gcs_bucket", "my-gcs-bucket"),
            ("gcs_prefix", "prefix"),
            ("access_token", "ya29.token"),
        ]);
        let pc = noop_peer_client();
        let rt = noop_runtime();
        let args = mk_args("gcs", &p, &pc, &rt);
        let r = DefaultObjectStoreProvider.build(&args).expect("should succeed");
        assert!(r.backend.is_some());
    }

    #[cfg(feature = "driver-remote")]
    #[test]
    fn builds_remote() {
        let p = params(&[
            ("server_address", "127.0.0.1:2126"),
            ("remote_auth_token", "tok"),
        ]);
        let pc = noop_peer_client();
        let rt = noop_runtime();
        let args = mk_args("remote", &p, &pc, &rt);
        let r = DefaultObjectStoreProvider.build(&args).expect("should succeed");
        assert!(r.backend.is_some());
        assert!(r.pending_remote_meta_store.is_some());
    }

    #[cfg(feature = "driver-remote")]
    #[test]
    fn remote_missing_server_address_errors() {
        let p = HashMap::new();
        let pc = noop_peer_client();
        let rt = noop_runtime();
        let args = mk_args("remote", &p, &pc, &rt);
        let err = expect_err(DefaultObjectStoreProvider.build(&args));
        assert!(err.contains("server_address"), "err was: {err}");
    }

    #[cfg(feature = "driver-remote")]
    #[test]
    fn builds_remote_explicit_root_mount() {
        let p = params(&[("server_address", "127.0.0.1:2126")]);
        let pc = noop_peer_client();
        let rt = noop_runtime();
        for root in ["/", ""] {
            let mut args = mk_args("remote", &p, &pc, &rt);
            args.mount_path = Some(root);
            let r = DefaultObjectStoreProvider.build(&args)
                .unwrap_or_else(|e| panic!("root mount_path {root:?} should build: {e}"));
            assert!(r.backend.is_some());
            assert!(r.pending_remote_meta_store.is_some());
        }
    }

    #[cfg(feature = "driver-remote")]
    #[test]
    fn remote_subpath_mount_rejected() {
        let p = params(&[("server_address", "127.0.0.1:2126")]);
        let pc = noop_peer_client();
        let rt = noop_runtime();
        let mut args = mk_args("remote", &p, &pc, &rt);
        args.mount_path = Some("/zone/acme");
        let err = expect_err(DefaultObjectStoreProvider.build(&args));
        assert!(err.contains("sub-path mount") && err.contains("/zone/acme"), "err was: {err}");
    }

    #[cfg(feature = "driver-remote")]
    #[test]
    fn remote_grpcs_without_tls_errors() {
        let p = params(&[
            ("server_address", "grpcs://hub.example.com:443"),
            ("remote_auth_token", "tok"),
        ]);
        let pc = noop_peer_client();
        let rt = noop_runtime();
        let args = mk_args("remote", &p, &pc, &rt);
        let err = expect_err(DefaultObjectStoreProvider.build(&args));
        assert!(err.contains("grpcs://"), "err was: {err}");
    }

    #[cfg(feature = "driver-remote")]
    #[test]
    fn remote_plaintext_nonloopback_errors() {
        let p = params(&[("server_address", "10.0.0.5:2126")]);
        let pc = noop_peer_client();
        let rt = noop_runtime();
        let args = mk_args("remote", &p, &pc, &rt);
        let err = expect_err(DefaultObjectStoreProvider.build(&args));
        assert!(err.contains("non-loopback") && err.contains("NEXUS_GRPC_ALLOW_INSECURE"), "err was: {err}");
    }

    #[cfg(feature = "driver-remote")]
    #[test]
    fn loopback_host_detection() {
        assert!(is_loopback_host("127.0.0.1:2126"));
        assert!(is_loopback_host("localhost:2126"));
        assert!(is_loopback_host("[::1]:2126"));
        assert!(is_loopback_host("127.0.0.1"));
        assert!(!is_loopback_host("10.0.0.5:2126"));
        assert!(!is_loopback_host("hub.example.com:443"));
    }

    #[cfg(feature = "driver-remote")]
    #[test]
    fn remote_tolerates_degenerate_timeout() {
        let pc = noop_peer_client();
        let rt = noop_runtime();
        for bad in [f64::INFINITY, 1e30, f64::NAN, -1.0, 0.0] {
            let p = params(&[
                ("server_address", "127.0.0.1:2126"),
                ("remote_timeout", &bad.to_string()),
            ]);
            let args = mk_args("remote", &p, &pc, &rt);
            let r = DefaultObjectStoreProvider.build(&args)
                .unwrap_or_else(|e| panic!("timeout={bad}: {e}"));
            assert!(r.backend.is_some());
        }
    }

    #[cfg(feature = "driver-remote")]
    #[test]
    fn tls_config_assembly() {
        let empty = HashMap::new();
        assert!(build_tls_config(&empty).is_none(), "no CA should mean plaintext");

        let p = params(&[
            ("remote_ca_pem", "-----BEGIN CERTIFICATE-----CA-----END CERTIFICATE-----"),
            ("remote_cert_pem", "-----BEGIN CERTIFICATE-----CERT-----END CERTIFICATE-----"),
            ("remote_key_pem", "-----BEGIN PRIVATE KEY-----KEY-----END PRIVATE KEY-----"),
        ]);
        let tls = build_tls_config(&p).expect("CA present should yield Some");
        assert_eq!(tls.ca_pem, b"-----BEGIN CERTIFICATE-----CA-----END CERTIFICATE-----");
        assert!(tls.cert_pem.is_some());
        assert!(tls.key_pem.is_some());
    }

    #[test]
    fn unknown_backend_type_errors() {
        let p = HashMap::new();
        let pc = noop_peer_client();
        let rt = noop_runtime();
        let args = mk_args("totally_unknown", &p, &pc, &rt);
        let err = expect_err(DefaultObjectStoreProvider.build(&args));
        assert!(err.contains("unknown backend_type"), "err was: {err}");
    }

    #[test]
    fn get_provider_returns_registered_instance() {
        let _ = set_provider(Arc::new(DefaultObjectStoreProvider));
        assert!(get_provider().is_some());
    }
}
