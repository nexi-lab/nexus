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

use nexus_plugin_abi::KernelHandle;
use parking_lot::Mutex;
use tonic::{async_trait, Request, Response, Status};

use crate::ann_index::AnnHit;
use crate::embedder::{build_default_embedder, EmbedError, Embedder};
use crate::fts_index::FtsHit;
use crate::index_manager::IndexManager;
use crate::kernel_io::{self, DirEntry, KernelIoError, DT_DIR, DT_REG};
use crate::search_proto::search_service_server::SearchService;
use crate::search_proto::{
    GlobRequest, GlobResponse, GrepMatch, GrepRequest, GrepResponse, IndexRequest, IndexResponse,
    QueryRequest, QueryResponse, QueryResult, QueryType,
};

/// Server-side default when the caller sends `max_results = 0`.
/// Kept generous — a well-scoped `pattern` almost never hits this,
/// and callers who want stricter limits still set them explicitly.
const DEFAULT_GLOB_MAX: usize = 10_000;
const DEFAULT_GREP_MAX: usize = 1_000;
const DEFAULT_QUERY_LIMIT: usize = 10;
const DEFAULT_INDEX_MAX_DOCS: usize = 10_000;

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
}

impl SearchServiceImpl {
    /// Production constructor — index manager rooted at the platform
    /// default (`$NEXUS_DATA_DIR/plugins/search/`).  Embedder init
    /// is deferred to the first SemanticQuery (D3).
    pub fn new(handle: Arc<KernelHandle>) -> Self {
        Self {
            handle,
            manager: Arc::new(IndexManager::new()),
            embedder_slot: Arc::new(Mutex::new(None)),
        }
    }

    /// Test / operator constructor — inject a pre-configured
    /// `IndexManager` (typically rooted at a tempdir for tests, or
    /// at an explicit data volume for operators overriding the
    /// default storage location).  Embedder init is deferred to the
    /// first SemanticQuery.
    pub fn with_manager(handle: Arc<KernelHandle>, manager: Arc<IndexManager>) -> Self {
        Self {
            handle,
            manager,
            embedder_slot: Arc::new(Mutex::new(None)),
        }
    }

    /// Test / operator constructor — inject BOTH a pre-configured
    /// `IndexManager` and an [`Embedder`].  Bypasses the
    /// [`build_default_embedder`] discovery step so integration tests
    /// can use a [`MockEmbedder`](crate::embedder::MockEmbedder)
    /// without pointing at real model files.
    pub fn with_manager_and_embedder(
        handle: Arc<KernelHandle>,
        manager: Arc<IndexManager>,
        embedder: Arc<dyn Embedder>,
    ) -> Self {
        Self {
            handle,
            manager,
            embedder_slot: Arc::new(Mutex::new(Some(embedder))),
        }
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
    q: &str,
    zone_id: &str,
    limit: usize,
    path_filter: &str,
) -> Result<Vec<QueryResult>, String> {
    let ann = manager
        .get_or_open_ann(zone_id, embedder.tag(), embedder.dim())
        .map_err(|e| format!("open ann for zone {zone_id:?}: {e}"))?;

    let query_vec = embedder
        .embed_batch(&[q])
        .map_err(|e| format!("embed query: {e}"))?
        .into_iter()
        .next()
        .ok_or_else(|| "embed query: empty result".to_string())?;

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
        chunk_index: 0,
        chunk_text,
        score,
        zone_id: zone_id.to_string(),
        mtime_ms,
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
    }
}

