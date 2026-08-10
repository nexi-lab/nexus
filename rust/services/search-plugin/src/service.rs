//! Implementation of `nexus.search.v1.SearchService`.
//!
//! The tonic async trait methods spawn onto a blocking pool because
//! the walker (`sys_readdir` + `sys_read` recursive descent) is
//! synchronous FFI into kernel-side code that may itself block on
//! metastore locks and / or federation `try_remote_fetch` RPCs.
//! Wrapping the sync body in `spawn_blocking` keeps the request off
//! the plugin's small tokio runtime and stops one heavy walk from
//! starving another gRPC request handling on the same executor.

use std::sync::Arc;

use futures_util::StreamExt;
use nexus_plugin_abi::KernelHandle;
use parking_lot::Mutex;
use tonic::{async_trait, Request, Response, Status};

use crate::ann_index::AnnHit;
use crate::embed_cache::{embed_query_cached, QueryEmbedCache};
use crate::embedder::{build_default_embedder, EmbedError, Embedder};
use crate::fts_index::FtsHit;
use crate::fusion::{self, DEFAULT_ALPHA, DEFAULT_RRF_K};
use crate::index_manager::IndexManager;
use crate::kernel_io::{self, DirEntry, KernelIoError, DT_DIR, DT_REG};
use crate::search_proto::search_service_server::SearchService;
use crate::search_proto::{
    AddIndexedDirectoryRequest, AddIndexedDirectoryResponse, BatchQueryRequest, BatchQueryResponse,
    FusionMethod, GlobRequest, GlobResponse, GrepMatch, GrepRequest, GrepResponse, HealthRequest,
    HealthResponse, IndexDocumentsRequest, IndexDocumentsResponse, IndexRequest, IndexResponse,
    ListIndexedDirectoriesRequest, ListIndexedDirectoriesResponse, ListZoneIndexingModesRequest,
    ListZoneIndexingModesResponse, LocateRequest, LocateResponse, NotifyFileChangeRequest,
    NotifyFileChangeResponse, ParkedDiscardRequest, ParkedDiscardResponse, ParkedListRequest,
    ParkedListResponse, ParkedRetryRequest, ParkedRetryResponse, QueryRequest, QueryResponse,
    QueryResult, QueryType, RefreshRequest, RefreshResponse, RemoveIndexedDirectoryRequest,
    RemoveIndexedDirectoryResponse, SetZoneIndexingModeRequest, SetZoneIndexingModeResponse,
    StatsRequest, StatsResponse,
};

/// Server-side default when the caller sends `max_results = 0`.
/// Kept generous — a well-scoped `pattern` almost never hits this,
/// and callers who want stricter limits still set them explicitly.
const DEFAULT_GLOB_MAX: usize = 10_000;
const DEFAULT_GREP_MAX: usize = 1_000;
const DEFAULT_QUERY_LIMIT: usize = 10;
const DEFAULT_INDEX_MAX_DOCS: usize = 10_000;

/// BatchQuery inner-query concurrency (#4610).  Each in-flight query
/// spawns up to two blocking tasks (hybrid's keyword + semantic legs),
/// so the ceiling stays small; `1` restores the pre-#4610 serial
/// behaviour.  Env override: `NEXUS_SEARCH_BATCH_CONCURRENCY`.
const BATCH_QUERY_CONCURRENCY_ENV: &str = "NEXUS_SEARCH_BATCH_CONCURRENCY";
const DEFAULT_BATCH_QUERY_CONCURRENCY: usize = 4;
const MAX_BATCH_QUERY_CONCURRENCY: usize = 16;

/// Kill-switch for the hybrid title arm (#4628; mirrors Python's
/// NEXUS_SEARCH_TITLE_ARM, default ON).  Read per-query so an
/// operator can flip it without a restart; "false" / "0" / "no"
/// (trimmed, case-insensitive) disable.
const TITLE_ARM_ENV: &str = "NEXUS_SEARCH_TITLE_ARM";

fn title_arm_env_enabled() -> bool {
    match std::env::var(TITLE_ARM_ENV) {
        Ok(v) => !matches!(v.trim().to_ascii_lowercase().as_str(), "false" | "0" | "no"),
        Err(_) => true,
    }
}

fn batch_query_concurrency() -> usize {
    std::env::var(BATCH_QUERY_CONCURRENCY_ENV)
        .ok()
        .and_then(|v| v.trim().parse::<usize>().ok())
        .map(|n| n.clamp(1, MAX_BATCH_QUERY_CONCURRENCY))
        .unwrap_or(DEFAULT_BATCH_QUERY_CONCURRENCY)
}

/// #4623: explicit-index incremental FTS commit cadence.  Every N
/// successfully indexed documents the FTS layer commits (and the
/// zone's result cache drops) so keyword hits become visible while
/// the embed-heavy remainder of the batch is still building.
const INDEX_INCREMENTAL_COMMIT_EVERY: usize = 8;

/// #4617: backend identity string on Stats — distinguishes this
/// generation from the deleted Python daemon's BM25S/pgvector stack.
const SEARCH_BACKEND_NAME: &str = "rust-plugin";

/// Belt-and-suspenders per-file size cap for grep — a 100 MB
/// binary blob would otherwise stall a single request for seconds.
/// Files above this size are skipped with a `tracing::debug` log.
const GREP_MAX_FILE_BYTES: usize = 8 * 1024 * 1024;

/// Same-shape cap for the P1 Index walker: one chunk per file, and
/// stuffing a 100 MB blob into a single tantivy doc would balloon
/// the writer heap AND murder BM25 scoring.  P4's chunker lifts this
/// once files are split into per-chunk documents.
const INDEX_MAX_FILE_BYTES: usize = 8 * 1024 * 1024;

/// Empty `zone_id` on a request means "the root zone" — same rule
/// the Python router uses when a token has no explicit zone scope
/// (see `nexus.contracts.constants.ROOT_ZONE_ID`).  Kept as a plain
/// string here so the plugin dep tree stays free of the wider
/// contracts crate.
const ROOT_ZONE_ID: &str = "root";

fn resolve_zone(z: &str) -> &str {
    if z.is_empty() {
        ROOT_ZONE_ID
    } else {
        z
    }
}

/// Wall-clock now in millis-since-epoch.  Broken out so the P6
/// recency scorer has a single injection point + tests can mock it
/// (via a #[cfg(test)] override) if we ever need to.  `SystemTime`
/// on a broken host clock returns 0; that's harmless — every hit's
/// age becomes very-negative, `.max(0)` clamps to 0, all hits get
/// the maximum boost.  Better than a panic on `unwrap`.
fn current_time_ms() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

pub struct SearchServiceImpl {
    handle: Arc<KernelHandle>,
    /// Per-zone `FtsIndex` + `AnnIndex` caches used by Query, Index,
    /// and SemanticQuery.  `Arc<IndexManager>` so tests can inject a
    /// manager rooted at a tempdir instead of the default.
    manager: Arc<IndexManager>,
    /// Lazy embedder slot.  `None` until the first SemanticQuery
    /// attempts to initialise; `Some(Arc<dyn Embedder>)` on success.
    /// Failed init leaves it `None` and is retried on the next call
    /// so an operator setting `NEXUS_SEARCH_MODEL_DIR` mid-run
    /// unblocks semantic search without a plugin restart.
    embedder_slot: Arc<Mutex<Option<Arc<dyn Embedder>>>>,
    /// P7 zone-scoped result cache.  Query checks it before
    /// dispatch; Index + Refresh invalidate the target zone so
    /// callers who mutated the corpus don't see stale results.
    query_cache: crate::query_cache::SharedQueryCache,
    /// Query-embedding cache (#4610).  Distinct from `query_cache`:
    /// that one keys on the FULL request (including `path_filter`),
    /// so a path-scoped fan-out sending the same q across N prefixes
    /// misses it N times — but all N share one embedding, and
    /// `FastEmbedder` serialises embeds on a single session mutex.
    /// Embeddings have no corpus dependency, so Index / Refresh do
    /// not invalidate this cache.
    embed_cache: Arc<QueryEmbedCache>,
    /// #4623: in-flight explicit Index / IndexDocuments / Refresh
    /// operations.  Surfaced on Stats as `indexing_in_progress` so
    /// pollers can tell "genuinely empty" from "still building".
    indexing_ops: Arc<std::sync::atomic::AtomicU32>,
    /// Builder override for the hybrid title arm (#4628) — `None` ⇒
    /// read `NEXUS_SEARCH_TITLE_ARM` per query (production);
    /// `Some(_)` pins it (tests must not race the process env).
    title_arm: Option<bool>,
}

/// RAII increment of [`SearchServiceImpl::indexing_ops`].
///
/// MUST be moved INTO the `spawn_blocking` closure doing the actual
/// index mutation: a guard owned by the async RPC future would be
/// dropped on RPC cancellation/timeout while the already-started
/// blocking task keeps mutating the indices — understating
/// `indexing_in_progress` during exactly the partial-build window
/// the counter exists to expose (#4623 review R1).
struct IndexingGuard(Arc<std::sync::atomic::AtomicU32>);

impl IndexingGuard {
    fn enter(counter: &Arc<std::sync::atomic::AtomicU32>) -> Self {
        counter.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        Self(Arc::clone(counter))
    }
}

impl Drop for IndexingGuard {
    fn drop(&mut self) {
        self.0.fetch_sub(1, std::sync::atomic::Ordering::SeqCst);
    }
}

impl SearchServiceImpl {
    /// Production constructor — index manager rooted at the platform
    /// default (`$NEXUS_DATA_DIR/plugins/search/`).  Embedder init
    /// is deferred to the first SemanticQuery (D3).
    pub fn new(handle: Arc<KernelHandle>) -> Self {
        Self::builder(handle).build()
    }

    /// Start a builder for a customised SearchServiceImpl.  All
    /// non-required knobs default to the same values `new` uses;
    /// tests + operators override just the fields they care about
    /// via chained setters.  Avoids the previous
    /// `with_manager` / `with_manager_and_embedder` /
    /// `with_manager_embedder_and_cache` constructor explosion.
    pub fn builder(handle: Arc<KernelHandle>) -> SearchServiceBuilder {
        SearchServiceBuilder {
            handle,
            manager: None,
            embedder: None,
            query_cache: None,
            embed_cache: None,
            title_arm: None,
        }
    }

    /// Whether the hybrid title arm runs for this query — builder
    /// pin wins; otherwise the env knob (default on).
    fn title_arm_enabled(&self) -> bool {
        self.title_arm.unwrap_or_else(title_arm_env_enabled)
    }

    /// Fetch (or lazily initialise) the embedder.  Fast path: read
    /// the mutex, clone the Arc, return.  Slow path (first call, or
    /// after a failed init): build via [`build_default_embedder`]
    /// OUTSIDE the mutex so a 300 ms – 1 s ONNX session build does
    /// not block concurrent Query / Index requests.  Two concurrent
    /// first-callers duplicate work but both succeed — a rare cost
    /// worth paying to keep the lock's critical section short.
    fn get_or_init_embedder(&self) -> Result<Arc<dyn Embedder>, EmbedError> {
        if let Some(e) = self.embedder_slot.lock().as_ref() {
            return Ok(Arc::clone(e));
        }
        let data_root = self.manager.root().to_path_buf();
        let built = build_default_embedder(&data_root)?;
        // Race-check: another thread may have won.
        let mut slot = self.embedder_slot.lock();
        if let Some(existing) = slot.as_ref() {
            return Ok(Arc::clone(existing));
        }
        *slot = Some(Arc::clone(&built));
        Ok(built)
    }

    /// Embedder resolution for INDEXING paths (review R2).  Returns
    /// `(embedder, embed_broken)` — distinguishing "no embedder
    /// configured" (clean NotAvailable ⇒ keyword-only mode, docs
    /// record their real mtime) from "configured but failed to
    /// initialise" (Load/Runtime ⇒ `embed_broken = true`, every doc
    /// indexed this pass records mtime None so the next
    /// Refresh/IndexDocuments retries its vectors once the embedder
    /// recovers, instead of the failure minting permanently
    /// keyword-only documents behind a success response).
    fn indexing_embedder(&self) -> (Option<Arc<dyn Embedder>>, bool) {
        match self.get_or_init_embedder() {
            Ok(e) => (Some(e), false),
            Err(EmbedError::NotAvailable(_)) => (None, false),
            Err(e) => {
                tracing::warn!(
                    err = %e,
                    "embedder init failed — this pass's docs stay ANN-retryable",
                );
                (None, true)
            }
        }
    }
}

/// Fluent builder for [`SearchServiceImpl`].  Every knob is
/// optional; unset knobs get the same defaults [`new`] uses.
///
/// Replaces the earlier `with_manager` / `with_manager_and_embedder`
/// / `with_manager_embedder_and_cache` triad — one builder covers
/// every combination of overrides without a combinatorial
/// constructor blow-up when the next knob lands.
pub struct SearchServiceBuilder {
    handle: Arc<KernelHandle>,
    manager: Option<Arc<IndexManager>>,
    embedder: Option<Arc<dyn Embedder>>,
    query_cache: Option<crate::query_cache::SharedQueryCache>,
    embed_cache: Option<Arc<QueryEmbedCache>>,
    title_arm: Option<bool>,
}

impl SearchServiceBuilder {
    /// Inject a pre-configured `IndexManager` (typically rooted at
    /// a tempdir for tests, or at an explicit data volume for
    /// operators overriding the default storage location).
    pub fn manager(mut self, manager: Arc<IndexManager>) -> Self {
        self.manager = Some(manager);
        self
    }

    /// Pre-seed the embedder slot so SemanticQuery / Hybrid skip
    /// the [`build_default_embedder`] discovery step.  Integration
    /// tests use this to inject a
    /// [`MockEmbedder`](crate::embedder::MockEmbedder) without
    /// pointing at real model files.
    pub fn embedder(mut self, embedder: Arc<dyn Embedder>) -> Self {
        self.embedder = Some(embedder);
        self
    }

    /// Inject a shared `QueryCache` — used by tests that need a
    /// shorter TTL than the 5-minute default so cache expiry is
    /// observable within a few seconds.
    pub fn query_cache(mut self, cache: crate::query_cache::SharedQueryCache) -> Self {
        self.query_cache = Some(cache);
        self
    }

    /// Inject a query-embedding cache (#4610) — tests use a tiny or
    /// zero capacity to make hit / bypass behaviour observable.
    pub fn embed_cache(mut self, cache: Arc<QueryEmbedCache>) -> Self {
        self.embed_cache = Some(cache);
        self
    }

    /// Pin the title arm on/off, bypassing NEXUS_SEARCH_TITLE_ARM —
    /// for tests that must not race the process environment.
    pub fn title_arm(mut self, enabled: bool) -> Self {
        self.title_arm = Some(enabled);
        self
    }

    pub fn build(self) -> SearchServiceImpl {
        SearchServiceImpl {
            handle: self.handle,
            manager: self
                .manager
                .unwrap_or_else(|| Arc::new(IndexManager::new())),
            embedder_slot: Arc::new(Mutex::new(self.embedder)),
            query_cache: self
                .query_cache
                .unwrap_or_else(|| Arc::new(crate::query_cache::QueryCache::new())),
            embed_cache: self
                .embed_cache
                .unwrap_or_else(|| Arc::new(QueryEmbedCache::from_env())),
            indexing_ops: Arc::new(std::sync::atomic::AtomicU32::new(0)),
            title_arm: self.title_arm,
        }
    }
}

