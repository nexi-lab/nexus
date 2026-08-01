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
use tonic::{async_trait, Request, Response, Status};

use crate::kernel_io::{self, DirEntry, KernelIoError, DT_DIR, DT_REG};
use crate::search_proto::search_service_server::SearchService;
use crate::search_proto::{GlobRequest, GlobResponse, GrepMatch, GrepRequest, GrepResponse};

/// Server-side default when the caller sends `max_results = 0`.
/// Kept generous — a well-scoped `pattern` almost never hits this,
/// and callers who want stricter limits still set them explicitly.
const DEFAULT_GLOB_MAX: usize = 10_000;
const DEFAULT_GREP_MAX: usize = 1_000;

/// Belt-and-suspenders per-file size cap for grep — a 100 MB
/// binary blob would otherwise stall a single request for seconds.
/// Files above this size are skipped with a `tracing::debug` log.
const GREP_MAX_FILE_BYTES: usize = 8 * 1024 * 1024;

pub struct SearchServiceImpl {
    handle: Arc<KernelHandle>,
}

impl SearchServiceImpl {
    pub fn new(handle: Arc<KernelHandle>) -> Self {
        Self { handle }
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
    matches.sort_by_key(|m| std::cmp::Reverse(mtimes.get(m.path.as_str()).copied().unwrap_or(i64::MIN)));
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
