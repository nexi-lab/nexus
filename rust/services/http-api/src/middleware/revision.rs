//! Read-your-writes fence for HTTP reads (Issue #4737).
//!
//! Extracts the fence parameters from the request
//! (`X-Nexus-Min-Revision` header or `?min_revision=` query param,
//! plus optional timeout), and enforces them by polling
//! `kernel.sys_stat(anchor).gen` until the required index is applied
//! on the serving node (or the timeout elapses).  Because raft applies
//! entries in order, a node whose stat shows the path at `gen >= G`
//! has applied every earlier entry of the zone — so a path-anchored
//! fence also fences listings, glob, grep and search for that zone.
//!
//! # Wire shape
//!
//! ```text
//!     X-Nexus-Min-Revision: /ws/a.txt@7        (or ?min_revision=/ws/a.txt@7)
//!     X-Nexus-Revision-Timeout-Ms: 5000        (or ?revision_timeout_ms=5000)
//! ```
//!
//! The read runs only after the fence is satisfied.  On success the
//! handler stamps `X-Nexus-Revision: <path>@<current_gen>` on its
//! response.  On timeout it returns `412 Precondition Failed` carrying
//! the current revision — never a stale answer.  Zone-anchored fences
//! (`root@1234`) answer `501 Not Implemented` on the pinned kernel
//! (`federation_cluster_info` Call is `unknown method`).
//!
//! # Contract
//!
//! `docs/architecture/consistency-contract.md` §3-4.
//!
//! # Handler wiring pattern
//!
//! ```ignore
//! use crate::middleware::revision::RevisionFence;
//! use crate::revision::REVISION_HEADER;
//!
//! pub async fn my_read_handler(
//!     State(state): State<AppState>,
//!     Extension(ctx): Extension<OperationContext>,
//!     fence: RevisionFence,          // extractor — no header ⇒ no-op fence
//!     Query(body): Query<MyReq>,
//! ) -> Result<Response, MyError> {
//!     let observed = fence.enforce(state.kernel.as_ref(), &ctx.zone_id).await?;
//!     // ... do the read against the backend ...
//!     let mut resp = Json(payload).into_response();
//!     if let Some(rev) = observed {
//!         resp.headers_mut().insert(REVISION_HEADER, rev.to_string().parse().unwrap());
//!     }
//!     Ok(resp)
//! }
//! ```

use std::sync::Arc;
use std::time::Duration;

use axum::extract::{FromRequestParts, Query};
use axum::http::request::Parts;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use kernel::kernel::syscall::KernelSyscall;
use serde::{Deserialize, Serialize};

use crate::revision::{
    ParseRevisionError, RevisionToken, DEFAULT_REVISION_TIMEOUT_MS, MAX_REVISION_TIMEOUT_MS,
    MIN_REVISION_HEADER, REVISION_HEADER, REVISION_TIMEOUT_HEADER,
};

/// Narrow interface the revision fence needs from a kernel: read the
/// current `gen` of a path.  Returns `0` when the path is not visible
/// on this node — treated identically to "gen 0" so an unseeded fence
/// times out honestly rather than becoming an existence oracle.
///
/// Kept trait-narrow — impls exist for the full [`KernelSyscall`] —
/// so tests can supply a fake gen source without stubbing every
/// syscall on [`KernelSyscall`] (whose result types are crate-private
/// in `kernel`, so a full outside-crate impl is not possible).
pub trait StatGen: Send + Sync + 'static {
    fn stat_gen(&self, path: &str, zone_id: &str) -> u64;
}

impl<K: KernelSyscall> StatGen for K {
    fn stat_gen(&self, path: &str, zone_id: &str) -> u64 {
        self.sys_stat(path, zone_id).map(|s| s.gen).unwrap_or(0)
    }
}