// ── Glob ──────────────────────────────────────────────────────────

fn do_glob(
    handle: &KernelHandle,
    root_path: &str,
    pattern: &str,
    max_results: usize,
    sort_recency: bool,
) -> Result<(Vec<String>, bool), String> {
    // Empty pattern ⇒ match everything (walk-and-list mode).  Callers
    // that literally want no matches send an obviously-unmatchable
    // pattern; the empty string is the more useful default.
    let matcher = if pattern.is_empty() {
        None
    } else {
        Some(
            globset::Glob::new(pattern)
                .map_err(|e| format!("invalid glob pattern {pattern:?}: {e}"))?
                .compile_matcher(),
        )
    };

    let mut out = Vec::new();
    let mut truncated = false;

    walk_recursive(handle, root_path, &mut |vfs_path, entry_type| {
        if out.len() >= max_results {
            truncated = true;
            return WalkAction::Stop;
        }
        // Match against the path RELATIVE to `root_path` — this is
        // what globset patterns naturally target (`docs/*.md` reads
        // relative to the walk root).  The RESPONSE, however, carries
        // the absolute vfs_path (line below + search.proto's
        // GlobResponse.paths contract, matching GrepMatch.path so
        // callers decode both rpcs' path outputs with one rule).
        let relative = strip_root(root_path, vfs_path);
        let matched = matcher
            .as_ref()
            .map(|m| m.is_match(relative))
            .unwrap_or(true);
        if matched {
            // Skip pure dirs in the returned list — glob is
            // file-oriented per the Python `search_service.glob`
            // contract.  Callers who need dir listing use
            // `sys_readdir` directly.
            if entry_type != DT_DIR {
                out.push(vfs_path.to_string());
            }
        }
        WalkAction::Continue
    })
    .map_err(walk_err_to_string)?;

    if sort_recency {
        sort_paths_by_mtime_desc(handle, &mut out);
    }

    Ok((out, truncated))
}

// ── Grep ──────────────────────────────────────────────────────────

// Same rationale as `grep_scan` above — the 9-arg signature is the
// wire request unpacked; clustering into a struct hides the intent
// at the call site.
#[allow(clippy::too_many_arguments)]
fn do_grep(
    handle: &KernelHandle,
    root_path: &str,
    pattern: &str,
    file_pattern: &str,
    ignore_case: bool,
    max_results: usize,
    before_context: usize,
    after_context: usize,
    invert_match: bool,
    sort_recency: bool,
) -> Result<(Vec<GrepMatch>, bool), String> {
    if pattern.is_empty() {
        return Err("grep pattern must not be empty".into());
    }

    let re = regex::RegexBuilder::new(pattern)
        .case_insensitive(ignore_case)
        .build()
        .map_err(|e| format!("invalid regex {pattern:?}: {e}"))?;

    let file_matcher = if file_pattern.is_empty() {
        None
    } else {
        Some(
            globset::Glob::new(file_pattern)
                .map_err(|e| format!("invalid file_pattern {file_pattern:?}: {e}"))?
                .compile_matcher(),
        )
    };

    let mut matches: Vec<GrepMatch> = Vec::new();
    let mut truncated = false;

    walk_recursive(handle, root_path, &mut |vfs_path, entry_type| {
        if matches.len() >= max_results {
            truncated = true;
            return WalkAction::Stop;
        }
        if entry_type != DT_REG {
            return WalkAction::Continue;
        }
        let relative = strip_root(root_path, vfs_path);
        if let Some(fm) = &file_matcher {
            if !fm.is_match(relative) {
                return WalkAction::Continue;
            }
        }

        match kernel_io::sys_read(handle, vfs_path) {
            Ok(bytes) => {
                if bytes.len() > GREP_MAX_FILE_BYTES {
                    tracing::debug!(
                        path = %vfs_path,
                        size = bytes.len(),
                        cap = GREP_MAX_FILE_BYTES,
                        "grep: skipping oversized file",
                    );
                    return WalkAction::Continue;
                }
                let text = match std::str::from_utf8(&bytes) {
                    Ok(s) => s,
                    Err(_) => {
                        // Binary file — skip silently (mirrors GNU grep
                        // default behaviour where `--binary-files=skip`
                        // is the safe common case).
                        return WalkAction::Continue;
                    }
                };
                grep_scan(
                    text,
                    vfs_path,
                    &re,
                    before_context,
                    after_context,
                    invert_match,
                    max_results,
                    &mut matches,
                    &mut truncated,
                );
                if truncated {
                    WalkAction::Stop
                } else {
                    WalkAction::Continue
                }
            }
            Err(KernelIoError::NotFound) => WalkAction::Continue,
            Err(e) => {
                tracing::warn!(
                    path = %vfs_path,
                    err = ?e,
                    "grep: sys_read failed — skipping file",
                );
                WalkAction::Continue
            }
        }
    })
    .map_err(walk_err_to_string)?;

    if sort_recency {
        sort_matches_by_mtime_desc(handle, &mut matches);
    }

    Ok((matches, truncated))
}

// `grep_scan` is a plain buffer walker — 9 args are the request
// contract (pattern + 2 context sizes + invert + cap + out slot +
// truncated slot) plus the file path stamped into each match.
// Splitting into a config struct would cost the clarity of the
// inline arg names at each call site.
#[allow(clippy::too_many_arguments)]
fn grep_scan(
    text: &str,
    path: &str,
    re: &regex::Regex,
    before_context: usize,
    after_context: usize,
    invert_match: bool,
    max_results: usize,
    out: &mut Vec<GrepMatch>,
    truncated: &mut bool,
) {
    // `str::lines` handles the trailing-newline case correctly
    // ("a\nb\n" ⇒ ["a", "b"]) — `split('\n')` would yield a
    // spurious empty tail line that invert-match would then treat
    // as a hit.  Do not switch back to `split`.
    let lines: Vec<&str> = text.lines().collect();
    for (idx, line) in lines.iter().enumerate() {
        if out.len() >= max_results {
            *truncated = true;
            return;
        }
        let hit = re.is_match(line);
        let want = if invert_match { !hit } else { hit };
        if !want {
            continue;
        }
        let before_start = idx.saturating_sub(before_context);
        let after_end = (idx + 1 + after_context).min(lines.len());
        let before: Vec<String> = lines[before_start..idx]
            .iter()
            .map(|s| s.to_string())
            .collect();
        let after: Vec<String> = lines[idx + 1..after_end]
            .iter()
            .map(|s| s.to_string())
            .collect();
        out.push(GrepMatch {
            path: path.to_string(),
            line_number: (idx as u32) + 1,
            line: line.to_string(),
            before,
            after,
        });
    }
}

// ── Recursive walker (sync, uses KernelHandle FFI) ────────────────

#[derive(Clone, Copy, PartialEq, Eq)]
enum WalkAction {
    Continue,
    Stop,
}

/// Recursively walk `root_path` via `sys_readdir`; invoke `visit` on
/// every entry (files AND dirs) with its full VFS path + entry_type.
///
/// The walker is DFS pre-order (yield entry, then recurse if it's a
/// dir).  Errors on a specific sub-tree are logged and the walker
/// continues — a permission-denied sub-dir does not abort the whole
/// walk.  A `NotFound` on `root_path` itself is bubbled up.
fn walk_recursive(
    handle: &KernelHandle,
    root_path: &str,
    visit: &mut dyn FnMut(&str, u8) -> WalkAction,
) -> Result<(), KernelIoError> {
    // Root-level readdir has to succeed OR we bubble the error.  A
    // NotFound here means the caller pointed us at nothing.
    let root_entries = kernel_io::sys_readdir(handle, root_path)?;
    walk_entries(handle, root_path, root_entries, visit);
    Ok(())
}

fn walk_entries(
    handle: &KernelHandle,
    parent: &str,
    entries: Vec<DirEntry>,
    visit: &mut dyn FnMut(&str, u8) -> WalkAction,
) -> WalkAction {
    for entry in entries {
        let child_path = kernel_io::join_vfs_path(parent, &entry.name);
        if visit(&child_path, entry.entry_type) == WalkAction::Stop {
            return WalkAction::Stop;
        }
        // Recurse into DT_DIR + DT_MOUNT (we walk THROUGH mounts
        // per the filesystem invariant that a mount replaces the
        // directory's contents; from the search plugin's view a
        // mount is a container of children just like a dir).
        if entry.entry_type == DT_DIR || entry.entry_type == kernel_io::DT_MOUNT {
            match kernel_io::sys_readdir(handle, &child_path) {
                Ok(child_entries) => {
                    if walk_entries(handle, &child_path, child_entries, visit) == WalkAction::Stop {
                        return WalkAction::Stop;
                    }
                }
                Err(KernelIoError::NotFound) => {
                    // Race with a concurrent unlink / unmount — skip.
                }
                Err(e) => {
                    tracing::warn!(
                        path = %child_path,
                        err = ?e,
                        "walk: sys_readdir failed — skipping subtree",
                    );
                }
            }
        }
    }
    WalkAction::Continue
}

fn strip_root<'a>(root: &str, path: &'a str) -> &'a str {
    let trimmed = root.trim_end_matches('/');
    if let Some(rest) = path.strip_prefix(trimmed) {
        rest.trim_start_matches('/')
    } else {
        path.trim_start_matches('/')
    }
}

fn walk_err_to_string(e: KernelIoError) -> String {
    match e {
        KernelIoError::NotFound => "root_path not found".into(),
        other => format!("kernel io: {other:?}"),
    }
}

// ── Recency sort (Issue #4553 mirror — the SPIRIT of the Python
// SearchDaemon's recency=on mode adapted to a scoreless enumeration
// API).  Glob has no fusion score to multiplicatively boost, so
// "freshness" here means "newer files sort first".  One sys_stat per
// UNIQUE path (grep may return many matches per file); paths with
// unknown mtime sort last so they never leapfrog dated results.
// Stable Timsort inside `sort_by` preserves encounter order among
// same-mtime items — matches from one file stay contiguous under
// grep even after the sort. ──────────────────────────────────────

/// Sort file paths by containing-file mtime descending (newest first).
/// Unknown-mtime paths sort last.
fn sort_paths_by_mtime_desc(handle: &KernelHandle, paths: &mut [String]) {
    let mtimes = fetch_mtimes(handle, paths.iter().map(|p| p.as_str()));
    // i64::MIN sinks unknown-mtime items to the end when sorting DESC.
    paths.sort_by_key(|p| std::cmp::Reverse(mtimes.get(p.as_str()).copied().unwrap_or(i64::MIN)));
}

/// Sort grep matches by containing-file mtime descending.  Matches
/// from the same file share an mtime key; stable sort preserves
/// their encounter (line) order.
fn sort_matches_by_mtime_desc(handle: &KernelHandle, matches: &mut [GrepMatch]) {
    let mtimes = fetch_mtimes(handle, matches.iter().map(|m| m.path.as_str()));
    matches.sort_by_key(|m| {
        std::cmp::Reverse(mtimes.get(m.path.as_str()).copied().unwrap_or(i64::MIN))
    });
}

/// Batch-fetch `modified_at_ms` for a set of paths.  Deduplicates via
/// the returned HashMap.  Failures (NotFound, kernel error, JSON
/// parse error, null mtime) silently drop the path — the sort then
/// bins it into the unknown-mtime tail rather than aborting.
fn fetch_mtimes<'a>(
    handle: &KernelHandle,
    paths: impl IntoIterator<Item = &'a str>,
) -> std::collections::HashMap<String, i64> {
    let mut seen: std::collections::HashSet<&str> = std::collections::HashSet::new();
    let mut out: std::collections::HashMap<String, i64> = std::collections::HashMap::new();
    for path in paths {
        if !seen.insert(path) {
            continue;
        }
        match kernel_io::sys_stat(handle, path) {
            Ok(info) => {
                if let Some(ms) = info.modified_at_ms {
                    out.insert(path.to_string(), ms);
                }
            }
            Err(e) => {
                tracing::debug!(
                    path = %path,
                    err = ?e,
                    "recency sort: sys_stat failed — treating as unknown mtime",
                );
            }
        }
    }
    out
}

// ── Query / SemanticQuery / Index ─────────────────────────────────

/// BM25 keyword search over the per-zone FTS index (Phase 1).
fn do_keyword_query(
    manager: &IndexManager,
    q: &str,
    zone_id: &str,
    limit: usize,
    path_filter: &str,
) -> Result<Vec<QueryResult>, String> {
    let index = manager
        .get_or_open(zone_id)
        .map_err(|e| format!("open index for zone {zone_id:?}: {e}"))?;

    let prefix = if path_filter.is_empty() {
        None
    } else {
        Some(path_filter)
    };

    let hits = index
        .search(q, limit, prefix)
        .map_err(|e| format!("search: {e}"))?;

    Ok(hits
        .into_iter()
        .map(|h| fts_hit_to_result(h, zone_id))
        .collect())
}

/// Vector-similarity search over the per-zone HNSW index (Phase 2).
/// Embeds `q` via the caller-supplied embedder, opens the ANN index
/// tagged with the embedder's `tag()`, runs top-k, and materialises
/// the FTS-stored fields (chunk_text, mtime_ms) so results carry the
/// same shape as keyword hits.
fn do_semantic_query(
    manager: &IndexManager,
    embedder: &Arc<dyn Embedder>,
    embed_cache: &QueryEmbedCache,
    q: &str,
    zone_id: &str,
    limit: usize,
    path_filter: &str,
) -> Result<Vec<QueryResult>, String> {
    let ann = manager
        .get_or_open_ann(zone_id, embedder.tag(), embedder.dim())
        .map_err(|e| format!("open ann for zone {zone_id:?}: {e}"))?;

    // #4610: cached — a fan-out repeating the same q across path
    // filters embeds once instead of serialising N times on the
    // embedder's session mutex.
    let query_vec = embed_query_cached(embedder.as_ref(), embed_cache, q)?;

    // Over-fetch when a path prefix is set — the post-scoring
    // filter would otherwise underfill the response.
    let fetch = if path_filter.is_empty() {
        limit
    } else {
        limit.saturating_mul(4).max(limit)
    };
    let ann_hits = ann
        .search(&query_vec, fetch)
        .map_err(|e| format!("ann search: {e}"))?;

    // Materialise chunk_text + mtime via the FTS index — the ANN
    // stores only vectors + paths, but the RPC contract carries the
    // full QueryResult shape so callers don't need a follow-up read.
    // A missing FTS row (drift scenario) leaves chunk_text empty;
    // the path itself is still meaningful.
    let fts = manager.get_or_open(zone_id).ok();

    let mut out = Vec::with_capacity(limit);
    for hit in ann_hits {
        if !path_filter.is_empty() && !hit.path.starts_with(path_filter) {
            continue;
        }
        out.push(enrich_ann_hit(fts.as_deref(), hit, zone_id));
        if out.len() >= limit {
            break;
        }
    }
    Ok(out)
}

fn enrich_ann_hit(
    fts: Option<&crate::fts_index::FtsIndex>,
    hit: AnnHit,
    zone_id: &str,
) -> QueryResult {
    // Score = 1 - cosine distance so higher = closer, matching the
    // BM25 "higher is better" convention keyword callers already
    // rely on.  ANN returns distance in [0, 2]; score falls in [-1, 1].
    let score = 1.0 - hit.distance;
    let (chunk_text, mtime_ms) = fts
        .and_then(|f| lookup_fts_by_path(f, &hit.path))
        .unwrap_or((String::new(), None));
    QueryResult {
        path: hit.path,
        chunk_index: hit.chunk_index,
        chunk_text,
        score,
        zone_id: zone_id.to_string(),
        mtime_ms,
        expanded_context: String::new(),
        title_score: None,
    }
}

