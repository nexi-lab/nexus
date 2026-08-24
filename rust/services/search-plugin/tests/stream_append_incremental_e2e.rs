//! End-to-end coverage for the DT_STREAM append-incremental
//! indexing path (P4 — sudocode FR-C).
//!
//! The plugin's `index_one` short-circuits DT_STREAM through a
//! per-path checkpoint (`indexed_byte_len` + `next_chunk_index`).
//! On refresh, only the tail bytes past the checkpoint get
//! chunked + added; prior chunks stay untouched.  For an
//! append-only keep-forever transcript this drops the K-refresh
//! cost from O(K·N) to O(N).
//!
//! These tests use the shared `common::MockKernel` — a real
//! `IndexManager` + real FTS + real ANN (no embedder wired, so
//! keyword-only) run against an in-memory VFS whose `sys_read`
//! returns the whole deframed stream (matches nexus-vfs #235's
//! host-shim contract).

use std::sync::Arc;

use nexus_search_plugin::index_manager::IndexManager;
use nexus_search_plugin::search_proto::search_service_server::SearchService;
use nexus_search_plugin::search_proto::{IndexRequest, QueryRequest, QueryType, RefreshRequest};
use nexus_search_plugin::service::SearchServiceImpl;
use tempfile::TempDir;
use tonic::Request;

mod common;
use common::{handle_for, MockKernel};

// ── Harness ─────────────────────────────────────────────────────

struct Harness {
    _dir: TempDir,
    mock: *mut MockKernel,
    svc: SearchServiceImpl,
    /// Kept for tests that inspect the on-disk state file.
    zone_root: std::path::PathBuf,
}

impl Harness {
    fn start() -> Self {
        let dir = TempDir::new().expect("tempdir");
        let zone_root = dir.path().join("root");
        let mock = Box::into_raw(Box::new(MockKernel::new()));
        let handle = handle_for(mock);
        let manager = Arc::new(IndexManager::with_root(dir.path().to_path_buf()));
        let svc = SearchServiceImpl::builder(Arc::new(handle))
            .manager(manager)
            .build();
        Self {
            _dir: dir,
            mock,
            svc,
            zone_root,
        }
    }

    #[allow(clippy::mut_from_ref)]
    fn mock_mut(&self) -> &mut MockKernel {
        // SAFETY: `self` owns the box; per-test single-threaded.
        unsafe { &mut *self.mock }
    }
}

impl Drop for Harness {
    fn drop(&mut self) {
        unsafe { drop(Box::from_raw(self.mock)) };
    }
}

async fn index_root(svc: &SearchServiceImpl) -> nexus_search_plugin::search_proto::IndexResponse {
    svc.index(Request::new(IndexRequest {
        root_path: "/".into(),
        zone_id: "root".into(),
        recursive: true,
        max_docs: 0,
        auth_token: String::new(),
    }))
    .await
    .expect("index")
    .into_inner()
}

async fn refresh_root(
    svc: &SearchServiceImpl,
) -> nexus_search_plugin::search_proto::RefreshResponse {
    svc.refresh(Request::new(RefreshRequest {
        root_path: "/".into(),
        zone_id: "root".into(),
        recursive: true,
        max_docs: 0,
        auth_token: String::new(),
    }))
    .await
    .expect("refresh")
    .into_inner()
}

async fn query(
    svc: &SearchServiceImpl,
    q: &str,
) -> nexus_search_plugin::search_proto::QueryResponse {
    svc.query(Request::new(QueryRequest {
        q: q.into(),
        zone_id: "root".into(),
        limit: 100,
        query_type: QueryType::Keyword as i32,
        ..Default::default()
    }))
    .await
    .expect("query")
    .into_inner()
}

// Read the on-disk StreamState checkpoint for `path` — proves the
// plugin persisted the append-checkpoint across the RPC boundary,
// not just carried it in memory.
fn read_stream_state(zone_root: &std::path::Path, path: &str) -> (u64, u32) {
    let bytes = std::fs::read(zone_root.join("index_state.json")).expect("state file exists");
    let v: serde_json::Value = serde_json::from_slice(&bytes).expect("parse state");
    let entry = &v["streams"][path];
    assert!(!entry.is_null(), "stream state for {path} must be present");
    (
        entry["indexed_byte_len"].as_u64().unwrap(),
        entry["next_chunk_index"].as_u64().unwrap() as u32,
    )
}

/// True when the on-disk state's `streams` map has an entry for `path`.
fn stream_state_present(zone_root: &std::path::Path, path: &str) -> bool {
    let bytes = std::fs::read(zone_root.join("index_state.json")).expect("state file exists");
    let v: serde_json::Value = serde_json::from_slice(&bytes).expect("parse state");
    !v["streams"][path].is_null()
}

