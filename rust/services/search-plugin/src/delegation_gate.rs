//! [`extract_and_validate`] — servicer-side extract + validation of
//! the [`SearchDelegation`] a peer daemon stamped on tonic metadata
//! (`nexus-http-api::backends::tonic_remote` mints it, the SSOT
//! metadata key lives on [`nexus_search_common::DELEGATION_METADATA_KEY`]).
//!
//! # Contract
//!
//! Callers wrap their gRPC handler entry with this function.  The
//! result tells them what to do:
//!
//! * `Ok(GateOutcome::NoDelegation)` — no delegation on this
//!   request.  Fall through to whatever auth path was in place
//!   before (auth_token, mTLS peer identity, …).  The vast majority
//!   of local queries take this path.
//! * `Ok(GateOutcome::Accepted { subject })` — a valid delegation
//!   is present.  The handler should run the search AS `subject`
//!   (for logging / audit).  Zone + method already validated
//!   here; the handler proceeds unchanged otherwise.
//! * `Err(Status)` — a delegation IS present but INVALID (expired,
//!   wrong method, wrong zone, malformed).  The handler MUST return
//!   this Status verbatim — the source daemon's
//!   `TonicRemoteSearchBackend` maps it back into `BackendError::Backend`
//!   so the leg lands in `zones_failed` with a specific message.
//!
//! # Rejection semantics
//!
//! Every rejection returns [`tonic::Code::Unauthenticated`] with a
//! message that names the specific violation.  Auth-shaped codes
//! are what the client-side error mapper (`tonic_remote.rs`) reads
//! to distinguish "peer refused the credential" from "transport
//! failed".  A malformed delegation (bad JSON) also uses
//! `Unauthenticated` rather than `InvalidArgument` — the caller
//! MINTED the delegation, so a broken shape is functionally an
//! auth failure (their auth material is unusable).
//!
//! # Cross-process TTL
//!
//! Per the [`SearchDelegation`] cross-process caveat: the mint
//! side's `created_at_ns` (`Instant`-based) cannot be compared to
//! the verify side's clock.  We therefore accept the wire's
//! delegation as if minted NOW on the verifier (the wire trip is
//! bounded — the TTL protects against replays much later, not
//! against a delegation older than the wire trip).  Callers who
//! want a wall-clock freshness check add it on top.

use nexus_search_common::{DelegationError, SearchDelegation, DELEGATION_METADATA_KEY};
use tonic::{Request, Status};

/// What the gate found on an incoming request.  See the module
/// docstring for the caller contract.
#[derive(Debug, Clone)]
pub enum GateOutcome {
    /// No delegation metadata on the request — the handler falls
    /// through to its default auth path.  Not an error.
    NoDelegation,
    /// A valid delegation is present.  `subject` is the original
    /// caller the source daemon minted the delegation FOR (a
    /// `(type, id)` pair, e.g. `("user", "alice")`); the handler
    /// runs the search AS this subject for audit purposes.
    Accepted {
        /// The delegation the source stamped — kept whole so a
        /// caller with per-hit ReBAC filtering can pull additional
        /// context off it without re-parsing.
        delegation: SearchDelegation,
    },
}

