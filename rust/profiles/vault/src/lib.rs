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

/// Plugin state: the vault service plus a single-threaded tokio runtime
/// for driving the async gRPC trait methods (which are sync under the
/// hood but declared async for tonic compatibility).
struct VaultPlugin {
    svc: PasswordVaultServiceImpl,
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
    let svc =
        PasswordVaultServiceImpl::new_with_kernel(kernel, "/vault", &master_key_path, backend)
            .expect("open vault");

    let rt = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("build tokio runtime for vault plugin");

    tracing::info!(
        vault_dir = %vault_dir.display(),
        "vault plugin loaded"
    );

    Box::new(VaultPlugin { svc, rt })
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
        let svc =
            PasswordVaultServiceImpl::new_with_kernel(kernel, "/vault", &master_key_path, backend)
                .unwrap();

        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();

        (dir, VaultPlugin { svc, rt })
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
}