/// Look up an FTS row by exact path via the STRING-indexed `path`
/// field's `TermQuery` (see `FtsIndex::get_by_path`).  Returns
/// `None` on miss (e.g. drift where ANN has the path but FTS was
/// pruned).  Cheap term-lookup — does not depend on the BM25
/// tokenisation matching any part of the path.
fn lookup_fts_by_path(
    fts: &crate::fts_index::FtsIndex,
    path: &str,
) -> Option<(String, Option<i64>)> {
    fts.get_by_path(path)
        .ok()
        .flatten()
        .map(|h| (h.chunk_text, h.mtime_ms))
}

fn fts_hit_to_result(hit: FtsHit, zone_id: &str) -> QueryResult {
    QueryResult {
        path: hit.path,
        chunk_index: hit.chunk_index,
        chunk_text: hit.chunk_text,
        score: hit.score,
        zone_id: zone_id.to_string(),
        mtime_ms: hit.mtime_ms,
        expanded_context: String::new(),
        title_score: None,
    }
}

/// Bundled fusion knobs from the wire — parsed once at the RPC
/// handler and forwarded to `do_hybrid_query`.  Zero-valued fields
/// resolve to Python-parity defaults inside this struct so
/// downstream code doesn't need to remember the sentinel rules.
#[derive(Debug, Clone, Copy)]
struct FusionOpts {
    method: FusionMethod,
    alpha: f32,
    rrf_k: u32,
    chunks_per_page: u32,
}

impl FusionOpts {
    fn from_request(req: &QueryRequest) -> Self {
        let method = FusionMethod::try_from(req.fusion_method).unwrap_or(FusionMethod::Unspecified);
        // Wire zero on either float means "server default" per D3
        // (matches Python's "None => config default" fallback).
        let alpha = if req.alpha == 0.0 {
            DEFAULT_ALPHA
        } else {
            req.alpha
        };
        let rrf_k = if req.rrf_k == 0 {
            DEFAULT_RRF_K
        } else {
            req.rrf_k
        };
        Self {
            method,
            alpha,
            rrf_k,
            chunks_per_page: req.chunks_per_page,
        }
    }
}

/// Read-side context expansion mode (P4, #4398).  Sourced from
/// `QueryRequest.expand`; unknown values fall back to `None` so a
/// forgetful caller gets the pre-P4 shape.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ExpandMode {
    /// No enrichment; `QueryResult.expanded_context` stays empty.
    None,
    /// Fill `expanded_context` with `prev + current + next` chunk
    /// text under the same path.
    Macro,
}

impl ExpandMode {
    fn from_str(s: &str) -> Self {
        match s {
            "macro" => Self::Macro,
            _ => Self::None,
        }
    }
}

/// Fill `expanded_context` on each hit when the caller asked for
/// `expand=macro`.  For each hit we fetch the file's full chunk
/// set via [`FtsIndex::get_chunks_by_path`] (cheap; typical files
/// have < 20 chunks) and concatenate previous + current + next.
/// A hit whose file only has one chunk gets an empty
/// `expanded_context` — same shape as `ExpandMode::None`, so
/// callers don't need to special-case.
///
/// Chunks-per-path cache within one Query call: a hit at
/// (path, chunk 2) and another at (path, chunk 5) would otherwise
/// each re-fetch the file's chunk set.  The cache avoids that.
fn apply_expand(manager: &IndexManager, zone_id: &str, mode: ExpandMode, hits: &mut [QueryResult]) {
    if matches!(mode, ExpandMode::None) {
        return;
    }
    let Ok(fts) = manager.get_or_open(zone_id) else {
        return;
    };
    let mut cache: std::collections::HashMap<String, Vec<crate::fts_index::FtsHit>> =
        std::collections::HashMap::new();
    for hit in hits.iter_mut() {
        let chunks = cache
            .entry(hit.path.clone())
            .or_insert_with(|| fts.get_chunks_by_path(&hit.path).unwrap_or_default());
        if chunks.len() <= 1 {
            continue;
        }
        let mut assembled = String::new();
        let mut wrote_any = false;
        for c in chunks.iter() {
            let delta = c.chunk_index as i64 - hit.chunk_index as i64;
            if (-1..=1).contains(&delta) {
                if wrote_any {
                    assembled.push_str("\n\n");
                }
                assembled.push_str(&c.chunk_text);
                wrote_any = true;
            }
        }
        if wrote_any {
            hit.expanded_context = assembled;
        }
    }
}

/// Over-fetch multiplier per source for hybrid.  A doc that ranks
/// 8 in keyword and 9 in semantic scores highly under RRF, but
/// only surfaces if BOTH sources return it — so each side fetches
/// more than the caller-visible `limit` to give fusion headroom.
const HYBRID_OVER_FETCH_MULT: usize = 2;

/// Over-fetch multiplier when POST-FUSION score adjustments (recency,
/// path-prefix boosts) are active (review R4).  Prefix weights are
/// capped at 10x by the path-contexts store, so a 10x candidate pool
/// covers any doc whose base score is within one full boost of the
/// unadjusted cut line.  This is a pragmatic bound, not a proof — a
/// doc scoring >10x below the cut can still be unreachable; the
/// alternative (applying boosts inside candidate collection) means
/// pushing operator config into the tantivy/HNSW scorers and is
/// tracked as follow-up work.  Clamped so limit=100 doesn't fan a
/// 1000-doc fetch into the blocking pool.
const ADJUSTMENT_OVER_FETCH_MULT: usize = 10;
const ADJUSTMENT_FETCH_CEILING: usize = 500;

/// Cap on per-query representative-chunk fetches for uncovered
/// title hits (Python TITLE_ARM_MAX_HYDRATION_FETCH).  Hits beyond
/// it degrade to chunk_text="" instead of unbounded FTS lookups.
const TITLE_ARM_MAX_HYDRATION_FETCH: usize = 32;

/// Run the title arm's locate: skeleton lookup (building it on
/// first use per index generation) + evidence gate.  Fail-soft —
/// a skeleton build error degrades to an empty arm with a debug
/// log; the search itself must never fail on the arm's account.
fn do_title_locate(
    manager: &IndexManager,
    q: &str,
    zone_id: &str,
    limit: usize,
    path_filter: &str,
) -> Vec<crate::title_index::TitleHit> {
    match manager.get_or_build_skeleton(zone_id) {
        Ok(skeleton) => {
            let prefix = (!path_filter.is_empty()).then_some(path_filter);
            let mut hits = skeleton.locate(q, limit, prefix);
            // Evidence gate: a lone incidental path-token overlap
            // (score 1.0) must not earn rank-based RRF votes;
            // require at least one real title-token match (2.0).
            hits.retain(|h| h.score >= crate::title_index::TITLE_ARM_MIN_SCORE);
            hits
        }
        Err(e) => {
            tracing::debug!(err = %e, zone = %zone_id, "title arm: skeleton unavailable — degrading");
            Vec::new()
        }
    }
}

/// Hydrate locate() hits to the QueryResult shape for fusion.  The
/// fusion identity is (path, chunk_index), so each hit needs a
/// representative chunk: borrow the best-scored keyword-leg row for
/// the path (aligns the key so RRF votes accumulate on one fused
/// entry instead of splitting), dense rows as the lowest-priority
/// borrow source, then a capped FTS chunk-0 fetch for still-
/// uncovered paths.  A doc with no FTS row (drift) stays
/// retrievable with empty text.  `score` = the locate score —
/// rrf_multi turns it into rank votes and stamps it as title_score
/// attribution.
fn hydrate_title_hits(
    fts: Option<&crate::fts_index::FtsIndex>,
    title_hits: &[crate::title_index::TitleHit],
    keyword: &[QueryResult],
    semantic: &[QueryResult],
    zone_id: &str,
) -> Vec<QueryResult> {
    let mut best_by_path: std::collections::HashMap<&str, &QueryResult> =
        std::collections::HashMap::new();
    for r in keyword {
        match best_by_path.get(r.path.as_str()) {
            Some(cur) if cur.score >= r.score => {}
            _ => {
                best_by_path.insert(&r.path, r);
            }
        }
    }
    let mut best_dense: std::collections::HashMap<&str, &QueryResult> =
        std::collections::HashMap::new();
    for r in semantic {
        match best_dense.get(r.path.as_str()) {
            Some(cur) if cur.score >= r.score => {}
            _ => {
                best_dense.insert(&r.path, r);
            }
        }
    }
    for (path, r) in best_dense {
        best_by_path.entry(path).or_insert(r);
    }

    let mut fetched: std::collections::HashMap<&str, crate::fts_index::FtsHit> =
        std::collections::HashMap::new();
    if let Some(fts) = fts {
        let uncovered: Vec<&str> = title_hits
            .iter()
            .map(|h| h.path.as_str())
            .filter(|p| !best_by_path.contains_key(*p))
            .take(TITLE_ARM_MAX_HYDRATION_FETCH)
            .collect();
        for path in uncovered {
            match fts.get_chunks_by_path(path) {
                // Sorted by chunk_index — first entry is chunk 0.
                Ok(chunks) => {
                    if let Some(c0) = chunks.into_iter().next() {
                        fetched.insert(path, c0);
                    }
                }
                Err(e) => tracing::debug!(
                    err = %e, path = %path,
                    "title arm: representative-chunk fetch failed — empty text",
                ),
            }
        }
    }

    title_hits
        .iter()
        .map(|h| {
            let (chunk_index, chunk_text, mtime_ms) =
                if let Some(leg) = best_by_path.get(h.path.as_str()) {
                    (leg.chunk_index, leg.chunk_text.clone(), leg.mtime_ms)
                } else if let Some(row) = fetched.get(h.path.as_str()) {
                    (row.chunk_index, row.chunk_text.clone(), row.mtime_ms)
                } else {
                    (0, String::new(), None)
                };
            QueryResult {
                path: h.path.clone(),
                chunk_index,
                chunk_text,
                score: h.score,
                zone_id: zone_id.to_string(),
                mtime_ms,
                expanded_context: String::new(),
                // Stamped by rrf_multi per arm vote, not here — so
                // merged chunk-arm entries get it too.
                title_score: None,
            }
        })
        .collect()
}

/// Fuse two source result lists per the caller's chosen method,
/// pool per-doc, and truncate to `limit`.  Pure math — the two
/// source lists come in already fetched.  The RPC handler runs
/// the fetches in parallel then hands them here.
fn fuse_hybrid(
    keyword: Vec<QueryResult>,
    semantic: Vec<QueryResult>,
    limit: usize,
    opts: FusionOpts,
) -> Vec<QueryResult> {
    let fused = match opts.method {
        FusionMethod::Unspecified | FusionMethod::Rrf => {
            fusion::rrf(&keyword, &semantic, opts.rrf_k)
        }
        FusionMethod::Weighted => fusion::weighted(&keyword, &semantic, opts.alpha),
        FusionMethod::RrfWeighted => {
            fusion::rrf_weighted(&keyword, &semantic, opts.rrf_k, opts.alpha)
        }
    };
    let pooled = fusion::pool_by_document(fused, opts.chunks_per_page);
    pooled.into_iter().take(limit).collect()
}

/// Sinks the Index walker writes into.  `fts` is required (Index
/// always populates the keyword index); `ann` + `embedder` are
/// optional so a slim deployment or a mid-boot with no embedder
/// still gets keyword search — semantic just returns zero hits
/// until Index is retried with the embedder wired up.  `state` is
/// the P5 mtime cache — updated when a file is added or dropped so
/// the next Refresh's diff pass has an accurate baseline.
struct IndexSinks<'a> {
    fts: &'a Arc<crate::fts_index::FtsIndex>,
    ann: Option<&'a Arc<crate::ann_index::AnnIndex>>,
    embedder: Option<&'a Arc<dyn Embedder>>,
    state: &'a crate::index_state::IndexState,
    /// True when an embedder IS configured but failed to initialise
    /// (review R2).  Distinct from `embedder: None` with a clean
    /// NotAvailable (keyword-only mode): a broken embedder means
    /// every doc indexed this pass must stay ANN-retryable (mtime
    /// None) so vectors land once the embedder recovers.
    embed_broken: bool,
    /// Does the zone have any `ann-*` directory on disk?  Content
    /// skips and the sweep consult this to tell "no vectors exist"
    /// (safe to finalize) from "vectors exist but the ANN sink is
    /// unreachable" (keep a retry tombstone) — review R6.
    zone_has_ann: bool,
}

/// Walk `root_path` and index every regular file.  Same walker Glob +
/// Grep use — `sys_readdir` for enumeration, `sys_read` for content,
/// `sys_stat` for mtime.  P1 = one chunk per file (path is the FTS
/// primary key, `add_document` is idempotent so re-indexing replaces
/// the prior doc); P4's chunker splits per file into multiple docs.
///
/// When `embedder` + `ann` are both present the walker also embeds
/// the file's text and adds it to the vector index — semantic +
/// keyword stay in sync from one call.  Missing embedder ⇒ FTS-only
/// (log a debug once per Index call so operators see it).
// 8 args: the R2 embed_broken flag pushed this over clippy's 7-arg
// default.  The alternatives (params struct for two call sites, or
// folding embed_broken into the Option) obscure more than they help.
#[allow(clippy::too_many_arguments)]
fn do_index(
    handle: &KernelHandle,
    manager: &IndexManager,
    embedder: Option<&Arc<dyn Embedder>>,
    embed_broken: bool,
    root_path: &str,
    zone_id: &str,
    recursive: bool,
    max_docs: usize,
) -> Result<(u32, u32), String> {
    // Serialize writers per zone: state is opened fresh and saved
    // whole at the end, so concurrent mutations would lose entries.
    let zone_lock = manager.zone_write_lock(zone_id);
    let _zone_guard = zone_lock.lock();

    let fts = manager
        .get_or_open(zone_id)
        .map_err(|e| format!("open index for zone {zone_id:?}: {e}"))?;

    // Only open the ANN if the embedder is around; otherwise we'd
    // create an empty ann-* directory on disk that would confuse
    // operators inspecting the layout.
    let ann = if let Some(e) = embedder {
        Some(
            manager
                .get_or_open_ann(zone_id, e.tag(), e.dim())
                .map_err(|err| format!("open ann for zone {zone_id:?}: {err}"))?,
        )
    } else {
        None
    };

    let state = crate::index_state::IndexState::open_or_create(manager.zone_root(zone_id))
        .map_err(|e| format!("open state for zone {zone_id:?}: {e}"))?;

    // Embedder-generation alignment (review R8): a model swap keys a
    // FRESH ann-<tag> directory; mtimes completed under another tag
    // must not verdict Unchanged against it.
    if let Some(e) = embedder {
        if state.ensure_embedder_generation(e.tag()) {
            tracing::warn!(
                zone = %zone_id,
                tag = %e.tag(),
                "embedder generation changed — invalidated mtime cache; full re-embed",
            );
        }
    }

    let sinks = IndexSinks {
        fts: &fts,
        ann: ann.as_ref(),
        embedder,
        state: &state,
        embed_broken,
        zone_has_ann: zone_has_ann_dir(manager, zone_id),
    };

    let mut indexed: u32 = 0;
    let mut skipped: u32 = 0;

    let visit_result = if recursive {
        walk_recursive(handle, root_path, &mut |vfs_path, entry_type| {
            if (indexed as usize) >= max_docs {
                return WalkAction::Stop;
            }
            if entry_type != DT_REG {
                return WalkAction::Continue;
            }
            match index_one(handle, &sinks, vfs_path) {
                IndexOne::Added => indexed += 1,
                IndexOne::Skipped => skipped += 1,
            }
            WalkAction::Continue
        })
        .map_err(walk_err_to_string)
    } else {
        // Non-recursive: enumerate ONLY direct children of root_path.
        // Matches Python `recursive=False`.
        match kernel_io::sys_readdir(handle, root_path) {
            Ok(entries) => {
                for entry in entries {
                    if (indexed as usize) >= max_docs {
                        break;
                    }
                    if entry.entry_type != DT_REG {
                        continue;
                    }
                    let child = kernel_io::join_vfs_path(root_path, &entry.name);
                    match index_one(handle, &sinks, &child) {
                        IndexOne::Added => indexed += 1,
                        IndexOne::Skipped => skipped += 1,
                    }
                }
                Ok(())
            }
            Err(e) => Err(walk_err_to_string(e)),
        }
    };

    // Commit BOTH sinks even on walker error so partial progress is
    // durable — callers retry with a narrower root_path per D5 SSOT.
    if let Err(e) = fts.commit() {
        tracing::warn!(err = %e, "fts commit failed after walk");
        return Err(format!("fts commit: {e}"));
    }
    if let Some(a) = ann.as_ref() {
        if let Err(e) = a.commit() {
            tracing::warn!(err = %e, "ann commit failed after walk");
            return Err(format!("ann commit: {e}"));
        }
    }
    // Persist the mtime cache so the next Refresh's diff pass sees
    // an up-to-date baseline.  A crash between the sink commits and
    // this save just means the next Refresh will re-index those
    // files (fresh mtime not yet in the cache) — safe, wasted work.
    if let Err(e) = state.save() {
        tracing::warn!(err = %e, "index_state save failed");
    }

    visit_result?;
    Ok((indexed, skipped))
}

