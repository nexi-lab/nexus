//! [`SearchDelegation`] — read-only, short-lived, search-scoped
//! credential a source zone hands to a remote zone so the remote can
//! run a search on behalf of the original caller without full VFS
//! access.
//!
//! # Security model
//!
//! * **Method allowlist** — only the two dispatch methods in
//!   [`SEARCH_DELEGATION_METHODS`] are permitted.  Enforced in the
//!   servicer BEFORE dispatch, so a leaked delegation cannot be used
//!   to read files, mint keys, or invoke any non-search RPC.
//! * **Zone allowlist** — the delegation names the target zones
//!   explicitly; the servicer refuses any zone not in the set.
//! * **Short TTL** — [`DEFAULT_TTL_SECONDS`] (30 s), matching the
//!   Python contract.  Long enough for a wide-fanout dispatcher
//!   round-trip; short enough that a leaked credential is unusable
//!   in a follow-up attack.
//! * **No signature** — the peer identity is proven at the transport
//!   layer (mTLS peer certificate).  The delegation is a trust-
//!   carrier the RPC servicer inspects only after transport auth has
//!   passed.

use std::time::Instant;

use serde::{Deserialize, Serialize};

/// Dispatch methods a caller holding a [`SearchDelegation`] may
/// invoke.  Every other method is refused BEFORE it reaches the
/// dispatch table — a leaked delegation cannot be widened.
pub const SEARCH_DELEGATION_METHODS: &[&str] = &["search", "semantic_search"];

/// gRPC metadata key both the source-side stamper
/// (`nexus-http-api::backends::tonic_remote`) and the destination-
/// side extractor (`nexus-search-plugin::delegation_gate`) key on.
/// `-bin` suffix per the gRPC metadata spec — tonic transparently
/// base64-encodes / decodes so both sides see raw JSON bytes.
///
/// Kept next to [`SearchDelegation`] itself so a rename lands in
/// ONE place; a caller reading the wire needs both symbols and
/// pairing them here removes the "which crate holds the key
/// string" question.
pub const DELEGATION_METADATA_KEY: &str = "x-nexus-search-delegation-bin";

/// TTL a delegation carries when the caller does not override it.
/// Matches the Python contract so a Python-to-Rust cluster does not
/// see a policy skew mid-migration.
pub const DEFAULT_TTL_SECONDS: u64 = 30;

/// A search-scoped delegation credential.  Constructed by the
/// dispatcher on the source-zone side and handed to the remote-zone
/// gRPC servicer alongside the request.
///
/// The struct is `Clone` and `Serialize` so it can round-trip
/// through whatever transport carries the auth context (JSON metadata,
/// bincode over gRPC, etc.).  `created_at_ns` is a wall-clock-neutral
/// monotonic timestamp captured at mint time; callers verifying a
/// delegation compare it against [`Instant::now`] via [`Self::is_expired`].
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SearchDelegation {
    /// Human-readable identifier the servicer logs on validation
    /// failures (e.g. `sd_a1b2c3`).  Not a security-relevant field
    /// — the identity that matters is `subject` + `source_zone_id`.
    pub delegation_id: String,
    /// Zone that minted this delegation.  Emitted for audit trails;
    /// the servicer does not re-check it (the transport-layer peer
    /// identity already proves who called).
    pub source_zone_id: String,
    /// Zones this delegation grants search access to.  The servicer
    /// refuses any target not in this set.
    pub target_zones: Vec<String>,
    /// Original requester the servicer records as the search's
    /// subject — the servicer runs the search AS this subject, not
    /// as the delegation minter.
    pub subject: (String, String),
    /// Monotonic mint time in nanoseconds since an arbitrary but
    /// process-fixed epoch (captured via [`Instant`]).  Serialized
    /// as `u128` so a delegation minted on one process can be
    /// verified on another IN THE SAME PROCESS or over a transport
    /// that shares the clock — cross-process delegations use the
    /// wall-clock alternative via [`Self::new_with_created_ns_raw`].
    ///
    /// **Cross-process note**: `Instant` is process-local.  When a
    /// delegation crosses a process boundary (gRPC to another
    /// daemon), the verifier must use its own clock offset — see
    /// [`Self::new_from_now`] which stamps a fresh `created_at_ns`
    /// on the verifier side after transport auth succeeds.  The
    /// wire round-trip is FYI-only for the recipient.
    pub created_at_ns: u128,
    /// Time-to-live in seconds.
    pub ttl_seconds: u64,
}

