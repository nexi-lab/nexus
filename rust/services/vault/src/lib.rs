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
/// for driving the async gRPC trait methods (which are sync under the
/// hood but declared async for tonic compatibility).
struct VaultPlugin {
    svc: PasswordVaultServiceImpl,
    secrets_svc: GenericSecretsServiceImpl,
    rt: tokio::runtime::Runtime,
}

fn create_vault(_kernel_handle: &KernelHandle) -> Box<VaultPlugin> {
    // Vault creates its own private Kernel instance — the host kernel
    // handle is available for coordination but vault storage is
    // self-contained (own metastore, own backend).
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

    let master_key_path = vault_dir.join("master.key");
    let svc = PasswordVaultServiceImpl::new_with_kernel(
        kernel.clone(),
        "/vault",
        &master_key_path,
        backend,
    )
    .expect("open vault");

    // GenericSecretsService shares the same kernel mount + master key.
    // The master key was created by PasswordVaultServiceImpl above;
    // load it (never creates a new one — the file already exists).
    let master_key = services::password_vault::crypto::load_or_create_master_key(&master_key_path)
        .expect("load master key for generic secrets");
    let secrets_svc =
        GenericSecretsServiceImpl::new_on_existing_mount(kernel, "/vault", master_key)
            .expect("open generic secrets");

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

/// Map tonic Status codes to plugin result codes.
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
//
// These test the full plugin dispatch path: protobuf encode → method
// string dispatch → service logic → protobuf decode. Each scenario
// is a real user journey (3+ steps, data flows step-to-step).

#[cfg(test)]
mod dispatch_e2e {
    use super::*;
    use prost::Message;
    use tempfile::TempDir;

    /// Create a VaultPlugin backed by a temp directory (not env-driven).
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

        let master_key_path = vault_dir.join("master.key");
        let svc = PasswordVaultServiceImpl::new_with_kernel(
            kernel.clone(),
            "/vault",
            &master_key_path,
            backend,
        )
        .unwrap();

        let master_key =
            services::password_vault::crypto::load_or_create_master_key(&master_key_path).unwrap();
        let secrets_svc =
            GenericSecretsServiceImpl::new_on_existing_mount(kernel, "/vault", master_key).unwrap();

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

    /// Encode a prost message to bytes (simulates what a gRPC client sends).
    fn encode<M: Message>(msg: &M) -> Vec<u8> {
        let mut buf = Vec::new();
        msg.encode(&mut buf).unwrap();
        buf
    }

    // ── Scenario 1: Full credential lifecycle via dispatch ─────────
    //
    // User problem: "Store a credential, verify it's stored, list all
    //   entries, soft-delete it, confirm it's gone, restore it."
    // Workflow: put_entry → get_entry → list_entries → delete_entry
    //   → get_entry (404) → restore_entry → get_entry (recovered)
    // Data flow: put returns title/version → get uses title →
    //   list confirms presence → delete uses title → restore uses
    //   title → get confirms recovery.

    #[test]
    fn full_credential_lifecycle_via_dispatch() {
        let (_dir, plugin) = fresh_plugin();

        // Step 1: put_entry — store a credential
        let put_payload = encode(&PutEntryRequest {
            entry: Some(entry("github", "hunter2", None)),
            audit: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "put_entry", &put_payload).unwrap();
        let put_resp = PutEntryResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(put_resp.title, "github");
        assert_eq!(put_resp.version, 1);

        // Step 2: get_entry — read it back via dispatch
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

        // Step 3: list_entries — confirm it appears in the list
        let list_payload = encode(&ListEntriesRequest {
            query: String::new(),
            limit: 0,
            audit: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "list_entries", &list_payload).unwrap();
        let list_resp = ListEntriesResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(list_resp.total_in_vault, 1);
        assert_eq!(list_resp.entries[0].title, "github");

        // Step 4: delete_entry — soft-delete
        let del_payload = encode(&DeleteEntryRequest {
            title: put_resp.title.clone(),
            audit: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "delete_entry", &del_payload).unwrap();
        let del_resp = DeleteEntryResponse::decode(resp_bytes.as_slice()).unwrap();
        assert!(del_resp.deleted);

        // Step 5: get_entry after delete — should return NotFound (-1)
        let err = dispatch_vault(&plugin, "get_entry", &get_payload).unwrap_err();
        assert_eq!(err, -1); // PluginResult::NotFound

        // Step 6: restore_entry — bring it back
        let restore_payload = encode(&RestoreEntryRequest {
            title: put_resp.title.clone(),
            audit: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "restore_entry", &restore_payload).unwrap();
        let restore_resp = RestoreEntryResponse::decode(resp_bytes.as_slice()).unwrap();
        assert!(restore_resp.restored);

        // Step 7: get_entry after restore — credential recovered
        let resp_bytes = dispatch_vault(&plugin, "get_entry", &get_payload).unwrap();
        let get_resp = GetEntryResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(get_resp.entry.unwrap().password.as_deref(), Some("hunter2"));
    }

    // ── Scenario 2: Password rotation with version history ─────────
    //
    // User problem: "Rotate a credential's password and verify old
    //   versions are preserved for compliance audit."
    // Workflow: put_entry(v1) → put_entry(v2) → put_entry(v3) →
    //   list_versions → get_entry(version=1) → get_entry(latest)
    // Data flow: put returns version → list_versions confirms 3 →
    //   get with explicit version retrieves old password.

    #[test]
    fn password_rotation_with_version_history() {
        let (_dir, plugin) = fresh_plugin();

        // Steps 1-3: store 3 versions
        for (i, pw) in ["initial-pw", "rotated-pw", "final-pw"].iter().enumerate() {
            let payload = encode(&PutEntryRequest {
                entry: Some(entry("aws-prod", pw, None)),
                audit: None,
            });
            let resp_bytes = dispatch_vault(&plugin, "put_entry", &payload).unwrap();
            let resp = PutEntryResponse::decode(resp_bytes.as_slice()).unwrap();
            assert_eq!(resp.version, (i + 1) as i32);
        }

        // Step 4: list_versions — auditor verifies all 3 exist
        let lv_payload = encode(&ListVersionsRequest {
            title: "aws-prod".into(),
            audit: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "list_versions", &lv_payload).unwrap();
        let lv_resp = ListVersionsResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(lv_resp.count, 3);
        let vers: Vec<i32> = lv_resp.versions.iter().map(|v| v.version).collect();
        assert_eq!(vers, vec![1, 2, 3]);

        // Step 5: get_entry(version=1) — auditor retrieves OLD password
        let get_v1 = encode(&GetEntryRequest {
            title: "aws-prod".into(),
            version: Some(1),
            audit: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "get_entry", &get_v1).unwrap();
        let resp = GetEntryResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(
            resp.entry.unwrap().password.as_deref(),
            Some("initial-pw"),
            "historical version must return original password"
        );

        // Step 6: get_entry(latest) — current password is v3
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
    //
    // User problem: "Set up 2FA for an account, later rotate the
    //   password, and verify TOTP still works after the update."
    // Workflow: put_entry(with totp) → generate_totp → put_entry(v2,
    //   keep totp) → generate_totp → get_entry (pw changed, totp
    //   redacted)
    // Data flow: put returns title → totp uses title → put v2 uses
    //   title → totp returns same code within window.

    #[test]
    fn totp_survives_password_rotation() {
        let (_dir, plugin) = fresh_plugin();

        // Step 1: store credential WITH TOTP seed
        let payload = encode(&PutEntryRequest {
            entry: Some(entry("aws", "original-pw", Some("JBSWY3DPEHPK3PXP"))),
            audit: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "put_entry", &payload).unwrap();
        let put_resp = PutEntryResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(put_resp.version, 1);

        // Step 2: generate_totp — verify crypto pipeline works
        let totp_payload = encode(&GenerateTotpRequest {
            title: "aws".into(),
            audit: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "generate_totp", &totp_payload).unwrap();
        let totp1 = GenerateTotpResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(totp1.code.len(), 6);
        assert!(totp1.code.chars().all(|c| c.is_ascii_digit()));
        assert_eq!(totp1.period_seconds, 30);

        // Step 3: rotate password, keep TOTP secret
        let payload = encode(&PutEntryRequest {
            entry: Some(entry("aws", "rotated-pw", Some("JBSWY3DPEHPK3PXP"))),
            audit: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "put_entry", &payload).unwrap();
        let put_resp = PutEntryResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(put_resp.version, 2);

        // Step 4: generate_totp after rotation — same code within 30s window
        let resp_bytes = dispatch_vault(&plugin, "generate_totp", &totp_payload).unwrap();
        let totp2 = GenerateTotpResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(
            totp1.code, totp2.code,
            "same seed + same window = same code"
        );

        // Step 5: get_entry — password changed, totp_secret redacted
        let get_payload = encode(&GetEntryRequest {
            title: "aws".into(),
            version: None,
            audit: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "get_entry", &get_payload).unwrap();
        let get_resp = GetEntryResponse::decode(resp_bytes.as_slice()).unwrap();
        let e = get_resp.entry.unwrap();
        assert_eq!(e.password.as_deref(), Some("rotated-pw"));
        assert!(e.totp_secret.is_none(), "totp_secret must be redacted");
    }

    // ── Scenario 4: Multi-credential search and cleanup ────────────
    //
    // User problem: "Manage multiple credentials — add several,
    //   search for a specific one, delete it, verify it's gone."
    // Workflow: put_entry ×3 → list_entries(query) → delete_entry →
    //   list_entries (verify count)
    // Data flow: put creates titles → list filters by query →
    //   delete uses matched title → list confirms removal.

    #[test]
    fn multi_credential_search_and_cleanup() {
        let (_dir, plugin) = fresh_plugin();

        // Steps 1-3: seed 3 credentials
        for (title, pw) in [("gmail", "pw1"), ("github", "pw2"), ("aws-prod", "pw3")] {
            let payload = encode(&PutEntryRequest {
                entry: Some(entry(title, pw, None)),
                audit: None,
            });
            dispatch_vault(&plugin, "put_entry", &payload).unwrap();
        }

        // Step 4: list_entries with query "git" — should match "github"
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

        // Step 5: delete the matched entry
        let del_payload = encode(&DeleteEntryRequest {
            title: "github".into(),
            audit: None,
        });
        dispatch_vault(&plugin, "delete_entry", &del_payload).unwrap();

        // Step 6: list_entries (no filter) — total now 2
        let list_all = encode(&ListEntriesRequest {
            query: String::new(),
            limit: 0,
            audit: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "list_entries", &list_all).unwrap();
        let list_resp = ListEntriesResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(list_resp.total_in_vault, 2);
        let titles: Vec<&str> = list_resp.entries.iter().map(|e| e.title.as_str()).collect();
        assert!(!titles.contains(&"github"), "deleted entry must not appear");
    }

    // ── Scenario 5: Unknown method returns NotFound ────────────────

    #[test]
    fn unknown_method_returns_not_found() {
        let (_dir, plugin) = fresh_plugin();
        let err = dispatch_vault(&plugin, "nonexistent_method", &[]).unwrap_err();
        assert_eq!(err, -1); // PluginResult::NotFound
    }

    // ── Scenario 6: Invalid protobuf returns InvalidArgument ───────

    #[test]
    fn invalid_protobuf_returns_invalid_argument() {
        let (_dir, plugin) = fresh_plugin();
        let err = dispatch_vault(&plugin, "get_entry", b"not-valid-protobuf").unwrap_err();
        assert_eq!(err, -2); // PluginResult::InvalidArgument
    }

    // ── Generic secrets dispatch tests ────────────────────────────

    // ── Scenario 7: Generic secret full lifecycle via dispatch ─────
    //
    // User problem: "Store an API key, read it back, list it,
    //   soft-delete it, verify it's gone, restore it."
    // Workflow: secret_put → secret_get → secret_list →
    //   secret_delete → secret_get (404) → secret_restore →
    //   secret_get (recovered)

    #[test]
    fn generic_secret_full_lifecycle_via_dispatch() {
        let (_dir, plugin) = fresh_plugin();

        // Step 1: put
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

        // Step 2: get
        let get_payload = encode(&secrets_proto::GetSecretRequest {
            namespace: "provider:openai".into(),
            key: "api_key".into(),
            version: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "secret_get", &get_payload).unwrap();
        let get_resp = secrets_proto::GetSecretResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(get_resp.value, "sk-test-123");
        assert_eq!(get_resp.version, 1);

        // Step 3: list
        let list_payload = encode(&secrets_proto::ListSecretsRequest {
            namespace: Some("provider:openai".into()),
            include_deleted: false,
        });
        let resp_bytes = dispatch_vault(&plugin, "secret_list", &list_payload).unwrap();
        let list_resp = secrets_proto::ListSecretsResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(list_resp.count, 1);
        assert_eq!(list_resp.secrets[0].key, "api_key");

        // Step 4: delete
        let del_payload = encode(&secrets_proto::DeleteSecretRequest {
            namespace: "provider:openai".into(),
            key: "api_key".into(),
        });
        let resp_bytes = dispatch_vault(&plugin, "secret_delete", &del_payload).unwrap();
        let del_resp = secrets_proto::DeleteSecretResponse::decode(resp_bytes.as_slice()).unwrap();
        assert!(del_resp.deleted);

        // Step 5: get after delete → NotFound
        let err = dispatch_vault(&plugin, "secret_get", &get_payload).unwrap_err();
        assert_eq!(err, -1);

        // Step 6: restore
        let restore_payload = encode(&secrets_proto::RestoreSecretRequest {
            namespace: "provider:openai".into(),
            key: "api_key".into(),
        });
        let resp_bytes = dispatch_vault(&plugin, "secret_restore", &restore_payload).unwrap();
        let restore_resp =
            secrets_proto::RestoreSecretResponse::decode(resp_bytes.as_slice()).unwrap();
        assert!(restore_resp.restored);

        // Step 7: get after restore → recovered
        let resp_bytes = dispatch_vault(&plugin, "secret_get", &get_payload).unwrap();
        let get_resp = secrets_proto::GetSecretResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(get_resp.value, "sk-test-123");
    }

    // ── Scenario 8: Namespace isolation ──────────────────────────
    //
    // Secrets in different namespaces don't interfere.

    #[test]
    fn generic_secret_namespace_isolation() {
        let (_dir, plugin) = fresh_plugin();

        // Put secrets in different namespaces
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

        // List only channel:telegram:1
        let list_payload = encode(&secrets_proto::ListSecretsRequest {
            namespace: Some("channel:telegram:1".into()),
            include_deleted: false,
        });
        let resp_bytes = dispatch_vault(&plugin, "secret_list", &list_payload).unwrap();
        let list_resp = secrets_proto::ListSecretsResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(list_resp.count, 1);
        assert_eq!(list_resp.secrets[0].namespace, "channel:telegram:1");

        // List all
        let list_all = encode(&secrets_proto::ListSecretsRequest {
            namespace: None,
            include_deleted: false,
        });
        let resp_bytes = dispatch_vault(&plugin, "secret_list", &list_all).unwrap();
        let list_resp = secrets_proto::ListSecretsResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(list_resp.count, 3);
    }

    // ── Scenario 9: Batch operations via dispatch ─────────────────

    #[test]
    fn generic_secret_batch_operations_via_dispatch() {
        let (_dir, plugin) = fresh_plugin();

        // Batch put
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

        // Batch get (including a nonexistent key)
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

    // ── Scenario 10: Version history via dispatch ──────────────────

    #[test]
    fn generic_secret_version_history_via_dispatch() {
        let (_dir, plugin) = fresh_plugin();

        // Store 3 versions
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

        // List versions
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

        // Get historical version
        let get_v1 = encode(&secrets_proto::GetSecretRequest {
            namespace: "provider:openai".into(),
            key: "api_key".into(),
            version: Some(1),
        });
        let resp_bytes = dispatch_vault(&plugin, "secret_get", &get_v1).unwrap();
        let resp = secrets_proto::GetSecretResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(resp.value, "v1");

        // Get latest
        let get_latest = encode(&secrets_proto::GetSecretRequest {
            namespace: "provider:openai".into(),
            key: "api_key".into(),
            version: None,
        });
        let resp_bytes = dispatch_vault(&plugin, "secret_get", &get_latest).unwrap();
        let resp = secrets_proto::GetSecretResponse::decode(resp_bytes.as_slice()).unwrap();
        assert_eq!(resp.value, "v3");
    }
}