/// A [`StatGen`] that always reports `gen = 0` — used as the default
/// wiring in [`crate::AppState::for_tests`] so unit tests that do not
/// exercise the fence never need to plumb a real kernel.  A fenced
/// request against this backend times out and 412s, which is the
/// documented no-kernel behaviour of the fence.
#[derive(Debug, Clone, Copy)]
pub struct ZeroGenKernel;

impl StatGen for ZeroGenKernel {
    fn stat_gen(&self, _path: &str, _zone_id: &str) -> u64 {
        0
    }
}

/// Default zone the parser assumes when a caller sends a bare integer
/// (`?min_revision=42` ⇒ `root@42`).
const DEFAULT_ZONE: &str = "root";

/// Poll cadence for the fence loop — exponential backoff between the
/// two bounds so a fast-applying gen returns within ~20 ms and a slow
/// one settles at 4 polls per second instead of hammering the kernel.
const INITIAL_POLL: Duration = Duration::from_millis(20);
const MAX_POLL: Duration = Duration::from_millis(250);

/// Parsed fence parameters carried on a request.  Built by the
/// [`FromRequestParts`] impl below; every handler that supports the
/// fence takes this as a normal extractor arg (no header ⇒ empty
/// fence, `enforce` becomes a no-op).
#[derive(Debug, Clone, Default)]
pub struct RevisionFence {
    /// The revision the caller wants observed before the read is
    /// served, or `None` when they did not ask for a fence.
    pub required: Option<RevisionToken>,
    /// Bounded (`0..=MAX_REVISION_TIMEOUT_MS`) — the extractor
    /// clamps.  Only consulted when `required.is_some()`.
    pub timeout_ms: u64,
}

impl RevisionFence {
    /// True when the caller asked for a fence and `enforce` will
    /// therefore do real work.
    pub fn active(&self) -> bool {
        self.required.is_some()
    }

    /// Wait for the required revision or return a [`FenceError`]
    /// carrying the right HTTP response.  No-op when the caller did
    /// not ask for a fence — returns `Ok(None)`.
    ///
    /// `zone_id` scopes the `sys_stat` call to the caller's zone —
    /// pass `&ctx.zone_id` from the request's
    /// `Extension<OperationContext>`.  A fence on a path the caller
    /// cannot see stats as `gen = 0` and times out at 412 (never an
    /// existence oracle).
    ///
    /// The whole poll loop runs on ONE [`spawn_blocking`] task —
    /// `KernelSyscall::sys_stat` is synchronous, so calling it from
    /// the async handler would block the runtime; batching the loop
    /// into a single blocking task also avoids `Arc::clone` +
    /// spawn-overhead per iteration.  Zone-anchored fences answer 501
    /// today: `nexusd-cluster` does not expose
    /// `federation_cluster_info` yet.
    pub async fn enforce(
        &self,
        kernel: Arc<dyn StatGen>,
        zone_id: &str,
    ) -> Result<Option<RevisionToken>, FenceError> {
        let Some(required) = self.required.clone() else {
            return Ok(None);
        };
        if !required.is_path() {
            return Err(FenceError::ZoneRevisionUnavailable {
                min_revision: required,
            });
        }
        let started = std::time::Instant::now();
        let timeout = Duration::from_millis(self.timeout_ms);
        let anchor = required.anchor.clone();
        let zone = zone_id.to_string();
        let min_gen = required.index;

        // Single spawn_blocking hosts the whole poll loop.  `sys_stat`
        // is sync, so the loop runs on the blocking pool; the caller
        // still awaits at async speed.  std::thread::sleep is fine
        // here — the task owns the OS thread until the loop ends.
        let (satisfied, current) = tokio::task::spawn_blocking(move || {
            let deadline = std::time::Instant::now() + timeout;
            let mut interval = INITIAL_POLL;
            loop {
                let current = kernel.stat_gen(&anchor, &zone);
                if current >= min_gen {
                    return (true, current);
                }
                let now = std::time::Instant::now();
                if now >= deadline {
                    return (false, current);
                }
                let remaining = deadline - now;
                std::thread::sleep(interval.min(remaining));
                interval = (interval * 2).min(MAX_POLL);
            }
        })
        .await
        .map_err(|e| FenceError::ProbeFailed {
            min_revision: required.clone(),
            message: format!("sys_stat join error: {e}"),
        })?;

        let observed = RevisionToken {
            anchor: required.anchor.clone(),
            index: current,
        };
        if satisfied {
            Ok(Some(observed))
        } else {
            Err(FenceError::NotApplied {
                min_revision: required,
                current_revision: observed,
                waited_ms: u64::try_from(started.elapsed().as_millis()).unwrap_or(u64::MAX),
            })
        }
    }
}

