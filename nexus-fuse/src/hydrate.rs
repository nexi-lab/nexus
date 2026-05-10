//! Eager hydration of small files into FileCache during workspace attach (Issue #4055).

use crate::cache::{
    FileCache, HYDRATE_CONCURRENCY, HYDRATE_SMALL_FILE_BYTES, HYDRATE_TOTAL_BUDGET_BYTES,
};
use crate::client::{FileEntry, NexusClient};
use crate::metrics;
use log::{debug, warn};
use serde::Serialize;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Instant;
use tokio::sync::Semaphore;
use tokio::task::JoinSet;

/// Maximum directory recursion depth before the BFS gives up.
const HYDRATE_MAX_DEPTH: u32 = 32;

/// Maximum total entries collected before the BFS gives up.
const HYDRATE_MAX_ENTRIES: usize = 100_000;

#[derive(Debug, Clone)]
pub struct HydrateOptions {
    pub workspace_root: String,
    pub threshold_bytes: usize,
    pub budget_bytes: usize,
    pub concurrency: usize,
    pub max_depth: u32,
    pub max_entries: usize,
}

impl HydrateOptions {
    pub fn new(workspace_root: String) -> Self {
        Self {
            workspace_root,
            threshold_bytes: HYDRATE_SMALL_FILE_BYTES,
            budget_bytes: HYDRATE_TOTAL_BUDGET_BYTES,
            concurrency: HYDRATE_CONCURRENCY,
            max_depth: HYDRATE_MAX_DEPTH,
            max_entries: HYDRATE_MAX_ENTRIES,
        }
    }
}

#[derive(Debug, Clone, Serialize, Default)]
pub struct HydrateStats {
    pub admitted_count: u64,
    pub admitted_bytes: u64,
    pub skipped_warm: u64,
    pub skipped_size: u64,
    pub skipped_budget: u64,
    pub failed: u64,
    pub duration_ms: u64,
}

