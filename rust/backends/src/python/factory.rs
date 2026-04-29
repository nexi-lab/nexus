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
    ObjectStoreBuildResult, ObjectStoreProvider, ObjectStoreProviderArgs,
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

        let mut pending_remote_meta_store: Option<Arc<dyn MetaStore>> = None;

        let backend: Option<Arc<dyn ObjectStore>> = if backend_type == "openai" {
            #[cfg(feature = "connectors")]
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
            #[cfg(not(feature = "connectors"))]
            {
                return Err("connectors feature not enabled".into());
            }
        } else if backend_type == "anthropic" {
            #[cfg(feature = "connectors")]
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
            #[cfg(not(feature = "connectors"))]
            {
                return Err("connectors feature not enabled".into());
            }
        } else if backend_type == "s3" {
            #[cfg(feature = "connectors")]
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
            #[cfg(not(feature = "connectors"))]
            {
                return Err("connectors feature not enabled".into());
            }
        } else if backend_type == "gcs" {
            #[cfg(feature = "connectors")]
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
            #[cfg(not(feature = "connectors"))]
            {
                return Err("connectors feature not enabled".into());
            }
        } else if backend_type == "gdrive" {
            #[cfg(feature = "connectors")]
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
            #[cfg(not(feature = "connectors"))]
            {
                return Err("connectors feature not enabled".into());
            }
        } else if backend_type == "gmail" {
            #[cfg(feature = "connectors")]
            {
                let token = args.access_token.unwrap_or("");
                let b =
                    crate::transports::api::google::gmail::GmailBackend::new(backend_name, token)
                        .map_err(|e| e.to_string())?;
                Some(Arc::new(b) as Arc<dyn ObjectStore>)
            }
            #[cfg(not(feature = "connectors"))]
            {
                return Err("connectors feature not enabled".into());
            }
        } else if backend_type == "slack" {
            #[cfg(feature = "connectors")]
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
            #[cfg(not(feature = "connectors"))]
            {
                return Err("connectors feature not enabled".into());
            }
        } else if backend_type == "remote" {
            // Remote backend — always available (core capability, not connector feature).
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
            let timeout = std::time::Duration::from_secs_f64(if args.remote_timeout > 0.0 {
                args.remote_timeout
            } else {
                30.0
            });
            let rt = Arc::clone(args.runtime);
            let transport = Arc::new(
                kernel::rpc_transport::RpcTransport::new(rt, addr, token, tls.as_ref(), timeout)
                    .map_err(|e| e.to_string())?,
            );
            let remote_ms = Arc::new(kernel::core::meta_store::remote::RemoteMetaStore::new(
                Arc::clone(&transport),
            )) as Arc<dyn MetaStore>;
            pending_remote_meta_store = Some(remote_ms);
            let b = crate::storage::remote::RemoteBackend::new(transport);
            Some(Arc::new(b) as Arc<dyn ObjectStore>)
        } else if backend_type == "hn" {
            #[cfg(feature = "connectors")]
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
            #[cfg(not(feature = "connectors"))]
            {
                return Err("connectors feature not enabled".into());
            }
        } else if backend_type == "cli" {
            #[cfg(feature = "connectors")]
            {
                let cmd = args.cli_command.unwrap_or("");
                let svc = args.cli_service.unwrap_or("");
                let auth = args.cli_auth_env_json.unwrap_or("");
                let b = crate::transports::api::cli::CLIBackend::new(backend_name, cmd, svc, auth)
                    .map_err(|e| e.to_string())?;
                Some(Arc::new(b) as Arc<dyn ObjectStore>)
            }
            #[cfg(not(feature = "connectors"))]
            {
                return Err("connectors feature not enabled".into());
            }
        } else if backend_type == "x" {
            #[cfg(feature = "connectors")]
            {
                let token = args.x_bearer_token.unwrap_or("");
                let b = crate::transports::api::social::x::XBackend::new(backend_name, token)
                    .map_err(|e| e.to_string())?;
                Some(Arc::new(b) as Arc<dyn ObjectStore>)
            }
            #[cfg(not(feature = "connectors"))]
            {
                return Err("connectors feature not enabled".into());
            }
        } else if let Some(root) = args.local_root {
            // local_root branch: backend_type ∈ { "local_connector",
            // "path_local", default cas-local }.  The three impls live
            // in `crate::storage::*` after the Phase 2 split of the
            // old kernel `_backend_impls.rs`.
            if backend_type == "local_connector" {
                let b = crate::storage::local_connector::LocalConnectorBackend::new(
                    Path::new(root),
                    args.follow_symlinks,
                    args.fsync,
                )
                .map_err(|e| e.to_string())?;
                Some(Arc::new(b) as Arc<dyn ObjectStore>)
            } else if backend_type == "path_local" {
                let b =
                    crate::storage::path_local::PathLocalBackend::new(Path::new(root), args.fsync)
                        .map_err(|e| e.to_string())?;
                Some(Arc::new(b) as Arc<dyn ObjectStore>)
            } else {
                // Default: CAS-local backend with the kernel's
                // chunk_fetcher pre-wired so local chunk misses on this
                // mount fall through to peer RPCs against
                // `backend_name.origins`.  The chunk_fetcher Arc is
                // smuggled in via `args.peer_client.chunk_fetcher_arc()`
                // (added on Kernel during Phase 2).
                let fetcher: Arc<dyn kernel::cas_remote::RemoteChunkFetcher> =
                    Arc::clone(&args.chunk_fetcher);
                let b = crate::storage::cas_local::CasLocalBackend::new_with_fetcher(
                    Path::new(root),
                    args.fsync,
                    fetcher,
                )
                .map_err(|e| e.to_string())?;
                Some(Arc::new(b) as Arc<dyn ObjectStore>)
            }
        } else {
            None
        };

        Ok(ObjectStoreBuildResult {
            backend,
            pending_remote_meta_store,
        })
    }
}