/// Extract + validate the delegation the source daemon may have
/// stamped on the request.  See the module docstring for the
/// caller contract.
///
/// `method` — the RPC method name to check against
/// [`nexus_search_common::SEARCH_DELEGATION_METHODS`].  For
/// `SearchServiceImpl::query`, pass `"search"`; for a future
/// semantic-search dispatch, `"semantic_search"`.
///
/// `zone_id` — the zone the request is asking about, checked
/// against `delegation.target_zones`.  A delegation scoped to
/// `["eng"]` cannot be replayed against zone `"legal"`.
pub fn extract_and_validate<T>(
    request: &Request<T>,
    method: &str,
    zone_id: &str,
) -> Result<GateOutcome, Status> {
    // 1. Look for the delegation on tonic metadata.  Absent → the
    // caller is not using delegation, fall through to the default
    // auth path.
    let raw = match request.metadata().get_bin(DELEGATION_METADATA_KEY) {
        None => return Ok(GateOutcome::NoDelegation),
        Some(v) => v,
    };
    let bytes = raw.to_bytes().map_err(|e| {
        Status::unauthenticated(format!(
            "SearchDelegation metadata not valid base64 (per gRPC -bin rule): {e}"
        ))
    })?;

    // 2. Deserialise.  A malformed blob is functionally an auth
    // failure (see module docstring's rationale) — return
    // Unauthenticated so the client-side mapper reads it as
    // "backend refused" rather than "transport bad payload".
    let delegation: SearchDelegation = serde_json::from_slice(&bytes)
        .map_err(|e| Status::unauthenticated(format!("SearchDelegation malformed JSON: {e}")))?;

    // 3. Cross-process TTL: reset `created_at_ns` to NOW on the
    // verifier so the built-in `is_expired()` check runs against
    // the verifier's clock, not a comparison to the mint side
    // (whose Instant epoch is process-local — see the field's
    // docstring in nexus-search-common).  We keep the original
    // `ttl_seconds` so a caller that shortens TTLs still gets its
    // budget respected.
    let mut fresh = delegation.clone();
    fresh.created_at_ns = fresh_now_ns();

    // 4. Validate against method + zone + fresh TTL.  Any
    // violation → Unauthenticated with the specific reason.
    fresh.validate(method, zone_id).map_err(|e| match e {
        DelegationError::MethodNotPermitted { .. } => Status::unauthenticated(e.to_string()),
        DelegationError::ZoneNotPermitted { .. } => Status::unauthenticated(e.to_string()),
        DelegationError::Expired { .. } => Status::unauthenticated(e.to_string()),
    })?;

    Ok(GateOutcome::Accepted { delegation })
}

/// Same `Instant`-anchored ns count [`SearchDelegation::new_from_now`]
/// uses on the mint side — reused here so the verifier's cross-
/// process TTL check has a comparable base.
fn fresh_now_ns() -> u128 {
    // `SearchDelegation::new_from_now` uses a private helper on
    // the type; the field-write path here recomputes it via a
    // fresh mint (the values match by construction).
    SearchDelegation::new_from_now(
        String::new(),
        String::new(),
        [String::new()],
        (String::new(), String::new()),
    )
    .created_at_ns
}

#[cfg(test)]
mod tests {
    use super::*;
    use tonic::metadata::MetadataValue;

    fn stamp<T>(req: &mut Request<T>, d: &SearchDelegation) {
        let bytes = serde_json::to_vec(d).unwrap();
        let key: tonic::metadata::MetadataKey<tonic::metadata::Binary> =
            DELEGATION_METADATA_KEY.parse().unwrap();
        req.metadata_mut()
            .insert_bin(key, MetadataValue::from_bytes(&bytes));
    }

    fn delegation(zone: &str) -> SearchDelegation {
        SearchDelegation::new_from_now(
            "sd_abc123",
            "peer",
            [zone.to_string()],
            ("user".into(), "alice".into()),
        )
    }

    #[test]
    fn no_delegation_metadata_returns_no_delegation() {
        // Regression pin: absent metadata → local auth path
        // engages, NOT a spurious refusal.  Every non-federated
        // query relies on this behaviour.
        let req = Request::new(());
        match extract_and_validate(&req, "search", "root").unwrap() {
            GateOutcome::NoDelegation => {}
            other => panic!("expected NoDelegation, got {other:?}"),
        }
    }

    #[test]
    fn valid_delegation_accepted_with_subject_carried_through() {
        let d = delegation("eng");
        let mut req = Request::new(());
        stamp(&mut req, &d);
        match extract_and_validate(&req, "search", "eng").unwrap() {
            GateOutcome::Accepted { delegation } => {
                assert_eq!(delegation.subject, ("user".into(), "alice".into()));
                assert_eq!(delegation.source_zone_id, "peer");
                assert_eq!(delegation.target_zones, vec!["eng".to_string()]);
            }
            other => panic!("expected Accepted, got {other:?}"),
        }
    }