/// Sinks the Index walker writes into.  `fts` is required (Index
/// always populates the keyword index); `ann` + `embedder` are
/// optional so a slim deployment or a mid-boot with no embedder
/// still gets keyword search — semantic just returns zero hits
/// until Index is retried with the embedder wired up.
struct IndexSinks<'a> {
    fts: &'a Arc<crate::fts_index::FtsIndex>,
    ann: Option<&'a Arc<crate::ann_index::AnnIndex>>,
    embedder: Option<&'a Arc<dyn Embedder>>,
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
fn do_index(
    handle: &KernelHandle,
    manager: &IndexManager,
    embedder: Option<&Arc<dyn Embedder>>,
    root_path: &str,
    zone_id: &str,
    recursive: bool,
    max_docs: usize,
) -> Result<(u32, u32), String> {
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

    let sinks = IndexSinks {
        fts: &fts,
        ann: ann.as_ref(),
        embedder,
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

    visit_result?;
    Ok((indexed, skipped))
}

/// Outcome of a single-file index attempt.  Distinguished so
/// `do_index` can tally added vs skipped without a per-call error
/// return (a skip is not an error).
enum IndexOne {
    Added,
    Skipped,
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
        return IndexOne::Skipped;
    }
    if bytes.len() > INDEX_MAX_FILE_BYTES {
        tracing::debug!(
            path = %vfs_path,
            len = bytes.len(),
            cap = INDEX_MAX_FILE_BYTES,
            "index: file over size cap — skipping",
        );
        return IndexOne::Skipped;
    }
    // Non-UTF8 files are treated as binary and skipped rather than
    // indexed as gibberish.  A future phase may want to add a
    // language-detect + transcode step, but that belongs with the
    // real chunker in P4.
    let text = match std::str::from_utf8(&bytes) {
        Ok(s) => s,
        Err(_) => {
            tracing::debug!(path = %vfs_path, "index: non-utf8 payload — skipping");
            return IndexOne::Skipped;
        }
    };
    let mtime_ms = kernel_io::sys_stat(handle, vfs_path)
        .ok()
        .and_then(|info| info.modified_at_ms);

    if let Err(e) = sinks.fts.add_document(vfs_path, 0, text, mtime_ms) {
        tracing::warn!(path = %vfs_path, err = %e, "index: fts add_document failed — skipping");
        return IndexOne::Skipped;
    }

    // ANN side — best-effort.  A failure to embed / add does NOT
    // fail the whole file; keyword still works, semantic just
    // misses this doc until the next Index retries.  Real batching
    // (embed 32 files at a time) lands in P4/P5 when the chunker
    // makes it worthwhile.
    if let (Some(ann), Some(embedder)) = (sinks.ann, sinks.embedder) {
        match embedder.embed_batch(&[text]) {
            Ok(mut vecs) => {
                if let Some(v) = vecs.pop() {
                    if let Err(e) = ann.add_vector(vfs_path, &v) {
                        tracing::warn!(
                            path = %vfs_path,
                            err = %e,
                            "index: ann add_vector failed — semantic misses this doc",
                        );
                    }
                }
            }
            Err(e) => {
                tracing::warn!(
                    path = %vfs_path,
                    err = %e,
                    "index: embed failed — semantic misses this doc",
                );
            }
        }
    }

    IndexOne::Added
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
        let q = req.q;
        if q.is_empty() {
            return Ok(Response::new(QueryResponse {
                results: Vec::new(),
                error: Some("q must not be empty".into()),
            }));
        }
        let zone_id = resolve_zone(&req.zone_id).to_string();
        let limit = if req.limit == 0 {
            DEFAULT_QUERY_LIMIT
        } else {
            req.limit as usize
        };
        let path_filter = req.path_filter;
        let query_type = QueryType::try_from(req.query_type).unwrap_or(QueryType::Unspecified);
        let manager = Arc::clone(&self.manager);

        let outcome = match query_type {
            QueryType::Unspecified | QueryType::Keyword => tokio::task::spawn_blocking(move || {
                do_keyword_query(&manager, &q, &zone_id, limit, &path_filter)
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
                tokio::task::spawn_blocking(move || {
                    do_semantic_query(&manager, &embedder, &q, &zone_id, limit, &path_filter)
                })
                .await
                .map_err(|e| Status::internal(format!("spawn_blocking joined error: {e}")))?
            }
            QueryType::Hybrid => Err("query_type=HYBRID not supported until P3".into()),
        };

        match outcome {
            Ok(results) => Ok(Response::new(QueryResponse {
                results,
                error: None,
            })),
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
        let embedder = self.get_or_init_embedder().ok();
        let outcome = tokio::task::spawn_blocking(move || {
            do_index(
                &handle,
                &manager,
                embedder.as_ref(),
                &root,
                &zone_id,
                recursive,
                max_docs,
            )
        })
        .await
        .map_err(|e| Status::internal(format!("spawn_blocking joined error: {e}")))?;

        match outcome {
            Ok((indexed_count, skipped_count)) => Ok(Response::new(IndexResponse {
                indexed_count,
                skipped_count,
                error: None,
            })),
            Err(err) => Ok(Response::new(IndexResponse {
                indexed_count: 0,
                skipped_count: 0,
                error: Some(err),
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
}
