//! ContentReplicationService — pure Rust replication scanner (§10 E1).
//!
//! Background scan loop: metastore range scan → policy lookup → gRPC fetch
//! or intra-node copy.  Policy resolution is injected at construction via
//! `MountReplicationPolicy` (matches Python `ReplicationPolicyResolver`
//! longest-prefix semantics).  All components already Rust.
//!
//! Launched as a background thread; stop via `ReplicationScanner::stop()`.

use kernel::kernel::OperationContext;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;

// ── Policy types ────────────────────────────────────────────────────────────

/// Where replicated content should be sent / pulled from.
#[allow(dead_code)]
pub enum ReplicationTarget {
    /// Replicate to / pull from all Raft voters in the zone.
    AllVoters,
    /// Replicate to / pull from specific peer VFS addresses (host:port).
    Nodes(Vec<String>),
    /// Intra-node copy to another local mount path.
    Mount(String),
}

/// Replication policy for a single path prefix.
#[allow(dead_code)]
pub struct MountReplicationPolicy {
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
pub struct ReplicationScanner {
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
    pub fn new(interval_ms: u64, zone_id: &str, policies: Vec<MountReplicationPolicy>) -> Self {
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
    /// 1. `metastore.list(zone_prefix)` — get all entries (no content_id skip).
    /// 2. `resolve_policy(entry.path)` — skip entries with no policy.
    /// 3. Dispatch by target type:
    ///    - `Mount(m)`: intra-node `sys_read` → `sys_write` (router picks mount).
    ///    - `Nodes(addrs)` / `AllVoters`: `PeerBlobClient::fetch_path` from the
    ///      first responsive peer → `sys_write` locally.
    ///
    /// Returns `(scanned, replicated, errors)`.
    #[allow(dead_code)]
    pub fn scan_and_replicate(&self, kernel: &kernel::kernel::Kernel) -> (usize, usize, usize) {
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
                    // Use the public peer_client_arc() accessor since
                    // raft is a downstream crate now.
                    let peer_client = kernel.peer_client_arc();
                    let fetched: Option<Vec<u8>> = addrs
                        .iter()
                        .find_map(|addr| peer_client.fetch(addr, &entry.path).ok());
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
    pub fn start(self: &Arc<Self>, kernel: Arc<kernel::kernel::Kernel>) {
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
    pub fn is_running(&self) -> bool {
        self.running.load(Ordering::Relaxed)
    }

    /// Signal the scanner to stop (takes effect after the current sleep expires).
    #[allow(dead_code)]
    pub fn stop(&self) {
        self.running.store(false, Ordering::Relaxed);
    }

    /// Get stats: (scanned, replicated, errors, last_scan_ms).
    #[allow(dead_code)]
    pub fn stats(&self) -> (u64, u64, u64, u64) {
        (
            self.scanned_count.load(Ordering::Relaxed),
            self.replicated_count.load(Ordering::Relaxed),
            self.error_count.load(Ordering::Relaxed),
            self.last_scan_ms.load(Ordering::Relaxed),
        )
    }
}

// ── Policy JSON parser ──────────────────────────────────────────────────────

/// Parse `policies_json` — a JSON array of `{path_prefix, target}` objects
/// where `target` is one of:
/// `{"type":"mount","mount":"/p"}`, `{"type":"nodes","nodes":["host:port",..]}`,
/// `{"type":"all_voters"}`.
fn parse_policies(policies_json: &str) -> Result<Vec<MountReplicationPolicy>, String> {
    use serde_json::Value;
    let v: Value =
        serde_json::from_str(policies_json).map_err(|e| format!("policies_json parse: {e}"))?;
    let arr = v
        .as_array()
        .ok_or_else(|| "policies_json must be an array".to_string())?;
    let mut out = Vec::with_capacity(arr.len());
    for item in arr {
        let path_prefix = item
            .get("path_prefix")
            .and_then(|v| v.as_str())
            .ok_or_else(|| "policy entry missing 'path_prefix'".to_string())?
            .to_string();
        let target_obj = item
            .get("target")
            .ok_or_else(|| "policy entry missing 'target'".to_string())?;
        let ty = target_obj
            .get("type")
            .and_then(|v| v.as_str())
            .ok_or_else(|| "policy target missing 'type'".to_string())?;
        let target = match ty {
            "mount" => {
                let mount = target_obj
                    .get("mount")
                    .and_then(|v| v.as_str())
                    .ok_or_else(|| "mount target missing 'mount'".to_string())?
                    .to_string();
                ReplicationTarget::Mount(mount)
            }
            "nodes" => {
                let nodes_v = target_obj
                    .get("nodes")
                    .and_then(|v| v.as_array())
                    .ok_or_else(|| "nodes target missing 'nodes' array".to_string())?;
                let nodes: Vec<String> = nodes_v
                    .iter()
                    .filter_map(|n| n.as_str().map(str::to_string))
                    .collect();
                ReplicationTarget::Nodes(nodes)
            }
            "all_voters" => ReplicationTarget::AllVoters,
            other => return Err(format!("unknown target type '{other}'")),
        };
        out.push(MountReplicationPolicy {
            path_prefix,
            target,
        });
    }
    Ok(out)
}

/// Construct + start a `ReplicationScanner` for `zone_id` with the parsed
/// `policies_json`.  Returns the running scanner as an opaque handle so
/// callers can drop it / read stats / call `stop()`.
///
/// Surfaced as a federation control-plane entry, not part of the
/// `FederationProvider` HAL trait — kernel never invokes the scanner;
/// the Python boot path opts in per zone+mount via the cdylib's
/// `federation_start_replication_scanner` PyO3 binding.
pub fn install_for_zone(
    kernel: Arc<kernel::kernel::Kernel>,
    zone_id: &str,
    policies_json: &str,
    interval_ms: u64,
) -> Result<Arc<ReplicationScanner>, String> {
    let policies = parse_policies(policies_json)?;
    let scanner = Arc::new(ReplicationScanner::new(interval_ms, zone_id, policies));
    scanner.start(kernel);
    Ok(scanner)
}
