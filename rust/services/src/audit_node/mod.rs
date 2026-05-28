//! AuditNode — consumer-side audit collect/gather service.
//!
//! Rust port of `src/nexus/services/audit_node/service.py`. Runtime
//! placement: an opt-in service started by an operator on a node joining
//! the federation as an audit-only role. Production nodes do NOT run this
//! — they generate traces via the `AuditHook` installed by
//! [`crate::audit::install`]. The audit-node is a *consumer*.
//!
//! Two responsibilities:
//!
//! 1. **Bootstrap** (concrete-`Kernel` only — see `bootstrap.rs`): create
//!    the audit-node's own zone, join every production zone as a raft
//!    learner, and register the `/audit/traces/` DT_STREAM locally on each
//!    joined zone (no `AuditHook` — consumer, not producer).
//!
//! 2. **Collect/gather loop** (this module, generic over `K: KernelAbi`):
//!    poll every joined zone's `/audit/traces/` stream from the persisted
//!    offset, append new entries to the audit-node's local zone, and
//!    persist the new offset. Local layout:
//!
//!    ```text
//!    /{audit_zone}/collect/{source_zone}/traces/   ← appended copies
//!    /{audit_zone}/collect/{source_zone}/offset    ← last-read position
//!    ```
//!
//! The collect loop is synchronous (the `KernelAbi` syscalls are blocking)
//! and runs on a dedicated OS thread; [`AuditNode::stop`] wakes it
//! promptly via a condvar. Integration correctness (cross-zone routing,
//! DT_STREAM offset chaining) is exercised by the docker federation E2E
//! `tests/e2e/docker/test_federation_audit.py`.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use contracts::OperationContext;
use parking_lot::{Condvar, Mutex};

use kernel::abi::KernelAbi;

mod bootstrap;

/// Default audit-trace DT_STREAM path on each production zone.
pub const DEFAULT_STREAM_PATH: &str = "/audit/traces/";
/// Default max records drained per zone per poll iteration.
pub const DEFAULT_BATCH_SIZE: usize = 256;
/// Default sleep between poll iterations.
pub const DEFAULT_POLL_INTERVAL: Duration = Duration::from_secs(1);

/// DT_REG entry-type discriminant (mirrors `kernel::core::dcache::DT_REG`).
const DT_REG: i32 = 1;

/// Per-source-zone offset tracker. The collect loop persists the
/// next-to-read offset after every successful batch flush so a restart
/// resumes where the previous run left off.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AuditCheckpoint {
    pub source_zone: String,
    pub offset: u64,
}

/// Audit-node service: collect/gather loop over joined production zones.
pub struct AuditNode<K: KernelAbi> {
    kernel: Arc<K>,
    audit_zone_id: String,
    stream_path: String,
    batch_size: usize,
    poll_interval: Duration,
    /// `source_zone -> checkpoint`. Seeded by `bootstrap` (or
    /// [`AuditNode::register_zone`] in tests).
    checkpoints: Mutex<HashMap<String, AuditCheckpoint>>,
    /// Collect-loop stop flag + wakeup condvar.
    stopped: Mutex<bool>,
    wakeup: Condvar,
}

impl<K: KernelAbi> AuditNode<K> {
    /// Construct with default stream path / batch size / poll interval.
    pub fn new(kernel: Arc<K>, audit_zone_id: impl Into<String>) -> Self {
        Self::with_config(
            kernel,
            audit_zone_id,
            DEFAULT_STREAM_PATH,
            DEFAULT_BATCH_SIZE,
            DEFAULT_POLL_INTERVAL,
        )
    }

    /// Construct with explicit configuration.
    pub fn with_config(
        kernel: Arc<K>,
        audit_zone_id: impl Into<String>,
        stream_path: impl Into<String>,
        batch_size: usize,
        poll_interval: Duration,
    ) -> Self {
        Self {
            kernel,
            audit_zone_id: audit_zone_id.into(),
            stream_path: stream_path.into(),
            batch_size,
            poll_interval,
            checkpoints: Mutex::new(HashMap::new()),
            stopped: Mutex::new(false),
            wakeup: Condvar::new(),
        }
    }

