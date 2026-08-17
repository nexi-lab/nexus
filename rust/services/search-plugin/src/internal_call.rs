//! Shared "this Query is an internal server-to-server call, skip
//! all outer middleware" signal used by BOTH peer fan-out and LLM
//! query expansion.
//!
//! # Two mechanisms, one concept
//!
//! - **Across processes** — [`INTERNAL_CALL_HEADER`] is stamped on
//!   outgoing peer requests.  Receiving plugins detect it and skip
//!   their own outer middleware.
//! - **Within the same process** — [`INSIDE_MIDDLEWARE`] is a
//!   `tokio::task_local!` guard set by each middleware's fan-out
//!   before it recursively re-enters `SearchService::query()`.  Any
//!   already-set guard on the current task means "outer middleware
//!   is running above me; don't recurse".
//!
//! One task-local + one header replaces the earlier `IN_EXPANSION` +
//! `IN_PEER_FANOUT` pair (three mechanisms for the same 'am I an
//! outer client call or an internal re-entry' question).  Adding a
//! fourth middleware later needs no new plumbing — it just wraps its
//! inner `self.query()` calls in `INSIDE_MIDDLEWARE.scope((), ...)`
//! and the header check upstream already covers it.
//!
//! # Why bundle both middleware behind one signal
//!
//! - **Loop safety** — a mutual-peer misconfig cannot amplify.
//! - **Cost safety** — a query received from a peer must NOT run
//!   LLM expansion on top: fleet-wide amplification of a single
//!   client query into N*M LLM calls is a real production hazard.
//!
//! Both boil down to "outer client calls run middleware; internal
//! calls do not".  Same rule, same signal.

/// gRPC metadata header set on outgoing peer fan-out requests.  The
/// receiving plugin skips all outer middleware (peer fan-out + LLM
/// query expansion) when this header is present.
///
/// Header value is opaque; presence-only.  tonic lower-cases metadata
/// keys and forwards them transparently.
pub const INTERNAL_CALL_HEADER: &str = "x-nexus-search-internal";

tokio::task_local! {
    /// Set by each middleware's fan-out before it recursively re-
    /// enters `SearchService::query()`.  The outer trait `query()`
    /// checks this at the top and, if set, skips all outer
    /// middleware — so the local branch of a fan-out (or a query-
    /// expansion variant call) does not re-enter its own layer or
    /// any sibling layer.
    pub static INSIDE_MIDDLEWARE: ();
}

/// Convenience: true if the current call should skip outer
/// middleware — either the metadata header is set (cross-process
/// signal from a peer) or `INSIDE_MIDDLEWARE` is scoped (in-process
/// signal from a middleware's own recursion).
pub fn is_internal_call<T>(request: &tonic::Request<T>) -> bool {
    if request.metadata().get(INTERNAL_CALL_HEADER).is_some() {
        return true;
    }
    INSIDE_MIDDLEWARE.try_with(|_| ()).is_ok()
}
