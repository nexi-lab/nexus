//! `DefaultObjectStoreProvider` — backends-side impl of
//! `kernel::hal::object_store_provider::ObjectStoreProvider`.
//!
//! The 17-way backend-type construction switch lives here, lifted out
//! of `sys_setattr` so kernel does not reference concrete backend
//! types (`OpenAIBackend`, `S3Backend`, …). Cycle break is the
//! `kernel::hal::object_store_provider::ObjectStoreProvider` trait +
//! the `OnceLock` slot installed by `crate::python::register`.
//!
//! The single switch lives here so adding / removing a backend type
//! is one file change instead of editing `generated_kernel_abi_pyo3`
//! plus regenerating the codegen.

use std::path::{Path, PathBuf};
use std::sync::Arc;

use kernel::abc::object_store::ObjectStore;
use kernel::hal::object_store_provider::{
    is_driver_enabled, ObjectStoreBuildResult, ObjectStoreProvider, ObjectStoreProviderArgs,
};
use kernel::meta_store::MetaStore;

/// The canonical `ObjectStoreProvider` installed by `nexus-cdylib` at
/// boot.
///
/// Stateless — every `build()` call constructs fresh instances.
pub struct DefaultObjectStoreProvider;

impl ObjectStoreProvider for DefaultObjectStoreProvider {
    fn build(&self, args: &ObjectStoreProviderArgs<'_>) -> Result<ObjectStoreBuildResult, String> {
        let backend_name = args.backend_name;
        let backend_type = args.backend_type;

        // ── DeploymentProfile-driven driver gate (SSOT) ────────────
        // Every non-empty backend_type runs through `is_driver_enabled`
        // — there is no implicit local-default bypass and no string-
        // alias bridging.  Callers MUST pass canonical driver names
        // (`path_local`, `cas-local`, `local_connector`, `s3`, …) that
        // appear in the active profile's driver set; see
        // `LOCAL_DEFAULT_DRIVERS` in
        // `nexus.contracts.deployment_profile`.
        //
        // Empty backend_type means "no backend requested" (the
        // metadata-only path used by DT_DIR / DT_LINK / DT_PIPE
        // callers).  We skip the gate and return `None` below so the
        // kernel finishes the metadata mutation without consulting any
        // driver.  Treating empty as a driver request would force every
        // profile to enumerate a placeholder name in the gate set just
        // to support metadata syscalls — pure Python-debt leak.
        if !backend_type.is_empty() && !is_driver_enabled(backend_type) {
            return Err(format!(
                "driver {backend_type:?} not enabled in current deployment profile"
            ));
        }

        // Prevent unused-warnings when most drivers are feature-gated out
        // (e.g. nexusd-cluster slim build).
        let _ = backend_name;

        let mut pending_remote_meta_store: Option<Arc<dyn MetaStore>> = None;

        let backend: Option<Arc<dyn ObjectStore>> = match backend_type {
            "openai" => {
                #[cfg(feature = "driver-openai")]
                {
                    let base = args.openai_base_url.unwrap_or("https://api.openai.com/v1");
                    let key = args.openai_api_key.unwrap_or("");
                    let model = args.openai_model.unwrap_or("gpt-4o");
                    let blob_root = match args.openai_blob_root {
                        Some(p) => PathBuf::from(p),
                        None => std::env::temp_dir()
                            .join("nexus_llm_spool")
                            .join(backend_name),
                    };
                    let rt = Arc::clone(args.runtime);
                    let b = crate::transports::api::ai::openai::OpenAIBackend::new(
                        backend_name,
                        base,
                        key,
                        model,
                        &blob_root,
                        rt,
                    )
                    .map_err(|e| e.to_string())?;
                    Some(Arc::new(b) as Arc<dyn ObjectStore>)
                }
                #[cfg(not(feature = "driver-openai"))]
                {
                    return Err(driver_not_compiled("openai"));
                }
            }
            "anthropic" => {
                #[cfg(feature = "driver-anthropic")]
                {
                    let base = args
                        .anthropic_base_url
                        .unwrap_or("https://api.anthropic.com");
                    let key = args.anthropic_api_key.unwrap_or("");
                    let model = args.anthropic_model.unwrap_or("claude-sonnet-4-20250514");
                    let blob_root = match args.anthropic_blob_root {
                        Some(p) => PathBuf::from(p),
                        None => std::env::temp_dir()
                            .join("nexus_llm_spool")
                            .join(backend_name),
                    };
                    let rt = Arc::clone(args.runtime);
                    let b = crate::transports::api::ai::anthropic::AnthropicBackend::new(
                        backend_name,
                        base,
                        key,
                        model,
                        &blob_root,
                        rt,
                    )
                    .map_err(|e| e.to_string())?;
                    Some(Arc::new(b) as Arc<dyn ObjectStore>)
                }
                #[cfg(not(feature = "driver-anthropic"))]
                {
                    return Err(driver_not_compiled("anthropic"));
                }
            }
            "s3" => {
                #[cfg(feature = "driver-s3")]
                {
                    let bucket = args.s3_bucket.unwrap_or("");
                    let prefix = args.s3_prefix.unwrap_or("");
                    let region = args.aws_region.unwrap_or("us-east-1");
                    let ak = args.aws_access_key.unwrap_or("");
                    let sk = args.aws_secret_key.unwrap_or("");
                    let b = crate::transports::blob::s3::S3Backend::new(
                        backend_name,
                        bucket,
                        prefix,
                        region,
                        ak,
                        sk,
                        args.s3_endpoint,
                    )
                    .map_err(|e| e.to_string())?;
                    Some(Arc::new(b) as Arc<dyn ObjectStore>)
                }
                #[cfg(not(feature = "driver-s3"))]
                {
                    return Err(driver_not_compiled("s3"));
                }
            }
            "gcs" => {
                #[cfg(feature = "driver-gcs")]
                {
                    let bucket = args.gcs_bucket.unwrap_or("");
                    let prefix = args.gcs_prefix.unwrap_or("");
                    let token = args.access_token.unwrap_or("");
                    let b = crate::transports::blob::gcs::GcsBackend::new(
                        backend_name,
                        bucket,
                        prefix,
                        token,
                    )
                    .map_err(|e| e.to_string())?;
                    Some(Arc::new(b) as Arc<dyn ObjectStore>)
                }
                #[cfg(not(feature = "driver-gcs"))]
                {
                    return Err(driver_not_compiled("gcs"));
                }
            }
            "gdrive" => {
                #[cfg(feature = "driver-gdrive")]
                {
                    let token = args.access_token.unwrap_or("");
                    let folder = args.root_folder_id.unwrap_or("root");
                    let b = crate::transports::api::google::gdrive::GDriveBackend::new(
                        backend_name,
                        token,
                        folder,
                    )
                    .map_err(|e| e.to_string())?;
                    Some(Arc::new(b) as Arc<dyn ObjectStore>)
                }
                #[cfg(not(feature = "driver-gdrive"))]
                {
                    return Err(driver_not_compiled("gdrive"));
                }
            }
            "gmail" => {
                #[cfg(feature = "driver-gmail")]
                {
                    let token = args.access_token.unwrap_or("");
                    let b = crate::transports::api::google::gmail::GmailBackend::new(
                        backend_name,
                        token,
                    )
                    .map_err(|e| e.to_string())?;
                    Some(Arc::new(b) as Arc<dyn ObjectStore>)
                }
                #[cfg(not(feature = "driver-gmail"))]
                {
                    return Err(driver_not_compiled("gmail"));
                }
            }
            "slack" => {
                #[cfg(feature = "driver-slack")]
                {
                    let token = args.bot_token.unwrap_or("");
                    let channel = args.default_channel.unwrap_or("");
                    let b = crate::transports::api::social::slack::SlackBackend::new(
                        backend_name,
                        token,
                        channel,
                    )
                    .map_err(|e| e.to_string())?;
                    Some(Arc::new(b) as Arc<dyn ObjectStore>)
                }
                #[cfg(not(feature = "driver-slack"))]
                {
                    return Err(driver_not_compiled("slack"));
                }
            }
            "remote" => {
                #[cfg(feature = "driver-remote")]
                {
                    // RpcTransport is kernel-internal so backends can `use` it; the
                    // RemoteMetaStore wraps the same transport and surfaces in the
                    // factory result so PyKernel.sys_setattr can install it on the
                    // pending slot before mount registration.
                    let addr = args
                        .server_address
                        .ok_or("backend_type='remote' requires server_address")?;
                    let token = args.remote_auth_token.unwrap_or("");
                    let tls = args
                        .remote_ca_pem
                        .map(|ca| kernel::rpc_transport::TlsConfig {
                            ca_pem: ca.to_vec(),
                            cert_pem: args.remote_cert_pem.map(|b| b.to_vec()),
                            key_pem: args.remote_key_pem.map(|b| b.to_vec()),
                        });
                    let timeout = std::time::Duration::from_secs_f64(
                        if args.remote_timeout > 0.0 {
                            args.remote_timeout
                        } else {
                            30.0
                        },
                    );
                    let rt = Arc::clone(args.runtime);
                    let transport = Arc::new(
                        kernel::rpc_transport::RpcTransport::new(
                            rt,
                            addr,
                            token,
                            tls.as_ref(),
                            timeout,
                        )
                        .map_err(|e| e.to_string())?,
                    );
                    let remote_ms = Arc::new(
                        kernel::core::meta_store::remote::RemoteMetaStore::new(Arc::clone(
                            &transport,
                        )),
                    ) as Arc<dyn MetaStore>;
                    pending_remote_meta_store = Some(remote_ms);
                    let b = crate::storage::remote::RemoteBackend::new(transport);
                    Some(Arc::new(b) as Arc<dyn ObjectStore>)
                }
                #[cfg(not(feature = "driver-remote"))]
                {
                    return Err(driver_not_compiled("remote"));
                }
            }
            "hn" => {
                #[cfg(feature = "driver-hn")]
                {
                    let stories = args.hn_stories_per_feed.unwrap_or(10);
                    let comments = args.hn_include_comments.unwrap_or(true);
                    let b = crate::transports::api::social::hn::HNBackend::new(
                        backend_name,
                        stories,
                        comments,
                    )
                    .map_err(|e| e.to_string())?;
                    Some(Arc::new(b) as Arc<dyn ObjectStore>)
                }
                #[cfg(not(feature = "driver-hn"))]
                {
                    return Err(driver_not_compiled("hn"));
                }
            }
            "cli" => {
                #[cfg(feature = "driver-cli")]
                {
                    let cmd = args.cli_command.unwrap_or("");
                    let svc = args.cli_service.unwrap_or("");
                    let auth = args.cli_auth_env_json.unwrap_or("");
                    let b = crate::transports::api::cli::CLIBackend::new(
                        backend_name,
                        cmd,
                        svc,
                        auth,
                    )
                    .map_err(|e| e.to_string())?;
                    Some(Arc::new(b) as Arc<dyn ObjectStore>)
                }
                #[cfg(not(feature = "driver-cli"))]
                {
                    return Err(driver_not_compiled("cli"));
                }
            }
            "x" => {
                #[cfg(feature = "driver-x")]
                {
                    let token = args.x_bearer_token.unwrap_or("");
                    let b = crate::transports::api::social::x::XBackend::new(backend_name, token)
                        .map_err(|e| e.to_string())?;
                    Some(Arc::new(b) as Arc<dyn ObjectStore>)
                }
                #[cfg(not(feature = "driver-x"))]
                {
                    return Err(driver_not_compiled("x"));
                }
            }
            "local_connector" => {
                #[cfg(feature = "driver-local-connector")]
                {
                    let root = args
                        .local_root
                        .ok_or("backend_type='local_connector' requires local_root")?;
                    let b = crate::storage::local_connector::LocalConnectorBackend::new(
                        Path::new(root),
                        args.follow_symlinks,
                        args.fsync,
                    )
                    .map_err(|e| e.to_string())?;
                    Some(Arc::new(b) as Arc<dyn ObjectStore>)
                }
                #[cfg(not(feature = "driver-local-connector"))]
                {
                    return Err(driver_not_compiled("local_connector"));
                }
            }
            "path_local" => {
                #[cfg(feature = "driver-path-local")]
                {
                    let root = args
                        .local_root
                        .ok_or("backend_type='path_local' requires local_root")?;
                    let b = crate::storage::path_local::PathLocalBackend::new(
                        Path::new(root),
                        args.fsync,
                    )
                    .map_err(|e| e.to_string())?;
                    Some(Arc::new(b) as Arc<dyn ObjectStore>)
                }
                #[cfg(not(feature = "driver-path-local"))]
                {
                    return Err(driver_not_compiled("path_local"));
                }
            }
            "cas-local" => {
                #[cfg(feature = "driver-cas-local")]
                {
                    let root = args
                        .local_root
                        .ok_or("backend_type='cas-local' requires local_root")?;
                    // CAS-local backend wires a per-mount scatter-gather
                    // fetcher built from `args.peer_client` (the kernel's
                    // live SSOT slot, snapshotted at this `sys_setattr`
                    // call) + `args.self_address`.  No `Kernel.chunk_fetcher`
                    // shadow to keep in sync with peer_client swaps.
                    let fetcher: Arc<dyn kernel::cas_remote::RemoteChunkFetcher> =
                        Arc::new(kernel::cas_remote::GrpcChunkFetcher::new(
                            Arc::clone(args.peer_client),
                            args.self_address.map(str::to_string),
                        ));
                    let b = crate::storage::cas_local::CasLocalBackend::new_with_fetcher(
                        Path::new(root),
                        args.fsync,
                        fetcher,
                    )
                    .map_err(|e| e.to_string())?;
                    Some(Arc::new(b) as Arc<dyn ObjectStore>)
                }
                #[cfg(not(feature = "driver-cas-local"))]
                {
                    return Err(driver_not_compiled("cas-local"));
                }
            }
            // Empty backend_type: no driver requested (DT_DIR / DT_LINK /
            // DT_PIPE metadata syscalls).  Gate skipped above.
            "" => None,
            other => {
                return Err(format!("unknown backend_type {other:?}"));
            }
        };

        Ok(ObjectStoreBuildResult {
            backend,
            pending_remote_meta_store,
        })
    }
}