/// Outcome of a single-file index attempt.  Distinguished so
/// `do_index` can tally added vs skipped without a per-call error
/// return (a skip is not an error).
enum IndexOne {
    // NOTE (review R5): content skips are DETERMINISTIC on the same
    // bytes (empty, oversize, binary, whitespace-only) — they record
    // their mtime in state so a Refresh dedups them like indexed
    // files and they stop consuming the repair budget every pass.
    // Transient skips (read errors, FTS write errors) record nothing
    // and are retried.
    Added,
    Skipped,
}

/// Record a deterministic content-skip so Refresh dedups it (same
/// mtime ⇒ Unchanged) instead of re-reading + re-skipping it on every
/// pass — with max_docs stable content-skips ahead of the tail, the
/// old behaviour starved the repair budget forever (review R5).
///
/// A skip is also a CONTENT TRANSITION (review R6): a previously
/// indexed file that became empty/oversize/binary must have its old
/// chunks PURGED, or deleted text stays searchable forever.  FTS
/// purges idempotently; ANN purges when the sink is open, else the
/// entry keeps a retry tombstone (mtime None) so a later pass with a
/// working ANN sink finishes the cleanup — unless the zone has no
/// ANN directory at all, in which case there is nothing to purge and
/// the real mtime finalizes the skip.
fn record_content_skip(handle: &KernelHandle, sinks: &IndexSinks<'_>, vfs_path: &str) -> IndexOne {
    sinks.fts.delete_all_chunks(vfs_path);
    match sinks.ann {
        Some(ann) => {
            ann.delete_all_chunks(vfs_path);
        }
        None if sinks.zone_has_ann => {
            // Vectors may exist but are unreachable — retry tombstone.
            sinks.state.record(vfs_path, None);
            return IndexOne::Skipped;
        }
        None => {}
    }
    let mtime_ms = kernel_io::sys_stat(handle, vfs_path)
        .ok()
        .and_then(|info| info.modified_at_ms);
    sinks.state.record(vfs_path, mtime_ms);
    IndexOne::Skipped
}

fn index_one(handle: &KernelHandle, sinks: &IndexSinks<'_>, vfs_path: &str) -> IndexOne {
    let bytes = match kernel_io::sys_read(handle, vfs_path) {
        Ok(b) => b,
        Err(e) => {
            tracing::debug!(path = %vfs_path, err = ?e, "index: sys_read failed — skipping");
            return IndexOne::Skipped;
        }
    };
    if bytes.is_empty() {
        return record_content_skip(handle, sinks, vfs_path);
    }
    if bytes.len() > INDEX_MAX_FILE_BYTES {
        tracing::debug!(
            path = %vfs_path,
            len = bytes.len(),
            cap = INDEX_MAX_FILE_BYTES,
            "index: file over size cap — skipping",
        );
        return record_content_skip(handle, sinks, vfs_path);
    }
    // Non-UTF8 files are treated as binary and skipped rather than
    // indexed as gibberish.  A future phase may want to add a
    // language-detect + transcode step, but that belongs with the
    // real chunker in P4.
    let text = match std::str::from_utf8(&bytes) {
        Ok(s) => s,
        Err(_) => {
            tracing::debug!(path = %vfs_path, "index: non-utf8 payload — skipping");
            return record_content_skip(handle, sinks, vfs_path);
        }
    };
    let mtime_ms = kernel_io::sys_stat(handle, vfs_path)
        .ok()
        .and_then(|info| info.modified_at_ms);

    // P4: chunk the file into semantically-coherent pieces.  The
    // chunker respects markdown-ish heading + code-fence structure
    // and keeps each chunk under the embedder's soft budget.
    let chunks = crate::chunker::chunk_document(text);
    if chunks.is_empty() {
        // Whitespace-only file — nothing to index; purge any prior
        // chunks + record per ANN reachability (review R6).
        return record_content_skip(handle, sinks, vfs_path);
    }

    // FTS side: drop the file's old chunk set, add the fresh one.
    // Both ops queue on the same writer transaction so commit()
    // lands them atomically — a reader never sees a partially-
    // reindexed file.
    sinks.fts.delete_all_chunks(vfs_path);
    for chunk in &chunks {
        if let Err(e) = sinks
            .fts
            .add_document(vfs_path, chunk.chunk_index, &chunk.text, mtime_ms)
        {
            tracing::warn!(
                path = %vfs_path,
                chunk = chunk.chunk_index,
                err = %e,
                "index: fts add_document failed — skipping remaining chunks",
            );
            return IndexOne::Skipped;
        }
    }

    // ANN side — keyword-degradation per query is fine, but a
    // transient embed failure must stay RETRYABLE (review R1):
    // recording the fresh mtime below despite a failed embed would
    // make the semantic hole permanent, because the next Refresh sees
    // the matching mtime and skips the doc forever.
    // ANN completeness starts false in BOTH embedder-down shapes
    // (review R7): a broken embedder (embed_broken) AND a clean
    // NotAvailable while ann-* directories exist.  In the latter, a
    // keyword-only edit would otherwise update FTS + record the fresh
    // mtime while the zone's OLD vectors stay live — when the
    // embedder returns, Refresh reads Unchanged forever and
    // semantic/hybrid ranking silently serves pre-edit vectors.
    let mut ann_complete = !sinks.embed_broken && !(sinks.embedder.is_none() && sinks.zone_has_ann);
    if let (Some(ann), Some(embedder)) = (sinks.ann, sinks.embedder) {
        let inputs: Vec<&str> = chunks.iter().map(|c| c.embed_input.as_str()).collect();
        match embedder.embed_batch(&inputs) {
            Ok(vecs) if vecs.len() == chunks.len() => {
                ann.delete_all_chunks(vfs_path);
                for (chunk, vec) in chunks.iter().zip(vecs.iter()) {
                    if let Err(e) = ann.add_vector(vfs_path, chunk.chunk_index, vec) {
                        ann_complete = false;
                        tracing::warn!(
                            path = %vfs_path,
                            chunk = chunk.chunk_index,
                            err = %e,
                            "index: ann add_vector failed — will retry on next refresh",
                        );
                    }
                }
            }
            Ok(vecs) => {
                ann_complete = false;
                tracing::warn!(
                    path = %vfs_path,
                    got = vecs.len(),
                    expected = chunks.len(),
                    "index: embedder returned wrong vec count — will retry on next refresh",
                );
            }
            Err(e) => {
                ann_complete = false;
                tracing::warn!(
                    path = %vfs_path,
                    err = %e,
                    "index: embed failed — will retry on next refresh",
                );
            }
        }
    }

    // Record in the P5 mtime cache so the next Refresh's diff pass
    // knows this file is up-to-date at `mtime_ms`.  Even a None
    // mtime is recorded (as None) so the file appears in the
    // known-paths snapshot — the verdict for None caches is
    // Changed, so it'll re-index next time, but it won't be
    // mistaken for a deleted file during the stale sweep.
    //
    // Recording is gated on ANN completeness: an incompletely
    // embedded doc is recorded with mtime None, which keeps it in
    // the known-paths snapshot (protecting it from the stale sweep)
    // while guaranteeing the next Refresh re-indexes it — the FTS
    // re-add is idempotent, so the retry costs a re-chunk only.
    if ann_complete {
        sinks.state.record(vfs_path, mtime_ms);
    } else {
        sinks.state.record(vfs_path, None);
    }

    IndexOne::Added
}

/// Drop `path` from every sink — used by the Refresh stale-sweep
/// when a file that was previously indexed no longer exists in the
/// current walk.  Both FTS and ANN's `delete_all_chunks` queue on
/// their writer transactions; the caller commits at end-of-Refresh.
/// Remove `vfs_path` from every sink.  Returns true when the state
/// entry was actually forgotten.
///
/// When no ANN sink is open (embedder unavailable/broken) but ANN
/// directories EXIST on disk, the state entry is a live deletion-set
/// tombstone for vectors we cannot reach right now — forgetting it
/// would orphan them permanently (review R4).  FTS chunks still drop
/// (idempotent); the tombstone survives until a refresh with a
/// working ANN sink completes the removal.
fn remove_one(sinks: &IndexSinks<'_>, vfs_path: &str, zone_has_ann: bool) -> bool {
    sinks.fts.delete_all_chunks(vfs_path);
    match sinks.ann {
        Some(ann) => {
            ann.delete_all_chunks(vfs_path);
            sinks.state.forget(vfs_path);
            true
        }
        None if !zone_has_ann => {
            // No ANN index exists at all — nothing to tombstone for.
            sinks.state.forget(vfs_path);
            true
        }
        None => {
            // Keep the tombstone: mtime None so the entry always
            // verdicts Changed and never masquerades as current.
            sinks.state.record(vfs_path, None);
            tracing::warn!(
                path = %vfs_path,
                "refresh sweep: ANN sink unavailable — kept deletion tombstone",
            );
            false
        }
    }
}

/// Does the zone root contain any `ann-*` directory?  Cheap readdir;
/// used by the sweep and the completion invariant to decide whether
/// an unopened ANN sink means "no vectors exist" or "vectors exist
/// but are unreachable".
///
/// FAIL-CLOSED on inspection errors (review R8): a read_dir or
/// entry-level I/O failure is treated as "vectors MAY exist" — the
/// conservative answer keeps documents ANN-retryable (mtime None)
/// instead of finalizing them over vectors we merely could not see.
/// The zone root not existing at all is a positive absence (fresh
/// zone) and safely reads false.
fn zone_has_ann_dir(manager: &IndexManager, zone_id: &str) -> bool {
    // No exists() preflight (review R9): Path::exists() returns false
    // for BOTH a genuinely absent root and a failed metadata call, so
    // it would reopen the error-to-absence hole read_dir handling
    // closes.  Only ErrorKind::NotFound is positive absence.
    match std::fs::read_dir(manager.zone_root(zone_id)) {
        Ok(rd) => rd.into_iter().any(|entry| match entry {
            Ok(e) => e.file_name().to_string_lossy().starts_with("ann-"),
            Err(e) => {
                tracing::warn!(err = %e, zone = %zone_id, "ann-dir scan entry error — assuming ANN present");
                true
            }
        }),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => false,
        Err(e) => {
            tracing::warn!(err = %e, zone = %zone_id, "ann-dir scan failed — assuming ANN present");
            true
        }
    }
}

/// Result counts from `do_refresh` — one number per RefreshResponse
/// field so the RPC handler doesn't have to remember the order.
#[derive(Debug, Default, Clone, Copy)]
struct RefreshCounts {
    reindexed: u32,
    removed: u32,
    unchanged: u32,
    skipped: u32,
    /// Sweep entries whose FTS chunks dropped but whose ANN cleanup
    /// is deferred behind a kept tombstone (ANN sink unavailable).
    /// Counted separately from `removed` (review R5): the INDEX still
    /// changed, so the query cache must invalidate even when
    /// `removed == 0`.
    tombstoned: u32,
    /// Walk stopped at the max_docs repair budget (review R4) — the
    /// caller should refresh again; the stale sweep was skipped.
    truncated: bool,
}

