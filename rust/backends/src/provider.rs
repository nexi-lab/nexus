//! `DefaultObjectStoreProvider` — concrete `ObjectStoreProvider` impl.
//!
//! Dispatches `args.backend_type` to the appropriate `ObjectStore`
//! constructor and enforces the driver gate before construction.
//! Registered by the host binary at startup via
//! `kernel::hal::object_store_provider::set_provider`.
//!
//! Dispatch arms are `#[cfg]`-gated on the same per-driver Cargo
//! features the rest of the crate uses, so a slim binary (e.g.
//! `nexus-cluster`, which compiles only `driver-path-local` +
//! `driver-remote`) gets only the arms it can actually serve; every
//! other `backend_type` falls through to the `unknown backend_type`
//! error.

use std::path::Path;
use std::sync::Arc;
use std::time::Duration;

use kernel::hal::object_store_provider::{
    is_driver_enabled, ObjectStoreBuildResult, ObjectStoreProvider, ObjectStoreProviderArgs,
};

/// Concrete factory registered by the host binary.
///
/// Implements the backend-type construction switch that mount setup
/// needs. Every arm is preceded by a driver-gate check — a disabled
/// driver returns `Err("driver 'X' not enabled in current deployment
/// profile")` regardless of whether the required args are present, so
/// the gate decision never leaks construction details for a driver the
/// profile forbids.
pub struct DefaultObjectStoreProvider;