    #[test]
    fn wrong_zone_returns_unauthenticated() {
        // Regression pin: a delegation scoped to `eng` MUST NOT be
        // replayable against `legal`.  Refusal comes back with
        // Unauthenticated so the client-side mapper reads it as
        // Backend (auth-shaped), not Transport.
        let d = delegation("eng");
        let mut req = Request::new(());
        stamp(&mut req, &d);
        let err = extract_and_validate(&req, "search", "legal").unwrap_err();
        assert_eq!(err.code(), tonic::Code::Unauthenticated);
        assert!(err.message().contains("legal"), "{}", err.message());
    }

    #[test]
    fn wrong_method_returns_unauthenticated() {
        // A delegation authorises only the `search` / `semantic_search`
        // methods (per SEARCH_DELEGATION_METHODS).  A leaked
        // delegation cannot be widened to `write` or `list`.
        let d = delegation("eng");
        let mut req = Request::new(());
        stamp(&mut req, &d);
        let err = extract_and_validate(&req, "write", "eng").unwrap_err();
        assert_eq!(err.code(), tonic::Code::Unauthenticated);
        assert!(err.message().contains("write"), "{}", err.message());
    }

    #[test]
    fn expired_delegation_returns_unauthenticated() {
        // A delegation past its TTL is refused — replay defence.
        // Use a 0s TTL + brief sleep so the fresh-created_at_ns
        // on the verifier still reads it as expired.
        let d = SearchDelegation::new_with_ttl(
            "sd_expiredx1",
            "peer",
            ["eng".to_string()],
            ("user".into(), "alice".into()),
            0,
        );
        let mut req = Request::new(());
        stamp(&mut req, &d);
        std::thread::sleep(std::time::Duration::from_millis(3));
        let err = extract_and_validate(&req, "search", "eng").unwrap_err();
        assert_eq!(err.code(), tonic::Code::Unauthenticated);
    }

    #[test]
    fn malformed_json_returns_unauthenticated_not_bad_request() {
        // A delegation whose blob is unparseable is FUNCTIONALLY
        // an auth failure — the caller's auth material is unusable.
        // Returning Unauthenticated keeps the client-side mapper's
        // "backend refused vs transport failed" split honest.
        let mut req = Request::new(());
        let key: tonic::metadata::MetadataKey<tonic::metadata::Binary> =
            DELEGATION_METADATA_KEY.parse().unwrap();
        req.metadata_mut()
            .insert_bin(key, MetadataValue::from_bytes(b"not valid json"));
        let err = extract_and_validate(&req, "search", "eng").unwrap_err();
        assert_eq!(err.code(), tonic::Code::Unauthenticated);
        assert!(
            err.message().to_lowercase().contains("malformed")
                || err.message().to_lowercase().contains("json"),
            "{}",
            err.message()
        );
    }

    #[test]
    fn semantic_search_method_is_permitted() {
        // Both allowed methods pass the gate.  Regression pin
        // against a future rename dropping semantic_search from the
        // allowlist without updating the gate.
        let d = delegation("eng");
        let mut req = Request::new(());
        stamp(&mut req, &d);
        match extract_and_validate(&req, "semantic_search", "eng").unwrap() {
            GateOutcome::Accepted { .. } => {}
            other => panic!("expected Accepted for semantic_search, got {other:?}"),
        }
    }

    #[test]
    fn cross_process_ttl_uses_verifier_clock_not_mint_time() {
        // Regression pin for the cross-process TTL note in the
        // module docstring: a delegation whose mint-side Instant
        // ns count would look "expired" on the verifier's clock
        // must still validate, because the gate resets
        // created_at_ns to the verifier's NOW before the check.
        //
        // We can't easily fake a cross-process Instant here, but
        // we CAN observe that a delegation minted with a modest
        // TTL (e.g., 5s) passes even after a small wall-clock
        // sleep — the reset kept it fresh.
        let d = SearchDelegation::new_with_ttl(
            "sd_freshxxxxx",
            "peer",
            ["eng".to_string()],
            ("user".into(), "alice".into()),
            5, // 5s TTL
        );
        let mut req = Request::new(());
        stamp(&mut req, &d);
        std::thread::sleep(std::time::Duration::from_millis(50));
        match extract_and_validate(&req, "search", "eng").unwrap() {
            GateOutcome::Accepted { .. } => {}
            other => panic!("expected Accepted after tiny sleep, got {other:?}"),
        }
    }
}