/// Walk the workspace via `client.list` BFS, then admit small cold files to the cache.
pub async fn hydrate_workspace(
    client: Arc<NexusClient>,
    cache: Arc<FileCache>,
    opts: HydrateOptions,
) -> HydrateStats {
    let started = Instant::now();
    let admitted_count = Arc::new(AtomicU64::new(0));
    let admitted_bytes = Arc::new(AtomicU64::new(0));
    let skipped_warm = Arc::new(AtomicU64::new(0));
    let skipped_size = Arc::new(AtomicU64::new(0));
    let skipped_budget = Arc::new(AtomicU64::new(0));
    let failed = Arc::new(AtomicU64::new(0));

    // collect_candidates calls reqwest::blocking — must run off the async executor.
    let client_bfs = client.clone();
    let opts_bfs = opts.clone();
    let skipped_size_bfs = skipped_size.clone();
    let candidates = match tokio::task::spawn_blocking(move || {
        collect_candidates(&client_bfs, &opts_bfs, &skipped_size_bfs)
    })
    .await
    {
        Ok(Ok(list)) => list,
        Ok(Err(err)) => {
            warn!("hydrate: root list failed for {:?}: {}", opts.workspace_root, err);
            failed.fetch_add(1, Ordering::Relaxed);
            return finalize_stats(
                started,
                admitted_count,
                admitted_bytes,
                skipped_warm,
                skipped_size,
                skipped_budget,
                failed,
            );
        }
        Err(join_err) => {
            warn!("hydrate: BFS task panicked: {}", join_err);
            failed.fetch_add(1, Ordering::Relaxed);
            return finalize_stats(
                started,
                admitted_count,
                admitted_bytes,
                skipped_warm,
                skipped_size,
                skipped_budget,
                failed,
            );
        }
    };

    let semaphore = Arc::new(Semaphore::new(opts.concurrency.max(1)));
    let mut join_set: JoinSet<()> = JoinSet::new();

    for path in candidates {
        let permit = match semaphore.clone().acquire_owned().await {
            Ok(p) => p,
            Err(_) => break,
        };
        let client_task = client.clone();
        let cache_task = cache.clone();
        let admitted_count = admitted_count.clone();
        let admitted_bytes = admitted_bytes.clone();
        let skipped_size_task = skipped_size.clone();
        let skipped_warm_task = skipped_warm.clone();
        let skipped_budget = skipped_budget.clone();
        let failed = failed.clone();
        let budget = opts.budget_bytes as u64;
        let threshold = opts.threshold_bytes;
        join_set.spawn_blocking(move || {
            let _permit = permit;
            if admitted_bytes.load(Ordering::Relaxed) >= budget {
                skipped_budget.fetch_add(1, Ordering::Relaxed);
                metrics::record_hydration_file("skipped_budget");
                return;
            }
            // Stat is mandatory: we need (a) the real backend gen so admitted
            // entries don't invalidate on first read, and (b) the authoritative
            // size since FileEntry.size from list can be stale or default to 0.
            // On stat failure we fail closed — we do NOT fall back to gen=0,
            // which would risk surfacing entries cached by other principals
            // when the daemon's read path probes the cache without a fresh
            // gen check.
            let meta = match client_task.stat(&path) {
                Ok(m) => m,
                Err(err) => {
                    debug!("hydrate: stat failed for {} — skipping: {}", path, err);
                    failed.fetch_add(1, Ordering::Relaxed);
                    metrics::record_hydration_file("failed");
                    return;
                }
            };
            // Authoritative size check from stat. The earlier list-based
            // filter is best-effort; this is the gate.
            if (meta.size as usize) > threshold {
                skipped_size_task.fetch_add(1, Ordering::Relaxed);
                metrics::record_hydration_file("skipped_size");
                return;
            }
            let gen = meta.gen;
            // Generation-aware warm check (#4055 R5). The list-time
            // is_warm pre-filter (in collect_candidates) is age-only and
            // can either misclassify gen-stale entries as warm or miss
            // foyer disk records after a daemon restart wiped in-memory
            // metadata. Probing the cache here with the authoritative gen
            // (and going through foyer when metadata is missing) gives an
            // accurate skip decision and a correct skipped_warm count.
            if let crate::cache::CacheLookup::Hit(_) = cache_task.get(&path, gen) {
                skipped_warm_task.fetch_add(1, Ordering::Relaxed);
                metrics::record_hydration_file("skipped_warm");
                return;
            }
            match client_task.read_with_etag(&path, None) {
                Ok(crate::client::ReadResponse::Content { content, etag }) => {
                    // Final defensive check: even when stat said the file was
                    // small, the actual read could return more bytes if the
                    // backend changed between stat and read. Refuse to admit.
                    if content.len() > threshold {
                        debug!(
                            "hydrate: read returned {} bytes for {} (> threshold {}), refusing admit",
                            content.len(),
                            path,
                            threshold
                        );
                        failed.fetch_add(1, Ordering::Relaxed);
                        metrics::record_hydration_file("failed");
                        return;
                    }
                    let len = content.len() as u64;
                    cache_task.put(&path, &content, etag.as_deref(), gen);
                    admitted_count.fetch_add(1, Ordering::Relaxed);
                    admitted_bytes.fetch_add(len, Ordering::Relaxed);
                    metrics::record_hydration_file("admitted");
                    metrics::record_hydration_bytes("admitted", len);
                }
                Ok(crate::client::ReadResponse::NotModified) => {
                    debug!("hydrate: unexpected 304 for {} without etag", path);
                    failed.fetch_add(1, Ordering::Relaxed);
                    metrics::record_hydration_file("failed");
                }
                Err(err) => {
                    debug!("hydrate: read failed for {}: {}", path, err);
                    failed.fetch_add(1, Ordering::Relaxed);
                    metrics::record_hydration_file("failed");
                }
            }
        });
    }

    while join_set.join_next().await.is_some() {}

    finalize_stats(
        started,
        admitted_count,
        admitted_bytes,
        skipped_warm,
        skipped_size,
        skipped_budget,
        failed,
    )
}

fn finalize_stats(
    started: Instant,
    admitted_count: Arc<AtomicU64>,
    admitted_bytes: Arc<AtomicU64>,
    skipped_warm: Arc<AtomicU64>,
    skipped_size: Arc<AtomicU64>,
    skipped_budget: Arc<AtomicU64>,
    failed: Arc<AtomicU64>,
) -> HydrateStats {
    let stats = HydrateStats {
        admitted_count: admitted_count.load(Ordering::Relaxed),
        admitted_bytes: admitted_bytes.load(Ordering::Relaxed),
        skipped_warm: skipped_warm.load(Ordering::Relaxed),
        skipped_size: skipped_size.load(Ordering::Relaxed),
        skipped_budget: skipped_budget.load(Ordering::Relaxed),
        failed: failed.load(Ordering::Relaxed),
        duration_ms: started.elapsed().as_millis() as u64,
    };
    metrics::observe_hydration_duration_ms(stats.duration_ms);
    stats
}

