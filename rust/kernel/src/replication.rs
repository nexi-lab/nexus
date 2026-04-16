//! ContentReplicationService — pure Rust replication scanner (§10 E1).
//!
//! Background scan loop: metastore range scan → check local CAS → gRPC ReadBlob
//! → ObjectStore write. All components already Rust.
//!
//! Policy resolution can remain Python initially (injected via callback).
//! Launched as background task via tokio runtime (already in kernel for gRPC).

#![allow(dead_code)]

use crate::kernel::OperationContext;
use parking_lot::Mutex;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::thread::JoinHandle;

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
    /// Handle to the background worker, so `stop()` can join and callers
    /// can verify the thread has actually exited (§ review fix #27).
    worker: Mutex<Option<JoinHandle<()>>>,
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
            worker: Mutex::new(None),
            scanned_count: Arc::new(AtomicU64::new(0)),
            replicated_count: Arc::new(AtomicU64::new(0)),
            error_count: Arc::new(AtomicU64::new(0)),
            last_scan_ms: Arc::new(AtomicU64::new(0)),
        }
    }

    /// Normalize mount path into zone-canonical form.
    ///
    /// Examples for `zone_id = "z1"`:
    /// - `"/z1/source"` -> `"/z1/source"` (already canonical)
    /// - `"/source"`    -> `"/z1/source"`
    /// - `"source"`     -> `"/z1/source"`
    fn canonical_mount_for_zone(&self, mount: &str) -> String {
        let normalized = if mount.starts_with('/') {
            mount.to_string()
        } else {
            format!("/{mount}")
        };
        let zone_root = format!("/{}", self.zone_id);
        let zone_prefix = format!("{zone_root}/");
        if normalized == zone_root || normalized.starts_with(&zone_prefix) {
            normalized
        } else if normalized == "/" {
            zone_root
        } else {
            format!("{zone_root}/{}", normalized.trim_start_matches('/'))
        }
    }

    /// Map a source path under `source_mount` to the corresponding target path
    /// under `target_mount`.
    ///
    /// Returns `None` when `source_path` is outside the source mount subtree.
    fn map_source_to_target_path(
        source_path: &str,
        source_mount: &str,
        target_mount: &str,
    ) -> Option<String> {
        if source_path == source_mount {
            return Some(target_mount.to_string());
        }
        let source_prefix = format!("{}/", source_mount.trim_end_matches('/'));
        if !source_path.starts_with(&source_prefix) {
            return None;
        }
        let suffix = &source_path[source_prefix.len()..];
        let target_base = target_mount.trim_end_matches('/');
        Some(format!("{target_base}/{suffix}"))
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
        let source_mount = self.canonical_mount_for_zone(&self.source_mount);
        let target_mount = self.canonical_mount_for_zone(&self.target_mount);
        let mut scanned = 0;
        let mut replicated = 0;
        let mut errors = 0;

        for entry in &entries {
            scanned += 1;
            let _etag = match &entry.etag {
                Some(e) if !e.is_empty() => e.clone(),
                _ => continue, // No content to replicate
            };

            // Only replicate entries under the configured source mount, and
            // map them into the target mount subtree.
            let target_path =
                match Self::map_source_to_target_path(&entry.path, &source_mount, &target_mount) {
                    Some(p) => p,
                    None => continue,
                };

            // Check target path is routable/writable.
            let target_read = kernel.route(&target_path, &self.zone_id, true, true);
            if target_read.is_err() {
                continue; // Not routable to target
            }

            // Try reading from source
            let content = match kernel.sys_read(&entry.path, &ctx) {
                Ok(r) => match r.data {
                    Some(data) => data,
                    None => continue,
                },
                Err(_) => continue,
            };

            // Write to the mapped target path.
            match kernel.sys_write(&target_path, &ctx, &content) {
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
        let handle = std::thread::Builder::new()
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
        *self.worker.lock() = Some(handle);
    }

    /// Check if the scanner is running.
    pub(crate) fn is_running(&self) -> bool {
        self.running.load(Ordering::Relaxed)
    }

    /// Signal the scanner to stop. Returns without waiting; prefer
    /// `stop_and_join` when deterministic shutdown matters.
    pub(crate) fn stop(&self) {
        self.running.store(false, Ordering::Relaxed);
    }

    /// Signal stop and block until the worker has actually exited
    /// (§ review fix #27). Safe to call multiple times; a no-op if the
    /// worker has already been joined.
    #[allow(dead_code)]
    pub(crate) fn stop_and_join(&self) {
        self.running.store(false, Ordering::Relaxed);
        let handle = self.worker.lock().take();
        if let Some(handle) = handle {
            // Swallow panics here — the scanner is background; we should
            // not propagate a worker panic to the caller of shutdown.
            let _ = handle.join();
        }
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