/// Incremental refresh: walk `root_path`, ask the mtime cache
/// whether each file needs reindexing, then sweep stale entries.
/// Same walker + sink shape as do_index; the diff is just the
/// per-file verdict before calling `index_one` and a post-walk
/// pass for cache-vs-corpus deletions.
#[allow(clippy::too_many_arguments)] // same rationale as do_index
fn do_refresh(
    handle: &KernelHandle,
    manager: &IndexManager,
    embedder: Option<&Arc<dyn Embedder>>,
    embed_broken: bool,
    root_path: &str,
    zone_id: &str,
    recursive: bool,
    max_docs: usize,
) -> Result<RefreshCounts, String> {
    // Serialize writers per zone — same rationale as do_index.
    let zone_lock = manager.zone_write_lock(zone_id);
    let _zone_guard = zone_lock.lock();

    let fts = manager
        .get_or_open(zone_id)
        .map_err(|e| format!("open index for zone {zone_id:?}: {e}"))?;

    let ann = if let Some(e) = embedder {
        Some(
            manager
                .get_or_open_ann(zone_id, e.tag(), e.dim())
                .map_err(|err| format!("open ann for zone {zone_id:?}: {err}"))?,
        )
    } else {
        None
    };

    let state = crate::index_state::IndexState::open_or_create(manager.zone_root(zone_id))
        .map_err(|e| format!("open state for zone {zone_id:?}: {e}"))?;

    // Embedder-generation alignment (review R8): a model swap keys a
    // FRESH ann-<tag> directory; mtimes completed under another tag
    // must not verdict Unchanged against it.
    if let Some(e) = embedder {
        if state.ensure_embedder_generation(e.tag()) {
            tracing::warn!(
                zone = %zone_id,
                tag = %e.tag(),
                "embedder generation changed — invalidated mtime cache; full re-embed",
            );
        }
    }

    let sinks = IndexSinks {
        fts: &fts,
        ann: ann.as_ref(),
        embedder,
        state: &state,
        embed_broken,
        zone_has_ann: zone_has_ann_dir(manager, zone_id),
    };

    let mut counts = RefreshCounts::default();
    // Track every path we visit so the stale-sweep at the end knows
    // which cached entries no longer exist in the corpus.
    let mut seen: std::collections::HashSet<String> = std::collections::HashSet::new();

    // Set when the walk stops early at `max_docs` — the sweep below
    // must NOT run then, or every cached path beyond the cap would be
    // falsely treated as deleted (review R3: on a >max_docs corpus the
    // first post-migration Refresh would discard the tail's deletion
    // set and its indexed docs).
    let mut truncated = false;

    let mut visit_file = |vfs_path: &str| -> WalkAction {
        // The cap budgets REPAIR work (re-index = chunk + embed), not
        // cheap unchanged visits (one sys_stat each).  Counting
        // unchanged visits toward the cap made a capped refresh
        // unable to advance past its first page: after the
        // post-migration pass repaired the first max_docs files,
        // every later refresh re-counted those now-unchanged entries
        // and stopped before ever reaching the tail (review R4).
        if (counts.reindexed as usize + counts.skipped as usize) >= max_docs {
            truncated = true;
            return WalkAction::Stop;
        }
        seen.insert(vfs_path.to_string());
        let fresh_mtime = kernel_io::sys_stat(handle, vfs_path)
            .ok()
            .and_then(|info| info.modified_at_ms);
        match sinks.state.verdict(vfs_path, fresh_mtime) {
            crate::index_state::RefreshVerdict::Unchanged => {
                counts.unchanged += 1;
            }
            crate::index_state::RefreshVerdict::Changed => {
                match index_one(handle, &sinks, vfs_path) {
                    IndexOne::Added => counts.reindexed += 1,
                    IndexOne::Skipped => counts.skipped += 1,
                }
            }
        }
        WalkAction::Continue
    };

    let visit_result = if recursive {
        walk_recursive(handle, root_path, &mut |vfs_path, entry_type| {
            if entry_type != DT_REG {
                return WalkAction::Continue;
            }
            visit_file(vfs_path)
        })
        .map_err(walk_err_to_string)
    } else {
        match kernel_io::sys_readdir(handle, root_path) {
            Ok(entries) => {
                for entry in entries {
                    if entry.entry_type != DT_REG {
                        continue;
                    }
                    let child = kernel_io::join_vfs_path(root_path, &entry.name);
                    if visit_file(&child) == WalkAction::Stop {
                        break;
                    }
                }
                Ok(())
            }
            Err(e) => Err(walk_err_to_string(e)),
        }
    };

    // Stale-sweep: for every path the cache knows about but the
    // walk didn't see, drop it from FTS + ANN + state.  Guarded by
    //
    //   (a) visit_result.is_ok() — a mid-walk crash would falsely
    //       report un-visited paths as deleted,
    //   (b) !truncated — a walk stopped at `max_docs` did not SEE the
    //       tail, so "not seen" proves nothing (review R3), and
    //   (c) path is under the caller's `root_path` scope — a
    //       Refresh scoped to /a/ must NOT wipe cached paths under
    //       /b/.  Without this guard, a scoped Refresh silently
    //       reindexes-with-drops for the WHOLE zone and any file
    //       that lives outside the caller's scope gets flagged as
    //       deleted.
    if truncated {
        tracing::warn!(
            max_docs,
            root = %root_path,
            "refresh walk truncated at max_docs — stale sweep skipped; \
             raise max_docs or refresh in narrower scopes to sweep deletions",
        );
    }
    if visit_result.is_ok() && !truncated {
        let scope = root_path.trim_end_matches('/');
        let has_ann_dir = sinks.zone_has_ann;
        for cached_path in sinks.state.known_paths() {
            let in_scope = scope.is_empty()
                || scope == "/"
                || cached_path == scope
                || cached_path.starts_with(&format!("{scope}/"));
            if in_scope && !seen.contains(&cached_path) {
                if remove_one(&sinks, &cached_path, has_ann_dir) {
                    counts.removed += 1;
                } else {
                    counts.tombstoned += 1;
                }
            }
        }
    }

    if let Err(e) = fts.commit() {
        return Err(format!("fts commit: {e}"));
    }
    if let Some(a) = ann.as_ref() {
        if let Err(e) = a.commit() {
            return Err(format!("ann commit: {e}"));
        }
    }
    if let Err(e) = state.save() {
        tracing::warn!(err = %e, "index_state save failed");
    }

    visit_result?;
    counts.truncated = truncated;
    Ok(counts)
}

// ── P8 Python-parity helpers ────────────────────────────────────

/// Index a pre-materialised set of documents.  Same commit-time
/// discipline as do_index (per-file delete_all_chunks + per-chunk
/// add + FTS/ANN commit); the only difference is text arrives via
/// the caller rather than sys_read.  Each doc's `zone_id` overrides
/// the request-level default.  Groups by zone so we open each
/// zone's FTS + ANN + IndexState once, not per doc.
fn do_index_documents(
    manager: &IndexManager,
    embedder: Option<&Arc<dyn Embedder>>,
    embed_broken: bool,
    default_zone: &str,
    documents: Vec<crate::search_proto::DocumentInput>,
    cache: &crate::query_cache::SharedQueryCache,
) -> Result<(u32, u32), String> {
    // Bucket by zone so per-zone open + commit happens once.
    let mut by_zone: std::collections::HashMap<String, Vec<crate::search_proto::DocumentInput>> =
        std::collections::HashMap::new();
    for doc in documents {
        let z = if doc.zone_id.is_empty() {
            default_zone.to_string()
        } else {
            doc.zone_id.clone()
        };
        by_zone.entry(z).or_default().push(doc);
    }

    let mut total_indexed: u32 = 0;
    let mut total_skipped: u32 = 0;

    for (zone_id, docs) in by_zone {
        // Serialize writers per zone — same rationale as do_index.
        let zone_lock = manager.zone_write_lock(&zone_id);
        let _zone_guard = zone_lock.lock();

        let fts = manager
            .get_or_open(&zone_id)
            .map_err(|e| format!("open fts for zone {zone_id:?}: {e}"))?;
        let ann = if let Some(e) = embedder {
            Some(
                manager
                    .get_or_open_ann(&zone_id, e.tag(), e.dim())
                    .map_err(|err| format!("open ann for zone {zone_id:?}: {err}"))?,
            )
        } else {
            None
        };
        let state = crate::index_state::IndexState::open_or_create(manager.zone_root(&zone_id))
            .map_err(|e| format!("open state for zone {zone_id:?}: {e}"))?;

        // Embedder-generation alignment — same rationale as do_index.
        if let Some(e) = embedder {
            if state.ensure_embedder_generation(e.tag()) {
                tracing::warn!(
                    zone = %zone_id,
                    tag = %e.tag(),
                    "embedder generation changed — invalidated mtime cache; full re-embed",
                );
            }
        }

        // #4623: incremental FTS visibility.  Embedding dominates a
        // large explicit batch (tens of seconds of CPU), and a single
        // end-of-batch commit left keyword queries answering
        // healthy-empty the whole time — indistinguishable from the
        // silent-degradation modes health sentinels exist to catch.
        // Commit (and drop the zone's cached results) every few docs
        // so keyword hits appear progressively; commits are a couple
        // of ms, noise next to the embed cost.  ANN stays end-commit:
        // the dense leg is what's being built, and hnsw dumps are not
        // cheap per-doc.
        let mut docs_since_commit: usize = 0;

        let zone_has_ann = zone_has_ann_dir(manager, &zone_id);
        // Content transition on explicit indexing (review R6): a doc
        // re-posted with empty/whitespace text must PURGE its prior
        // chunks, not leave stale text searchable behind a skip.
        let content_skip = |path: &str| {
            fts.delete_all_chunks(path);
            match ann.as_ref() {
                Some(a) => {
                    a.delete_all_chunks(path);
                    state.forget(path);
                }
                None if zone_has_ann => {
                    // Vectors may exist but are unreachable — retry
                    // tombstone so a later pass finishes the purge.
                    state.record(path, None);
                }
                None => {
                    state.forget(path);
                }
            }
        };
        for doc in docs {
            if doc.text.trim().is_empty() {
                content_skip(&doc.path);
                total_skipped += 1;
                continue;
            }
            let chunks = crate::chunker::chunk_document(&doc.text);
            if chunks.is_empty() {
                content_skip(&doc.path);
                total_skipped += 1;
                continue;
            }
            // FTS: drop-old-then-add-new (mirrors do_index).
            fts.delete_all_chunks(&doc.path);
            let mut fts_ok = true;
            for chunk in &chunks {
                if let Err(e) =
                    fts.add_document(&doc.path, chunk.chunk_index, &chunk.text, doc.mtime_ms)
                {
                    tracing::warn!(path = %doc.path, err = %e, "index_documents: fts add failed");
                    fts_ok = false;
                    break;
                }
            }
            if !fts_ok {
                total_skipped += 1;
                continue;
            }

            // ANN: keyword-degradation is fine per query, but a
            // transient embed failure must stay RETRYABLE.  Recording
            // the mtime below despite a failed embed would make the
            // hole permanent — the next Refresh sees the matching
            // mtime and never retries the missing vectors (a remote
            // provider 429/timeout would silently produce a
            // forever-keyword-only doc; review R1).
            // Same completion invariant as index_one (review R7):
            // embedder absent + existing ann-* dirs ⇒ stay retryable.
            let mut ann_complete = !embed_broken && !(embedder.is_none() && zone_has_ann);
            if let (Some(ann), Some(emb)) = (ann.as_ref(), embedder) {
                let inputs: Vec<&str> = chunks.iter().map(|c| c.embed_input.as_str()).collect();
                match emb.embed_batch(&inputs) {
                    Ok(vecs) if vecs.len() == chunks.len() => {
                        ann.delete_all_chunks(&doc.path);
                        for (chunk, vec) in chunks.iter().zip(vecs.iter()) {
                            if let Err(e) = ann.add_vector(&doc.path, chunk.chunk_index, vec) {
                                ann_complete = false;
                                tracing::warn!(
                                    path = %doc.path,
                                    err = %e,
                                    "index_documents: ann add failed — will retry on next index/refresh",
                                );
                            }
                        }
                    }
                    Ok(vecs) => {
                        ann_complete = false;
                        tracing::warn!(
                            path = %doc.path,
                            got = vecs.len(),
                            want = chunks.len(),
                            "index_documents: embed count mismatch — will retry on next index/refresh",
                        );
                    }
                    Err(e) => {
                        ann_complete = false;
                        tracing::warn!(
                            path = %doc.path,
                            err = %e,
                            "index_documents: embed failed — will retry on next index/refresh",
                        );
                    }
                }
            }

            // Only a FULLY indexed doc (FTS + ANN when an embedder is
            // configured) gets its real mtime recorded.  An incomplete
            // doc records mtime None EXPLICITLY — merely skipping the
            // record would leave a PRIOR same-mtime entry in place and
            // Refresh would read Unchanged forever (review R2).  A
            // None mtime always verdicts Changed, so the next
            // Refresh / IndexDocuments retries the missing vectors;
            // FTS re-adds are idempotent (delete-then-add).
            if ann_complete {
                state.record(&doc.path, doc.mtime_ms);
            } else {
                state.record(&doc.path, None);
            }
            total_indexed += 1;
            docs_since_commit += 1;
            if docs_since_commit >= INDEX_INCREMENTAL_COMMIT_EVERY {
                if let Err(e) = fts.commit() {
                    return Err(format!("fts incremental commit for zone {zone_id:?}: {e}"));
                }
                // Cached results captured before this commit would
                // mask the fresh docs for the cache TTL — drop them
                // with every visibility step, not just at the end.
                cache.invalidate_zone(&zone_id);
                docs_since_commit = 0;
            }
        }

        if let Err(e) = fts.commit() {
            return Err(format!("fts commit for zone {zone_id:?}: {e}"));
        }
        if let Some(a) = ann.as_ref() {
            if let Err(e) = a.commit() {
                return Err(format!("ann commit for zone {zone_id:?}: {e}"));
            }
        }
        if let Err(e) = state.save() {
            tracing::warn!(err = %e, zone = %zone_id, "index_state save failed");
        }
        cache.invalidate_zone(&zone_id);
    }

    Ok((total_indexed, total_skipped))
}

/// Per-file change event.  "delete" drops the file from every
/// index; "create" / "update" is a no-op here because we can't
/// re-index without the text — the caller is expected to follow
/// with IndexDocuments or wait for the next Refresh.  Returns a
/// status string mirroring the Python `notify_file_change`
/// contract.
fn do_notify_file_change(
    manager: &IndexManager,
    zone_id: &str,
    path: &str,
    change_type: &str,
    cache: &crate::query_cache::SharedQueryCache,
) -> Result<String, String> {
    // Serialize writers per zone — same rationale as do_index.
    let zone_lock = manager.zone_write_lock(zone_id);
    let _zone_guard = zone_lock.lock();

    let fts = manager
        .get_or_open(zone_id)
        .map_err(|e| format!("open fts for zone {zone_id:?}: {e}"))?;
    let state = crate::index_state::IndexState::open_or_create(manager.zone_root(zone_id))
        .map_err(|e| format!("open state for zone {zone_id:?}: {e}"))?;

    match change_type {
        "delete" => {
            fts.delete_all_chunks(path);
            // Only open ANN if it happens to already be there —
            // creating an empty ANN dir on a delete is
            // counterproductive.  We'd need the embedder tag to
            // open; ANN cleanup is deferred to the next Refresh's
            // stale sweep.  That sweep is driven by known_paths(),
            // so the path must stay KNOWN as a tombstone (mtime
            // None) — forgetting it here would make the orphaned
            // vectors undiscoverable forever and semantic queries
            // would keep returning the deleted path (review R3).
            // Refresh sees the tombstone, misses the path in the
            // walk, and removes FTS remnants + ANN + state together.
            state.record(path, None);
            if let Err(e) = fts.commit() {
                return Err(format!("fts commit: {e}"));
            }
            // The tombstone IS the deferred-ANN-cleanup record — if it
            // cannot be persisted the delete must FAIL so the caller
            // retries, instead of reporting success while the vectors
            // silently outlive their document (review R4).
            if let Err(e) = state.save() {
                return Err(format!("delete tombstone persist failed: {e}"));
            }
            cache.invalidate_zone(zone_id);
            Ok("accepted".to_string())
        }
        "create" | "update" | "" => {
            // No text on the wire — nothing to add.  Return
            // "skipped" so the caller knows it's an ack, not a
            // re-index.  A future NotifyFileChangeRequest with an
            // inline text field would let us do more here, but
            // matching Python's shape means callers pair this
            // event with a follow-up IndexDocuments.
            Ok("skipped".to_string())
        }
        other => Err(format!("unknown change_type: {other:?}")),
    }
}

/// Path-only existence check.  Fetches chunks under `path` via
/// FTS's `get_chunks_by_path`; ANN isn't queried because that
/// would need the embedder + tag and Locate is meant to be cheap.
fn do_locate(
    manager: &IndexManager,
    zone_id: &str,
    path: &str,
) -> Result<(bool, u32, Option<i64>), String> {
    let fts = manager
        .get_or_open(zone_id)
        .map_err(|e| format!("open fts for zone {zone_id:?}: {e}"))?;
    let hits = fts
        .get_chunks_by_path(path)
        .map_err(|e| format!("locate: {e}"))?;
    if hits.is_empty() {
        return Ok((false, 0, None));
    }
    let mtime_ms = hits
        .iter()
        .find_map(|h| h.mtime_ms)
        // FtsHit's mtime_ms is Option<i64>; find_map short-circuits
        // on the first Some — every chunk of a file shares the
        // same mtime so any Some is fine.
        ;
    Ok((true, hits.len() as u32, mtime_ms))
}

// ── tonic trait impl ──────────────────────────────────────────────