    /// Register a source zone with an explicit starting offset. Usable
    /// directly in tests.
    pub fn register_zone(&self, source_zone: impl Into<String>, offset: u64) {
        let zone = source_zone.into();
        self.checkpoints.lock().insert(
            zone.clone(),
            AuditCheckpoint {
                source_zone: zone,
                offset,
            },
        );
    }

    /// Register a source zone, resuming from its persisted offset (0 if
    /// none). Called by `bootstrap` for each joined zone so a restart
    /// continues where the previous run left off.
    pub fn resume_zone(&self, source_zone: impl Into<String>) {
        let zone = source_zone.into();
        let offset = self.read_offset(&zone);
        self.register_zone(zone, offset);
    }

    /// Local path the copies for `source_zone` are appended to.
    fn collect_traces_path(&self, source_zone: &str) -> String {
        format!("/{}/collect/{}/traces", self.audit_zone_id, source_zone)
    }

    /// Local path the persisted offset for `source_zone` is stored at.
    fn offset_path(&self, source_zone: &str) -> String {
        format!("/{}/collect/{}/offset", self.audit_zone_id, source_zone)
    }

    /// Source-zone audit stream path, e.g. `/{zone}/audit/traces`.
    fn source_stream_path(&self, source_zone: &str) -> String {
        format!("/{}{}", source_zone, self.stream_path)
            .trim_end_matches('/')
            .to_string()
    }

    /// System context for the audit-node's own syscalls (bypasses the
    /// permission gate; audit collection is infrastructure).
    fn sys_ctx(&self) -> OperationContext {
        let mut ctx = OperationContext::new(
            /* user_id */ "audit-node",
            /* zone_id */ &self.audit_zone_id,
            /* is_admin */ true,
            /* agent_id */ Some("audit-node"),
            /* is_system */ true,
        );
        ctx.subject_type = "service".to_string();
        ctx
    }

    /// Read the persisted offset for `source_zone`, defaulting to 0.
    fn read_offset(&self, source_zone: &str) -> u64 {
        let path = self.offset_path(source_zone);
        let ctx = self.sys_ctx();
        let Ok(result) = self.kernel.sys_read(&path, &ctx, 0, 0) else {
            return 0;
        };
        let Some(data) = result.data else {
            return 0;
        };
        serde_json::from_slice::<OffsetRecord>(&data)
            .map(|r| r.offset)
            .unwrap_or(0)
    }

    /// Persist `offset` for `source_zone` (ensures the DT_REG inode first).
    fn write_offset(&self, source_zone: &str, offset: u64) {
        let path = self.offset_path(source_zone);
        let ctx = self.sys_ctx();
        let payload = match serde_json::to_vec(&OffsetRecord { offset }) {
            Ok(p) => p,
            Err(e) => {
                tracing::warn!(error = %e, zone = source_zone, "audit-node offset encode failed");
                return;
            }
        };
        // sys_write requires the file to exist — ensure the DT_REG entry.
        let _ = self.kernel.sys_setattr(
            &path,
            DT_REG,
            /* backend_name */ "",
            /* backend */ None,
            /* metastore */ None,
            /* raft_backend */ None,
            /* io_profile */ "",
            /* zone_id */ &self.audit_zone_id,
            /* is_external */ false,
            /* capacity */ 0,
            /* read_fd */ None,
            /* write_fd */ None,
            /* mime_type */ None,
            /* modified_at_ms */ None,
            /* content_id */ None,
            /* size */ None,
            /* version */ None,
            /* created_at_ms */ None,
            /* link_target */ None,
            /* source */ None,
            /* remote_metastore */ None,
        );
        if let Err(e) = self.kernel.sys_write(&path, &ctx, &payload, 0) {
            tracing::warn!(error = ?e, zone = source_zone, "audit-node offset persist failed");
        }
    }

