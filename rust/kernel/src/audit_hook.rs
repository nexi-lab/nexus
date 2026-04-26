//! AuditHook — native Rust NativeInterceptHook that records VFS operations
//! to a WAL-backed DT_STREAM audit log.
//!
//! Hot-path cost: AuditRecord struct construction + mpsc::SyncSender::try_send
//! (~100–300 ns). JSON serialization and WalStreamCore::write_nowait happen in
//! a background thread, entirely off the VFS dispatch critical path.
//!
//! Registration:
//!   let hook = AuditHook::new(Arc::clone(&wal_stream));
//!   kernel.register_native_hook(Box::new(hook));

use std::sync::mpsc;
use std::sync::Arc;

use chrono::SecondsFormat;
use serde::Serialize;

use crate::dispatch::{HookContext, NativeInterceptHook};
use crate::wal_stream::WalStreamCore;

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
    /// that serialises records to JSON and calls `stream.write_nowait`.
    pub fn new(stream: Arc<WalStreamCore>) -> Self {
        let (tx, rx) = mpsc::sync_channel::<AuditRecord>(Self::CHANNEL_CAP);

        std::thread::Builder::new()
            .name("audit-flush".into())
            .spawn(move || {
                while let Ok(record) = rx.recv() {
                    match serde_json::to_vec(&record) {
                        Ok(json) => {
                            if let Err(e) = stream.write_nowait(&json) {
                                tracing::warn!(error = %e, "audit stream write failed");
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::dispatch::{HookIdentity, WriteHookCtx};
    use crate::wal_stream::WalConsensus;
    use crate::wal_stream::WalStreamCore;
    use std::collections::BTreeMap;
    use std::sync::Mutex;

    struct MemConsensus {
        inner: Mutex<BTreeMap<String, Vec<u8>>>,
    }
    impl MemConsensus {
        fn new() -> Arc<Self> {
            Arc::new(Self {
                inner: Mutex::new(BTreeMap::new()),
            })
        }
    }
    impl WalConsensus for MemConsensus {
        fn append(&self, key: &str, data: &[u8]) -> Result<(), String> {
            self.inner
                .lock()
                .unwrap()
                .insert(key.to_string(), data.to_vec());
            Ok(())
        }
        fn get(&self, key: &str) -> Result<Option<Vec<u8>>, String> {
            Ok(self.inner.lock().unwrap().get(key).cloned())
        }
    }

    #[test]
    fn on_post_write_sends_record_to_stream() {
        let stream = Arc::new(WalStreamCore::new(MemConsensus::new(), "audit-test".into()));
        let hook = AuditHook::new(Arc::clone(&stream));

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
            content_hash: None,
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
        use crate::dispatch::{DeleteHookCtx, HookIdentity};
        let stream = Arc::new(WalStreamCore::new(MemConsensus::new(), "audit-del".into()));
        let hook = AuditHook::new(Arc::clone(&stream));

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