#[async_trait]
impl SearchService for SearchServiceImpl {
    async fn glob(&self, request: Request<GlobRequest>) -> Result<Response<GlobResponse>, Status> {
        let req = request.into_inner();
        let root = if req.root_path.is_empty() {
            "/".to_string()
        } else {
            req.root_path
        };
        let pattern = req.pattern;
        let cap = if req.max_results == 0 {
            DEFAULT_GLOB_MAX
        } else {
            req.max_results as usize
        };
        // Clone the Arc into the blocking task — KernelHandle is
        // Send + Sync per the plugin ABI's unsafe impl; the Arc
        // keeps the underlying callback table alive for as long as
        // any request is in flight.
        let sort_recency = req.sort_recency;
        let handle = Arc::clone(&self.handle);
        let outcome = tokio::task::spawn_blocking(move || {
            do_glob(&handle, &root, &pattern, cap, sort_recency)
        })
        .await
        .map_err(|e| Status::internal(format!("spawn_blocking joined error: {e}")))?;

        match outcome {
            Ok((paths, truncated)) => Ok(Response::new(GlobResponse {
                paths,
                truncated,
                error: None,
            })),
            Err(err) => Ok(Response::new(GlobResponse {
                paths: Vec::new(),
                truncated: false,
                error: Some(err),
            })),
        }
    }

    async fn grep(&self, request: Request<GrepRequest>) -> Result<Response<GrepResponse>, Status> {
        let req = request.into_inner();
        let root = if req.root_path.is_empty() {
            "/".to_string()
        } else {
            req.root_path
        };
        let cap = if req.max_results == 0 {
            DEFAULT_GREP_MAX
        } else {
            req.max_results as usize
        };
        let handle = Arc::clone(&self.handle);
        let outcome = tokio::task::spawn_blocking(move || {
            do_grep(
                &handle,
                &root,
                &req.pattern,
                &req.file_pattern,
                req.ignore_case,
                cap,
                req.before_context as usize,
                req.after_context as usize,
                req.invert_match,
                req.sort_recency,
            )
        })
        .await
        .map_err(|e| Status::internal(format!("spawn_blocking joined error: {e}")))?;

        match outcome {
            Ok((matches, truncated)) => Ok(Response::new(GrepResponse {
                matches,
                truncated,
                error: None,
            })),
            Err(err) => Ok(Response::new(GrepResponse {
                matches: Vec::new(),
                truncated: false,
                error: Some(err),
            })),
        }
    }

    async fn query(
        &self,
        request: Request<QueryRequest>,
    ) -> Result<Response<QueryResponse>, Status> {
        let req = request.into_inner();
        if req.q.is_empty() {
            return Ok(Response::new(QueryResponse {
                results: Vec::new(),
                error: Some("q must not be empty".into()),
            }));
        }
        // Canonicalise the zone BEFORE cache lookup so the cache
        // and Index/Refresh invalidation agree on the key.  Wire
        // zone_id="" resolves to ROOT_ZONE_ID everywhere else
        // (do_index, do_refresh, do_*_query); the cache must see
        // the same resolved value or invalidation misses a call
        // that inserted under the empty string.
        let mut req_for_cache = req.clone();
        req_for_cache.zone_id = resolve_zone(&req.zone_id).to_string();

        // P7 cache check.  Zone is the auth boundary (D5), so a
        // hit here is safe to serve directly — we don't need to
        // re-check permission, the kernel router already did.  A
        // hit skips FTS + ANN + fusion + scoring + pooling +
        // expand entirely.
        if let Some(cached) = self.query_cache.get(&req_for_cache) {
            return Ok(Response::new(QueryResponse {
                results: cached,
                error: None,
            }));
        }
        // Parse borrow-only fields FIRST so later `let q = req.q`
        // moves don't leave `req` partially moved for later reads.
        let query_type = QueryType::try_from(req.query_type).unwrap_or(QueryType::Unspecified);
        let fusion_opts = FusionOpts::from_request(&req);
        let expand_mode = ExpandMode::from_str(&req.expand);
        let recency_mode = crate::scoring::RecencyMode::parse_wire(&req.recency_mode);
        let recency_weight = if req.recency_weight == 0.0 {
            crate::scoring::DEFAULT_RECENCY_WEIGHT
        } else {
            req.recency_weight
        };
        let recency_half_life_days = if req.recency_half_life_days == 0.0 {
            crate::scoring::DEFAULT_RECENCY_HALF_LIFE_DAYS
        } else {
            req.recency_half_life_days
        };
        let prefix_boosts = req.path_prefix_boosts.clone();
        let limit = if req.limit == 0 {
            DEFAULT_QUERY_LIMIT
        } else {
            req.limit as usize
        };
        // Post-fusion score adjustments (recency, prefix boosts) can
        // PROMOTE a hit from below the requested limit — so the pool
        // they act on must be over-fetched, or a heavily boosted doc
        // initially ranked at limit+1 is unreachable (review R3).
        // Final truncation to `limit` happens after apply_all+re-sort.
        let adjustments_active = matches!(
            recency_mode,
            crate::scoring::RecencyMode::On | crate::scoring::RecencyMode::Auto
        ) || !prefix_boosts.is_empty();
        let fetch_limit = if adjustments_active {
            limit
                .saturating_mul(ADJUSTMENT_OVER_FETCH_MULT)
                .clamp(limit, ADJUSTMENT_FETCH_CEILING.max(limit))
        } else {
            limit
        };
        let zone_id = resolve_zone(&req.zone_id).to_string();
        // Now safe to move fields out.
        let q = req.q;
        let path_filter = req.path_filter;
        let manager = Arc::clone(&self.manager);
        // Retained copies for post-outcome scoring — `q` moves into
        // the spawn_blocking closures below; recency-auto needs to
        // read the query text to decide whether to fire.  String
        // clone is cheap next to the fetch itself.
        let q_for_scoring = q.clone();
        // Cheap Arc clone + String clone retained for the post-
        // outcome expand-macro enrichment (both go via the FTS
        // sibling; keeping them out of the match arms' move scope
        // avoids threading them back).
        let manager_for_expand = Arc::clone(&self.manager);
        let zone_for_expand = zone_id.clone();

        let outcome = match query_type {
            QueryType::Unspecified | QueryType::Keyword => tokio::task::spawn_blocking(move || {
                do_keyword_query(&manager, &q, &zone_id, fetch_limit, &path_filter)
            })
            .await
            .map_err(|e| Status::internal(format!("spawn_blocking joined error: {e}")))?,
            QueryType::Semantic => {
                // Initialise the embedder BEFORE spawn_blocking so a
                // Load / NotAvailable error surfaces synchronously
                // with an actionable message instead of getting
                // wrapped as an opaque JoinError.
                let embedder = match self.get_or_init_embedder() {
                    Ok(e) => e,
                    Err(e) => {
                        return Ok(Response::new(QueryResponse {
                            results: Vec::new(),
                            error: Some(format!("semantic unavailable: {e}")),
                        }));
                    }
                };
                let embed_cache = Arc::clone(&self.embed_cache);
                tokio::task::spawn_blocking(move || {
                    do_semantic_query(
                        &manager,
                        &embedder,
                        &embed_cache,
                        &q,
                        &zone_id,
                        fetch_limit,
                        &path_filter,
                    )
                })
                .await
                .map_err(|e| Status::internal(format!("spawn_blocking joined error: {e}")))?
            }
            QueryType::Hybrid => {
                // Same init-before-spawn pattern as SEMANTIC — hybrid
                // needs the embedder to run its semantic leg, so a
                // missing embedder degrades hybrid the same way (with
                // a clearer "hybrid unavailable" prefix).
                let embedder = match self.get_or_init_embedder() {
                    Ok(e) => e,
                    Err(e) => {
                        return Ok(Response::new(QueryResponse {
                            results: Vec::new(),
                            error: Some(format!("hybrid unavailable: {e}")),
                        }));
                    }
                };
                // Parallel-fetch retrofit (P3 audit finding #3):
                // real-fastembed semantic ~300 ms + BM25 keyword ~10 ms
                // = 310 ms wall-clock sequential.  Spawning both legs
                // on the blocking pool + joining brings wall-clock to
                // max(kw, sem) ≈ 300 ms — no free lunch on total CPU
                // but user-visible p95 halves in the worst-case ratio.
                let over_fetch = limit
                    .saturating_mul(HYBRID_OVER_FETCH_MULT)
                    .max(limit)
                    .max(fetch_limit);
                // Title arm (#4628): a third parallel leg over the
                // in-memory skeleton.  Independent of kw/sem, so it
                // joins the same spawn_blocking fan-out.  Runs only
                // when enabled — a disabled arm costs nothing (no
                // skeleton is ever built).
                let title_arm_on = self.title_arm_enabled();
                let (kw_task, sem_task, title_task) = {
                    let mgr_kw = Arc::clone(&manager);
                    let mgr_sem = Arc::clone(&manager);
                    let mgr_title = Arc::clone(&manager);
                    let embedder = Arc::clone(&embedder);
                    let embed_cache = Arc::clone(&self.embed_cache);
                    let q_kw = q.clone();
                    let q_title = q.clone();
                    let q_sem = q;
                    let zone_kw = zone_id.clone();
                    let zone_title = zone_id.clone();
                    let zone_sem = zone_id;
                    let path_kw = path_filter.clone();
                    let path_title = path_filter.clone();
                    let path_sem = path_filter;
                    (
                        tokio::task::spawn_blocking(move || {
                            do_keyword_query(&mgr_kw, &q_kw, &zone_kw, over_fetch, &path_kw)
                        }),
                        tokio::task::spawn_blocking(move || {
                            do_semantic_query(
                                &mgr_sem,
                                &embedder,
                                &embed_cache,
                                &q_sem,
                                &zone_sem,
                                over_fetch,
                                &path_sem,
                            )
                        }),
                        tokio::task::spawn_blocking(move || {
                            if !title_arm_on {
                                return Vec::new();
                            }
                            // limit * 2 mirrors Python's locate
                            // over-fetch (fusion headroom).
                            do_title_locate(
                                &mgr_title,
                                &q_title,
                                &zone_title,
                                limit.saturating_mul(2),
                                &path_title,
                            )
                        }),
                    )
                };
                let (kw_join, sem_join, title_join) = tokio::join!(kw_task, sem_task, title_task);
                let kw = kw_join
                    .map_err(|e| Status::internal(format!("spawn_blocking joined error: {e}")))?;
                let sem = sem_join
                    .map_err(|e| Status::internal(format!("spawn_blocking joined error: {e}")))?;
                // A panicked title task degrades to an empty arm —
                // the arm is additive evidence, never a failure
                // source (fail-soft parity with Python).
                let title_hits = title_join.unwrap_or_default();
                match (kw, sem) {
                    (Ok(keyword), Ok(semantic)) => {
                        // Keyword-lane sub-fusion (#4628).  Empty
                        // title arm ⇒ the lane passes through
                        // UNCHANGED — non-title queries stay byte-
                        // identical to the pre-title-arm plugin
                        // under every fusion method (the WEIGHTED
                        // method normalises raw scores, so even a
                        // score-preserving no-op re-fusion would
                        // shift its blend).
                        let kw_lane = if title_hits.is_empty() {
                            keyword
                        } else {
                            let fts = manager_for_expand.get_or_open(&zone_for_expand).ok();
                            let hydrated = hydrate_title_hits(
                                fts.as_deref(),
                                &title_hits,
                                &keyword,
                                &semantic,
                                &zone_for_expand,
                            );
                            fusion::rrf_multi(
                                &[
                                    (fusion::ArmKind::Chunk, keyword.as_slice()),
                                    (fusion::ArmKind::Title, hydrated.as_slice()),
                                ],
                                fusion_opts.rrf_k,
                            )
                        };
                        // Keep the over-fetched pool through fusion
                        // when adjustments still need to reorder it;
                        // the final truncate-to-limit happens post-
                        // adjustment below.
                        Ok(fuse_hybrid(kw_lane, semantic, fetch_limit, fusion_opts))
                    }
                    // A source-side error on either leg — surface it
                    // as the response's `error` field rather than a
                    // gRPC Status, matching keyword / semantic
                    // behaviour.  If both fail we surface the
                    // keyword error (arbitrary but stable).
                    (Err(e), _) | (Ok(_), Err(e)) => Err(e),
                }
            }
        };

        match outcome {
            Ok(mut results) => {
                // P6 post-fusion adjustments.  Order matters:
                // 1. recency + prefix boost adjust scores in place,
                // 2. re-sort so the pool + limit steps see the new
                //    ranking,
                // 3. pool by chunks_per_page (which respects the
                //    post-boost order),
                // 4. truncate to `limit`,
                // 5. enrich with expand=macro context.
                if matches!(
                    recency_mode,
                    crate::scoring::RecencyMode::On | crate::scoring::RecencyMode::Auto
                ) || !prefix_boosts.is_empty()
                {
                    let now_ms = current_time_ms();
                    crate::scoring::apply_all(
                        &mut results,
                        recency_mode,
                        recency_weight,
                        recency_half_life_days,
                        now_ms,
                        &q_for_scoring,
                        &prefix_boosts,
                    );
                    // Re-sort descending by score so pooling +
                    // truncation see the new ranking.  Deterministic
                    // tie-break on (path, chunk_index) — same shape
                    // as fusion::finalise.
                    results.sort_by(|a, b| {
                        b.score
                            .partial_cmp(&a.score)
                            .unwrap_or(std::cmp::Ordering::Equal)
                            .then_with(|| a.path.cmp(&b.path))
                            .then_with(|| a.chunk_index.cmp(&b.chunk_index))
                    });
                }
                // Uniform post-outcome pooling.  Keyword / semantic
                // do NOT pool internally, only hybrid's fuse_hybrid
                // does; applying here gives all three query modes
                // the same #4542 chunks_per_page semantics.  On the
                // hybrid path this is a no-op (fuse_hybrid already
                // pooled + truncated) so it's cheap to always run.
                if fusion_opts.chunks_per_page > 0 {
                    results = fusion::pool_by_document(results, fusion_opts.chunks_per_page);
                }
                // Final caller-visible truncation — unconditional, so
                // the adjustment over-fetch never leaks past `limit`.
                if results.len() > limit {
                    results.truncate(limit);
                }
                // Post-outcome enrichment.  Runs inside the async
                // handler (no spawn_blocking) because expand-macro's
                // work is a handful of FTS TermQuery lookups (~10 µs
                // each, cached per unique path) — not worth another
                // hop through the blocking pool.
                if matches!(expand_mode, ExpandMode::Macro) {
                    apply_expand(
                        &manager_for_expand,
                        &zone_for_expand,
                        expand_mode,
                        &mut results,
                    );
                }
                // P7 cache write.  Store the fully-processed
                // response (post-scoring, post-pool, post-expand)
                // so the next hit skips all of it.  Errors are
                // NOT cached — a stale FTS index / missing zone
                // may resolve on retry, and we don't want the
                // cache to sticky-fail those.
                self.query_cache.insert(&req_for_cache, results.clone());
                Ok(Response::new(QueryResponse {
                    results,
                    error: None,
                }))
            }
            Err(err) => Ok(Response::new(QueryResponse {
                results: Vec::new(),
                error: Some(err),
            })),
        }
    }

