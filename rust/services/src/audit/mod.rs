//! AuditHook — native Rust [`NativeInterceptHook`] that records VFS operations
//! to a WAL-backed DT_STREAM audit log.
//!
//! Hot-path cost: AuditRecord struct construction + mpsc::SyncSender::try_send
//! (~100–300 ns). JSON serialization and WalStreamCore::write_nowait happen in
//! a background thread, entirely off the VFS dispatch critical path.
//!
//! Per the architecture's `services` ⊥ `backends` ⊥ `transport` ⊥
//! `raft` peer-crate split, construction + registration is owned by the
//! service tier (this module's [`install`] function); the kernel only
//! exposes [`Kernel::prepare_audit_stream`] and the
//! [`Kernel::register_native_hook`] in-tree API.
//!
//! ## Boot wiring (Linux LSM analogue)
//!
//! ```ignore
//! // From Python (or any cdylib caller):
//! services::audit::install(&kernel, "root", "/audit/traces/")?;
//! // 1. kernel.prepare_audit_stream(...) — kernel concern (stream lifecycle)
//! // 2. AuditHook::new(stream)           — service concern (hook impl)
//! // 3. kernel.register_native_hook(...) — kernel API (LSM-style EXPORT_SYMBOL)
//! ```

use std::sync::mpsc;
use std::sync::Arc;

use chrono::SecondsFormat;
use serde::Serialize;

use kernel::core::dispatch::{HookContext, NativeInterceptHook};
use kernel::kernel::{Kernel, KernelError};

/// A single VFS operation record, serialised to JSON and appended to the
/// audit WAL stream.
#[derive(Debug, Serialize)]
pub struct AuditRecord {
    /// Schema version — increment when fields are added/removed.
    pub v: u8,
    /// ISO-8601 timestamp with millisecond precision.
    pub ts: String,
    pub agent_id: String,
    pub user_id: String,
    pub zone_id: String,
    /// VFS operation name: "write", "read", "delete", "rename", …
    pub op: &'static str,
    pub path: String,
    /// "ok" (only successful operations are audited; pre-hook aborts are not).
    pub status: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub size_bytes: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub is_new: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub new_path: Option<String>,
}

/// VFS audit hook — implements `NativeInterceptHook` so it can be registered
/// with `Kernel::register_native_hook` and receive post-dispatch callbacks
/// without crossing the PyO3 boundary.
pub struct AuditHook {
    sender: mpsc::SyncSender<AuditRecord>,
}

impl AuditHook {
    /// Background flush channel capacity. At ~300 B per JSON record this is
    /// ~2.5 MB worst-case before try_send drops records (best-effort audit).
    const CHANNEL_CAP: usize = 8192;

    /// Create an AuditHook backed by `stream`. Spawns a background flush thread
    /// that serialises records to JSON and calls `stream.push`.
    pub fn new(stream: Arc<dyn kernel::stream::StreamBackend>) -> Self {
        let (tx, rx) = mpsc::sync_channel::<AuditRecord>(Self::CHANNEL_CAP);

        std::thread::Builder::new()
            .name("audit-flush".into())
            .spawn(move || {
                while let Ok(record) = rx.recv() {
                    match serde_json::to_vec(&record) {
                        Ok(json) => {
                            if let Err(e) = stream.push(&json) {
                                tracing::warn!(error = ?e, "audit stream write failed");
                            }
                        }
                        Err(e) => {
                            tracing::warn!(error = %e, "audit record serialisation failed");
                        }
                    }
                }
            })
            .expect("failed to spawn audit flush thread");

        Self { sender: tx }
    }

    fn build_record(ctx: &HookContext, op: &'static str) -> AuditRecord {
        let path = ctx.path().to_string();
        let id = ctx.identity();
        let (size_bytes, is_new, new_path) = match ctx {
            HookContext::Write(c) => (c.size_bytes, Some(c.is_new_file), None),
            HookContext::Read(c) => (c.content.as_ref().map(|b| b.len() as u64), None, None),
            HookContext::Rename(c) => (None, None, Some(c.new_path.clone())),
            _ => (None, None, None),
        };
        AuditRecord {
            v: 1,
            ts: chrono::Utc::now().to_rfc3339_opts(SecondsFormat::Millis, true),
            agent_id: id.agent_id.clone(),
            user_id: id.user_id.clone(),
            zone_id: id.zone_id.clone(),
            op,
            path,
            status: "ok",
            size_bytes,
            is_new,
            new_path,
        }
    }
}

impl NativeInterceptHook for AuditHook {
    fn name(&self) -> &str {
        "audit"
    }

    fn on_post(&self, ctx: &HookContext) {
        let op = match ctx {
            HookContext::Write(_) => "write",
            HookContext::Read(_) => "read",
            HookContext::Delete(_) => "delete",
            HookContext::Rename(_) => "rename",
            HookContext::Mkdir(_) => "mkdir",
            HookContext::Rmdir(_) => "rmdir",
            HookContext::Copy(_) => "copy",
            HookContext::Stat(_) => "stat",
            HookContext::Access(_) => "access",
            HookContext::WriteBatch(_) => "write_batch",
        };
        let record = Self::build_record(ctx, op);
        // Non-blocking — drop silently on backpressure (audit is best-effort).
        let _ = self.sender.try_send(record);
    }
}