/// True when the on-disk state's `files` map has an entry for `path`.
/// Used to pin the SSOT invariant: a DT_STREAM path must NEVER
/// appear in `files` — that was the pre-fix bug where
/// `record_content_skip` cross-wrote stream paths into the files map.
fn file_state_present(zone_root: &std::path::Path, path: &str) -> bool {
    let bytes = std::fs::read(zone_root.join("index_state.json")).expect("state file exists");
    let v: serde_json::Value = serde_json::from_slice(&bytes).expect("parse state");
    !v["files"][path].is_null()
}

// ── Tests ───────────────────────────────────────────────────────

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn stream_append_only_chunks_the_tail() {
    // Seed a DT_STREAM with token in an early frame.  First Index
    // full-chunks it.  Then append more content with a token in the
    // new tail; a subsequent Refresh must ONLY add chunks for the
    // tail (chunk_index continues; the checkpoint advances by the
    // exact tail byte count), and both tokens must remain findable
    // — proving prior chunks stayed put across the incremental pass.
    let h = Harness::start();
    let mock = h.mock_mut();
    mock.add_dir("/");
    mock.add_dir("/logs");
    mock.add_stream(
        "/logs/audit-stream",
        b"turn-1: user asked ALPHATOKEN\n",
        1_700_000_000_000,
    );

    let r1 = index_root(&h.svc).await;
    assert!(r1.error.is_none(), "unexpected index error: {:?}", r1.error);
    assert!(r1.indexed_count >= 1);

    let (byte_len_1, chunk_count_1) = read_stream_state(&h.zone_root, "/logs/audit-stream");
    assert_eq!(
        byte_len_1, 30,
        "first-pass checkpoint must equal the seeded byte length"
    );
    assert!(chunk_count_1 >= 1, "first pass must have produced ≥1 chunk");

    // Baseline keyword hit before append.
    let q1 = query(&h.svc, "ALPHATOKEN").await;
    assert!(q1.error.is_none());
    let paths1: Vec<&str> = q1.results.iter().map(|r| r.path.as_str()).collect();
    assert!(
        paths1.contains(&"/logs/audit-stream"),
        "ALPHATOKEN must be indexed after first pass; got {paths1:?}",
    );

    // Append a new turn to the stream — mtime advances so the
    // Refresh verdict flips to Changed.
    mock.append_to_stream(
        "/logs/audit-stream",
        b"turn-2: agent said BETATOKEN\n",
        1_700_000_000_100,
    );

    let r2 = refresh_root(&h.svc).await;
    assert!(
        r2.error.is_none(),
        "unexpected refresh error: {:?}",
        r2.error
    );

    // Checkpoint MUST have advanced by exactly the appended byte
    // count, and chunk_count MUST have grown (new tail chunks were
    // added, not replacements).
    let (byte_len_2, chunk_count_2) = read_stream_state(&h.zone_root, "/logs/audit-stream");
    assert_eq!(
        byte_len_2,
        30 + 29,
        "checkpoint advances by exactly the appended bytes"
    );
    assert!(
        chunk_count_2 > chunk_count_1,
        "chunk_count must grow on append (was {chunk_count_1}, now {chunk_count_2})",
    );

    // BOTH tokens must be findable — proving the old chunk survived
    // the incremental pass AND the new chunk was added.
    let q_old = query(&h.svc, "ALPHATOKEN").await;
    assert!(
        q_old.results.iter().any(|r| r.path == "/logs/audit-stream"),
        "old token from prior chunks must survive incremental append",
    );
    let q_new = query(&h.svc, "BETATOKEN").await;
    assert!(
        q_new.results.iter().any(|r| r.path == "/logs/audit-stream"),
        "new token from appended tail must be indexed",
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn stream_unchanged_refresh_is_noop_at_checkpoint() {
    // A refresh that sees no new bytes AND the same mtime MUST NOT
    // re-index the stream — it stays a `unchanged` verdict via the
    // per-path mtime cache (populated by the full first pass).
    // Proves the FR-B recency signal + the P4 checkpoint agree on
    // the "quiescent stream → zero work" case.
    let h = Harness::start();
    let mock = h.mock_mut();
    mock.add_dir("/");
    mock.add_dir("/logs");
    mock.add_stream("/logs/audit-stream", b"turn-1: hello\n", 1_700_000_000_000);

    let _ = index_root(&h.svc).await;
    let (before_bytes, before_chunks) = read_stream_state(&h.zone_root, "/logs/audit-stream");

    let r = refresh_root(&h.svc).await;
    assert!(r.error.is_none());
    assert_eq!(
        r.reindexed_count, 0,
        "quiescent stream must not re-index on refresh",
    );
    assert!(
        r.unchanged_count >= 1,
        "quiescent stream must count as unchanged"
    );

    let (after_bytes, after_chunks) = read_stream_state(&h.zone_root, "/logs/audit-stream");
    assert_eq!(
        (before_bytes, before_chunks),
        (after_bytes, after_chunks),
        "checkpoint must NOT move on a no-op refresh",
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn stream_multi_append_chunk_count_is_monotonic() {
    // 3 successive appends → chunk_count strictly monotonic across
    // refreshes; byte-length monotonic too.  Pins the "we truly
    // incrementally-chunked each tail, not full-re-chunked" contract
    // beyond a single append.
    let h = Harness::start();
    let mock = h.mock_mut();
    mock.add_dir("/");
    mock.add_dir("/logs");
    mock.add_stream("/logs/s", b"chunk1 alpha\n", 1);
    let _ = index_root(&h.svc).await;
    let (b1, c1) = read_stream_state(&h.zone_root, "/logs/s");

    mock.append_to_stream("/logs/s", b"chunk2 beta\n", 2);
    let _ = refresh_root(&h.svc).await;
    let (b2, c2) = read_stream_state(&h.zone_root, "/logs/s");

    mock.append_to_stream("/logs/s", b"chunk3 gamma\n", 3);
    let _ = refresh_root(&h.svc).await;
    let (b3, c3) = read_stream_state(&h.zone_root, "/logs/s");

    assert!(
        b1 < b2 && b2 < b3,
        "byte-len strictly monotonic across 3 appends: {b1}<{b2}<{b3}"
    );
    assert!(
        c1 <= c2 && c2 <= c3,
        "chunk count monotonic: {c1}<={c2}<={c3}"
    );
    assert!(c3 > c1, "chunk count grew across 3 appends: {c1} → {c3}");

    // Every token findable.
    for token in ["alpha", "beta", "gamma"] {
        let q = query(&h.svc, token).await;
        assert!(
            q.results.iter().any(|r| r.path == "/logs/s"),
            "token {token} must survive multi-append incremental pass",
        );
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn stream_retention_trim_resets_checkpoint_and_reindexes() {
    // Retention-trim: the kernel-side WAL trims sealed cold
    // segments, so `sys_read` returns FEWER bytes than we last
    // checkpointed.  The plugin must detect this and drop prior
    // chunks + reset the checkpoint + re-index whatever's left.
    // Without this recovery the plugin's chunk indices would
    // collide with truncated bytes and Search would return stale
    // content the WAL no longer serves.
    let h = Harness::start();
    let mock = h.mock_mut();
    mock.add_dir("/");
    mock.add_dir("/logs");
    mock.add_stream(
        "/logs/wal",
        b"turn-1: OLDTOKEN\nturn-2: MIDTOKEN\nturn-3: NEWTOKEN\n",
        1,
    );

    let _ = index_root(&h.svc).await;
    let (b1, _c1) = read_stream_state(&h.zone_root, "/logs/wal");
    assert!(b1 > 0);
    // Baseline: all three tokens findable.
    for token in ["OLDTOKEN", "MIDTOKEN", "NEWTOKEN"] {
        let q = query(&h.svc, token).await;
        assert!(
            q.results.iter().any(|r| r.path == "/logs/wal"),
            "baseline token {token} must index",
        );
    }

    // Simulate retention-trim: replace the stream content with a
    // strictly shorter blob (only the newest turn survives; the
    // OLDTOKEN + MIDTOKEN turns are now trimmed away).  Bump mtime
    // so the Refresh verdict flips to Changed.
    let mock = h.mock_mut();
    // Drop the prior registration so add_stream fully replaces.
    // (The mock preserves entry_type + dirs; add_stream on an
    // existing path just overwrites the FileEntry — which is what
    // we want for a trim simulation.)
    mock.add_stream("/logs/wal", b"turn-3: NEWTOKEN\n", 2);

    let r = refresh_root(&h.svc).await;
    assert!(r.error.is_none(), "unexpected refresh error: {:?}", r.error);

    // Checkpoint must have RESET (not just advanced) — the retention
    // recovery drops prior chunks and re-chunks the shorter blob
    // from offset 0.
    let (b2, _c2) = read_stream_state(&h.zone_root, "/logs/wal");
    assert!(
        b2 < b1,
        "post-trim byte-len ({b2}) must be strictly less than pre-trim ({b1})",
    );

    // NEWTOKEN must still be findable — the fresh re-index picked it up.
    let q_new = query(&h.svc, "NEWTOKEN").await;
    assert!(
        q_new.results.iter().any(|r| r.path == "/logs/wal"),
        "post-trim survivor NEWTOKEN must be re-indexed",
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn deleted_stream_hits_stale_sweep_and_purges_checkpoint() {
    // Regression pin for the pre-fix `known_paths()` gap: it returned
    // only DT_REG keys, so a deleted DT_STREAM's chunks + checkpoint
    // lingered in the index forever.  Refresh's stale-sweep only
    // sees a cached path via `known_paths()` — if streams were
    // missing from the union, the sweep never fired for them.
    //
    // The user-visible failure without this test: query for a token
    // in a deleted stream still returns the deleted stream's path.
    let h = Harness::start();
    let mock = h.mock_mut();
    mock.add_dir("/");
    mock.add_dir("/logs");
    mock.add_stream("/logs/audit", b"turn-1: DELETEDTOKEN\n", 1);

    let _ = index_root(&h.svc).await;
    assert!(stream_state_present(&h.zone_root, "/logs/audit"));

    // Baseline: token findable before delete.
    let q = query(&h.svc, "DELETEDTOKEN").await;
    assert!(q.results.iter().any(|r| r.path == "/logs/audit"));

    // Delete the stream (remove from VFS): the walk no longer sees
    // it, so the Refresh stale-sweep must drop chunks + checkpoint.
    h.mock_mut().remove_path("/logs/audit");
    let r = refresh_root(&h.svc).await;
    assert!(r.error.is_none(), "refresh error: {:?}", r.error);
    assert!(
        r.removed_count >= 1,
        "stale-sweep must have removed the deleted stream (got removed_count={})",
        r.removed_count,
    );

    // Post-sweep: token must NOT be findable, and the checkpoint
    // must be gone from the on-disk state.
    let q = query(&h.svc, "DELETEDTOKEN").await;
    assert!(
        q.results.iter().all(|r| r.path != "/logs/audit"),
        "deleted-stream chunks must be purged; got hits: {:?}",
        q.results,
    );
    assert!(
        !stream_state_present(&h.zone_root, "/logs/audit"),
        "stream checkpoint must be dropped after stale-sweep",
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn new_stream_at_previously_deleted_path_indexes_from_offset_zero() {
    // Composite pin: if the stale-sweep failed to purge the deleted
    // stream's checkpoint (the pre-fix bug), a NEW stream later
    // created at the same path would inherit the stale
    // `indexed_byte_len` and treat everything below the old offset as
    // "already indexed" — SILENT data loss.  With the union
    // `known_paths()` + the `EntryKind::Stream` skip-record fix, the
    // reborn stream must index from offset 0.
    let h = Harness::start();
    let mock = h.mock_mut();
    mock.add_dir("/");
    mock.add_dir("/logs");
    mock.add_stream("/logs/audit", b"OLDSTREAM: content in old lifetime\n", 1);
    let _ = index_root(&h.svc).await;
    let (old_bytes, _) = read_stream_state(&h.zone_root, "/logs/audit");
    assert!(old_bytes > 0);

    // Delete + Refresh (stale-sweep purges the old lifetime).
    h.mock_mut().remove_path("/logs/audit");
    let _ = refresh_root(&h.svc).await;
    assert!(!stream_state_present(&h.zone_root, "/logs/audit"));

    // Reborn at the same path with SHORTER content than the old
    // lifetime — proves the reborn stream indexed from offset 0 and
    // did not inherit the stale checkpoint.  A stale `indexed_byte_len`
    // (old_bytes) larger than the reborn stream's length would trip
    // the retention-trim branch (which happens to also recover), so
    // instead we assert the reborn checkpoint EQUALS the reborn blob's
    // length — impossible if the stale offset had shadowed the write.
    h.mock_mut()
        .add_stream("/logs/audit", b"REBORN: new content only\n", 100);
    let r = refresh_root(&h.svc).await;
    assert!(r.error.is_none(), "refresh error: {:?}", r.error);
    let (new_bytes, new_chunks) = read_stream_state(&h.zone_root, "/logs/audit");
    assert_eq!(
        new_bytes,
        b"REBORN: new content only\n".len() as u64,
        "reborn stream must checkpoint at its own byte-length, NOT the deleted lifetime's",
    );
    assert!(new_chunks >= 1, "reborn stream must produce ≥1 chunk");

    // Old token gone, new token indexed.
    let q_old = query(&h.svc, "OLDSTREAM").await;
    assert!(
        q_old.results.iter().all(|r| r.path != "/logs/audit"),
        "old-lifetime token must not survive reborn",
    );
    let q_new = query(&h.svc, "REBORN").await;
    assert!(
        q_new.results.iter().any(|r| r.path == "/logs/audit"),
        "reborn-lifetime token must be indexed",
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn retention_trim_to_empty_records_stream_skip_in_streams_map_not_files_map() {
    // Regression pin for the SSOT invariant that MUST fire through
    // `record_content_skip(EntryKind::Stream)`.
    //
    // The earlier version of this test used a whitespace-only
    // stream, which is chunked by `index_one_stream_append`'s
    // append-path whitespace-tail branch (`stream_advance(new_len,
    // next_chunk_index, mtime)`) — it never reached
    // `record_content_skip`.  A test that does not exercise the
    // fixed code cannot pin the fix.
    //
    // This version drives the flow through the fixed code:
    //   1. Index a stream with real content — checkpoint > 0.
    //   2. Wipe the stream to zero bytes (`new_len < indexed_byte_len`).
    //   3. Refresh — retention-trim recovery fires → `index_one_stream_full`
    //      is called with `bytes: &[]` → the `bytes.is_empty()` branch
    //      → `record_content_skip(EntryKind::Stream)`.
    //
    // Pre-fix, `record_content_skip` unconditionally called
    // `state.record(path, mtime)` (DT_REG-shaped), so the stream
    // path appeared in the FILES map — violating "a path lives in
    // exactly one map".  Post-fix, the STREAMS map gets a
    // `(0, 0, mtime)` checkpoint and the FILES map stays clean.
    let h = Harness::start();
    let mock = h.mock_mut();
    mock.add_dir("/");
    mock.add_dir("/logs");
    mock.add_stream("/logs/wal", b"turn-1: ORIGINALCONTENT\nturn-2: MORE\n", 1);

    let _ = index_root(&h.svc).await;
    let (before_bytes, _) = read_stream_state(&h.zone_root, "/logs/wal");
    assert!(before_bytes > 0, "first Index must checkpoint > 0");

    // Wipe: `new_len=0 < before_bytes` triggers the retention-trim
    // recovery in `index_one_stream_append` →
    // `index_one_stream_full(bytes=[])` → the `bytes.is_empty()`
    // branch → `record_content_skip(EntryKind::Stream)`.
    mock.add_stream("/logs/wal", b"", 2);
    let r = refresh_root(&h.svc).await;
    assert!(r.error.is_none(), "refresh error: {:?}", r.error);

    assert!(
        !file_state_present(&h.zone_root, "/logs/wal"),
        "SSOT violation: `record_content_skip(EntryKind::Stream)` cross-wrote DT_STREAM path into FILES map",
    );
    assert!(
        stream_state_present(&h.zone_root, "/logs/wal"),
        "stream skip-checkpoint must remain in STREAMS map after content-skip",
    );
    let (after_bytes, after_chunks) = read_stream_state(&h.zone_root, "/logs/wal");
    assert_eq!(
        (after_bytes, after_chunks),
        (0, 0),
        "wiped-stream checkpoint must be (0, 0) — the correct 'zero chunks' shape",
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn deleted_stream_tombstone_never_lands_in_files_map() {
    // Companion pin for the `remove_one` tombstone-branch fix.
    // When ANN is unavailable during a sweep, `remove_one` keeps a
    // tombstone (`mtime = None`) so the path stays in `known_paths()`
    // and a later pass finishes the purge.  Pre-fix the tombstone
    // always went to the FILES map — even for a DT_STREAM path —
    // same cross-map class as `record_content_skip`.  This harness
    // always has an ANN sink, so `remove_one` hits the
    // successful-purge branch here; the assertion is broader — no
    // branch of `remove_one` may leave a DT_STREAM path in the
    // FILES map.
    let h = Harness::start();
    let mock = h.mock_mut();
    mock.add_dir("/");
    mock.add_dir("/logs");
    mock.add_stream("/logs/audit", b"turn-1: DELETEDCONTENT\n", 1);
    let _ = index_root(&h.svc).await;
    assert!(stream_state_present(&h.zone_root, "/logs/audit"));
    assert!(!file_state_present(&h.zone_root, "/logs/audit"));

    // Delete + Refresh → `remove_one` fires for the stream path.
    h.mock_mut().remove_path("/logs/audit");
    let _ = refresh_root(&h.svc).await;

    assert!(
        !file_state_present(&h.zone_root, "/logs/audit"),
        "SSOT violation: `remove_one` cross-wrote DT_STREAM path into FILES map",
    );
}
