//! `nexus-vault` — password-vault dylib plugin.
//!
//! Loaded by `nexusd-cluster` at startup via `--plugin-dir`. Registers
//! as a `RustService` named `"password-vault"` through the plugin ABI's
//! `declare_service_plugin!` macro.
//!
//! Data layout (under `$NEXUS_DATA_DIR/vault/`):
//!   vault/vault-meta.redb  — private kernel metastore (entry metadata)
//!   vault/content/         — PathLocalBackend (encrypted entry blobs)
//!   vault/master.key       — 32-byte AES-256 master key
//!                            (auto-generated on first load)
//!
//! Unified storage layout (under `/vault` kernel mount):
//!   /vault/entries/passwords/{title}           → SecretIndex
//!   /vault/entries/{namespace}/{key}            → SecretIndex
//!   /vault/versions/passwords/{title}/{v:010}   → StoredEntry
//!   /vault/versions/{namespace}/{key}/{v:010}   → StoredEntry

use std::path::PathBuf;
use std::sync::Arc;

use nexus_plugin_abi::{declare_service_plugin, KernelHandle};
use prost::Message;
use tonic::Request;

use services::password_vault::proto::password_vault_service_server::PasswordVaultService;
use services::password_vault::proto::*;
use services::password_vault::PasswordVaultServiceImpl;

use services::generic_secrets::proto::generic_secrets_service_server::GenericSecretsService;
use services::generic_secrets::GenericSecretsServiceImpl;

// Alias to disambiguate from password_vault proto types.
mod secrets_proto {
    pub use services::generic_secrets::proto::*;
}

/// Plugin state: the vault service plus a single-threaded tokio runtime
/// for driving the async gRPC trait methods.
struct VaultPlugin {
    svc: PasswordVaultServiceImpl,
    secrets_svc: GenericSecretsServiceImpl,
    rt: tokio::runtime::Runtime,
}