    async fn index(
        &self,
        request: Request<IndexRequest>,
    ) -> Result<Response<IndexResponse>, Status> {
        let indexing = IndexingGuard::enter(&self.indexing_ops);
        let req = request.into_inner();
        let root = if req.root_path.is_empty() {
            "/".to_string()
        } else {
            req.root_path
        };
        let zone_id = resolve_zone(&req.zone_id).to_string();
        let recursive = req.recursive;
        let max_docs = if req.max_docs == 0 {
            DEFAULT_INDEX_MAX_DOCS
        } else {
            req.max_docs as usize
        };
        let handle = Arc::clone(&self.handle);
        let manager = Arc::clone(&self.manager);
        // Best-effort embedder init.  A NotAvailable / Load error
        // does NOT fail Index — keyword indexing runs anyway, ANN
        // just stays empty until an operator wires the embedder up
        // and re-runs Index.  This matches the "graceful degradation"
        // posture SemanticQuery uses.
        let (embedder, embed_broken) = self.indexing_embedder();
        // Retain the zone id for post-outcome cache invalidation
        // (the closure below moves the owned copy into
        // spawn_blocking).
        let zone_for_invalidate = zone_id.clone();
        let outcome = tokio::task::spawn_blocking(move || {
            let _indexing = indexing; // held until the WORK ends, not the RPC
            do_index(
                &handle,
                &manager,
                embedder.as_ref(),
                embed_broken,
                &root,
                &zone_id,
                recursive,
                max_docs,
            )
        })
        .await
        .map_err(|e| Status::internal(format!("spawn_blocking joined error: {e}")))?;

        match outcome {
            Ok((indexed_count, skipped_count)) => {
                // P7 cache invalidation.  The corpus for this zone
                // just changed; any cached Query response is
                // potentially stale.  Drop the whole zone's cache
                // — coarse but safe, and cheap (per-zone hashmap
                // remove is O(1) on the outer + O(n) on the entries
                // dropped).
                self.query_cache.invalidate_zone(&zone_for_invalidate);
                Ok(Response::new(IndexResponse {
                    indexed_count,
                    skipped_count,
                    error: None,
                }))
            }
            Err(err) => Ok(Response::new(IndexResponse {
                indexed_count: 0,
                skipped_count: 0,
                error: Some(err),
            })),
        }
    }

    async fn refresh(
        &self,
        request: Request<RefreshRequest>,
    ) -> Result<Response<RefreshResponse>, Status> {
        let indexing = IndexingGuard::enter(&self.indexing_ops);
        let req = request.into_inner();
        let root = if req.root_path.is_empty() {
            "/".to_string()
        } else {
            req.root_path
        };
        let zone_id = resolve_zone(&req.zone_id).to_string();
        let recursive = req.recursive;
        let max_docs = if req.max_docs == 0 {
            DEFAULT_INDEX_MAX_DOCS
        } else {
            req.max_docs as usize
        };
        let handle = Arc::clone(&self.handle);
        let manager = Arc::clone(&self.manager);
        // Same best-effort embedder posture as Index: a missing
        // embedder means ANN stays unchanged this Refresh; keyword
        // side still incrementally updates.
        let (embedder, embed_broken) = self.indexing_embedder();
        let zone_for_invalidate = zone_id.clone();
        let outcome = tokio::task::spawn_blocking(move || {
            let _indexing = indexing; // held until the WORK ends, not the RPC
            do_refresh(
                &handle,
                &manager,
                embedder.as_ref(),
                embed_broken,
                &root,
                &zone_id,
                recursive,
                max_docs,
            )
        })
        .await
        .map_err(|e| Status::internal(format!("spawn_blocking joined error: {e}")))?;

        match outcome {
            Ok(counts) => {
                // P7 cache invalidation on any successful Refresh
                // that actually changed anything.  A no-op Refresh
                // (all cached unchanged) leaves the cache intact —
                // avoids gratuitously blowing away hot entries when
                // an operator polls Refresh on a quiet corpus.
                if counts.reindexed > 0
                    || counts.removed > 0
                    || counts.tombstoned > 0
                    || counts.skipped > 0
                {
                    self.query_cache.invalidate_zone(&zone_for_invalidate);
                }
                Ok(Response::new(RefreshResponse {
                    reindexed_count: counts.reindexed,
                    removed_count: counts.removed,
                    unchanged_count: counts.unchanged,
                    skipped_count: counts.skipped,
                    error: None,
                    truncated: counts.truncated,
                }))
            }
            Err(err) => Ok(Response::new(RefreshResponse {
                reindexed_count: 0,
                removed_count: 0,
                unchanged_count: 0,
                skipped_count: 0,
                error: Some(err),
                truncated: false,
            })),
        }
    }

    // ── P8 Python-parity RPCs — stubs.  Each returns a typed
    //    error on the response's `error` field so callers can tell
    //    "not implemented yet" from a real failure.  Handlers land
    //    in the following commits.

    async fn batch_query(
        &self,
        request: Request<BatchQueryRequest>,
    ) -> Result<Response<BatchQueryResponse>, Status> {
        let req = request.into_inner();
        // #4610: the batch used to run strictly serially, so a caller
        // batching N queries paid N × full query latency — measured
        // live as the throughput ceiling on Koodle's cross-workspace
        // fan-out (DeepBuildAI/koodle#2176: ~30% workspace coverage,
        // client-side width increases only made it worse).  Each
        // query still runs the full cache + scoring pipeline via the
        // `query` handler, so per-query behaviour (result cache,
        // fusion, recency, error surfaces) is identical to singles.
        //
        // Two changes:
        //
        // 1. Pre-warm the query-embedding cache once per DUPLICATE
        //    embedding-needing text.  The fan-out pattern sends the
        //    same q across many path filters; without this, the
        //    parallel dispatch below would thundering-herd N
        //    identical embeds into the embedder's serialising
        //    session mutex on a cold cache.  Singleton texts skip
        //    pre-warm — they embed inside their own query, in
        //    parallel.  Pre-warm failures are ignored: each inner
        //    query retries and surfaces its own error exactly as
        //    before.
        //
        // 2. Bounded, order-preserving concurrent dispatch
        //    (`buffered`).  The bound keeps one giant benchmark
        //    batch from flooding the blocking pool; 1 restores the
        //    serial behaviour.
        let mut dupe_counts: std::collections::HashMap<&str, usize> =
            std::collections::HashMap::new();
        for q_req in &req.queries {
            let qt = QueryType::try_from(q_req.query_type).unwrap_or(QueryType::Unspecified);
            if matches!(qt, QueryType::Semantic | QueryType::Hybrid) && !q_req.q.is_empty() {
                *dupe_counts.entry(q_req.q.as_str()).or_default() += 1;
            }
        }
        let dupe_texts: Vec<String> = dupe_counts
            .into_iter()
            .filter(|(_, n)| *n > 1)
            .map(|(t, _)| t.to_string())
            .collect();
        if !dupe_texts.is_empty() {
            // Embedder init failure is fine here — the inner queries
            // will produce their per-query "unavailable" errors.
            if let Ok(embedder) = self.get_or_init_embedder() {
                let cache = Arc::clone(&self.embed_cache);
                let _ = tokio::task::spawn_blocking(move || {
                    for text in dupe_texts {
                        let _ = embed_query_cached(embedder.as_ref(), &cache, &text);
                    }
                })
                .await;
            }
        }

        let results: Vec<Result<Response<QueryResponse>, Status>> = futures_util::stream::iter(
            req.queries
                .into_iter()
                .map(|q_req| self.query(Request::new(q_req))),
        )
        .buffered(batch_query_concurrency())
        .collect()
        .await;
        let mut responses = Vec::with_capacity(results.len());
        for resp in results {
            responses.push(resp?.into_inner());
        }
        Ok(Response::new(BatchQueryResponse { responses }))
    }

    async fn index_documents(
        &self,
        request: Request<IndexDocumentsRequest>,
    ) -> Result<Response<IndexDocumentsResponse>, Status> {
        let indexing = IndexingGuard::enter(&self.indexing_ops);
        let req = request.into_inner();
        let default_zone = resolve_zone(&req.zone_id).to_string();
        let manager = Arc::clone(&self.manager);
        let (embedder, embed_broken) = self.indexing_embedder();
        let cache = Arc::clone(&self.query_cache);
        let outcome = tokio::task::spawn_blocking(move || {
            let _indexing = indexing; // held until the WORK ends, not the RPC
            do_index_documents(
                &manager,
                embedder.as_ref(),
                embed_broken,
                &default_zone,
                req.documents,
                &cache,
            )
        })
        .await
        .map_err(|e| Status::internal(format!("spawn_blocking joined error: {e}")))?;

        match outcome {
            Ok((indexed, skipped)) => Ok(Response::new(IndexDocumentsResponse {
                indexed_count: indexed,
                skipped_count: skipped,
                parked_paths: Vec::new(), // parked queue lands in step 4
                error: None,
            })),
            Err(err) => Ok(Response::new(IndexDocumentsResponse {
                indexed_count: 0,
                skipped_count: 0,
                parked_paths: Vec::new(),
                error: Some(err),
            })),
        }
    }

    async fn notify_file_change(
        &self,
        request: Request<NotifyFileChangeRequest>,
    ) -> Result<Response<NotifyFileChangeResponse>, Status> {
        let req = request.into_inner();
        let zone_id = resolve_zone(&req.zone_id).to_string();
        let change = req.change_type.clone();
        let path = req.path.clone();
        let manager = Arc::clone(&self.manager);
        let cache = Arc::clone(&self.query_cache);
        let outcome = tokio::task::spawn_blocking(move || {
            do_notify_file_change(&manager, &zone_id, &path, &change, &cache)
        })
        .await
        .map_err(|e| Status::internal(format!("spawn_blocking joined error: {e}")))?;

        match outcome {
            Ok(status) => Ok(Response::new(NotifyFileChangeResponse {
                status,
                error: None,
            })),
            Err(err) => Ok(Response::new(NotifyFileChangeResponse {
                status: String::new(),
                error: Some(err),
            })),
        }
    }

    async fn locate(
        &self,
        request: Request<LocateRequest>,
    ) -> Result<Response<LocateResponse>, Status> {
        let req = request.into_inner();
        let zone_id = resolve_zone(&req.zone_id).to_string();
        let path = req.path;
        let manager = Arc::clone(&self.manager);
        let zone_reply = zone_id.clone();
        let outcome = tokio::task::spawn_blocking(move || do_locate(&manager, &zone_id, &path))
            .await
            .map_err(|e| Status::internal(format!("spawn_blocking joined error: {e}")))?;

        match outcome {
            Ok((indexed, chunk_count, mtime_ms)) => Ok(Response::new(LocateResponse {
                indexed,
                chunk_count,
                mtime_ms,
                zone_id: zone_reply,
            })),
            // Errors surface as indexed=false — Locate is a check,
            // not an assertion.  Callers who need the error text
            // can look at server logs.
            Err(err) => {
                tracing::warn!(err = %err, "locate failed");
                Ok(Response::new(LocateResponse {
                    indexed: false,
                    chunk_count: 0,
                    mtime_ms: None,
                    zone_id: zone_reply,
                }))
            }
        }
    }

    async fn parked_list(
        &self,
        request: Request<ParkedListRequest>,
    ) -> Result<Response<ParkedListResponse>, Status> {
        let req = request.into_inner();
        let zone_id = resolve_zone(&req.zone_id).to_string();
        let manager = Arc::clone(&self.manager);
        let zone_for_entries = zone_id.clone();
        let outcome = tokio::task::spawn_blocking(move || {
            crate::parked_state::ParkedQueue::open_or_create(manager.zone_root(&zone_id))
                .map(|q| q.list())
                .map_err(|e| format!("open parked queue: {e}"))
        })
        .await
        .map_err(|e| Status::internal(format!("spawn_blocking joined error: {e}")))?;

        match outcome {
            Ok(entries) => Ok(Response::new(ParkedListResponse {
                entries: entries
                    .into_iter()
                    .map(|e| crate::search_proto::ParkedEntry {
                        path: e.path,
                        zone_id: zone_for_entries.clone(),
                        parked_at_ms: e.parked_at_ms,
                        reason: e.reason,
                    })
                    .collect(),
                error: None,
            })),
            Err(err) => Ok(Response::new(ParkedListResponse {
                entries: Vec::new(),
                error: Some(err),
            })),
        }
    }

    async fn parked_retry(
        &self,
        request: Request<ParkedRetryRequest>,
    ) -> Result<Response<ParkedRetryResponse>, Status> {
        let req = request.into_inner();
        let zone_id = resolve_zone(&req.zone_id).to_string();
        let paths = req.paths;
        let manager = Arc::clone(&self.manager);
        let outcome = tokio::task::spawn_blocking(move || -> Result<(u32, u32), String> {
            let q = crate::parked_state::ParkedQueue::open_or_create(manager.zone_root(&zone_id))
                .map_err(|e| format!("open parked queue: {e}"))?;
            // Empty paths ⇒ retry every parked doc (matches
            // Python).  "Retry" here just drops entries from the
            // queue — real retry means the caller follows with
            // IndexDocuments carrying the fresh text.  Same shape
            // Python has: /parked/retry acks the caller; the retry
            // actually happens on the next IndexDocuments call.
            let targets: Vec<String> = if paths.is_empty() {
                q.list().into_iter().map(|e| e.path).collect()
            } else {
                paths
            };
            let mut retried: u32 = 0;
            for p in &targets {
                if q.remove(p) {
                    retried += 1;
                }
            }
            q.save().map_err(|e| format!("save parked queue: {e}"))?;
            let still = q.len() as u32;
            Ok((retried, still))
        })
        .await
        .map_err(|e| Status::internal(format!("spawn_blocking joined error: {e}")))?;

        match outcome {
            Ok((retried, still)) => Ok(Response::new(ParkedRetryResponse {
                retried_count: retried,
                still_parked_count: still,
                error: None,
            })),
            Err(err) => Ok(Response::new(ParkedRetryResponse {
                retried_count: 0,
                still_parked_count: 0,
                error: Some(err),
            })),
        }
    }

    async fn parked_discard(
        &self,
        request: Request<ParkedDiscardRequest>,
    ) -> Result<Response<ParkedDiscardResponse>, Status> {
        let req = request.into_inner();
        let zone_id = resolve_zone(&req.zone_id).to_string();
        let paths = req.paths;
        let manager = Arc::clone(&self.manager);
        let outcome = tokio::task::spawn_blocking(move || -> Result<u32, String> {
            let q = crate::parked_state::ParkedQueue::open_or_create(manager.zone_root(&zone_id))
                .map_err(|e| format!("open parked queue: {e}"))?;
            let targets: Vec<String> = if paths.is_empty() {
                q.list().into_iter().map(|e| e.path).collect()
            } else {
                paths
            };
            let mut discarded: u32 = 0;
            for p in &targets {
                if q.remove(p) {
                    discarded += 1;
                }
            }
            q.save().map_err(|e| format!("save parked queue: {e}"))?;
            Ok(discarded)
        })
        .await
        .map_err(|e| Status::internal(format!("spawn_blocking joined error: {e}")))?;

        match outcome {
            Ok(count) => Ok(Response::new(ParkedDiscardResponse {
                discarded_count: count,
                error: None,
            })),
            Err(err) => Ok(Response::new(ParkedDiscardResponse {
                discarded_count: 0,
                error: Some(err),
            })),
        }
    }