/// Query-string carrier for the two fence params.  Kept private —
/// handlers extract [`RevisionFence`] itself, not this shape.
#[derive(Debug, Default, Deserialize)]
struct RevisionQuery {
    #[serde(default, rename = "min_revision")]
    min_revision: Option<String>,
    #[serde(default, rename = "revision_timeout_ms")]
    revision_timeout_ms: Option<String>,
}

/// axum extractor.  Header takes precedence, query param is the
/// fallback (a `curl` on a fixed URL can attach the fence via the
/// header without editing the query string).  Malformed input is a 400.
impl<S: Send + Sync> FromRequestParts<S> for RevisionFence {
    type Rejection = FenceError;

    async fn from_request_parts(parts: &mut Parts, _state: &S) -> Result<Self, Self::Rejection> {
        // Header wins so a caller can attach a fence to a fixed URL
        // without editing the query string.
        let header_raw = parts
            .headers
            .get(MIN_REVISION_HEADER)
            .and_then(|v| v.to_str().ok())
            .map(str::to_string);
        let timeout_header_raw = parts
            .headers
            .get(REVISION_TIMEOUT_HEADER)
            .and_then(|v| v.to_str().ok())
            .map(str::to_string);

        let Query(q): Query<RevisionQuery> = Query::from_request_parts(parts, &())
            .await
            .unwrap_or(Query(RevisionQuery::default()));

        let required = match header_raw.or(q.min_revision) {
            Some(raw) if !raw.trim().is_empty() => Some(
                RevisionToken::parse(&raw, DEFAULT_ZONE).map_err(FenceError::ParseMinRevision)?,
            ),
            _ => None,
        };

        let raw_timeout = timeout_header_raw.or(q.revision_timeout_ms);
        let timeout_ms = match raw_timeout {
            Some(raw) if !raw.trim().is_empty() => {
                let parsed: i64 = raw
                    .trim()
                    .parse()
                    .map_err(|_| FenceError::ParseTimeoutNotInt)?;
                if parsed < 0 || parsed as u64 > MAX_REVISION_TIMEOUT_MS {
                    return Err(FenceError::TimeoutOutOfRange {
                        max_ms: MAX_REVISION_TIMEOUT_MS,
                    });
                }
                parsed as u64
            }
            _ => DEFAULT_REVISION_TIMEOUT_MS,
        };

        Ok(RevisionFence {
            required,
            timeout_ms,
        })
    }
}

/// Fence rejection shapes and their HTTP status mapping:
///
/// * `ParseMinRevision` / `ParseTimeoutNotInt` / `TimeoutOutOfRange` → 400.
/// * `NotApplied` → 412 with `current_revision` / `waited_ms` payload
///   and `X-Nexus-Revision` header so the caller can retry or degrade.
/// * `ZoneRevisionUnavailable` → 501 (kernel does not expose zone
///   applied_index — client should fence on a path token instead).
/// * `ProbeFailed` → 503.
#[derive(Debug, thiserror::Error)]
pub enum FenceError {
    #[error("Invalid X-Nexus-Min-Revision: {0}. Expected '<path>@<gen>' as returned by a write.")]
    ParseMinRevision(#[source] ParseRevisionError),
    #[error("Invalid X-Nexus-Revision-Timeout-Ms: must be an integer number of ms")]
    ParseTimeoutNotInt,
    #[error("Invalid X-Nexus-Revision-Timeout-Ms: must be between 0 and {max_ms} ms")]
    TimeoutOutOfRange { max_ms: u64 },
    #[error("revision {} not applied on this node (current {}, waited {waited_ms} ms)",
        .min_revision, .current_revision)]
    NotApplied {
        min_revision: RevisionToken,
        current_revision: RevisionToken,
        waited_ms: u64,
    },
    #[error("this server cannot fence reads on a zone revision (min_revision={min_revision})")]
    ZoneRevisionUnavailable { min_revision: RevisionToken },
    #[error("revision probe failed for {min_revision}: {message}")]
    ProbeFailed {
        min_revision: RevisionToken,
        message: String,
    },
}

