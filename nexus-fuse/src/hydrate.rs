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
}

impl HydrateOptions {
    pub fn new(workspace_root: String) -> Self {
        Self {
            workspace_root,
            threshold_bytes: HYDRATE_SMALL_FILE_BYTES,
            budget_bytes: HYDRATE_TOTAL_BUDGET_BYTES,
            concurrency: HYDRATE_CONCURRENCY,
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
    let cache_bfs = cache.clone();
    let opts_bfs = opts.clone();
    let skipped_warm_bfs = skipped_warm.clone();
    let skipped_size_bfs = skipped_size.clone();
    let candidates = match tokio::task::spawn_blocking(move || {
        collect_candidates(
            &client_bfs,
            &cache_bfs,
            &opts_bfs,
            &skipped_warm_bfs,
            &skipped_size_bfs,
        )
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
        let skipped_budget = skipped_budget.clone();
        let failed = failed.clone();
        let budget = opts.budget_bytes as u64;

        join_set.spawn_blocking(move || {
            let _permit = permit;
            if admitted_bytes.load(Ordering::Relaxed) >= budget {
                skipped_budget.fetch_add(1, Ordering::Relaxed);
                metrics::record_hydration_file("skipped_budget");
                return;
            }
            match client_task.read_with_etag(&path, None) {
                Ok(crate::client::ReadResponse::Content { content, etag }) => {
                    let len = content.len() as u64;
                    cache_task.put(&path, &content, etag.as_deref(), 0);
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
    cache: &FileCache,
    opts: &HydrateOptions,
    skipped_warm: &Arc<AtomicU64>,
    skipped_size: &Arc<AtomicU64>,
) -> Result<Vec<String>, crate::error::NexusClientError> {
    let mut candidates: Vec<String> = Vec::new();
    let mut queue: Vec<(String, u32)> = vec![(opts.workspace_root.clone(), 0)];
    let mut total_seen: usize = 0;
    let mut root_listed = false;

    while let Some((dir, depth)) = queue.pop() {
        if depth > HYDRATE_MAX_DEPTH || total_seen >= HYDRATE_MAX_ENTRIES {
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
            total_seen += 1;
            let full_path = join_path(&dir, &entry.name);
            if is_directory(&entry) {
                queue.push((full_path, depth + 1));
                continue;
            }
            if (entry.size as usize) > opts.threshold_bytes {
                skipped_size.fetch_add(1, Ordering::Relaxed);
                metrics::record_hydration_file("skipped_size");
                continue;
            }
            if cache.is_warm(&full_path) {
                skipped_warm.fetch_add(1, Ordering::Relaxed);
                metrics::record_hydration_file("skipped_warm");
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
            FileCache::new_with_config(&format!("http://test-{}.invalid", label), config).unwrap(),
        )
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
}