    /// Drain up to `batch_size` records from one zone's audit stream into
    /// the local collect stream. Returns the number of records collected.
    /// Advances + persists the checkpoint offset on success.
    fn drain_zone(&self, source_zone: &str, start_offset: u64) -> u64 {
        let source_path = self.source_stream_path(source_zone);
        let ctx = self.sys_ctx();

        // Drain via offset chaining — each record is one AuditRecord JSON
        // blob written by the producing AuditHook. stream_next_offset
        // advances the cursor; data == None means the stream is drained.
        let mut entries: Vec<Vec<u8>> = Vec::new();
        let mut new_offset = start_offset;
        for _ in 0..self.batch_size {
            let result = match self.kernel.sys_read(&source_path, &ctx, 0, new_offset) {
                Ok(r) => r,
                Err(e) => {
                    tracing::warn!(error = ?e, zone = source_zone, offset = new_offset, "audit-node drain read failed");
                    break;
                }
            };
            match result.data {
                None => break,
                Some(data) => {
                    entries.push(data);
                    new_offset = result
                        .stream_next_offset
                        .map(|o| o as u64)
                        .unwrap_or(new_offset);
                }
            }
        }
        if entries.is_empty() {
            return start_offset;
        }

        let target_path = self.collect_traces_path(source_zone);
        let count = entries.len();
        for raw in entries {
            // DT_STREAM append (offset 0 — kernel io.rs short-circuits
            // DT_STREAM appends regardless of offset, same as AuditHook).
            if let Err(e) = self.kernel.sys_write(&target_path, &ctx, &raw, 0) {
                tracing::warn!(error = ?e, zone = source_zone, "audit-node collect write failed");
            }
        }

        self.write_offset(source_zone, new_offset);
        tracing::debug!(zone = source_zone, drained = count, new_offset, "audit-node drained");
        new_offset
    }

    /// Drain a single batch from every checkpointed zone. Returns the
    /// total records collected this iteration (observability / tests).
    pub fn poll_once(&self) -> usize {
        // Snapshot the (zone, offset) pairs so the drain syscalls don't
        // hold the checkpoints lock.
        let pending: Vec<(String, u64)> = {
            let checkpoints = self.checkpoints.lock();
            checkpoints
                .values()
                .map(|c| (c.source_zone.clone(), c.offset))
                .collect()
        };

        let mut total = 0;
        for (zone, offset) in pending {
            let new_offset = self.drain_zone(&zone, offset);
            let collected = new_offset.saturating_sub(offset);
            if collected > 0 {
                total += collected as usize;
                if let Some(cp) = self.checkpoints.lock().get_mut(&zone) {
                    cp.offset = new_offset;
                }
            }
        }
        total
    }

    /// Run the collect loop until [`AuditNode::stop`] is called. Blocks
    /// the calling thread; operators spawn it on a dedicated OS thread.
    pub fn run(&self) {
        tracing::info!(
            zones = self.checkpoints.lock().len(),
            batch = self.batch_size,
            interval_ms = self.poll_interval.as_millis() as u64,
            "audit-node collect loop starting"
        );
        loop {
            self.poll_once();
            let mut stopped = self.stopped.lock();
            if *stopped {
                break;
            }
            // Sleep poll_interval, or wake early on stop().
            self.wakeup.wait_for(&mut stopped, self.poll_interval);
            if *stopped {
                break;
            }
        }
        tracing::info!("audit-node collect loop stopped");
    }

    /// Signal [`AuditNode::run`] to exit on its next wakeup.
    pub fn stop(&self) {
        *self.stopped.lock() = true;
        self.wakeup.notify_all();
    }

    /// Number of registered source zones (observability / tests).
    pub fn zone_count(&self) -> usize {
        self.checkpoints.lock().len()
    }
}

/// On-disk shape of the persisted offset file.
#[derive(serde::Serialize, serde::Deserialize)]
struct OffsetRecord {
    offset: u64,
}