/// Boot-time DI entry point — install an `AuditHook` for `zone_id`.
///
/// Service-tier responsibility (this whole module) — kernel only owns
/// the stream lifecycle.  Three steps, each crossing a clean tier
/// boundary:
///
/// 1. `kernel.prepare_audit_stream(zone_id, stream_path)` — kernel
///    creates the `WalStreamCore`, registers it with the stream
///    manager, seeds the inode.
/// 2. `AuditHook::new(stream)` — local services concern: build the
///    hook impl from the WAL stream handle.
/// 3. `kernel.register_native_hook(Box::new(hook))` — kernel API
///    (LSM-style); kernel records the hook in its native dispatch
///    registry without ever knowing the concrete type.
///
/// Idempotent: prepare_audit_stream's underlying StreamManager.register
/// is idempotent on duplicate paths, but the `register_native_hook`
/// side is not — calling `install` twice for the same zone would
/// double-register the hook.  Callers (typically `nexus.__init__`
/// boot path) call this exactly once per zone.
pub fn install(kernel: &Kernel, zone_id: &str, stream_path: &str) -> Result<(), KernelError> {
    let stream = kernel.prepare_audit_stream(zone_id, stream_path)?;
    // AuditHook needs the trait surface — concrete WalStreamCore
    // upcasts via `as Arc<dyn StreamBackend>`.
    let hook = AuditHook::new(stream as Arc<dyn kernel::stream::StreamBackend>);
    kernel.register_native_hook(Box::new(hook));
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use kernel::abc::meta_store::{FileMetadata, MetaStore, MetaStoreError};
    use kernel::core::dispatch::{HookIdentity, WriteHookCtx};
    use kernel::core::stream::wal::WalStreamCore;
    use std::collections::BTreeMap;
    use std::sync::Mutex;

    /// In-memory MetaStore mock for the audit-stream tests.  Implements
    /// only the stream-entries surface; structured FileMetadata calls
    /// are unused here so they're stubs.
    struct MemKvStore {
        inner: Mutex<BTreeMap<String, Vec<u8>>>,
    }
    impl MemKvStore {
        fn new() -> Arc<Self> {
            Arc::new(Self {
                inner: Mutex::new(BTreeMap::new()),
            })
        }
    }
    impl MetaStore for MemKvStore {
        fn get(&self, _path: &str) -> Result<Option<FileMetadata>, MetaStoreError> {
            Ok(None)
        }
        fn put(&self, _path: &str, _meta: FileMetadata) -> Result<(), MetaStoreError> {
            Ok(())
        }
        fn delete(&self, _path: &str) -> Result<bool, MetaStoreError> {
            Ok(false)
        }
        fn list(&self, _prefix: &str) -> Result<Vec<FileMetadata>, MetaStoreError> {
            Ok(Vec::new())
        }
        fn exists(&self, _path: &str) -> Result<bool, MetaStoreError> {
            Ok(false)
        }
        fn append_stream_entry(&self, key: &str, data: &[u8]) -> Result<(), MetaStoreError> {
            self.inner
                .lock()
                .unwrap()
                .insert(key.to_string(), data.to_vec());
            Ok(())
        }
        fn get_stream_entry(&self, key: &str) -> Result<Option<Vec<u8>>, MetaStoreError> {
            Ok(self.inner.lock().unwrap().get(key).cloned())
        }
    }

    #[test]
    fn on_post_write_sends_record_to_stream() {
        let stream = Arc::new(WalStreamCore::new(MemKvStore::new(), "audit-test".into()));
        let hook = AuditHook::new(Arc::clone(&stream) as Arc<dyn kernel::stream::StreamBackend>);

        let ctx = HookContext::Write(WriteHookCtx {
            path: "/workspace/foo.rs".into(),
            identity: HookIdentity {
                agent_id: "agent:sudo-code".into(),
                user_id: "u1".into(),
                zone_id: "root".into(),
                is_admin: false,
            },
            content: vec![],
            is_new_file: true,
            content_id: None,
            new_version: 1,
            size_bytes: Some(1024),
        });
        hook.on_post(&ctx);

        // Give the flush thread a moment to process.
        std::thread::sleep(std::time::Duration::from_millis(50));

        // The audit stream should have exactly one entry.
        let data = stream.read_at(0).unwrap();
        assert!(data.is_some(), "expected audit record in stream");
        let record: serde_json::Value = serde_json::from_slice(&data.unwrap()).expect("valid JSON");
        assert_eq!(record["op"], "write");
        assert_eq!(record["path"], "/workspace/foo.rs");
        assert_eq!(record["size_bytes"], 1024u64);
        assert_eq!(record["is_new"], true);
        assert_eq!(record["status"], "ok");
    }

    #[test]
    fn on_post_delete_records_delete_op() {
        use kernel::core::dispatch::{DeleteHookCtx, HookIdentity};
        let stream = Arc::new(WalStreamCore::new(MemKvStore::new(), "audit-del".into()));
        let hook = AuditHook::new(Arc::clone(&stream) as Arc<dyn kernel::stream::StreamBackend>);

        let ctx = HookContext::Delete(DeleteHookCtx {
            path: "/workspace/gone.txt".into(),
            identity: HookIdentity::default(),
        });
        hook.on_post(&ctx);

        std::thread::sleep(std::time::Duration::from_millis(50));

        let data = stream.read_at(0).unwrap().expect("record present");
        let record: serde_json::Value = serde_json::from_slice(&data).unwrap();
        assert_eq!(record["op"], "delete");
        assert_eq!(record["path"], "/workspace/gone.txt");
    }
}