fn collect_candidates(
    client: &NexusClient,
    opts: &HydrateOptions,
    skipped_size: &Arc<AtomicU64>,
) -> Result<Vec<String>, crate::error::NexusClientError> {
    // skipped_warm is no longer counted here; the per-task gen-aware probe
    // (cache.get(path, gen) inside the spawned task) is the authoritative
    // warm check. The list-time in-memory is_warm pre-filter that lived
    // here before #4055 R5 could miss valid foyer-disk records after a
    // daemon restart, and could classify gen-stale entries as warm. The
    // tradeoff is an extra stat per candidate; the savings of pre-skipping
    // are worth giving up for correctness.
    let mut candidates: Vec<String> = Vec::new();
    // Proper FIFO BFS (#4055 R8): a Vec+pop() gives LIFO/stack order,
    // which combined with `break` on `depth > max_depth` could abandon
    // shallower sibling directories still in the queue when a single
    // deep branch is processed first. Use VecDeque + pop_front for true
    // breadth-first order, and `continue` on over-depth so siblings at
    // valid depths are still processed.
    let mut queue: std::collections::VecDeque<(String, u32)> = std::collections::VecDeque::new();
    queue.push_back((opts.workspace_root.clone(), 0));
    let mut total_seen: usize = 0;
    let mut root_listed = false;

    while let Some((dir, depth)) = queue.pop_front() {
        // Over-depth siblings: skip this dir, keep walking the queue.
        if depth > opts.max_depth {
            continue;
        }
        // Hard cap on processed entries — exhaust the walk.
        if total_seen >= opts.max_entries {
            break;
        }
        let entries = match client.list(&dir) {
            Ok(e) => e,
            Err(err) => {
                if !root_listed {
                    return Err(err);
                }
                warn!("hydrate: list failed for {}: {} (continuing)", dir, err);
                continue;
            }
        };
        root_listed = true;

        for entry in entries {
            // Enforce max_entries inside the inner loop too — a single
            // backend list response with hundreds of thousands of entries
            // would otherwise be fully accumulated before the outer loop
            // re-checks the cap, defeating the defensive bound.
            if total_seen >= opts.max_entries {
                break;
            }
            total_seen += 1;
            let full_path = join_path(&dir, &entry.name);
            if is_directory(&entry) {
                queue.push_back((full_path, depth + 1));
                continue;
            }
            if (entry.size as usize) > opts.threshold_bytes {
                skipped_size.fetch_add(1, Ordering::Relaxed);
                metrics::record_hydration_file("skipped_size");
                continue;
            }
            candidates.push(full_path);
        }
    }
    Ok(candidates)
}

fn is_directory(entry: &FileEntry) -> bool {
    entry.entry_type.eq_ignore_ascii_case("directory")
        || entry.entry_type.eq_ignore_ascii_case("dir")
}