impl ObjectStoreProvider for DefaultObjectStoreProvider {
    fn build(&self, args: &ObjectStoreProviderArgs<'_>) -> Result<ObjectStoreBuildResult, String> {
        // Gate first, before any construction — a forbidden driver must
        // fail with the documented gate error even when its args are
        // well-formed.
        if !is_driver_enabled(args.backend_type) {
            return Err(format!(
                "driver '{}' not enabled in current deployment profile",
                args.backend_type
            ));
        }

        match args.backend_type {
            #[cfg(feature = "driver-path-local")]
            "path_local" => {
                let root = args.local_root.ok_or("path_local requires local_root")?;
                let backend =
                    crate::storage::path_local::PathLocalBackend::new(Path::new(root), args.fsync)
                        .map_err(|e| format!("path_local init at '{root}': {e}"))?;
                Ok(backend_only(Arc::new(backend)))
            }

            // Canonical runtime backend_type is `cas-local` (hyphen) per
            // `deployment_profile.py::DRIVER_CAS_LOCAL` — it is also the
            // default `backend_type` in `nexus_fs_metadata.py`. The Cargo
            // feature keeps the `driver-cas-local` spelling.
            #[cfg(feature = "driver-cas-local")]
            "cas-local" => {
                let root = args.local_root.ok_or("cas-local requires local_root")?;
                // Per-mount scatter-gather fetcher, wired against the
                // kernel's live peer client so a local chunk miss falls
                // through to peer RPCs. `self_address` lets the fetcher
                // skip this node when scattering reads.
                let fetcher = Arc::new(kernel::cas_remote::GrpcChunkFetcher::new(
                    Arc::clone(args.peer_client),
                    args.self_address.map(String::from),
                ));
                let backend = crate::storage::cas_local::CasLocalBackend::new_with_fetcher(
                    Path::new(root),
                    args.fsync,
                    fetcher,
                )
                .map_err(|e| format!("cas-local init at '{root}': {e}"))?;
                Ok(backend_only(Arc::new(backend)))
            }

            #[cfg(feature = "driver-local-connector")]
            "local_connector" => {
                let root = args
                    .local_root
                    .ok_or("local_connector requires local_root")?;
                let backend = crate::storage::local_connector::LocalConnectorBackend::new(
                    Path::new(root),
                    args.follow_symlinks,
                    args.fsync,
                )
                .map_err(|e| format!("local_connector init at '{root}': {e}"))?;
                Ok(backend_only(Arc::new(backend)))
            }

            #[cfg(feature = "driver-remote")]
            "remote" => {
                let raw_address = args
                    .server_address
                    .ok_or("remote requires server_address")?;
                // Fail closed on EXPLICIT sub-path remote mounts.
                // `RemoteBackend` builds with root-mount semantics (empty
                // `zone_path`); it cannot yet reconstruct a sub-path hub
                // path because `to_server_path` conflates mount-relative
                // route paths with zone-prefixed content ids (Issue #4273,
                // follow-up to #3786), so a `/zone/acme` mount would
                // silently misroute reads /
                // writes / deletes onto a different hub path.
                //
                // An absent `mount_path` (`None`) or root ("/" / "")
                // defaults to root-mount semantics — existing remote root
                // callers mount with `path="/"` and pass no `mount_path`.
                // bridge-2 threads `sys_setattr`'s path so a real sub-path
                // mount carries a non-root `mount_path` and is rejected
                // here rather than misrouted.
                if let Some(mp) = args.mount_path {
                    if !mp.trim_end_matches('/').is_empty() {
                        return Err(format!(
                            "remote sub-path mount '{mp}' not yet supported \
                             (RemoteBackend path reconstruction — Issue #4273); \
                             mount remote zones at \"/\" for now"
                        ));
                    }
                }
                let auth_token = args.remote_auth_token.unwrap_or("");
                let tls = build_tls_config(args);
                // Honor the grpc/grpcs scheme and fail closed on insecure
                // transports before sending `remote_auth_token`, mirroring
                // `src/nexus/remote/rpc_transport.py`: grpcs:// demands TLS,
                // and plaintext to a non-loopback host is refused unless
                // NEXUS_GRPC_ALLOW_INSECURE is set (trusted private nets).
                let address = resolve_remote_address(raw_address, tls.is_some())?;
                // `remote_timeout` is a non-`Option` f64 from mount
                // config. Treat any value that is not a usable positive
                // finite duration — non-positive (a "not set" sentinel,
                // or a zero gRPC deadline that would fail every call),
                // NaN, +inf, or a magnitude that overflows `Duration` —
                // as unset and fall back to a sane default. `try_from`
                // (vs `from_secs_f64`) keeps a bad value from panicking
                // the syscall thread, honoring build()'s Err-not-panic
                // contract.
                let timeout = Duration::try_from_secs_f64(args.remote_timeout)
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
                // Remote backends back both the object store and the
                // metastore with the same RPC transport; the kernel
                // installs the metastore via `pending_remote_meta_store`.
                let meta_store: Arc<dyn kernel::meta_store::MetaStore> = Arc::new(
                    kernel::meta_store::remote::RemoteMetaStore::new(Arc::clone(&transport)),
                );
                // Root-mount semantics, matching `RemoteBackend`'s only
                // working behavior today. Sub-path remote mounts need the
                // mount point threaded into `RemoteBackend::with_zone_path`,
                // but that path also requires `RemoteBackend` to stop
                // conflating mount-relative route paths with hub-returned
                // (zone-prefixed) content ids in `to_server_path` — see the
                // Issue #4273 (follow-up to #3786). That fix
                // plus the mount-path wiring belong to bridge-2, not here.
                let backend: Arc<dyn kernel::abc::object_store::ObjectStore> =
                    Arc::new(crate::storage::remote::RemoteBackend::new(transport));
                Ok(ObjectStoreBuildResult {
                    backend: Some(backend),
                    pending_remote_meta_store: Some(meta_store),
                })
            }

            #[cfg(feature = "driver-s3")]
            "s3" => {
                let bucket = args.s3_bucket.ok_or("s3 requires s3_bucket")?;
                let region = args.aws_region.ok_or("s3 requires aws_region")?;
                let access_key = args.aws_access_key.ok_or("s3 requires aws_access_key")?;
                let secret_key = args.aws_secret_key.ok_or("s3 requires aws_secret_key")?;
                let prefix = args.s3_prefix.unwrap_or("");
                let backend = crate::transports::blob::s3::S3Backend::new(
                    args.backend_name,
                    bucket,
                    prefix,
                    region,
                    access_key,
                    secret_key,
                    args.s3_endpoint,
                )
                .map_err(|e| format!("s3 init: {e}"))?;
                Ok(backend_only(Arc::new(backend)))
            }

            #[cfg(feature = "driver-gcs")]
            "gcs" => {
                let bucket = args.gcs_bucket.ok_or("gcs requires gcs_bucket")?;
                let prefix = args.gcs_prefix.unwrap_or("");
                let access_token = args.access_token.ok_or("gcs requires access_token")?;
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

/// Assemble a `TlsConfig` from the remote-* PEM args. Returns `None`
/// when no CA is supplied (plaintext gRPC — local testing only); a CA
/// with optional client cert/key enables one-way or mutual TLS.
#[cfg(feature = "driver-remote")]
fn build_tls_config(
    args: &ObjectStoreProviderArgs<'_>,
) -> Option<kernel::rpc_transport::TlsConfig> {
    let ca_pem = args.remote_ca_pem?;
    Some(kernel::rpc_transport::TlsConfig {
        ca_pem: ca_pem.to_vec(),
        cert_pem: args.remote_cert_pem.map(<[u8]>::to_vec),
        key_pem: args.remote_key_pem.map(<[u8]>::to_vec),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use kernel::hal::object_store_provider::{get_provider, set_enabled_drivers, set_provider};
    use std::sync::{Arc, Mutex};

    // The driver gate (`set_enabled_drivers`) and `is_driver_enabled`
    // are backed by a process-wide `OnceLock<RwLock<HashSet>>`. cargo
    // runs the tests in this module on parallel threads of one process,
    // so any test that touches the gate must serialise against every
    // test that reads it (i.e. calls `build`). This lock provides that
    // serialisation; `unwrap_or_else(into_inner)` keeps a panic in one
    // test from poisoning the lock and cascading into the rest.
    static GATE_LOCK: Mutex<()> = Mutex::new(());

    fn lock_gate() -> std::sync::MutexGuard<'static, ()> {
        GATE_LOCK.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// Open the gate to every driver this crate compiles, so a
    /// build-success test passes the gate regardless of what a prior
    /// test left in the global set.
    fn enable_all() {
        set_enabled_drivers([
            "path_local",
            "cas-local",
            "local_connector",
            "remote",
            "s3",
            "gcs",
        ]);
    }

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

    /// Build an args struct with every optional field cleared; tests
    /// override only the fields they care about via `..base_args(..)`.
    fn base_args<'a>(
        backend_type: &'a str,
        peer_client: &'a Arc<dyn kernel::hal::peer::PeerBlobClient>,
        runtime: &'a Arc<tokio::runtime::Runtime>,
    ) -> ObjectStoreProviderArgs<'a> {
        ObjectStoreProviderArgs {
            backend_type,
            backend_name: "test",
            mount_path: None,
            local_root: None,
            fsync: false,
            follow_symlinks: false,
            openai_base_url: None,
            openai_api_key: None,
            openai_model: None,
            openai_blob_root: None,
            anthropic_base_url: None,
            anthropic_api_key: None,
            anthropic_model: None,
            anthropic_blob_root: None,
            s3_bucket: None,
            s3_prefix: None,
            aws_region: None,
            aws_access_key: None,
            aws_secret_key: None,
            s3_endpoint: None,
            gcs_bucket: None,
            gcs_prefix: None,
            access_token: None,
            root_folder_id: None,
            bot_token: None,
            default_channel: None,
            hn_stories_per_feed: None,
            hn_include_comments: None,
            cli_command: None,
            cli_service: None,
            cli_auth_env_json: None,
            x_bearer_token: None,
            server_address: None,
            remote_auth_token: None,
            remote_ca_pem: None,
            remote_cert_pem: None,
            remote_key_pem: None,
            remote_timeout: 30.0,
            peer_client,
            self_address: None,
            runtime,
        }
    }

    /// `unwrap_err` needs `T: Debug`, which `ObjectStoreBuildResult`
    /// does not implement; pull the error out by hand instead.
    fn expect_err(r: Result<ObjectStoreBuildResult, String>) -> String {
        match r {
            Ok(_) => panic!("expected Err, got Ok"),
            Err(e) => e,
        }
    }

    #[cfg(feature = "driver-path-local")]
    #[test]
    fn builds_path_local() {
        let _g = lock_gate();
        enable_all();
        let dir = tempfile::tempdir().unwrap();
        let peer_client = noop_peer_client();
        let runtime = noop_runtime();
        let args = ObjectStoreProviderArgs {
            local_root: Some(dir.path().to_str().unwrap()),
            ..base_args("path_local", &peer_client, &runtime)
        };
        let result = DefaultObjectStoreProvider
            .build(&args)
            .expect("path_local build should succeed");
        assert!(result.backend.is_some());
        assert!(result.pending_remote_meta_store.is_none());
    }

    #[cfg(feature = "driver-path-local")]
    #[test]
    fn path_local_missing_root_errors() {
        let _g = lock_gate();
        enable_all();
        let peer_client = noop_peer_client();
        let runtime = noop_runtime();
        let args = base_args("path_local", &peer_client, &runtime);
        let err = expect_err(DefaultObjectStoreProvider.build(&args));
        assert!(err.contains("local_root"), "err was: {err}");
    }

    #[cfg(feature = "driver-cas-local")]
    #[test]
    fn builds_cas_local() {
        let _g = lock_gate();
        enable_all();
        let dir = tempfile::tempdir().unwrap();
        let peer_client = noop_peer_client();
        let runtime = noop_runtime();
        let args = ObjectStoreProviderArgs {
            local_root: Some(dir.path().to_str().unwrap()),
            self_address: Some("nexus-self:2126"),
            ..base_args("cas-local", &peer_client, &runtime)
        };
        let result = DefaultObjectStoreProvider
            .build(&args)
            .expect("cas-local build should succeed");
        assert!(result.backend.is_some());
        assert!(result.pending_remote_meta_store.is_none());
    }

    #[cfg(feature = "driver-local-connector")]
    #[test]
    fn builds_local_connector() {
        let _g = lock_gate();
        enable_all();
        let dir = tempfile::tempdir().unwrap();
        let peer_client = noop_peer_client();
        let runtime = noop_runtime();
        let args = ObjectStoreProviderArgs {
            local_root: Some(dir.path().to_str().unwrap()),
            ..base_args("local_connector", &peer_client, &runtime)
        };
        let result = DefaultObjectStoreProvider
            .build(&args)
            .expect("local_connector build should succeed");
        assert!(result.backend.is_some());
    }

    #[cfg(feature = "driver-s3")]
    #[test]
    fn builds_s3() {
        let _g = lock_gate();
        enable_all();
        let peer_client = noop_peer_client();
        let runtime = noop_runtime();
        let args = ObjectStoreProviderArgs {
            s3_bucket: Some("my-bucket"),
            s3_prefix: Some("prefix"),
            aws_region: Some("us-east-1"),
            aws_access_key: Some("AKIAIOSFODNN7EXAMPLE"),
            aws_secret_key: Some("wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"),
            ..base_args("s3", &peer_client, &runtime)
        };
        let result = DefaultObjectStoreProvider
            .build(&args)
            .expect("s3 build should succeed");
        assert!(result.backend.is_some());
        assert!(result.pending_remote_meta_store.is_none());
    }

    #[cfg(feature = "driver-s3")]
    #[test]
    fn s3_missing_bucket_errors() {
        let _g = lock_gate();
        enable_all();
        let peer_client = noop_peer_client();
        let runtime = noop_runtime();
        let args = ObjectStoreProviderArgs {
            aws_region: Some("us-east-1"),
            aws_access_key: Some("key"),
            aws_secret_key: Some("secret"),
            ..base_args("s3", &peer_client, &runtime)
        };
        let err = expect_err(DefaultObjectStoreProvider.build(&args));
        assert!(err.contains("s3_bucket"), "err was: {err}");
    }

    #[cfg(feature = "driver-gcs")]
    #[test]
    fn builds_gcs() {
        let _g = lock_gate();
        enable_all();
        let peer_client = noop_peer_client();
        let runtime = noop_runtime();
        let args = ObjectStoreProviderArgs {
            gcs_bucket: Some("my-gcs-bucket"),
            gcs_prefix: Some("prefix"),
            access_token: Some("ya29.token"),
            ..base_args("gcs", &peer_client, &runtime)
        };
        let result = DefaultObjectStoreProvider
            .build(&args)
            .expect("gcs build should succeed");
        assert!(result.backend.is_some());
    }

    // ── remote arm (ships in the slim cluster binary) ──────────────

    /// The remote arm is the only one that returns a non-`None`
    /// `pending_remote_meta_store`. `RpcTransport`'s channel is lazy
    /// (no TCP on construct), so this builds without a live server.
    #[cfg(feature = "driver-remote")]
    #[test]
    fn builds_remote() {
        let _g = lock_gate();
        enable_all();
        let peer_client = noop_peer_client();
        let runtime = noop_runtime();
        // No mount_path supplied → defaults to root-mount semantics.
        let args = ObjectStoreProviderArgs {
            server_address: Some("127.0.0.1:2126"),
            remote_auth_token: Some("tok"),
            ..base_args("remote", &peer_client, &runtime)
        };
        let result = DefaultObjectStoreProvider
            .build(&args)
            .expect("remote build should succeed");
        assert!(result.backend.is_some(), "remote must produce a backend");
        // The metastore side-effect is unique to the remote arm.
        assert!(
            result.pending_remote_meta_store.is_some(),
            "remote must populate pending_remote_meta_store"
        );
    }

    #[cfg(feature = "driver-remote")]
    #[test]
    fn remote_missing_server_address_errors() {
        let _g = lock_gate();
        enable_all();
        let peer_client = noop_peer_client();
        let runtime = noop_runtime();
        let args = base_args("remote", &peer_client, &runtime);
        let err = expect_err(DefaultObjectStoreProvider.build(&args));
        assert!(err.contains("server_address"), "err was: {err}");
    }

    /// An explicit root `mount_path` ("/" or "") is accepted (root-mount
    /// semantics — the only remote mount RemoteBackend handles correctly).
    #[cfg(feature = "driver-remote")]
    #[test]
    fn builds_remote_explicit_root_mount() {
        let _g = lock_gate();
        enable_all();
        let peer_client = noop_peer_client();
        let runtime = noop_runtime();
        for root in ["/", ""] {
            let args = ObjectStoreProviderArgs {
                server_address: Some("127.0.0.1:2126"),
                mount_path: Some(root),
                ..base_args("remote", &peer_client, &runtime)
            };
            let result = DefaultObjectStoreProvider
                .build(&args)
                .unwrap_or_else(|e| panic!("root mount_path {root:?} should build: {e}"));
            assert!(result.backend.is_some());
            assert!(result.pending_remote_meta_store.is_some());
        }
    }

    /// Fail closed: a sub-path remote mount is rejected (RemoteBackend
    /// cannot reconstruct sub-path hub paths yet — Issue #4273) rather
    /// than silently misrouting onto a different hub path.
    #[cfg(feature = "driver-remote")]
    #[test]
    fn remote_subpath_mount_rejected() {
        let _g = lock_gate();
        enable_all();
        let peer_client = noop_peer_client();
        let runtime = noop_runtime();
        let args = ObjectStoreProviderArgs {
            server_address: Some("127.0.0.1:2126"),
            mount_path: Some("/zone/acme"),
            ..base_args("remote", &peer_client, &runtime)
        };
        let err = expect_err(DefaultObjectStoreProvider.build(&args));
        assert!(
            err.contains("sub-path mount") && err.contains("/zone/acme"),
            "err was: {err}"
        );
    }

    /// Security policy: `grpcs://` with no TLS material must be rejected
    /// (no silent plaintext downgrade while carrying an auth token).
    #[cfg(feature = "driver-remote")]
    #[test]
    fn remote_grpcs_without_tls_errors() {
        let _g = lock_gate();
        enable_all();
        let peer_client = noop_peer_client();
        let runtime = noop_runtime();
        let args = ObjectStoreProviderArgs {
            server_address: Some("grpcs://hub.example.com:443"),
            remote_auth_token: Some("tok"),
            ..base_args("remote", &peer_client, &runtime)
        };
        let err = expect_err(DefaultObjectStoreProvider.build(&args));
        assert!(err.contains("grpcs://"), "err was: {err}");
    }

    /// Security policy: plaintext to a non-loopback host is refused by
    /// default (fail-closed); loopback is allowed.
    #[cfg(feature = "driver-remote")]
    #[test]
    fn remote_plaintext_nonloopback_errors() {
        let _g = lock_gate();
        enable_all();
        let peer_client = noop_peer_client();
        let runtime = noop_runtime();
        let args = ObjectStoreProviderArgs {
            server_address: Some("10.0.0.5:2126"),
            ..base_args("remote", &peer_client, &runtime)
        };
        let err = expect_err(DefaultObjectStoreProvider.build(&args));
        assert!(
            err.contains("non-loopback") && err.contains("NEXUS_GRPC_ALLOW_INSECURE"),
            "err was: {err}"
        );
    }

    /// `is_loopback_host`: localhost / loopback IPs (v4 + v6) are local;
    /// routable addresses are not.
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

    /// Regression: a degenerate (`+inf`/overflowing) `remote_timeout`
    /// must fall back to the default, never panic `build()`.
    #[cfg(feature = "driver-remote")]
    #[test]
    fn remote_tolerates_degenerate_timeout() {
        let _g = lock_gate();
        enable_all();
        let peer_client = noop_peer_client();
        let runtime = noop_runtime();
        for bad in [f64::INFINITY, 1e30, f64::NAN, -1.0, 0.0] {
            let args = ObjectStoreProviderArgs {
                server_address: Some("127.0.0.1:2126"),
                remote_timeout: bad,
                ..base_args("remote", &peer_client, &runtime)
            };
            let result = DefaultObjectStoreProvider
                .build(&args)
                .unwrap_or_else(|e| panic!("remote build panicked/failed on timeout={bad}: {e}"));
            assert!(result.backend.is_some());
        }
    }

    /// `build_tls_config`: no CA → plaintext (`None`); CA present →
    /// `Some` with the CA bytes and optional client cert/key mapped.
    #[cfg(feature = "driver-remote")]
    #[test]
    fn tls_config_assembly() {
        let peer_client = noop_peer_client();
        let runtime = noop_runtime();

        let plaintext = base_args("remote", &peer_client, &runtime);
        assert!(
            build_tls_config(&plaintext).is_none(),
            "no CA should mean plaintext (None)"
        );

        let ca = b"-----BEGIN CERTIFICATE-----CA-----END CERTIFICATE-----";
        let cert = b"-----BEGIN CERTIFICATE-----CERT-----END CERTIFICATE-----";
        let key = b"-----BEGIN PRIVATE KEY-----KEY-----END PRIVATE KEY-----";
        let mtls = ObjectStoreProviderArgs {
            remote_ca_pem: Some(ca),
            remote_cert_pem: Some(cert),
            remote_key_pem: Some(key),
            ..base_args("remote", &peer_client, &runtime)
        };
        let tls = build_tls_config(&mtls).expect("CA present should yield Some");
        assert_eq!(tls.ca_pem, ca);
        assert_eq!(tls.cert_pem.as_deref(), Some(&cert[..]));
        assert_eq!(tls.key_pem.as_deref(), Some(&key[..]));
    }

    #[test]
    fn unknown_backend_type_errors() {
        let _g = lock_gate();
        // The gate is checked before dispatch, so to reach the match's
        // catch-all arm the unknown name must itself be "enabled".
        set_enabled_drivers(["totally_unknown"]);
        let peer_client = noop_peer_client();
        let runtime = noop_runtime();
        let args = base_args("totally_unknown", &peer_client, &runtime);
        let err = expect_err(DefaultObjectStoreProvider.build(&args));
        assert!(err.contains("unknown backend_type"), "err was: {err}");
    }

    /// Gate REJECT path: a `backend_type` absent from the enabled set
    /// fails with the documented error before any construction. Uses
    /// `path_local` (compiled in every feature config) requested while
    /// the gate only allows `remote`, so this runs in the shipped slim
    /// cluster build too.
    #[cfg(feature = "driver-path-local")]
    #[test]
    fn driver_gate_rejects_disabled_driver() {
        let _g = lock_gate();
        // Enable only remote; path_local is excluded.
        set_enabled_drivers(["remote"]);
        let dir = tempfile::tempdir().unwrap();
        let peer_client = noop_peer_client();
        let runtime = noop_runtime();
        // Provide a valid local_root: the gate must reject regardless.
        let args = ObjectStoreProviderArgs {
            local_root: Some(dir.path().to_str().unwrap()),
            ..base_args("path_local", &peer_client, &runtime)
        };
        let err = expect_err(DefaultObjectStoreProvider.build(&args));
        assert_eq!(
            err, "driver 'path_local' not enabled in current deployment profile",
            "expected gate rejection, got: {err}"
        );
    }

    /// Gate ACCEPT path: when the `backend_type` IS in the enabled set,
    /// the gate passes and construction proceeds (here it then fails on
    /// the missing `local_root` — proving the error is NOT a gate
    /// rejection). Feature-independent, same as the reject test.
    #[cfg(feature = "driver-path-local")]
    #[test]
    fn driver_gate_accepts_enabled_driver() {
        let _g = lock_gate();
        set_enabled_drivers(["path_local"]);
        let peer_client = noop_peer_client();
        let runtime = noop_runtime();
        let args = base_args("path_local", &peer_client, &runtime);
        let err = expect_err(DefaultObjectStoreProvider.build(&args));
        assert!(
            !err.contains("not enabled"),
            "gate should have passed; err was: {err}"
        );
        assert!(err.contains("local_root"), "err was: {err}");
    }

    /// `set_provider` + `get_provider` round-trip. The provider
    /// `OnceLock` is process-wide and set-once, so tolerate an already
    /// registered provider from another test.
    #[test]
    fn get_provider_returns_registered_instance() {
        let _ = set_provider(Arc::new(DefaultObjectStoreProvider));
        assert!(get_provider().is_some());
    }
}