    async fn add_indexed_directory(
        &self,
        request: Request<AddIndexedDirectoryRequest>,
    ) -> Result<Response<AddIndexedDirectoryResponse>, Status> {
        let req = request.into_inner();
        let zone_id = resolve_zone(&req.zone_id).to_string();
        let path = req.path;
        let manager = Arc::clone(&self.manager);
        let outcome = tokio::task::spawn_blocking(move || -> Result<bool, String> {
            let r = crate::indexed_dirs_state::IndexedDirsRegistry::open_or_create(
                manager.zone_root(&zone_id),
            )
            .map_err(|e| format!("open indexed_dirs: {e}"))?;
            let added = r.add(&path, current_time_ms());
            r.save().map_err(|e| format!("save indexed_dirs: {e}"))?;
            Ok(added)
        })
        .await
        .map_err(|e| Status::internal(format!("spawn_blocking joined error: {e}")))?;

        match outcome {
            Ok(added) => Ok(Response::new(AddIndexedDirectoryResponse {
                added,
                error: None,
            })),
            Err(err) => Ok(Response::new(AddIndexedDirectoryResponse {
                added: false,
                error: Some(err),
            })),
        }
    }

    async fn remove_indexed_directory(
        &self,
        request: Request<RemoveIndexedDirectoryRequest>,
    ) -> Result<Response<RemoveIndexedDirectoryResponse>, Status> {
        let req = request.into_inner();
        let zone_id = resolve_zone(&req.zone_id).to_string();
        let path = req.path;
        let manager = Arc::clone(&self.manager);
        let outcome = tokio::task::spawn_blocking(move || -> Result<bool, String> {
            let r = crate::indexed_dirs_state::IndexedDirsRegistry::open_or_create(
                manager.zone_root(&zone_id),
            )
            .map_err(|e| format!("open indexed_dirs: {e}"))?;
            let removed = r.remove(&path);
            r.save().map_err(|e| format!("save indexed_dirs: {e}"))?;
            Ok(removed)
        })
        .await
        .map_err(|e| Status::internal(format!("spawn_blocking joined error: {e}")))?;

        match outcome {
            Ok(removed) => Ok(Response::new(RemoveIndexedDirectoryResponse {
                removed,
                error: None,
            })),
            Err(err) => Ok(Response::new(RemoveIndexedDirectoryResponse {
                removed: false,
                error: Some(err),
            })),
        }
    }

    async fn list_indexed_directories(
        &self,
        request: Request<ListIndexedDirectoriesRequest>,
    ) -> Result<Response<ListIndexedDirectoriesResponse>, Status> {
        let req = request.into_inner();
        let zone_id = resolve_zone(&req.zone_id).to_string();
        let zone_for_reply = zone_id.clone();
        let manager = Arc::clone(&self.manager);
        let outcome = tokio::task::spawn_blocking(move || {
            crate::indexed_dirs_state::IndexedDirsRegistry::open_or_create(
                manager.zone_root(&zone_id),
            )
            .map(|r| r.list())
            .map_err(|e| format!("open indexed_dirs: {e}"))
        })
        .await
        .map_err(|e| Status::internal(format!("spawn_blocking joined error: {e}")))?;

        match outcome {
            Ok(entries) => Ok(Response::new(ListIndexedDirectoriesResponse {
                directories: entries
                    .into_iter()
                    .map(|e| crate::search_proto::IndexedDirectory {
                        path: e.path,
                        zone_id: zone_for_reply.clone(),
                        added_at_ms: e.added_at_ms,
                    })
                    .collect(),
                error: None,
            })),
            Err(err) => Ok(Response::new(ListIndexedDirectoriesResponse {
                directories: Vec::new(),
                error: Some(err),
            })),
        }
    }

    async fn set_zone_indexing_mode(
        &self,
        request: Request<SetZoneIndexingModeRequest>,
    ) -> Result<Response<SetZoneIndexingModeResponse>, Status> {
        let req = request.into_inner();
        let zone_id = resolve_zone(&req.zone_id).to_string();
        let mode = req.mode;
        let manager = Arc::clone(&self.manager);
        let outcome = tokio::task::spawn_blocking(move || -> Result<(), String> {
            let reg = crate::zone_modes_state::ZoneModesRegistry::open_or_create(
                manager.root().to_path_buf(),
            )
            .map_err(|e| format!("open zone_modes: {e}"))?;
            reg.set(&zone_id, &mode)
                .map_err(|e| format!("set zone mode: {e}"))?;
            reg.save().map_err(|e| format!("save zone_modes: {e}"))?;
            Ok(())
        })
        .await
        .map_err(|e| Status::internal(format!("spawn_blocking joined error: {e}")))?;

        match outcome {
            Ok(()) => Ok(Response::new(SetZoneIndexingModeResponse { error: None })),
            Err(err) => Ok(Response::new(SetZoneIndexingModeResponse {
                error: Some(err),
            })),
        }
    }

    async fn list_zone_indexing_modes(
        &self,
        _request: Request<ListZoneIndexingModesRequest>,
    ) -> Result<Response<ListZoneIndexingModesResponse>, Status> {
        let manager = Arc::clone(&self.manager);
        let outcome = tokio::task::spawn_blocking(move || {
            crate::zone_modes_state::ZoneModesRegistry::open_or_create(manager.root().to_path_buf())
                .map(|r| r.list())
                .map_err(|e| format!("open zone_modes: {e}"))
        })
        .await
        .map_err(|e| Status::internal(format!("spawn_blocking joined error: {e}")))?;

        match outcome {
            Ok(entries) => Ok(Response::new(ListZoneIndexingModesResponse {
                modes: entries
                    .into_iter()
                    .map(|(zone_id, mode)| crate::search_proto::ZoneIndexingMode { zone_id, mode })
                    .collect(),
                error: None,
            })),
            Err(err) => Ok(Response::new(ListZoneIndexingModesResponse {
                modes: Vec::new(),
                error: Some(err),
            })),
        }
    }

    async fn health(
        &self,
        _request: Request<HealthRequest>,
    ) -> Result<Response<HealthResponse>, Status> {
        // Health is trivial + valuable during cutover — land the
        // real impl inline rather than another commit.  Semantic
        // "degraded" surfaces when the embedder slot is empty; if
        // the plugin ever fails to open the FTS on request, that
        // caller sees `unavailable` at the failing RPC (this poll
        // says "healthy" because it doesn't itself open anything).
        let has_embedder = self.embedder_slot.lock().is_some();
        let status = if has_embedder { "healthy" } else { "degraded" };
        let detail = if has_embedder {
            "fts + ann online".to_string()
        } else {
            "fts online; semantic unavailable (embedder not initialised — normal on lite profile)"
                .to_string()
        };
        Ok(Response::new(HealthResponse {
            status: status.to_string(),
            detail,
        }))
    }

    async fn stats(
        &self,
        request: Request<StatsRequest>,
    ) -> Result<Response<StatsResponse>, Status> {
        let req = request.into_inner();
        let zone_id = resolve_zone(&req.zone_id).to_string();
        let manager = Arc::clone(&self.manager);
        let embedder_tag_dim = self
            .embedder_slot
            .lock()
            .as_ref()
            .map(|e| (e.tag().to_string(), e.dim()));
        // #4617: identity fields.  The live embedder's tag when one is
        // initialised; otherwise the CONFIGURED tag (env / feature
        // default) so pollers see the model identity without stats
        // ever forcing an ONNX session build.
        let embedding_model = embedder_tag_dim
            .as_ref()
            .map(|(tag, _)| tag.clone())
            .or_else(crate::embedder::configured_embedder_tag)
            .unwrap_or_default();
        // #4623: non-zero while explicit Index/IndexDocuments/Refresh
        // ops are in flight — "empty results" during that window mean
        // "still building", not "no matches".
        let indexing_in_progress = self.indexing_ops.load(std::sync::atomic::Ordering::SeqCst);
        let outcome = tokio::task::spawn_blocking(move || -> Result<StatsResponse, String> {
            // FTS side: count chunks + distinct paths in the zone.
            // FtsIndex doesn't expose a raw count today, so we open
            // it (which is cheap when already cached) and let the
            // caller derive from search + get_chunks_by_path.  For
            // v0 we surface zero counts on error — Stats is a poll
            // surface, not a source of truth.
            let fts_open = manager.get_or_open(&zone_id);
            let (fts_doc_count, fts_path_count) = match fts_open {
                Ok(_fts) => {
                    // Rough approximation — no cheap enumerator on
                    // tantivy without a full read.  Real accounting
                    // lands with the P5 IndexState-driven counter in
                    // a follow-up.  For now surface (parked
                    // + indexed_dirs) counts so callers see SOMETHING
                    // useful even if FTS totals are zero.
                    let state =
                        crate::index_state::IndexState::open_or_create(manager.zone_root(&zone_id));
                    match state {
                        Ok(s) => (s.len() as u32, s.len() as u32),
                        Err(_) => (0, 0),
                    }
                }
                Err(_) => (0, 0),
            };
            let ann_chunk_count = if let Some((tag, dim)) = embedder_tag_dim {
                manager
                    .get_or_open_ann(&zone_id, &tag, dim)
                    .map(|a| a.live_count() as u32)
                    .unwrap_or(0)
            } else {
                0
            };
            let parked_count =
                crate::parked_state::ParkedQueue::open_or_create(manager.zone_root(&zone_id))
                    .map(|q| q.len() as u32)
                    .unwrap_or(0);
            Ok(StatsResponse {
                fts_doc_count,
                fts_path_count,
                ann_chunk_count,
                parked_count,
                error: None,
                backend: SEARCH_BACKEND_NAME.to_string(),
                embedding_model,
                indexing_in_progress,
            })
        })
        .await
        .map_err(|e| Status::internal(format!("spawn_blocking joined error: {e}")))?;

        match outcome {
            Ok(resp) => Ok(Response::new(resp)),
            Err(err) => Ok(Response::new(StatsResponse {
                fts_doc_count: 0,
                fts_path_count: 0,
                ann_chunk_count: 0,
                parked_count: 0,
                error: Some(err),
                backend: SEARCH_BACKEND_NAME.to_string(),
                embedding_model: String::new(),
                indexing_in_progress: 0,
            })),
        }
    }
}

#[cfg(test)]
mod tests {
    //! Unit tests for the search primitives that DO NOT need a real
    //! kernel — pure logic (strip_root / grep_scan on in-memory
    //! strings).  Kernel-driven walk tests live in `tests/e2e.rs`
    //! where a MockKernelHandle can provide a canned filesystem.

    use super::*;

    #[test]
    fn strip_root_handles_trailing_slash() {
        assert_eq!(strip_root("/", "/foo/bar"), "foo/bar");
        assert_eq!(strip_root("/root", "/root/a/b"), "a/b");
        assert_eq!(strip_root("/root/", "/root/a/b"), "a/b");
    }

    #[test]
    fn zone_has_ann_dir_fails_closed_on_inspection_errors() {
        // Review R9: only positive absence (NotFound) may read false —
        // a permission/metadata failure must read "ANN may exist" so
        // outage-time indexing keeps documents retryable instead of
        // finalizing over vectors it merely could not see.
        let tmp = tempfile::tempdir().expect("tempdir");
        let manager = IndexManager::with_root(tmp.path().to_path_buf());

        // Absent zone root: positive absence.
        assert!(!zone_has_ann_dir(&manager, "fresh-zone"));

        // Present with an ann dir: present.
        std::fs::create_dir_all(manager.zone_root("z").join("ann-mock-v2")).unwrap();
        assert!(zone_has_ann_dir(&manager, "z"));

        // Present without ann dirs: absent.
        std::fs::create_dir_all(manager.zone_root("kw-only")).unwrap();
        assert!(!zone_has_ann_dir(&manager, "kw-only"));

        // Unreadable zone root (unix): inspection failure ⇒ assume present.
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let locked = manager.zone_root("locked");
            std::fs::create_dir_all(&locked).unwrap();
            std::fs::set_permissions(&locked, std::fs::Permissions::from_mode(0o000)).unwrap();
            let verdict = zone_has_ann_dir(&manager, "locked");
            // Restore perms BEFORE asserting so tempdir cleanup works
            // even on failure.
            std::fs::set_permissions(&locked, std::fs::Permissions::from_mode(0o755)).unwrap();
            assert!(verdict, "permission failure must read as ANN-present");
        }
    }

    #[test]
    fn grep_scan_finds_basic_match() {
        let re = regex::Regex::new(r"hello").unwrap();
        let mut out = Vec::new();
        let mut truncated = false;
        grep_scan(
            "line 1\nhello world\nline 3\n",
            "/f.txt",
            &re,
            0,
            0,
            false,
            100,
            &mut out,
            &mut truncated,
        );
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].line, "hello world");
        assert_eq!(out[0].line_number, 2);
        assert!(!truncated);
    }

    #[test]
    fn grep_scan_context_lines() {
        let re = regex::Regex::new(r"middle").unwrap();
        let mut out = Vec::new();
        let mut truncated = false;
        grep_scan(
            "a\nb\nc\nmiddle\ne\nf\ng\n",
            "/f.txt",
            &re,
            2,
            2,
            false,
            100,
            &mut out,
            &mut truncated,
        );
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].before, vec!["b".to_string(), "c".to_string()]);
        assert_eq!(out[0].after, vec!["e".to_string(), "f".to_string()]);
    }

    #[test]
    fn grep_scan_invert_returns_non_matches() {
        let re = regex::Regex::new(r"skip").unwrap();
        let mut out = Vec::new();
        let mut truncated = false;
        grep_scan(
            "skip\nkeep\nskip\nkeep2\n",
            "/f.txt",
            &re,
            0,
            0,
            true,
            100,
            &mut out,
            &mut truncated,
        );
        assert_eq!(out.len(), 2);
        assert_eq!(out[0].line, "keep");
        assert_eq!(out[1].line, "keep2");
    }

    #[test]
    fn grep_scan_respects_max_results() {
        let re = regex::Regex::new(r".*").unwrap();
        let mut out = Vec::new();
        let mut truncated = false;
        grep_scan(
            "a\nb\nc\nd\ne\n",
            "/f.txt",
            &re,
            0,
            0,
            false,
            2,
            &mut out,
            &mut truncated,
        );
        assert_eq!(out.len(), 2);
        assert!(truncated);
    }

    #[test]
    fn batch_query_concurrency_defaults_and_clamps() {
        // SAFETY: no other test in this binary reads
        // NEXUS_SEARCH_BATCH_CONCURRENCY (same convention as the
        // embedder.rs env tests).
        let saved = std::env::var(BATCH_QUERY_CONCURRENCY_ENV).ok();
        unsafe { std::env::remove_var(BATCH_QUERY_CONCURRENCY_ENV) };
        assert_eq!(batch_query_concurrency(), DEFAULT_BATCH_QUERY_CONCURRENCY);
        unsafe { std::env::set_var(BATCH_QUERY_CONCURRENCY_ENV, "8") };
        assert_eq!(batch_query_concurrency(), 8);
        unsafe { std::env::set_var(BATCH_QUERY_CONCURRENCY_ENV, "0") };
        assert_eq!(
            batch_query_concurrency(),
            1,
            "0 clamps to serial, not panic"
        );
        unsafe { std::env::set_var(BATCH_QUERY_CONCURRENCY_ENV, "9999") };
        assert_eq!(batch_query_concurrency(), MAX_BATCH_QUERY_CONCURRENCY);
        unsafe { std::env::set_var(BATCH_QUERY_CONCURRENCY_ENV, "not-a-number") };
        assert_eq!(batch_query_concurrency(), DEFAULT_BATCH_QUERY_CONCURRENCY);
        match saved {
            Some(v) => unsafe { std::env::set_var(BATCH_QUERY_CONCURRENCY_ENV, v) },
            None => unsafe { std::env::remove_var(BATCH_QUERY_CONCURRENCY_ENV) },
        }
    }
}