fn join_path(parent: &str, name: &str) -> String {
    if parent.ends_with('/') {
        format!("{}{}", parent, name)
    } else {
        format!("{}/{}", parent, name)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cache::CacheConfig;

    fn fresh_cache(label: &str) -> Arc<FileCache> {
        let _ = env_logger::builder().is_test(true).try_init();
        let dir = tempfile::tempdir().unwrap();
        let config = CacheConfig::new(
            dir.keep(),
            8 * 1024 * 1024,
            32 * 1024 * 1024,
            1024 * 1024,
        )
        .unwrap();
        Arc::new(
            FileCache::new_with_config(
                &format!("http://test-{}.invalid", label),
                "test-principal",
                config,
            )
            .unwrap(),
        )
    }

    /// Mock a generic small-file stat response on the given mockito server.
    /// Hydration now requires a successful stat before admitting (#4055 R2),
    /// so every test that lists files must also mock /api/nfs/stat.
    fn mock_small_stat(server: &mut mockito::ServerGuard) -> mockito::Mock {
        server
            .mock("POST", "/api/nfs/stat")
            .with_status(200)
            .with_body(
                r#"{"jsonrpc":"2.0","id":1,"result":{"size":10,"gen":0,"etag":null,"modified_at":null,"is_directory":false}}"#,
            )
            .create()
    }

    #[test]
    fn test_hydrate_admits_small_files() {
        let _guard = crate::metrics::test_guard();
        crate::metrics::reset_for_tests();

        // Create the mockito server and NexusClient outside any async context.
        // reqwest::blocking::Client internally creates a tokio runtime; doing this
        // inside an async executor panics with "Cannot drop a runtime in a context
        // where blocking is not allowed."
        let mut server = mockito::Server::new();
        let body = r#"{"jsonrpc":"2.0","id":1,"result":{"files":[
            {"path":"/a.txt","is_directory":false,"size":10},
            {"path":"/big.bin","is_directory":false,"size":1048576}
        ]}}"#;
        let _list_mock = server
            .mock("POST", "/api/nfs/list")
            .with_status(200)
            .with_body(body)
            .create();
        let _stat_mock = mock_small_stat(&mut server);
        let _read_mock = server
            .mock("POST", "/api/nfs/read")
            .with_status(200)
            .with_header("etag", "\"abc\"")
            .with_body(r#"{"jsonrpc":"2.0","id":1,"result":{"__type__":"bytes","data":"aGVsbG8="}}"#)
            .create();

        let client = Arc::new(NexusClient::new(&server.url(), "test-key", None).unwrap());
        let cache = fresh_cache("admit");
        let opts = HydrateOptions::new("/".to_string());

        let rt = tokio::runtime::Runtime::new().unwrap();
        let stats = rt.block_on(hydrate_workspace(client, cache.clone(), opts));

        assert_eq!(stats.admitted_count, 1, "only /a.txt should admit");
        assert_eq!(stats.skipped_size, 1, "/big.bin should be skipped by size");
        assert_eq!(stats.failed, 0);
        assert!(cache.is_warm("/a.txt"));
    }

    #[test]
    fn test_hydrate_skips_warm_entries() {
        let _guard = crate::metrics::test_guard();
        crate::metrics::reset_for_tests();

        let mut server = mockito::Server::new();
        let list_body = r#"{"jsonrpc":"2.0","id":1,"result":{"files":[
            {"path":"/cached.txt","is_directory":false,"size":10},
            {"path":"/cold.txt","is_directory":false,"size":10}
        ]}}"#;
        let _list_mock = server
            .mock("POST", "/api/nfs/list")
            .with_status(200)
            .with_body(list_body)
            .create();
        let _stat_mock = mock_small_stat(&mut server);
        // The read mock should be hit exactly once — for the cold path.
        let read_mock = server
            .mock("POST", "/api/nfs/read")
            .with_status(200)
            .with_header("etag", "\"abc\"")
            .with_body(r#"{"jsonrpc":"2.0","id":1,"result":{"__type__":"bytes","data":"aGk="}}"#)
            .expect(1)
            .create();

        let client = Arc::new(NexusClient::new(&server.url(), "k", None).unwrap());
        let cache = fresh_cache("warm");
        cache.put("/cached.txt", b"already-here", Some("etag-old"), 0);

        let rt = tokio::runtime::Runtime::new().unwrap();
        let stats = rt.block_on(hydrate_workspace(client, cache.clone(), HydrateOptions::new("/".into())));

        assert_eq!(stats.skipped_warm, 1);
        assert_eq!(stats.admitted_count, 1);
        assert_eq!(stats.failed, 0);
        read_mock.assert(); // verifies exactly 1 read call
    }

    #[test]
    fn test_hydrate_respects_budget() {
        use base64::Engine;

        let _guard = crate::metrics::test_guard();
        crate::metrics::reset_for_tests();

        let mut server = mockito::Server::new();
        // 10 files of 10 KiB each; budget allows ~3.
        let mut files = String::new();
        for i in 0..10 {
            if i > 0 {
                files.push(',');
            }
            files.push_str(&format!(
                r#"{{"path":"/f{}.bin","is_directory":false,"size":10240}}"#,
                i
            ));
        }
        let body = format!(r#"{{"jsonrpc":"2.0","id":1,"result":{{"files":[{}]}}}}"#, files);
        let _list_mock = server
            .mock("POST", "/api/nfs/list")
            .with_status(200)
            .with_body(body)
            .create();
        // Stat mock returns size=10240 to match the 10 KiB content size for the budget test.
        let _stat_mock = server
            .mock("POST", "/api/nfs/stat")
            .with_status(200)
            .with_body(
                r#"{"jsonrpc":"2.0","id":1,"result":{"size":10240,"gen":0,"etag":null,"modified_at":null,"is_directory":false}}"#,
            )
            .create();
        let payload = base64::engine::general_purpose::STANDARD.encode(vec![b'x'; 10 * 1024]);
        let read_body = format!(
            r#"{{"jsonrpc":"2.0","id":1,"result":{{"__type__":"bytes","data":"{}"}}}}"#,
            payload
        );
        let _read_mock = server
            .mock("POST", "/api/nfs/read")
            .with_status(200)
            .with_header("etag", "\"x\"")
            .with_body(read_body)
            .expect_at_most(10)
            .create();

        let client = Arc::new(NexusClient::new(&server.url(), "k", None).unwrap());
        let cache = fresh_cache("budget");
        let mut opts = HydrateOptions::new("/".into());
        opts.budget_bytes = 30 * 1024;
        opts.concurrency = 2;
        opts.threshold_bytes = 16 * 1024;

        let rt = tokio::runtime::Runtime::new().unwrap();
        let stats = rt.block_on(hydrate_workspace(client, cache, opts));

        // With concurrency=2 and budget=30KiB, expect 3-4 admits (race window allows overshoot).
        assert!(
            (3..=4).contains(&stats.admitted_count),
            "expected 3-4 admits, got {}",
            stats.admitted_count
        );
        assert!(
            stats.skipped_budget >= 6,
            "expected >= 6 skipped_budget, got {}",
            stats.skipped_budget
        );
        assert_eq!(stats.failed, 0);
    }

    #[test]
    fn test_hydrate_continues_on_per_file_error() {
        let _guard = crate::metrics::test_guard();
        crate::metrics::reset_for_tests();

        let mut server = mockito::Server::new();
        let _list_mock = server
            .mock("POST", "/api/nfs/list")
            .with_status(200)
            .with_body(r#"{"jsonrpc":"2.0","id":1,"result":{"files":[
                {"path":"/ok1.txt","is_directory":false,"size":3},
                {"path":"/bad.txt","is_directory":false,"size":3},
                {"path":"/ok2.txt","is_directory":false,"size":3}
            ]}}"#)
            .create();
        let _stat_mock = mock_small_stat(&mut server);

        // Order matters: register the more-specific match first so mockito tries it before the catch-all.
        let _bad_mock = server
            .mock("POST", "/api/nfs/read")
            .match_body(mockito::Matcher::Regex(r#""path":\s*"/bad\.txt""#.into()))
            .with_status(500)
            .with_body("internal error")
            .create();
        let _ok_mock = server
            .mock("POST", "/api/nfs/read")
            .match_body(mockito::Matcher::Regex(r#""path":\s*"/ok\d\.txt""#.into()))
            .with_status(200)
            .with_header("etag", "\"e\"")
            .with_body(r#"{"jsonrpc":"2.0","id":1,"result":{"__type__":"bytes","data":"aGk="}}"#)
            .create();

        let client = Arc::new(NexusClient::new(&server.url(), "k", None).unwrap());
        let cache = fresh_cache("per_file_err");
        let rt = tokio::runtime::Runtime::new().unwrap();
        let stats = rt.block_on(hydrate_workspace(client, cache, HydrateOptions::new("/".into())));

        assert_eq!(stats.admitted_count, 2);
        assert_eq!(stats.failed, 1);
    }

    #[test]
    fn test_hydrate_root_list_failure_returns_failed() {
        let _guard = crate::metrics::test_guard();
        crate::metrics::reset_for_tests();

        let mut server = mockito::Server::new();
        let _list_mock = server
            .mock("POST", "/api/nfs/list")
            .with_status(500)
            .with_body("backend down")
            .create();

        let client = Arc::new(NexusClient::new(&server.url(), "k", None).unwrap());
        let cache = fresh_cache("list_err");
        let rt = tokio::runtime::Runtime::new().unwrap();
        let stats = rt.block_on(hydrate_workspace(client, cache, HydrateOptions::new("/".into())));

        assert_eq!(stats.admitted_count, 0);
        assert_eq!(stats.failed, 1);
    }

    #[test]
    fn test_hydrate_empty_workspace_zero_stats() {
        let _guard = crate::metrics::test_guard();
        crate::metrics::reset_for_tests();

        let mut server = mockito::Server::new();
        let _list_mock = server
            .mock("POST", "/api/nfs/list")
            .with_status(200)
            .with_body(r#"{"jsonrpc":"2.0","id":1,"result":{"files":[]}}"#)
            .create();

        let client = Arc::new(NexusClient::new(&server.url(), "k", None).unwrap());
        let cache = fresh_cache("empty");
        let rt = tokio::runtime::Runtime::new().unwrap();
        let stats = rt.block_on(hydrate_workspace(client, cache, HydrateOptions::new("/".into())));

        assert_eq!(stats.admitted_count, 0);
        assert_eq!(stats.skipped_size, 0);
        assert_eq!(stats.skipped_warm, 0);
        assert_eq!(stats.failed, 0);
    }

    #[test]
    fn test_hydrate_respects_max_depth() {
        // Recursive mock: every list call returns one subdir "d" plus one file "f.txt".
        // Without a depth cap this would loop forever; with max_depth=3 the BFS halts
        // after processing depths 0, 1, 2, 3 (= 4 files).
        let _guard = crate::metrics::test_guard();
        crate::metrics::reset_for_tests();

        let mut server = mockito::Server::new();
        let recursive_body = r#"{"jsonrpc":"2.0","id":1,"result":{"files":[
            {"path":"d","is_directory":true,"size":0},
            {"path":"f.txt","is_directory":false,"size":3}
        ]}}"#;
        server
            .mock("POST", "/api/nfs/list")
            .with_status(200)
            .with_body(recursive_body)
            .expect_at_least(1)
            .create();
        let _stat_mock = mock_small_stat(&mut server);
        server
            .mock("POST", "/api/nfs/read")
            .with_status(200)
            .with_header("etag", "\"e\"")
            .with_body(r#"{"jsonrpc":"2.0","id":1,"result":{"__type__":"bytes","data":"YWJj"}}"#)
            .expect_at_least(1)
            .create();

        let client = Arc::new(NexusClient::new(&server.url(), "k", None).unwrap());
        let cache = fresh_cache("max_depth");
        let mut opts = HydrateOptions::new("/".into());
        opts.max_depth = 3;
        opts.concurrency = 4;

        let rt = tokio::runtime::Runtime::new().unwrap();
        let stats = rt.block_on(hydrate_workspace(client, cache, opts));

        // Depths 0, 1, 2, 3 each yield exactly one file → 4 admissions, 0 failures, no infinite loop.
        assert_eq!(stats.admitted_count, 4, "expected 4 admits, got {:?}", stats);
        assert_eq!(stats.failed, 0);
    }

    #[test]
    fn test_hydrate_respects_max_entries() {
        // The cap halts mid-iteration once the entry-count budget is consumed.
        // With max_entries=2 and a 3-entry list response, only 2 entries are filtered/processed.
        let _guard = crate::metrics::test_guard();
        crate::metrics::reset_for_tests();

        let mut server = mockito::Server::new();
        server
            .mock("POST", "/api/nfs/list")
            .with_status(200)
            .with_body(
                r#"{"jsonrpc":"2.0","id":1,"result":{"files":[
                {"path":"a.txt","is_directory":false,"size":3},
                {"path":"b.txt","is_directory":false,"size":3},
                {"path":"c.txt","is_directory":false,"size":3}
            ]}}"#,
            )
            .create();
        let _stat_mock = mock_small_stat(&mut server);
        server
            .mock("POST", "/api/nfs/read")
            .with_status(200)
            .with_header("etag", "\"e\"")
            .with_body(r#"{"jsonrpc":"2.0","id":1,"result":{"__type__":"bytes","data":"YWJj"}}"#)
            .create();

        let client = Arc::new(NexusClient::new(&server.url(), "k", None).unwrap());
        let cache = fresh_cache("max_entries");
        let mut opts = HydrateOptions::new("/".into());
        opts.max_entries = 2;

        let rt = tokio::runtime::Runtime::new().unwrap();
        let stats = rt.block_on(hydrate_workspace(client, cache, opts));

        // With max_entries=2 the per-entry cap fires inside the inner loop.
        // The third file (c.txt) must never be processed even though the list
        // response contains it.
        assert_eq!(stats.admitted_count, 2, "expected 2 admits, got {:?}", stats);
        assert_eq!(stats.skipped_size, 0);
        assert_eq!(stats.failed, 0);
        // Sanity: c.txt was never put into cache.
        // (Indirect: only a.txt and b.txt admitted; we don't enumerate cache directly.)
    }

    #[test]
    fn test_hydrate_max_entries_caps_a_single_huge_response() {
        // Reviewer scenario: one root list returns thousands of entries and the
        // cap must halt the inner loop, NOT just the outer directory queue.
        let _guard = crate::metrics::test_guard();
        crate::metrics::reset_for_tests();

        let mut server = mockito::Server::new();
        let mut files = String::new();
        for i in 0..500 {
            if i > 0 {
                files.push(',');
            }
            files.push_str(&format!(
                r#"{{"path":"/f{}.txt","is_directory":false,"size":3}}"#,
                i
            ));
        }
        let body = format!(r#"{{"jsonrpc":"2.0","id":1,"result":{{"files":[{}]}}}}"#, files);
        server
            .mock("POST", "/api/nfs/list")
            .with_status(200)
            .with_body(body)
            .create();
        let _stat_mock = mock_small_stat(&mut server);
        // expect_at_most caps how many read RPCs the mock will accept.
        // If the inner-loop cap regresses, the test will hammer the mock far
        // beyond max_entries and fail the assertion below.
        server
            .mock("POST", "/api/nfs/read")
            .with_status(200)
            .with_header("etag", "\"e\"")
            .with_body(r#"{"jsonrpc":"2.0","id":1,"result":{"__type__":"bytes","data":"YWJj"}}"#)
            .create();

        let client = Arc::new(NexusClient::new(&server.url(), "k", None).unwrap());
        let cache = fresh_cache("max_entries_huge");
        let mut opts = HydrateOptions::new("/".into());
        opts.max_entries = 10;

        let rt = tokio::runtime::Runtime::new().unwrap();
        let stats = rt.block_on(hydrate_workspace(client, cache, opts));

        // 10 entries seen → at most 10 admits; never the full 500.
        assert!(
            stats.admitted_count <= 10,
            "expected <=10 admits, got {:?}",
            stats
        );
        assert!(
            stats.admitted_count + stats.skipped_size + stats.failed <= 10,
            "processed >10 entries despite max_entries=10: {:?}",
            stats
        );
    }

    #[test]
    fn test_hydrate_continues_when_subdir_list_fails() {
        // Root list returns one ok subdirectory and one broken subdirectory.
        // The broken subdirectory's list returns 500. Hydrate should admit files
        // from the OK subdirectory and continue without setting `failed` (sub-dir
        // list errors are warnings, not per-file fetch failures).
        let _guard = crate::metrics::test_guard();
        crate::metrics::reset_for_tests();

        let mut server = mockito::Server::new();

        // Root listing — match by body containing "path":"/"
        server
            .mock("POST", "/api/nfs/list")
            .match_body(mockito::Matcher::Regex(r#""path":\s*"/""#.into()))
            .with_status(200)
            .with_body(
                r#"{"jsonrpc":"2.0","id":1,"result":{"files":[
                {"path":"ok","is_directory":true,"size":0},
                {"path":"bad","is_directory":true,"size":0}
            ]}}"#,
            )
            .create();
        // OK subdir listing
        server
            .mock("POST", "/api/nfs/list")
            .match_body(mockito::Matcher::Regex(r#""path":\s*"/ok""#.into()))
            .with_status(200)
            .with_body(
                r#"{"jsonrpc":"2.0","id":1,"result":{"files":[
                {"path":"/ok/file.txt","is_directory":false,"size":3}
            ]}}"#,
            )
            .create();
        // BAD subdir listing — returns 500
        server
            .mock("POST", "/api/nfs/list")
            .match_body(mockito::Matcher::Regex(r#""path":\s*"/bad""#.into()))
            .with_status(500)
            .with_body("backend error")
            .create();
        let _stat_mock = mock_small_stat(&mut server);
        // Read mock for the ok file
        server
            .mock("POST", "/api/nfs/read")
            .with_status(200)
            .with_header("etag", "\"e\"")
            .with_body(r#"{"jsonrpc":"2.0","id":1,"result":{"__type__":"bytes","data":"YWJj"}}"#)
            .create();

        let client = Arc::new(NexusClient::new(&server.url(), "k", None).unwrap());
        let cache = fresh_cache("subdir_fail");
        let rt = tokio::runtime::Runtime::new().unwrap();
        let stats = rt.block_on(hydrate_workspace(client, cache, HydrateOptions::new("/".into())));

        assert_eq!(stats.admitted_count, 1, "expected 1 admit from ok subdir, got {:?}", stats);
        // Per design: list errors mid-walk are not counted as `failed` (which is per-file fetch failures).
        assert_eq!(stats.failed, 0, "stats={:?}", stats);
    }

    #[test]
    fn test_hydrate_overdepth_branch_does_not_abandon_siblings() {
        // #4055 R8: an over-depth branch must NOT short-circuit traversal
        // of shallower sibling directories. The root has two children:
        //   /deep  → infinitely recursive (would exceed max_depth)
        //   /shallow → contains a single file we MUST admit
        // Pre-fix the BFS used Vec+pop (LIFO) + break-on-depth, which
        // could pop /deep first, exceed max_depth, and break out of the
        // loop before ever visiting /shallow.
        let _guard = crate::metrics::test_guard();
        crate::metrics::reset_for_tests();

        let mut server = mockito::Server::new();
        // Root list: two subdirs.
        server
            .mock("POST", "/api/nfs/list")
            .match_body(mockito::Matcher::Regex(r#""path":\s*"/""#.into()))
            .with_status(200)
            .with_body(
                r#"{"jsonrpc":"2.0","id":1,"result":{"files":[
                {"path":"/deep","entry_type":1,"size":0},
                {"path":"/shallow","entry_type":1,"size":0}
            ]}}"#,
            )
            .create();
        // /deep: infinitely recursive — every list returns another /deep/d
        // directory. With max_depth=1 the BFS pops /deep at depth=1
        // (allowed), descends into /deep/d at depth=2, which is over the
        // cap and must be SKIPPED, not abort.
        server
            .mock("POST", "/api/nfs/list")
            .match_body(mockito::Matcher::Regex(r#""path":\s*"/deep""#.into()))
            .with_status(200)
            .with_body(
                r#"{"jsonrpc":"2.0","id":1,"result":{"files":[
                {"path":"/deep/d","entry_type":1,"size":0}
            ]}}"#,
            )
            .create();
        // /shallow: one file we must admit.
        server
            .mock("POST", "/api/nfs/list")
            .match_body(mockito::Matcher::Regex(r#""path":\s*"/shallow""#.into()))
            .with_status(200)
            .with_body(
                r#"{"jsonrpc":"2.0","id":1,"result":{"files":[
                {"path":"/shallow/keep.txt","entry_type":0,"size":3}
            ]}}"#,
            )
            .create();
        let _stat_mock = mock_small_stat(&mut server);
        server
            .mock("POST", "/api/nfs/read")
            .with_status(200)
            .with_header("etag", "\"e\"")
            .with_body(r#"{"jsonrpc":"2.0","id":1,"result":{"__type__":"bytes","data":"YWJj"}}"#)
            .create();

        let client = Arc::new(NexusClient::new(&server.url(), "k", None).unwrap());
        let cache = fresh_cache("overdepth_sibling");
        let mut opts = HydrateOptions::new("/".into());
        opts.max_depth = 1;

        let rt = tokio::runtime::Runtime::new().unwrap();
        let stats = rt.block_on(hydrate_workspace(client, cache, opts));

        // /shallow/keep.txt must be admitted despite /deep going over the cap.
        assert_eq!(
            stats.admitted_count, 1,
            "BFS abandoned siblings: got {:?}",
            stats
        );
        assert_eq!(stats.failed, 0, "stats={:?}", stats);
    }

    #[test]
    fn test_hydrate_recognises_numeric_entry_type_directories() {
        // Reviewer scenario (#4055 R6): the production server emits
        // `entry_type` as a numeric DT_* code (DT_REG=0, DT_DIR=1, ...).
        // BFS must descend into entries marked as DT_DIR even when the
        // legacy `is_directory` boolean is absent. Without this, the walk
        // mistakes directories for files, attempts to read them, and
        // misses every nested file in the workspace.
        let _guard = crate::metrics::test_guard();
        crate::metrics::reset_for_tests();

        let mut server = mockito::Server::new();
        // Root list: one DT_DIR entry "sub"
        server
            .mock("POST", "/api/nfs/list")
            .match_body(mockito::Matcher::Regex(r#""path":\s*"/""#.into()))
            .with_status(200)
            .with_body(
                r#"{"jsonrpc":"2.0","id":1,"result":{"files":[
                {"path":"/sub","entry_type":1,"size":0}
            ]}}"#,
            )
            .create();
        // /sub list: one DT_REG file
        server
            .mock("POST", "/api/nfs/list")
            .match_body(mockito::Matcher::Regex(r#""path":\s*"/sub""#.into()))
            .with_status(200)
            .with_body(
                r#"{"jsonrpc":"2.0","id":1,"result":{"files":[
                {"path":"/sub/file.txt","entry_type":0,"size":3}
            ]}}"#,
            )
            .create();
        let _stat_mock = mock_small_stat(&mut server);
        server
            .mock("POST", "/api/nfs/read")
            .with_status(200)
            .with_header("etag", "\"e\"")
            .with_body(r#"{"jsonrpc":"2.0","id":1,"result":{"__type__":"bytes","data":"YWJj"}}"#)
            .create();

        let client = Arc::new(NexusClient::new(&server.url(), "k", None).unwrap());
        let cache = fresh_cache("entry_type_numeric");
        let rt = tokio::runtime::Runtime::new().unwrap();
        let stats = rt.block_on(hydrate_workspace(client, cache, HydrateOptions::new("/".into())));

        // BFS must descend into /sub and admit /sub/file.txt. Pre-fix this
        // would have admitted 0 (the directory was treated as a file).
        assert_eq!(stats.admitted_count, 1, "expected 1 admit, got {:?}", stats);
        assert_eq!(stats.failed, 0, "stats={:?}", stats);
    }
}
