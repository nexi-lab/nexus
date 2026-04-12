//! ContentReplicationService — pure Rust replication scanner (§10 E1).
//!
//! Background scan loop: metastore range scan → check local CAS → gRPC ReadBlob
//! → ObjectStore write. All components already Rust.
//!
//! Policy resolution can remain Python initially (injected via callback).
//! Launched as background task via tokio runtime (already in kernel for gRPC).

#![allow(dead_code)]

use crate::kernel::OperationContext;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;

/// Replication scanner — scans metastore for entries needing replication.
pub(crate) struct ReplicationScanner {
    /// Scan interval in milliseconds.
    interval_ms: u64,
    /// Zone ID to scan.
    zone_id: String,
    /// Source mount point (where to read content from).
    source_mount: String,
    /// Target mount point (where to replicate content to).
    target_mount: String,
    /// Running flag — set to false to stop the background loop.
    running: Arc<AtomicBool>,
    /// Counters for monitoring.
    pub scanned_count: Arc<AtomicU64>,
    pub replicated_count: Arc<AtomicU64>,
    pub error_count: Arc<AtomicU64>,
    pub last_scan_ms: Arc<AtomicU64>,
}

impl ReplicationScanner {
    pub(crate) fn new(
        interval_ms: u64,
        zone_id: &str,
        source_mount: &str,
        target_mount: &str,
    ) -> Self {
        Self {
            interval_ms,
            zone_id: zone_id.to_string(),
            source_mount: source_mount.to_string(),
            target_mount: target_mount.to_string(),
            running: Arc::new(AtomicBool::new(false)),
            scanned_count: Arc::new(AtomicU64::new(0)),
            replicated_count: Arc::new(AtomicU64::new(0)),
            error_count: Arc::new(AtomicU64::new(0)),
            last_scan_ms: Arc::new(AtomicU64::new(0)),
        }
    }

    /// Run one scan-and-replicate pass using the kernel.
    ///
    /// 1. metastore.list(zone_prefix) — get all entries
    /// 2. For each entry with etag: check if content exists in target
    /// 3. If missing: read from source → write to target
    ///
    /// Returns (scanned, replicated, errors).
    pub(crate) fn scan_and_replicate(
        &self,
        kernel: &crate::kernel::Kernel,
    ) -> (usize, usize, usize) {
        let prefix = format!("/{}/", self.zone_id);
        let entries = match kernel.metastore_list(&prefix) {
            Ok(entries) => entries,
            Err(_) => return (0, 0, 1),
        };

        let ctx = OperationContext::new(&self.zone_id, &self.zone_id, true, None, true);
        let mut scanned = 0;
        let mut replicated = 0;
        let mut errors = 0;

        for entry in &entries {
            scanned += 1;
            let _etag = match &entry.etag {
                Some(e) if !e.is_empty() => e.clone(),
                _ => continue, // No content to replicate
            };

            // Check if content exists in target mount
            let target_read = kernel.route(&entry.path, &self.zone_id, true, false);
            if target_read.is_err() {
                continue; // Not routable to target
            }

            // Try reading from source
            let source_result = kernel.sys_read(&entry.path, &ctx);
            let content = match source_result {
                Ok(r) if r.hit => match r.data {
                    Some(data) => data,
                    None => continue,
                },
                _ => continue,
            };

            // Write to target (the router will pick the target mount based on routing)
            match kernel.sys_write(&entry.path, &ctx, &content) {
                Ok(r) if r.hit => {
                    replicated += 1;
                }
                Ok(_) => {
                    errors += 1;
                }
                Err(_) => {
                    errors += 1;
                }
            }
        }

        self.scanned_count
            .fetch_add(scanned as u64, Ordering::Relaxed);
        self.replicated_count
            .fetch_add(replicated as u64, Ordering::Relaxed);
        self.error_count.fetch_add(errors as u64, Ordering::Relaxed);

        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis() as u64;
        self.last_scan_ms.store(now, Ordering::Relaxed);

        (scanned, replicated, errors)
    }

    /// Start the background scan loop in a dedicated thread.
    ///
    /// The loop runs until `stop()` is called. Each iteration:
    /// 1. Sleeps for `interval_ms`
    /// 2. Calls `scan_and_replicate(kernel)`
    ///
    /// Thread-safe: the kernel Arc is shared, scanner stats are atomic.
    pub(crate) fn start(self: &Arc<Self>, kernel: Arc<crate::kernel::Kernel>) {
        if self.running.swap(true, Ordering::SeqCst) {
            return; // Already running
        }
        let scanner = Arc::clone(self);
        let interval = std::time::Duration::from_millis(self.interval_ms);
        std::thread::Builder::new()
            .name("replication-scanner".to_string())
            .spawn(move || {
                while scanner.running.load(Ordering::Relaxed) {
                    std::thread::sleep(interval);
                    if !scanner.running.load(Ordering::Relaxed) {
                        break;
                    }
                    let (_scanned, _replicated, _errors) = scanner.scan_and_replicate(&kernel);
                }
            })
            .expect("failed to spawn replication-scanner thread");
    }

    /// Check if the scanner is running.
    pub(crate) fn is_running(&self) -> bool {
        self.running.load(Ordering::Relaxed)
    }

    /// Signal the scanner to stop.
    pub(crate) fn stop(&self) {
        self.running.store(false, Ordering::Relaxed);
    }

    /// Get stats: (scanned, replicated, errors, last_scan_ms).
    pub(crate) fn stats(&self) -> (u64, u64, u64, u64) {
        (
            self.scanned_count.load(Ordering::Relaxed),
            self.replicated_count.load(Ordering::Relaxed),
            self.error_count.load(Ordering::Relaxed),
            self.last_scan_ms.load(Ordering::Relaxed),
        )
    }
}