/// 412 body — carries the observed revision + wait time so the caller
/// can retry or fall back without re-issuing the read to guess where
/// this node is.
#[derive(Debug, Serialize)]
struct NotAppliedDetail {
    error: &'static str,
    message: String,
    min_revision: String,
    current_revision: String,
    waited_ms: u64,
}

#[derive(Debug, Serialize)]
struct UnavailableDetail {
    error: &'static str,
    message: String,
    min_revision: String,
}

#[derive(Debug, Serialize)]
struct ProbeFailedDetail {
    error: &'static str,
    message: String,
    min_revision: String,
}

impl IntoResponse for FenceError {
    fn into_response(self) -> Response {
        match self {
            FenceError::ParseMinRevision(_)
            | FenceError::ParseTimeoutNotInt
            | FenceError::TimeoutOutOfRange { .. } => {
                let msg = self.to_string();
                (
                    StatusCode::BAD_REQUEST,
                    Json(serde_json::json!({ "detail": msg })),
                )
                    .into_response()
            }
            FenceError::NotApplied {
                min_revision,
                current_revision,
                waited_ms,
            } => {
                let observed = current_revision.to_string();
                let mut resp = (
                    StatusCode::PRECONDITION_FAILED,
                    Json(serde_json::json!({
                        "detail": NotAppliedDetail {
                            error: "revision_not_applied",
                            message: format!(
                                "This node sees {} {} at revision {}; {} was required. Waited {} ms.",
                                if min_revision.is_path() { "path" } else { "zone" },
                                min_revision.anchor,
                                current_revision.index,
                                min_revision.index,
                                waited_ms,
                            ),
                            min_revision: min_revision.to_string(),
                            current_revision: observed.clone(),
                            waited_ms,
                        }
                    })),
                )
                    .into_response();
                if let Ok(v) = observed.parse() {
                    resp.headers_mut().insert(REVISION_HEADER, v);
                }
                resp
            }
            FenceError::ZoneRevisionUnavailable { min_revision } => (
                StatusCode::NOT_IMPLEMENTED,
                Json(serde_json::json!({
                    "detail": UnavailableDetail {
                        error: "zone_revision_unavailable",
                        message: "This server cannot fence reads on a zone revision. \
                             Use the path-anchored revision a write returns."
                            .to_string(),
                        min_revision: min_revision.to_string(),
                    }
                })),
            )
                .into_response(),
            FenceError::ProbeFailed {
                min_revision,
                message,
            } => (
                StatusCode::SERVICE_UNAVAILABLE,
                Json(serde_json::json!({
                    "detail": ProbeFailedDetail {
                        error: "zone_revision_probe_failed",
                        message,
                        min_revision: min_revision.to_string(),
                    }
                })),
            )
                .into_response(),
        }
    }
}

