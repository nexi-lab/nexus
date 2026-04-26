//! ContentReplicationService — pure Rust replication scanner (§10 E1).
//!
//! Background scan loop: metastore range scan → policy lookup → gRPC fetch
//! or intra-node copy.  Policy resolution is injected at construction via
//! `MountReplicationPolicy` (matches Python `ReplicationPolicyResolver`
//! longest-prefix semantics).  All components already Rust.
//!
//! Launched as a background thread; stop via `ReplicationScanner::stop()`.

use crate::kernel::OperationContext;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;

// ── Policy types ────────────────────────────────────────────────────────────

/// Where replicated content should be sent / pulled from.
#[allow(dead_code)]
pub(crate) enum ReplicationTarget {
    /// Replicate to / pull from all Raft voters in the zone.
    AllVoters,
    /// Replicate to / pull from specific peer VFS addresses (host:port).
    Nodes(Vec<String>),
    /// Intra-node copy to another local mount path.
    Mount(String),
}

/// Replication policy for a single path prefix.
#[allow(dead_code)]
pub(crate) struct MountReplicationPolicy {
    /// VFS path prefix this policy applies to (e.g. `/zone1/data`).
    pub path_prefix: String,
    /// Target for replication.
    pub target: ReplicationTarget,
}

// ── Longest-prefix resolver ─────────────────────────────────────────────────

/// Return the policy whose `path_prefix` is the longest prefix of `path`,
/// or `None` if no policy matches.  Mirrors `ReplicationPolicyResolver.get_policy()`.
#[allow(dead_code)]
fn resolve_policy<'a>(
    path: &str,
    policies: &'a [MountReplicationPolicy],
) -> Option<&'a MountReplicationPolicy> {
    policies
        .iter()
        .filter(|p| path.starts_with(&p.path_prefix))
        .max_by_key(|p| p.path_prefix.len())
}

// ── Scanner ─────────────────────────────────────────────────────────────────

/// Replication scanner — scans metastore for entries needing replication.
pub(crate) struct ReplicationScanner {
    /// Scan interval in milliseconds.
    #[allow(dead_code)]
    interval_ms: u64,
    /// Zone ID to scan (used as metastore prefix and OperationContext zone).
    #[allow(dead_code)]
    zone_id: String,
    /// Ordered list of mount replication policies (longest-prefix wins).
    #[allow(dead_code)]
    policies: Vec<MountReplicationPolicy>,
    /// Running flag — set to false to stop the background loop.
    running: Arc<AtomicBool>,
    /// Counters for monitoring.
    pub scanned_count: Arc<AtomicU64>,
    pub replicated_count: Arc<AtomicU64>,
    pub error_count: Arc<AtomicU64>,
    pub last_scan_ms: Arc<AtomicU64>,
}

impl ReplicationScanner {
    /// Construct a new scanner.  `policies` is evaluated in longest-prefix
    /// order on every scan; callers supply the full list (Python side reads
    /// mount configs and serialises them as JSON).
    #[allow(dead_code)]
    pub(crate) fn new(
        interval_ms: u64,
        zone_id: &str,
        policies: Vec<MountReplicationPolicy>,
    ) -> Self {
        Self {
            interval_ms,
            zone_id: zone_id.to_string(),
            policies,
            running: Arc::new(AtomicBool::new(false)),
            scanned_count: Arc::new(AtomicU64::new(0)),
            replicated_count: Arc::new(AtomicU64::new(0)),
            error_count: Arc::new(AtomicU64::new(0)),
            last_scan_ms: Arc::new(AtomicU64::new(0)),
        }
    }

    /// Run one scan-and-replicate pass.
    ///
    /// Semantics (path-first, matching Python `ContentReplicationService`):
    ///
    /// 1. `metastore.list(zone_prefix)` — get all entries (no etag skip).
    /// 2. `resolve_policy(entry.path)` — skip entries with no policy.
    /// 3. Dispatch by target type:
    ///    - `Mount(m)`: intra-node `sys_read` → `sys_write` (router picks mount).
    ///    - `Nodes(addrs)` / `AllVoters`: `PeerBlobClient::fetch_path` from the
    ///      first responsive peer → `sys_write` locally.
    ///
    /// Returns `(scanned, replicated, errors)`.
    #[allow(dead_code)]
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

            let policy = match resolve_policy(&entry.path, &self.policies) {
                Some(p) => p,
                None => continue,
            };

            match &policy.target {
                ReplicationTarget::Mount(_m) => {
                    // Intra-node: read from source, write to same path (router
                    // selects the appropriate mount based on its routing table).
                    let content = match kernel.sys_read(&entry.path, &ctx) {
                        Ok(r) => match r.data {
                            Some(data) => data,
                            None => continue,
                        },
                        Err(_) => continue,
                    };
                    match kernel.sys_write(&entry.path, &ctx, &content, 0) {
                        Ok(r) if r.hit => replicated += 1,
                        Ok(_) => errors += 1,
                        Err(_) => errors += 1,
                    }
                }

                ReplicationTarget::Nodes(addrs) => {
                    // Pull from the first peer that responds, store locally.
                    let fetched = addrs
                        .iter()
                        .find_map(|addr| kernel.peer_client.fetch_path(addr, &entry.path).ok());
                    match fetched {
                        Some(content) => match kernel.sys_write(&entry.path, &ctx, &content, 0) {
                            Ok(r) if r.hit => replicated += 1,
                            Ok(_) => errors += 1,
                            Err(_) => errors += 1,
                        },
                        None => errors += 1,
                    }
                }

                ReplicationTarget::AllVoters => {
                    // Voter address discovery requires access to ZoneManager,
                    // which is not yet plumbed into ReplicationScanner.  Use
                    // Nodes(addrs) with explicit addresses for now.
                    // TODO(replication): resolve voter list from zone_manager.
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
    /// The thread holds an `Arc<Self>` so the scanner stays alive until `stop()`
    /// is called and the current sleep expires.
    #[allow(dead_code)]
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
    #[allow(dead_code)]
    pub(crate) fn is_running(&self) -> bool {
        self.running.load(Ordering::Relaxed)
    }

    /// Signal the scanner to stop (takes effect after the current sleep expires).
    #[allow(dead_code)]
    pub(crate) fn stop(&self) {
        self.running.store(false, Ordering::Relaxed);
    }

    /// Get stats: (scanned, replicated, errors, last_scan_ms).
    #[allow(dead_code)]
    pub(crate) fn stats(&self) -> (u64, u64, u64, u64) {
        (
            self.scanned_count.load(Ordering::Relaxed),
            self.replicated_count.load(Ordering::Relaxed),
            self.error_count.load(Ordering::Relaxed),
            self.last_scan_ms.load(Ordering::Relaxed),
        )
    }
}