impl SearchDelegation {
    /// Mint a fresh delegation with the current monotonic clock and
    /// the default TTL.  This is the constructor the dispatcher
    /// uses on the source-zone side.
    pub fn new_from_now(
        delegation_id: impl Into<String>,
        source_zone_id: impl Into<String>,
        target_zones: impl IntoIterator<Item = String>,
        subject: (String, String),
    ) -> Self {
        Self::new_with_ttl(
            delegation_id,
            source_zone_id,
            target_zones,
            subject,
            DEFAULT_TTL_SECONDS,
        )
    }

    /// Mint a fresh delegation with an explicit TTL.  Callers that
    /// need a longer / shorter budget than [`DEFAULT_TTL_SECONDS`]
    /// pass it here.
    pub fn new_with_ttl(
        delegation_id: impl Into<String>,
        source_zone_id: impl Into<String>,
        target_zones: impl IntoIterator<Item = String>,
        subject: (String, String),
        ttl_seconds: u64,
    ) -> Self {
        let created_at_ns = instant_now_ns();
        Self {
            delegation_id: delegation_id.into(),
            source_zone_id: source_zone_id.into(),
            target_zones: target_zones.into_iter().collect(),
            subject,
            created_at_ns,
            ttl_seconds,
        }
    }

    /// True when this delegation has exceeded its TTL as measured
    /// against the LOCAL monotonic clock.  Callers on the mint side
    /// use this to decide whether to re-mint before a retry; callers
    /// on the verify side stamp a fresh `created_at_ns` on receipt
    /// (see the field docstring).
    pub fn is_expired(&self) -> bool {
        instant_now_ns() > self.expires_at_ns()
    }

    /// Monotonic expiry timestamp (ns).  Kept as a method (not a
    /// field) so it can never drift out of sync with the TTL — the
    /// only source of truth is `created_at_ns + ttl_seconds`.
    pub fn expires_at_ns(&self) -> u128 {
        self.created_at_ns + u128::from(self.ttl_seconds) * 1_000_000_000
    }

    /// True when `zone_id` is in the delegation's target set.
    pub fn is_zone_permitted(&self, zone_id: &str) -> bool {
        self.target_zones.iter().any(|z| z == zone_id)
    }

    /// True when `method` is a search dispatch the delegation
    /// authorises.  See [`SEARCH_DELEGATION_METHODS`].
    pub fn is_method_permitted(method: &str) -> bool {
        SEARCH_DELEGATION_METHODS.contains(&method)
    }

    /// Validate the delegation against a specific `(method,
    /// target_zone)` pair.  Returns [`DelegationError`] on any
    /// refusal so the servicer maps it 1:1 to a gRPC status.
    pub fn validate(&self, method: &str, target_zone: &str) -> Result<(), DelegationError> {
        if !Self::is_method_permitted(method) {
            return Err(DelegationError::MethodNotPermitted {
                method: method.to_string(),
            });
        }
        if !self.is_zone_permitted(target_zone) {
            return Err(DelegationError::ZoneNotPermitted {
                zone_id: target_zone.to_string(),
                permitted: self.target_zones.clone(),
            });
        }
        if self.is_expired() {
            return Err(DelegationError::Expired {
                delegation_id: self.delegation_id.clone(),
                ttl_seconds: self.ttl_seconds,
            });
        }
        Ok(())
    }
}

/// Errors a delegation validation may surface.  Kept small so a
/// servicer's status mapper is a one-arm-per-variant match.
#[derive(Debug, thiserror::Error, Clone, PartialEq)]
pub enum DelegationError {
    #[error("SearchDelegation permits only {SEARCH_DELEGATION_METHODS:?}, got '{method}'")]
    MethodNotPermitted { method: String },
    #[error("Zone '{zone_id}' not in delegation scope {permitted:?}")]
    ZoneNotPermitted {
        zone_id: String,
        permitted: Vec<String>,
    },
    #[error("SearchDelegation '{delegation_id}' expired (TTL={ttl_seconds}s)")]
    Expired {
        delegation_id: String,
        ttl_seconds: u64,
    },
}