#[allow(dead_code)]
fn driver_not_compiled(name: &str) -> String {
    format!("driver `{name}` not compiled into this binary")
}

#[cfg(test)]
mod tests {
    //! Regression tests for the SSOT driver gate.  Every dispatch — including
    //! local-host backends like `path_local` — must go through
    //! `is_driver_enabled`; the previous "kernel default" skip-branch is
    //! gone.  These tests use process-wide `set_enabled_drivers`, so they
    //! must run in a fresh test binary (they self-restore an open gate at
    //! the end so other tests in the same process aren't affected).

    use super::*;
    use kernel::hal::object_store_provider::set_enabled_drivers;
    use kernel::hal::peer::NoopPeerBlobClient;
    use std::sync::Arc;

    fn make_args<'a>(
        backend_type: &'a str,
        local_root: Option<&'a str>,
        peer: &'a Arc<dyn kernel::hal::peer::PeerBlobClient>,
        rt: &'a Arc<tokio::runtime::Runtime>,
    ) -> ObjectStoreProviderArgs<'a> {
        ObjectStoreProviderArgs {
            backend_type,
            backend_name: "test",
            local_root,
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
            remote_timeout: 0.0,
            peer_client: peer,
            self_address: None,
            runtime: rt,
        }
    }

    /// `path_local` mount fails when `path_local` is not in the active
    /// profile's driver gate.  Pre-SSOT this was bypassed via the
    /// `is_local_default` short-circuit.
    #[test]
    fn path_local_rejected_when_not_in_gate() {
        // Gate the test process to a set that excludes path_local.
        set_enabled_drivers(["remote"]);
        let peer: Arc<dyn kernel::hal::peer::PeerBlobClient> = NoopPeerBlobClient::arc();
        let rt = Arc::new(tokio::runtime::Runtime::new().expect("rt"));
        let tmp = std::env::temp_dir().join("nexus-driver-gate-regression");
        let _ = std::fs::create_dir_all(&tmp);
        let root = tmp.to_string_lossy().to_string();
        let args = make_args("path_local", Some(&root), &peer, &rt);
        let res = DefaultObjectStoreProvider.build(&args);
        // Restore an open gate immediately so we don't poison sibling tests.
        set_enabled_drivers(std::iter::empty::<String>());
        match res {
            Err(err) => assert!(
                err.contains("path_local") && err.contains("not enabled"),
                "unexpected error: {err}"
            ),
            Ok(_) => panic!("path_local must be rejected when gate omits it"),
        }
    }
}