/// Set `X-Nexus-Revision: <token>` on a response.  Small helper so
/// handlers stamping the header do not each re-do the header-name +
/// parse dance.  Silently drops a token that fails to parse into an
/// `HeaderValue` (unreachable in practice — the token is
/// `<anchor>@<u64>` with an ASCII path).
pub fn stamp_revision(response: &mut Response, token: &RevisionToken) {
    if let Ok(v) = token.to_string().parse() {
        response.headers_mut().insert(REVISION_HEADER, v);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::Body;
    use axum::http::Request;

    async fn extract(req: Request<Body>) -> Result<RevisionFence, FenceError> {
        let (mut parts, _) = req.into_parts();
        RevisionFence::from_request_parts(&mut parts, &()).await
    }

    #[tokio::test]
    async fn no_header_no_query_gives_empty_fence() {
        let req = Request::get("/x").body(Body::empty()).unwrap();
        let fence = extract(req).await.unwrap();
        assert!(!fence.active());
        assert!(fence.required.is_none());
        assert_eq!(fence.timeout_ms, DEFAULT_REVISION_TIMEOUT_MS);
    }

    #[tokio::test]
    async fn header_wins_over_query() {
        let req = Request::get("/x?min_revision=/ws/from-query@1")
            .header(MIN_REVISION_HEADER, "/ws/from-header@9")
            .body(Body::empty())
            .unwrap();
        let fence = extract(req).await.unwrap();
        let req_tok = fence.required.expect("fence should be active");
        assert_eq!(req_tok.anchor, "/ws/from-header");
        assert_eq!(req_tok.index, 9);
    }

    #[tokio::test]
    async fn query_used_when_no_header() {
        let req = Request::get("/x?min_revision=/ws/a.txt@42")
            .body(Body::empty())
            .unwrap();
        let fence = extract(req).await.unwrap();
        let tok = fence.required.unwrap();
        assert_eq!(tok.anchor, "/ws/a.txt");
        assert_eq!(tok.index, 42);
    }

    #[tokio::test]
    async fn timeout_header_wins_over_query() {
        let req = Request::get("/x?revision_timeout_ms=100")
            .header(MIN_REVISION_HEADER, "/ws/a@1")
            .header(REVISION_TIMEOUT_HEADER, "300")
            .body(Body::empty())
            .unwrap();
        let fence = extract(req).await.unwrap();
        assert_eq!(fence.timeout_ms, 300);
    }

    #[tokio::test]
    async fn malformed_min_revision_rejects_400() {
        let req = Request::get("/x")
            .header(MIN_REVISION_HEADER, "/ws/a@notanumber")
            .body(Body::empty())
            .unwrap();
        let err = extract(req).await.expect_err("should reject");
        assert!(matches!(err, FenceError::ParseMinRevision(_)));
        let resp = err.into_response();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    }

    #[tokio::test]
    async fn timeout_out_of_range_rejects_400() {
        let req = Request::get("/x")
            .header(REVISION_TIMEOUT_HEADER, "99999999")
            .header(MIN_REVISION_HEADER, "/ws/a@1")
            .body(Body::empty())
            .unwrap();
        let err = extract(req).await.err().unwrap();
        assert!(matches!(err, FenceError::TimeoutOutOfRange { .. }));
    }

    #[tokio::test]
    async fn negative_timeout_rejects_400() {
        let req = Request::get("/x")
            .header(MIN_REVISION_HEADER, "/ws/a@1")
            .header(REVISION_TIMEOUT_HEADER, "-1")
            .body(Body::empty())
            .unwrap();
        let err = extract(req).await.err().unwrap();
        assert!(matches!(err, FenceError::TimeoutOutOfRange { .. }));
    }

    #[test]
    fn not_applied_412_body_carries_current_revision_and_stamps_header() {
        let err = FenceError::NotApplied {
            min_revision: RevisionToken {
                anchor: "/ws/a.txt".to_string(),
                index: 7,
            },
            current_revision: RevisionToken {
                anchor: "/ws/a.txt".to_string(),
                index: 3,
            },
            waited_ms: 5_000,
        };
        let resp = err.into_response();
        assert_eq!(resp.status(), StatusCode::PRECONDITION_FAILED);
        assert_eq!(
            resp.headers()
                .get(REVISION_HEADER)
                .and_then(|v| v.to_str().ok()),
            Some("/ws/a.txt@3")
        );
    }

    #[test]
    fn zone_revision_maps_to_501() {
        let err = FenceError::ZoneRevisionUnavailable {
            min_revision: RevisionToken {
                anchor: "root".to_string(),
                index: 1234,
            },
        };
        let resp = err.into_response();
        assert_eq!(resp.status(), StatusCode::NOT_IMPLEMENTED);
    }

    // ── Fence enforcement against a fake StatGen ──────────────────

    struct FakeGen {
        seq: std::sync::Mutex<Vec<u64>>,
    }
    impl FakeGen {
        fn new(seq: Vec<u64>) -> Self {
            Self {
                seq: std::sync::Mutex::new(seq),
            }
        }
    }
    impl StatGen for FakeGen {
        fn stat_gen(&self, _path: &str, _zone_id: &str) -> u64 {
            let mut seq = self.seq.lock().unwrap();
            if seq.is_empty() {
                0
            } else {
                seq.remove(0)
            }
        }
    }

    #[tokio::test]
    async fn enforce_no_fence_is_noop() {
        let fence = RevisionFence::default();
        let kernel: Arc<dyn StatGen> = Arc::new(FakeGen::new(vec![]));
        let observed = fence.enforce(kernel, "root").await.unwrap();
        assert!(observed.is_none());
    }

    #[tokio::test]
    async fn enforce_zone_token_501() {
        let fence = RevisionFence {
            required: Some(RevisionToken {
                anchor: "root".to_string(),
                index: 1,
            }),
            timeout_ms: 100,
        };
        let kernel: Arc<dyn StatGen> = Arc::new(FakeGen::new(vec![]));
        let err = fence.enforce(kernel, "root").await.err().unwrap();
        assert!(matches!(err, FenceError::ZoneRevisionUnavailable { .. }));
    }

    #[tokio::test]
    async fn enforce_path_satisfied_first_poll() {
        let fence = RevisionFence {
            required: Some(RevisionToken {
                anchor: "/ws/a.txt".to_string(),
                index: 3,
            }),
            timeout_ms: 500,
        };
        // First stat already at gen=5 (>=3).
        let kernel: Arc<dyn StatGen> = Arc::new(FakeGen::new(vec![5]));
        let observed = fence.enforce(kernel, "root").await.unwrap().unwrap();
        assert_eq!(observed.anchor, "/ws/a.txt");
        assert_eq!(observed.index, 5);
    }

    #[tokio::test]
    async fn enforce_path_satisfied_after_poll() {
        let fence = RevisionFence {
            required: Some(RevisionToken {
                anchor: "/ws/a.txt".to_string(),
                index: 5,
            }),
            timeout_ms: 500,
        };
        // gen advances 0 → 3 → 7 across polls.
        let kernel: Arc<dyn StatGen> = Arc::new(FakeGen::new(vec![0, 3, 7]));
        let observed = fence.enforce(kernel, "root").await.unwrap().unwrap();
        assert_eq!(observed.index, 7);
    }

    #[tokio::test]
    async fn enforce_path_timeout_412() {
        let fence = RevisionFence {
            required: Some(RevisionToken {
                anchor: "/ws/a.txt".to_string(),
                index: 99,
            }),
            timeout_ms: 60, // short — must give up after a few polls
        };
        // Ever-growing sequence but never reaches 99 in the window.
        let kernel: Arc<dyn StatGen> = Arc::new(FakeGen::new(vec![1, 2, 3, 4, 5, 6, 7, 8]));
        let err = fence.enforce(kernel, "root").await.err().unwrap();
        match err {
            FenceError::NotApplied {
                min_revision,
                current_revision,
                ..
            } => {
                assert_eq!(min_revision.index, 99);
                assert!(current_revision.index < 99);
            }
            other => panic!("expected NotApplied, got {other:?}"),
        }
    }
}