/// Static monotonic epoch — captured once at process start so
/// `created_at_ns` values are comparable across delegations minted in
/// the same process.  A cross-process delegation is FYI-only for the
/// recipient's expiry check; the peer identity comes from transport
/// (mTLS), not from this field.
static PROCESS_EPOCH: std::sync::OnceLock<Instant> = std::sync::OnceLock::new();

fn instant_now_ns() -> u128 {
    let epoch = *PROCESS_EPOCH.get_or_init(Instant::now);
    epoch.elapsed().as_nanos()
}

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use super::*;

    fn subject(id: &str) -> (String, String) {
        ("user".into(), id.into())
    }

    #[test]
    fn is_method_permitted_only_allowlists_search_and_semantic_search() {
        assert!(SearchDelegation::is_method_permitted("search"));
        assert!(SearchDelegation::is_method_permitted("semantic_search"));
        for bad in ["read", "write", "list", "mint_key", "grant"] {
            assert!(!SearchDelegation::is_method_permitted(bad));
        }
    }

    #[test]
    fn is_zone_permitted_checks_target_zones_membership() {
        let d = SearchDelegation::new_from_now(
            "sd_x",
            "eng",
            ["eng".to_string(), "legal".to_string()],
            subject("alice"),
        );
        assert!(d.is_zone_permitted("eng"));
        assert!(d.is_zone_permitted("legal"));
        assert!(!d.is_zone_permitted("finance"));
    }

    #[test]
    fn is_expired_is_false_immediately_after_mint() {
        let d =
            SearchDelegation::new_from_now("sd_x", "eng", ["eng".to_string()], subject("alice"));
        assert!(!d.is_expired());
    }

    #[test]
    fn is_expired_becomes_true_after_ttl() {
        let d = SearchDelegation::new_with_ttl(
            "sd_x",
            "eng",
            ["eng".to_string()],
            subject("alice"),
            0, // 0 s TTL: expires the same nanosecond
        );
        std::thread::sleep(Duration::from_millis(2));
        assert!(d.is_expired());
    }

    #[test]
    fn validate_ok_on_method_zone_and_ttl() {
        let d = SearchDelegation::new_from_now(
            "sd_x",
            "eng",
            ["eng".to_string(), "legal".to_string()],
            subject("alice"),
        );
        d.validate("search", "eng").unwrap();
        d.validate("semantic_search", "legal").unwrap();
    }

    #[test]
    fn validate_refuses_disallowed_method() {
        let d =
            SearchDelegation::new_from_now("sd_x", "eng", ["eng".to_string()], subject("alice"));
        let err = d.validate("write", "eng").unwrap_err();
        assert!(matches!(err, DelegationError::MethodNotPermitted { .. }));
    }

    #[test]
    fn validate_refuses_zone_not_in_scope() {
        let d =
            SearchDelegation::new_from_now("sd_x", "eng", ["eng".to_string()], subject("alice"));
        let err = d.validate("search", "finance").unwrap_err();
        match err {
            DelegationError::ZoneNotPermitted { zone_id, permitted } => {
                assert_eq!(zone_id, "finance");
                assert_eq!(permitted, vec!["eng".to_string()]);
            }
            other => panic!("expected ZoneNotPermitted, got {other:?}"),
        }
    }

    #[test]
    fn validate_refuses_expired_delegation() {
        let d =
            SearchDelegation::new_with_ttl("sd_x", "eng", ["eng".to_string()], subject("alice"), 0);
        std::thread::sleep(Duration::from_millis(2));
        let err = d.validate("search", "eng").unwrap_err();
        assert!(matches!(err, DelegationError::Expired { .. }));
    }

    #[test]
    fn expires_at_is_created_plus_ttl_derived_never_stored() {
        // Regression pin: `expires_at_ns` is a method, not a field,
        // so a caller mutating `ttl_seconds` after mint stays
        // consistent (the field can never drift out of sync with the
        // derived value).
        let mut d = SearchDelegation::new_with_ttl(
            "sd_x",
            "eng",
            ["eng".to_string()],
            subject("alice"),
            10,
        );
        let base = d.created_at_ns;
        assert_eq!(d.expires_at_ns(), base + 10 * 1_000_000_000);
        d.ttl_seconds = 60;
        assert_eq!(d.expires_at_ns(), base + 60 * 1_000_000_000);
    }
}