fn create_vault(_kernel_handle: &KernelHandle) -> Box<VaultPlugin> {
    let data_dir = std::env::var("NEXUS_DATA_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("./nexus-data"));
    let vault_dir = data_dir.join("vault");
    std::fs::create_dir_all(&vault_dir).expect("create vault data dir");

    let kernel = Arc::new(kernel::kernel::Kernel::new());
    let meta_path = vault_dir.join("vault-meta.redb");
    if let Some(p) = meta_path.to_str() {
        kernel
            .set_metastore_path(p)
            .expect("set vault metastore path");
    }

    let content_dir = vault_dir.join("content");
    let backend: Arc<dyn kernel::abc::object_store::ObjectStore> = Arc::new(
        backends::storage::path_local::PathLocalBackend::new(&content_dir, /* fsync */ true)
            .expect("create PathLocalBackend for vault content"),
    );
    let backend_name = backend.name().to_string();

    // Mount backend at /vault — shared by both services.
    kernel
        .sys_setattr(
            "/vault",
            /* DT_MOUNT */ 2,
            &backend_name,
            Some(backend),
            None,
            None,
            "memory",
            "root",
            false,
            0,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        )
        .expect("mount vault backend");

    let master_key_path = vault_dir.join("master.key");
    let master_key = services::password_vault::crypto::load_or_create_master_key(&master_key_path)
        .expect("load/create master key");

    // GenericSecretsService owns the unified storage layer.
    let secrets_svc =
        GenericSecretsServiceImpl::new_on_existing_mount(kernel, "/vault", master_key)
            .expect("open generic secrets");

    // PasswordVaultService wraps GenericSecretsService (namespace="passwords").
    let svc = PasswordVaultServiceImpl::new_with_secrets(secrets_svc.clone());

    let rt = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("build tokio runtime for vault plugin");

    tracing::info!(
        vault_dir = %vault_dir.display(),
        "vault plugin loaded (password-vault + generic-secrets)"
    );

    Box::new(VaultPlugin {
        svc,
        secrets_svc,
        rt,
    })
}

fn dispatch_vault(plugin: &VaultPlugin, method: &str, payload: &[u8]) -> Result<Vec<u8>, i32> {
    match method {
        "put_entry" => {
            let req = PutEntryRequest::decode(payload).map_err(|_| -2)?;
            let resp = plugin
                .rt
                .block_on(plugin.svc.put_entry(Request::new(req)))
                .map_err(|_| -3)?
                .into_inner();
            let mut buf = Vec::new();
            resp.encode(&mut buf).map_err(|_| -3)?;
            Ok(buf)
        }
        "get_entry" => {
            let req = GetEntryRequest::decode(payload).map_err(|_| -2)?;
            let resp = plugin
                .rt
                .block_on(plugin.svc.get_entry(Request::new(req)))
                .map_err(status_to_plugin_error)?
                .into_inner();
            let mut buf = Vec::new();
            resp.encode(&mut buf).map_err(|_| -3)?;
            Ok(buf)
        }
        "list_entries" => {
            let req = ListEntriesRequest::decode(payload).map_err(|_| -2)?;
            let resp = plugin
                .rt
                .block_on(plugin.svc.list_entries(Request::new(req)))
                .map_err(|_| -3)?
                .into_inner();
            let mut buf = Vec::new();
            resp.encode(&mut buf).map_err(|_| -3)?;
            Ok(buf)
        }
        "delete_entry" => {
            let req = DeleteEntryRequest::decode(payload).map_err(|_| -2)?;
            let resp = plugin
                .rt
                .block_on(plugin.svc.delete_entry(Request::new(req)))
                .map_err(status_to_plugin_error)?
                .into_inner();
            let mut buf = Vec::new();
            resp.encode(&mut buf).map_err(|_| -3)?;
            Ok(buf)
        }
        "restore_entry" => {
            let req = RestoreEntryRequest::decode(payload).map_err(|_| -2)?;
            let resp = plugin
                .rt
                .block_on(plugin.svc.restore_entry(Request::new(req)))
                .map_err(status_to_plugin_error)?
                .into_inner();
            let mut buf = Vec::new();
            resp.encode(&mut buf).map_err(|_| -3)?;
            Ok(buf)
        }
        "list_versions" => {
            let req = ListVersionsRequest::decode(payload).map_err(|_| -2)?;
            let resp = plugin
                .rt
                .block_on(plugin.svc.list_versions(Request::new(req)))
                .map_err(status_to_plugin_error)?
                .into_inner();
            let mut buf = Vec::new();
            resp.encode(&mut buf).map_err(|_| -3)?;
            Ok(buf)
        }
        "generate_totp" => {
            let req = GenerateTotpRequest::decode(payload).map_err(|_| -2)?;
            let resp = plugin
                .rt
                .block_on(plugin.svc.generate_totp(Request::new(req)))
                .map_err(status_to_plugin_error)?
                .into_inner();
            let mut buf = Vec::new();
            resp.encode(&mut buf).map_err(|_| -3)?;
            Ok(buf)
        }
        // ── Generic secrets dispatch ──────────────────────────────────
        "secret_put" => {
            let req = secrets_proto::PutSecretRequest::decode(payload).map_err(|_| -2)?;
            let resp = plugin
                .rt
                .block_on(plugin.secrets_svc.put_secret(Request::new(req)))
                .map_err(status_to_plugin_error)?
                .into_inner();
            let mut buf = Vec::new();
            resp.encode(&mut buf).map_err(|_| -3)?;
            Ok(buf)
        }
        "secret_get" => {
            let req = secrets_proto::GetSecretRequest::decode(payload).map_err(|_| -2)?;
            let resp = plugin
                .rt
                .block_on(plugin.secrets_svc.get_secret(Request::new(req)))
                .map_err(status_to_plugin_error)?
                .into_inner();
            let mut buf = Vec::new();
            resp.encode(&mut buf).map_err(|_| -3)?;
            Ok(buf)
        }
        "secret_delete" => {
            let req = secrets_proto::DeleteSecretRequest::decode(payload).map_err(|_| -2)?;
            let resp = plugin
                .rt
                .block_on(plugin.secrets_svc.delete_secret(Request::new(req)))
                .map_err(status_to_plugin_error)?
                .into_inner();
            let mut buf = Vec::new();
            resp.encode(&mut buf).map_err(|_| -3)?;
            Ok(buf)
        }
        "secret_restore" => {
            let req = secrets_proto::RestoreSecretRequest::decode(payload).map_err(|_| -2)?;
            let resp = plugin
                .rt
                .block_on(plugin.secrets_svc.restore_secret(Request::new(req)))
                .map_err(status_to_plugin_error)?
                .into_inner();
            let mut buf = Vec::new();
            resp.encode(&mut buf).map_err(|_| -3)?;
            Ok(buf)
        }
        "secret_list" => {
            let req = secrets_proto::ListSecretsRequest::decode(payload).map_err(|_| -2)?;
            let resp = plugin
                .rt
                .block_on(plugin.secrets_svc.list_secrets(Request::new(req)))
                .map_err(status_to_plugin_error)?
                .into_inner();
            let mut buf = Vec::new();
            resp.encode(&mut buf).map_err(|_| -3)?;
            Ok(buf)
        }
        "secret_list_versions" => {
            let req = secrets_proto::ListSecretVersionsRequest::decode(payload).map_err(|_| -2)?;
            let resp = plugin
                .rt
                .block_on(plugin.secrets_svc.list_secret_versions(Request::new(req)))
                .map_err(status_to_plugin_error)?
                .into_inner();
            let mut buf = Vec::new();
            resp.encode(&mut buf).map_err(|_| -3)?;
            Ok(buf)
        }
        "secret_batch_put" => {
            let req = secrets_proto::BatchPutSecretsRequest::decode(payload).map_err(|_| -2)?;
            let resp = plugin
                .rt
                .block_on(plugin.secrets_svc.batch_put_secrets(Request::new(req)))
                .map_err(status_to_plugin_error)?
                .into_inner();
            let mut buf = Vec::new();
            resp.encode(&mut buf).map_err(|_| -3)?;
            Ok(buf)
        }
        "secret_batch_get" => {
            let req = secrets_proto::BatchGetSecretsRequest::decode(payload).map_err(|_| -2)?;
            let resp = plugin
                .rt
                .block_on(plugin.secrets_svc.batch_get_secrets(Request::new(req)))
                .map_err(status_to_plugin_error)?
                .into_inner();
            let mut buf = Vec::new();
            resp.encode(&mut buf).map_err(|_| -3)?;
            Ok(buf)
        }
        "secret_delete_version" => {
            let req = secrets_proto::DeleteSecretVersionRequest::decode(payload).map_err(|_| -2)?;
            let resp = plugin
                .rt
                .block_on(plugin.secrets_svc.delete_secret_version(Request::new(req)))
                .map_err(status_to_plugin_error)?
                .into_inner();
            let mut buf = Vec::new();
            resp.encode(&mut buf).map_err(|_| -3)?;
            Ok(buf)
        }
        "secret_update_description" => {
            let req =
                secrets_proto::UpdateSecretDescriptionRequest::decode(payload).map_err(|_| -2)?;
            let resp = plugin
                .rt
                .block_on(
                    plugin
                        .secrets_svc
                        .update_secret_description(Request::new(req)),
                )
                .map_err(status_to_plugin_error)?
                .into_inner();
            let mut buf = Vec::new();
            resp.encode(&mut buf).map_err(|_| -3)?;
            Ok(buf)
        }
        _ => Err(-1), // PluginResult::NotFound
    }
}

fn status_to_plugin_error(status: tonic::Status) -> i32 {
    match status.code() {
        tonic::Code::NotFound => -1,
        tonic::Code::InvalidArgument => -2,
        _ => -3,
    }
}

declare_service_plugin!("password-vault", VaultPlugin, {
    create: create_vault,
    dispatch: dispatch_vault,
});

// ── Plugin dispatch E2E tests ──────────────────────────────────────

#[cfg(test)]
mod dispatch_e2e {
    use super::*;
    use prost::Message;
    use tempfile::TempDir;

    fn fresh_plugin() -> (TempDir, VaultPlugin) {
        let dir = TempDir::new().unwrap();
        let vault_dir = dir.path().join("vault");
        std::fs::create_dir_all(&vault_dir).unwrap();

        let kernel = Arc::new(kernel::kernel::Kernel::new());
        let meta_path = vault_dir.join("vault-meta.redb");
        if let Some(p) = meta_path.to_str() {
            kernel.set_metastore_path(p).unwrap();
        }

        let content_dir = vault_dir.join("content");
        let backend: Arc<dyn kernel::abc::object_store::ObjectStore> = Arc::new(
            backends::storage::path_local::PathLocalBackend::new(&content_dir, true).unwrap(),
        );
        let backend_name = backend.name().to_string();

        // Mount backend.
        kernel
            .sys_setattr(
                "/vault",
                2,
                &backend_name,
                Some(backend),
                None,
                None,
                "memory",
                "root",
                false,
                0,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            )
            .unwrap();

        let master_key_path = vault_dir.join("master.key");
        let master_key =
            services::password_vault::crypto::load_or_create_master_key(&master_key_path).unwrap();

        let secrets_svc =
            GenericSecretsServiceImpl::new_on_existing_mount(kernel, "/vault", master_key).unwrap();
        let svc = PasswordVaultServiceImpl::new_with_secrets(secrets_svc.clone());

        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();

        (
            dir,
            VaultPlugin {
                svc,
                secrets_svc,
                rt,
            },
        )
    }

    fn entry(title: &str, password: &str, totp_secret: Option<&str>) -> VaultEntry {
        VaultEntry {
            title: title.into(),
            username: Some("alice".into()),
            password: Some(password.into()),
            url: Some("https://example.com".into()),
            notes: None,
            tags: None,
            totp_secret: totp_secret.map(String::from),
            extra_json: None,
        }
    }

    fn encode<M: Message>(msg: &M) -> Vec<u8> {
        let mut buf = Vec::new();
        msg.encode(&mut buf).unwrap();
        buf
    }

    // ── Scenario 1: Full credential lifecycle via dispatch ─────────

    #[test]
    fn full_credential_lifecycle_via_dispatch() {
        let (_dir, plugin) = fresh_plugin();

        let put_payload = encode(&PutEntryRequest {
            entry: Some(entry("github", "hunter2", None)),
            audit: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "put_entry", &put_payload).unwrap();
        let put_resp = PutEntryResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(put_resp.title, "github");
        assert_eq!(put_resp.version, 1);

        let get_payload = encode(&GetEntryRequest {
            title: put_resp.title.clone(),
            version: None,
            audit: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "get_entry", &get_payload).unwrap();
        let get_resp = GetEntryResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(get_resp.entry.as_ref().unwrap().title, "github");
        assert_eq!(
            get_resp.entry.as_ref().unwrap().password.as_deref(),
            Some("hunter2")
        );

        let list_payload = encode(&ListEntriesRequest {
            query: String::new(),
            limit: 0,
            audit: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "list_entries", &list_payload).unwrap();
        let list_resp = ListEntriesResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(list_resp.total_in_vault, 1);
        assert_eq!(list_resp.entries[0].title, "github");

        let del_payload = encode(&DeleteEntryRequest {
            title: put_resp.title.clone(),
            audit: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "delete_entry", &del_payload).unwrap();
        let del_resp = DeleteEntryResponse::decode(resp_bytes.as_slice()).unwrap();
        assert!(del_resp.deleted);

        let err = dispatch_vault(&plugin, "get_entry", &get_payload).unwrap_err();
        assert_eq!(err, -1);

        let restore_payload = encode(&RestoreEntryRequest {
            title: put_resp.title.clone(),
            audit: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "restore_entry", &restore_payload).unwrap();
        let restore_resp = RestoreEntryResponse::decode(resp_bytes.as_slice()).unwrap();
        assert!(restore_resp.restored);

        let resp_bytes = dispatch_vault(&plugin, "get_entry", &get_payload).unwrap();
        let get_resp = GetEntryResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(get_resp.entry.unwrap().password.as_deref(), Some("hunter2"));
    }

    // ── Scenario 2: Password rotation with version history ─────────

    #[test]
    fn password_rotation_with_version_history() {
        let (_dir, plugin) = fresh_plugin();

        for (i, pw) in ["initial-pw", "rotated-pw", "final-pw"].iter().enumerate() {
            let payload = encode(&PutEntryRequest {
                entry: Some(entry("aws-prod", pw, None)),
                audit: None,
            });
            let resp_bytes = dispatch_vault(&plugin, "put_entry", &payload).unwrap();
            let resp = PutEntryResponse::decode(resp_bytes.as_slice()).unwrap();
            assert_eq!(resp.version, (i + 1) as i32);
        }

        let lv_payload = encode(&ListVersionsRequest {
            title: "aws-prod".into(),
            audit: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "list_versions", &lv_payload).unwrap();
        let lv_resp = ListVersionsResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(lv_resp.count, 3);
        let vers: Vec<i32> = lv_resp.versions.iter().map(|v| v.version).collect();
        assert_eq!(vers, vec![1, 2, 3]);

        let get_v1 = encode(&GetEntryRequest {
            title: "aws-prod".into(),
            version: Some(1),
            audit: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "get_entry", &get_v1).unwrap();
        let resp = GetEntryResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(resp.entry.unwrap().password.as_deref(), Some("initial-pw"));

        let get_latest = encode(&GetEntryRequest {
            title: "aws-prod".into(),
            version: None,
            audit: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "get_entry", &get_latest).unwrap();
        let resp = GetEntryResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(resp.entry.unwrap().password.as_deref(), Some("final-pw"));
    }

    // ── Scenario 3: TOTP survives password rotation ────────────────

    #[test]
    fn totp_survives_password_rotation() {
        let (_dir, plugin) = fresh_plugin();

        let payload = encode(&PutEntryRequest {
            entry: Some(entry("aws", "original-pw", Some("JBSWY3DPEHPK3PXP"))),
            audit: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "put_entry", &payload).unwrap();
        let put_resp = PutEntryResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(put_resp.version, 1);

        let totp_payload = encode(&GenerateTotpRequest {
            title: "aws".into(),
            audit: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "generate_totp", &totp_payload).unwrap();
        let totp1 = GenerateTotpResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(totp1.code.len(), 6);
        assert!(totp1.code.chars().all(|c| c.is_ascii_digit()));
        assert_eq!(totp1.period_seconds, 30);

        let payload = encode(&PutEntryRequest {
            entry: Some(entry("aws", "rotated-pw", Some("JBSWY3DPEHPK3PXP"))),
            audit: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "put_entry", &payload).unwrap();
        let put_resp = PutEntryResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(put_resp.version, 2);

        let resp_bytes = dispatch_vault(&plugin, "generate_totp", &totp_payload).unwrap();
        let totp2 = GenerateTotpResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(totp1.code, totp2.code);

        let get_payload = encode(&GetEntryRequest {
            title: "aws".into(),
            version: None,
            audit: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "get_entry", &get_payload).unwrap();
        let get_resp = GetEntryResponse::decode(resp_bytes.as_slice()).unwrap();
        let e = get_resp.entry.unwrap();
        assert_eq!(e.password.as_deref(), Some("rotated-pw"));
        assert!(e.totp_secret.is_none());
    }

    // ── Scenario 4: Multi-credential search and cleanup ────────────

    #[test]
    fn multi_credential_search_and_cleanup() {
        let (_dir, plugin) = fresh_plugin();

        for (title, pw) in [("gmail", "pw1"), ("github", "pw2"), ("aws-prod", "pw3")] {
            let payload = encode(&PutEntryRequest {
                entry: Some(entry(title, pw, None)),
                audit: None,
            });
            dispatch_vault(&plugin, "put_entry", &payload).unwrap();
        }

        let list_payload = encode(&ListEntriesRequest {
            query: "git".into(),
            limit: 0,
            audit: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "list_entries", &list_payload).unwrap();
        let list_resp = ListEntriesResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(list_resp.matched, 1);
        assert_eq!(list_resp.total_in_vault, 3);
        assert_eq!(list_resp.entries[0].title, "github");

        let del_payload = encode(&DeleteEntryRequest {
            title: "github".into(),
            audit: None,
        });
        dispatch_vault(&plugin, "delete_entry", &del_payload).unwrap();

        let list_all = encode(&ListEntriesRequest {
            query: String::new(),
            limit: 0,
            audit: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "list_entries", &list_all).unwrap();
        let list_resp = ListEntriesResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(list_resp.total_in_vault, 2);
        let titles: Vec<&str> = list_resp.entries.iter().map(|e| e.title.as_str()).collect();
        assert!(!titles.contains(&"github"));
    }

    // ── Scenario 5: Unknown method returns NotFound ────────────────

    #[test]
    fn unknown_method_returns_not_found() {
        let (_dir, plugin) = fresh_plugin();
        let err = dispatch_vault(&plugin, "nonexistent_method", &[]).unwrap_err();
        assert_eq!(err, -1);
    }

    // ── Scenario 6: Invalid protobuf returns InvalidArgument ───────

    #[test]
    fn invalid_protobuf_returns_invalid_argument() {
        let (_dir, plugin) = fresh_plugin();
        let err = dispatch_vault(&plugin, "get_entry", b"not-valid-protobuf").unwrap_err();
        assert_eq!(err, -2);
    }

    // ── Generic secrets dispatch tests ────────────────────────────

    #[test]
    fn generic_secret_full_lifecycle_via_dispatch() {
        let (_dir, plugin) = fresh_plugin();

        let put_payload = encode(&secrets_proto::PutSecretRequest {
            namespace: "provider:openai".into(),
            key: "api_key".into(),
            value: "sk-test-123".into(),
            description: Some("OpenAI prod key".into()),
        });
        let resp_bytes = dispatch_vault(&plugin, "secret_put", &put_payload).unwrap();
        let put_resp = secrets_proto::PutSecretResponse::decode(resp_bytes.as_slice()).unwrap();
        let meta = put_resp.metadata.unwrap();
        assert_eq!(meta.namespace, "provider:openai");
        assert_eq!(meta.key, "api_key");
        assert_eq!(meta.current_version, 1);
        assert!(!meta.deleted);

        let get_payload = encode(&secrets_proto::GetSecretRequest {
            namespace: "provider:openai".into(),
            key: "api_key".into(),
            version: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "secret_get", &get_payload).unwrap();
        let get_resp = secrets_proto::GetSecretResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(get_resp.value, "sk-test-123");
        assert_eq!(get_resp.version, 1);

        let list_payload = encode(&secrets_proto::ListSecretsRequest {
            namespace: Some("provider:openai".into()),
            include_deleted: false,
        });
        let resp_bytes = dispatch_vault(&plugin, "secret_list", &list_payload).unwrap();
        let list_resp = secrets_proto::ListSecretsResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(list_resp.count, 1);
        assert_eq!(list_resp.secrets[0].key, "api_key");

        let del_payload = encode(&secrets_proto::DeleteSecretRequest {
            namespace: "provider:openai".into(),
            key: "api_key".into(),
        });
        let resp_bytes = dispatch_vault(&plugin, "secret_delete", &del_payload).unwrap();
        let del_resp = secrets_proto::DeleteSecretResponse::decode(resp_bytes.as_slice()).unwrap();
        assert!(del_resp.deleted);

        let err = dispatch_vault(&plugin, "secret_get", &get_payload).unwrap_err();
        assert_eq!(err, -1);

        let restore_payload = encode(&secrets_proto::RestoreSecretRequest {
            namespace: "provider:openai".into(),
            key: "api_key".into(),
        });
        let resp_bytes = dispatch_vault(&plugin, "secret_restore", &restore_payload).unwrap();
        let restore_resp =
            secrets_proto::RestoreSecretResponse::decode(resp_bytes.as_slice()).unwrap();
        assert!(restore_resp.restored);

        let resp_bytes = dispatch_vault(&plugin, "secret_get", &get_payload).unwrap();
        let get_resp = secrets_proto::GetSecretResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(get_resp.value, "sk-test-123");
    }

    #[test]
    fn generic_secret_namespace_isolation() {
        let (_dir, plugin) = fresh_plugin();

        for (ns, k, v) in [
            ("channel:telegram:1", "token", "tok-123"),
            ("channel:lark:2", "appSecret", "lark-secret"),
            ("provider:openai", "api_key", "sk-456"),
        ] {
            let payload = encode(&secrets_proto::PutSecretRequest {
                namespace: ns.into(),
                key: k.into(),
                value: v.into(),
                description: None,
            });
            dispatch_vault(&plugin, "secret_put", &payload).unwrap();
        }

        let list_payload = encode(&secrets_proto::ListSecretsRequest {
            namespace: Some("channel:telegram:1".into()),
            include_deleted: false,
        });
        let resp_bytes = dispatch_vault(&plugin, "secret_list", &list_payload).unwrap();
        let list_resp = secrets_proto::ListSecretsResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(list_resp.count, 1);
        assert_eq!(list_resp.secrets[0].namespace, "channel:telegram:1");

        let list_all = encode(&secrets_proto::ListSecretsRequest {
            namespace: None,
            include_deleted: false,
        });
        let resp_bytes = dispatch_vault(&plugin, "secret_list", &list_all).unwrap();
        let list_resp = secrets_proto::ListSecretsResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(list_resp.count, 3);
    }

    #[test]
    fn generic_secret_batch_operations_via_dispatch() {
        let (_dir, plugin) = fresh_plugin();

        let batch_put_payload = encode(&secrets_proto::BatchPutSecretsRequest {
            secrets: vec![
                secrets_proto::PutSecretRequest {
                    namespace: "auth:jwt".into(),
                    key: "webui_secret".into(),
                    value: "jwt-secret-value".into(),
                    description: Some("JWT signing key".into()),
                },
                secrets_proto::PutSecretRequest {
                    namespace: "auth:acp:github".into(),
                    key: "auth_token".into(),
                    value: "ghp_token123".into(),
                    description: None,
                },
            ],
        });
        let resp_bytes = dispatch_vault(&plugin, "secret_batch_put", &batch_put_payload).unwrap();
        let batch_put_resp =
            secrets_proto::BatchPutSecretsResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(batch_put_resp.count, 2);

        let batch_get_payload = encode(&secrets_proto::BatchGetSecretsRequest {
            queries: vec![
                secrets_proto::GetSecretRequest {
                    namespace: "auth:jwt".into(),
                    key: "webui_secret".into(),
                    version: None,
                },
                secrets_proto::GetSecretRequest {
                    namespace: "auth:acp:github".into(),
                    key: "auth_token".into(),
                    version: None,
                },
                secrets_proto::GetSecretRequest {
                    namespace: "nonexistent".into(),
                    key: "nope".into(),
                    version: None,
                },
            ],
        });
        let resp_bytes = dispatch_vault(&plugin, "secret_batch_get", &batch_get_payload).unwrap();
        let batch_get_resp =
            secrets_proto::BatchGetSecretsResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(batch_get_resp.count, 2);
        assert_eq!(
            batch_get_resp.secrets["auth:jwt:webui_secret"],
            "jwt-secret-value"
        );
        assert_eq!(
            batch_get_resp.secrets["auth:acp:github:auth_token"],
            "ghp_token123"
        );
    }

    #[test]
    fn generic_secret_version_history_via_dispatch() {
        let (_dir, plugin) = fresh_plugin();

        for (i, val) in ["v1", "v2", "v3"].iter().enumerate() {
            let payload = encode(&secrets_proto::PutSecretRequest {
                namespace: "provider:openai".into(),
                key: "api_key".into(),
                value: val.to_string(),
                description: None,
            });
            let resp_bytes = dispatch_vault(&plugin, "secret_put", &payload).unwrap();
            let resp = secrets_proto::PutSecretResponse::decode(resp_bytes.as_slice()).unwrap();
            assert_eq!(resp.metadata.unwrap().current_version, (i + 1) as i32);
        }

        let lv_payload = encode(&secrets_proto::ListSecretVersionsRequest {
            namespace: "provider:openai".into(),
            key: "api_key".into(),
        });
        let resp_bytes = dispatch_vault(&plugin, "secret_list_versions", &lv_payload).unwrap();
        let lv_resp =
            secrets_proto::ListSecretVersionsResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(lv_resp.count, 3);
        let vers: Vec<i32> = lv_resp.versions.iter().map(|v| v.version).collect();
        assert_eq!(vers, vec![1, 2, 3]);

        let get_v1 = encode(&secrets_proto::GetSecretRequest {
            namespace: "provider:openai".into(),
            key: "api_key".into(),
            version: Some(1),
        });
        let resp_bytes = dispatch_vault(&plugin, "secret_get", &get_v1).unwrap();
        let resp = secrets_proto::GetSecretResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(resp.value, "v1");

        let get_latest = encode(&secrets_proto::GetSecretRequest {
            namespace: "provider:openai".into(),
            key: "api_key".into(),
            version: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "secret_get", &get_latest).unwrap();
        let resp = secrets_proto::GetSecretResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(resp.value, "v3");
    }

    // ── Scenario 11: Cross-service visibility ──────────────────────
    //
    // Password entries stored via PasswordVaultService should be visible
    // via GenericSecretsService in namespace="passwords", and vice versa.
    // Data flow: put_entry returns title → secret_list finds it →
    //   secret_get uses returned key → verify JSON contains password.

    #[test]
    fn password_entries_visible_via_generic_secrets_api() {
        let (_dir, plugin) = fresh_plugin();

        // Step 1: Store via password vault — capture returned title
        let put_payload = encode(&PutEntryRequest {
            entry: Some(entry("github", "hunter2", None)),
            audit: None,
        });
        let resp = dispatch_vault(&plugin, "put_entry", &put_payload).unwrap();
        let put_resp = PutEntryResponse::decode(resp.as_slice()).unwrap();
        let title = put_resp.title; // ← data flows from here

        // Step 2: List via generic secrets — use namespace to find the entry
        let list_payload = encode(&secrets_proto::ListSecretsRequest {
            namespace: Some("passwords".into()),
            include_deleted: false,
        });
        let resp = dispatch_vault(&plugin, "secret_list", &list_payload).unwrap();
        let list_resp = secrets_proto::ListSecretsResponse::decode(resp.as_slice()).unwrap();
        assert_eq!(list_resp.count, 1);
        assert_eq!(list_resp.secrets[0].namespace, "passwords");
        let discovered_key = &list_resp.secrets[0].key; // ← data flows
        assert_eq!(discovered_key, &title, "discovered key matches put title");

        // Step 3: Get raw value using key from step 2
        let get_payload = encode(&secrets_proto::GetSecretRequest {
            namespace: "passwords".into(),
            key: discovered_key.clone(),
            version: None,
        });
        let resp = dispatch_vault(&plugin, "secret_get", &get_payload).unwrap();
        let get_resp = secrets_proto::GetSecretResponse::decode(resp.as_slice()).unwrap();
        assert!(get_resp.value.contains("hunter2"), "raw JSON contains password");
        assert!(
            get_resp.value.contains(&format!("\"title\":\"{}\"", title)),
            "raw JSON title matches put title"
        );
    }

    // ── Scenario 12: Coexistence — password + generic secrets ──────
    //
    // Both services share /vault/entries/ and /vault/versions/.
    // User problem: "Store passwords AND API keys, then selectively
    //   delete one type without affecting the other."
    // Data flow: put returns titles/keys → list uses namespace to scope
    //   → delete uses returned key → verify cross-type isolation.

    #[test]
    fn coexistence_password_and_generic_secrets_no_interference() {
        let (_dir, plugin) = fresh_plugin();

        // Step 1: Store passwords — capture returned titles
        let mut pw_titles = Vec::new();
        for (title, pw) in [("gmail", "pw1"), ("github", "pw2")] {
            let payload = encode(&PutEntryRequest {
                entry: Some(entry(title, pw, None)),
                audit: None,
            });
            let resp = dispatch_vault(&plugin, "put_entry", &payload).unwrap();
            let put_resp = PutEntryResponse::decode(resp.as_slice()).unwrap();
            pw_titles.push(put_resp.title);
        }
        assert_eq!(pw_titles.len(), 2);

        // Step 2: Store generic secrets — capture returned metadata
        let mut secret_keys: Vec<(String, String)> = Vec::new();
        for (ns, k, v) in [
            ("provider:openai", "api_key", "sk-123"),
            ("auth:jwt", "webui_secret", "jwt-val"),
        ] {
            let payload = encode(&secrets_proto::PutSecretRequest {
                namespace: ns.into(),
                key: k.into(),
                value: v.into(),
                description: None,
            });
            let resp = dispatch_vault(&plugin, "secret_put", &payload).unwrap();
            let meta = secrets_proto::PutSecretResponse::decode(resp.as_slice())
                .unwrap()
                .metadata
                .unwrap();
            secret_keys.push((meta.namespace, meta.key));
        }
        assert_eq!(secret_keys.len(), 2);

        // Step 3: Password list sees only passwords (no generic secrets leaking)
        let list_pw = encode(&ListEntriesRequest {
            query: String::new(),
            limit: 0,
            audit: None,
        });
        let resp = dispatch_vault(&plugin, "list_entries", &list_pw).unwrap();
        let list_resp = ListEntriesResponse::decode(resp.as_slice()).unwrap();
        assert_eq!(list_resp.total_in_vault, 2);
        let listed_titles: Vec<&str> = list_resp.entries.iter().map(|e| e.title.as_str()).collect();
        for t in &pw_titles {
            assert!(listed_titles.contains(&t.as_str()), "password title {t} visible");
        }

        // Step 4: Generic list (all namespaces): 4 total
        let list_all = encode(&secrets_proto::ListSecretsRequest {
            namespace: None,
            include_deleted: false,
        });
        let resp = dispatch_vault(&plugin, "secret_list", &list_all).unwrap();
        let list_resp = secrets_proto::ListSecretsResponse::decode(resp.as_slice()).unwrap();
        assert_eq!(list_resp.count, 4, "2 passwords + 2 generic = 4 total");

        // Step 5: Delete a generic secret using key from step 2
        let (del_ns, del_key) = &secret_keys[0]; // provider:openai / api_key
        let del = encode(&secrets_proto::DeleteSecretRequest {
            namespace: del_ns.clone(),
            key: del_key.clone(),
        });
        dispatch_vault(&plugin, "secret_delete", &del).unwrap();

        // Step 6: Password list still has 2 (cross-type isolation)
        let resp = dispatch_vault(&plugin, "list_entries", &list_pw).unwrap();
        let list_resp = ListEntriesResponse::decode(resp.as_slice()).unwrap();
        assert_eq!(list_resp.total_in_vault, 2, "password entries unaffected by generic delete");

        // Step 7: Delete a password using title from step 1
        let del_pw = encode(&DeleteEntryRequest {
            title: pw_titles[0].clone(),
            audit: None,
        });
        dispatch_vault(&plugin, "delete_entry", &del_pw).unwrap();

        // Step 8: Surviving generic secret value intact
        let (surv_ns, surv_key) = &secret_keys[1]; // auth:jwt / webui_secret
        let get = encode(&secrets_proto::GetSecretRequest {
            namespace: surv_ns.clone(),
            key: surv_key.clone(),
            version: None,
        });
        let resp = dispatch_vault(&plugin, "secret_get", &get).unwrap();
        let get_resp = secrets_proto::GetSecretResponse::decode(resp.as_slice()).unwrap();
        assert_eq!(get_resp.value, "jwt-val", "generic secret value preserved after password delete");
    }

    // ── Scenario 13: Kernel readdir verifies unified VFS layout ────
    //
    // User problem: "After storage merge, verify the kernel VFS shows
    //   the correct unified directory structure."
    // Data flow: put returns title/key → readdir uses those to find
    //   exact paths → version paths derived from put metadata.
    //
    // Uses fresh_plugin_with_kernel() for direct kernel access.

    fn fresh_plugin_with_kernel() -> (TempDir, VaultPlugin, Arc<kernel::kernel::Kernel>) {
        let dir = TempDir::new().unwrap();
        let vault_dir = dir.path().join("vault");
        std::fs::create_dir_all(&vault_dir).unwrap();

        let kernel = Arc::new(kernel::kernel::Kernel::new());
        let meta_path = vault_dir.join("vault-meta.redb");
        if let Some(p) = meta_path.to_str() {
            kernel.set_metastore_path(p).unwrap();
        }
        let content_dir = vault_dir.join("content");
        let backend: Arc<dyn kernel::abc::object_store::ObjectStore> = Arc::new(
            backends::storage::path_local::PathLocalBackend::new(&content_dir, true).unwrap(),
        );
        let backend_name = backend.name().to_string();
        kernel
            .sys_setattr(
                "/vault", 2, &backend_name, Some(backend),
                None, None, "memory", "root", false, 0,
                None, None, None, None, None, None, None, None, None, None, None,
            )
            .unwrap();
        let master_key =
            services::password_vault::crypto::load_or_create_master_key(
                &vault_dir.join("master.key"),
            )
            .unwrap();
        let secrets_svc = GenericSecretsServiceImpl::new_on_existing_mount(
            kernel.clone(), "/vault", master_key,
        )
        .unwrap();
        let svc = PasswordVaultServiceImpl::new_with_secrets(secrets_svc.clone());
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();

        (dir, VaultPlugin { svc, secrets_svc, rt }, kernel)
    }

    #[test]
    fn unified_vfs_layout_verified_via_kernel_readdir() {
        let (_dir, plugin, kernel) = fresh_plugin_with_kernel();

        // Step 1: Store password — capture returned title
        let payload = encode(&PutEntryRequest {
            entry: Some(entry("github", "pw1", None)),
            audit: None,
        });
        let resp = dispatch_vault(&plugin, "put_entry", &payload).unwrap();
        let pw_title = PutEntryResponse::decode(resp.as_slice()).unwrap().title;

        // Step 2: Store generic secret — capture returned namespace:key
        let payload = encode(&secrets_proto::PutSecretRequest {
            namespace: "provider:openai".into(),
            key: "api_key".into(),
            value: "sk-456".into(),
            description: None,
        });
        let resp = dispatch_vault(&plugin, "secret_put", &payload).unwrap();
        let meta = secrets_proto::PutSecretResponse::decode(resp.as_slice())
            .unwrap()
            .metadata
            .unwrap();
        let secret_ns = meta.namespace;
        let secret_key = meta.key;

        // Step 3: Verify /vault/entries/ has namespace dirs from steps 1-2
        let entries_root = kernel.sys_readdir("/vault/entries", "root", true);
        let ns_names: Vec<&str> = entries_root
            .iter()
            .filter_map(|(p, _)| p.rsplit('/').next())
            .collect();
        assert!(ns_names.contains(&"passwords"), "passwords namespace dir exists");
        assert!(ns_names.contains(&secret_ns.as_str()), "secret namespace dir exists");

        // Step 4: Verify password entry path uses title from step 1
        let pw_entries = kernel.sys_readdir("/vault/entries/passwords", "root", true);
        assert_eq!(pw_entries.len(), 1);
        assert!(
            pw_entries[0].0.ends_with(&format!("/{pw_title}")),
            "password entry filename matches put title"
        );

        // Step 5: Verify generic secret entry path uses ns:key from step 2
        let secret_entries = kernel.sys_readdir(
            &format!("/vault/entries/{secret_ns}"),
            "root",
            true,
        );
        assert_eq!(secret_entries.len(), 1);
        assert!(
            secret_entries[0].0.ends_with(&format!("/{secret_key}")),
            "secret entry filename matches put key"
        );

        // Step 6: Verify version files exist under unified versions/ tree
        let pw_vers = kernel.sys_readdir(
            &format!("/vault/versions/passwords/{pw_title}"),
            "root",
            true,
        );
        assert_eq!(pw_vers.len(), 1, "1 version for password");

        let secret_vers = kernel.sys_readdir(
            &format!("/vault/versions/{secret_ns}/{secret_key}"),
            "root",
            true,
        );
        assert_eq!(secret_vers.len(), 1, "1 version for generic secret");
    }

    // ── Scenario 14: Bidirectional mutation via both APIs ───────────
    //
    // Write a password via PasswordVaultService, update it via
    // GenericSecretsService (raw JSON), read back via PasswordVaultService.

    #[test]
    fn bidirectional_mutation_password_then_generic_then_password() {
        let (_dir, plugin) = fresh_plugin();

        // Step 1: Create password entry via PasswordVaultService
        let payload = encode(&PutEntryRequest {
            entry: Some(entry("aws", "original-pw", None)),
            audit: None,
        });
        let resp = dispatch_vault(&plugin, "put_entry", &payload).unwrap();
        let put_resp = PutEntryResponse::decode(resp.as_slice()).unwrap();
        assert_eq!(put_resp.version, 1);

        // Step 2: Read the raw JSON via GenericSecretsService
        let get_raw = encode(&secrets_proto::GetSecretRequest {
            namespace: "passwords".into(),
            key: "aws".into(),
            version: None,
        });
        let resp = dispatch_vault(&plugin, "secret_get", &get_raw).unwrap();
        let raw = secrets_proto::GetSecretResponse::decode(resp.as_slice()).unwrap();
        assert!(raw.value.contains("\"password\":\"original-pw\""));

        // Step 3: Overwrite via GenericSecretsService with modified JSON
        let modified_json = raw.value.replace("original-pw", "modified-via-generic");
        let put_raw = encode(&secrets_proto::PutSecretRequest {
            namespace: "passwords".into(),
            key: "aws".into(),
            value: modified_json,
            description: None,
        });
        let resp = dispatch_vault(&plugin, "secret_put", &put_raw).unwrap();
        let meta = secrets_proto::PutSecretResponse::decode(resp.as_slice())
            .unwrap()
            .metadata
            .unwrap();
        assert_eq!(meta.current_version, 2);

        // Step 4: Read back via PasswordVaultService — should see the change
        let get_pw = encode(&GetEntryRequest {
            title: "aws".into(),
            version: None,
            audit: None,
        });
        let resp = dispatch_vault(&plugin, "get_entry", &get_pw).unwrap();
        let get_resp = GetEntryResponse::decode(resp.as_slice()).unwrap();
        assert_eq!(
            get_resp.entry.unwrap().password.as_deref(),
            Some("modified-via-generic"),
            "mutation via GenericSecretsService visible through PasswordVaultService"
        );
        assert_eq!(get_resp.version, 2);

        // Step 5: Version history shows both versions
        let lv = encode(&ListVersionsRequest {
            title: "aws".into(),
            audit: None,
        });
        let resp = dispatch_vault(&plugin, "list_versions", &lv).unwrap();
        let lv_resp = ListVersionsResponse::decode(resp.as_slice()).unwrap();
        assert_eq!(lv_resp.count, 2);
    }
}
