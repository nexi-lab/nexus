//! Encoding + decoding for the ReBAC tuple wire format persisted in
//! the [`crate::store::ReBACTupleStore`] key space.
//!
//! # The key convention
//!
//! ```text
//!   <zone>|<object_type>|<object_id>|<relation>|<subject_type>|<subject_id>[|<subject_relation>]
//! ```
//!
//! * Pipe-delimited, 6 or 7 segments (the 7th is the optional
//!   `subject_relation` for userset-as-subject tuples — the Zanzibar
//!   "members of subject_type:subject_id have this relation on the
//!   object" pattern).
//! * The `<zone>` prefix is the tuple's scoping zone (nexus's mount-
//!   root concept); every downstream reader filters by prefix to
//!   materialise a per-zone graph.  Matches the convention documented
//!   on [`crate::inmem::InMemoryReBACTupleStore`] and the raft-store
//!   docstring.
//!
//! # Why a string encoding (not bincode / serde_json)
//!
//! The store's `list()` returns `(String, Vec<u8>)` — the KEY is what
//! [`crate::store::ReBACTupleStore::zone_revision`] indexes by, and
//! the graph cache rebuilds the graph by walking `list()` output.  A
//! string key means:
//!
//!   * `zone_of(key)` is `str::split_once('|')` (no deserialisation)
//!   * A raft-log dump is human-inspectable — an operator can `grep
//!     "^root|doc:x"` an on-disk `sm_control_state` snapshot to audit
//!     grants.  Bincode / JSON payloads would obscure this.
//!   * The value is free for grant metadata (granter, timestamp) —
//!     the tuple's identity lives entirely in the key.
//!
//! # Segment escaping — fail-loud, not silent
//!
//! A literal `|` in any segment would ambiguate the split.  Real
//! ReBAC entities (users, files, groups) never carry pipes in
//! identifiers; a caller that tries to encode one is programmer
//! error — [`encode`] returns `Err`, not a silently-escaped key.
//! This surfaces the mistake at the write site instead of at graph-
//! rebuild time where a decode-fail would silently drop the tuple.

use crate::store::ReBACTupleStoreError;
use lib::types::ReBACTuple;

/// Segment delimiter for the tuple key.  Kept private so callers do
/// not build ad-hoc keys — every write and read goes through
/// [`encode`] / [`decode`].
const SEP: char = '|';

/// Encode a `(zone, tuple)` pair into the store's key format.
///
/// Fails when any segment contains the delimiter [`SEP`] — see the
/// module docstring for the fail-loud rationale.  The `subject_
/// relation` (7th segment) is emitted only when `Some`.
pub fn encode(zone: &str, tuple: &ReBACTuple) -> Result<String, ReBACTupleStoreError> {
    let subject_relation_seg: Option<&str> = tuple.subject_relation.as_deref();
    let mut segments: Vec<&str> = vec![
        zone,
        &tuple.object_type,
        &tuple.object_id,
        &tuple.relation,
        &tuple.subject_type,
        &tuple.subject_id,
    ];
    if let Some(sr) = subject_relation_seg {
        segments.push(sr);
    }
    for seg in &segments {
        if seg.contains(SEP) {
            return Err(ReBACTupleStoreError::Backend(format!(
                "rebac tuple segment contains reserved delimiter {SEP:?}: {seg:?}"
            )));
        }
    }
    Ok(segments.join(&SEP.to_string()))
}

/// Extract the zone from a key without a full decode.  Returns
/// `None` for a malformed key (no `|`); callers treat that as "not
/// in the requested zone" and skip.
///
/// `#[inline]` — hit per-entry during the graph-cache rebuild scan
/// (one call per key in `store.list()`); avoiding the call cost
/// keeps the O(N) scan tight.
#[inline]
pub fn zone_of(key: &str) -> Option<&str> {
    key.split_once(SEP).map(|(z, _)| z)
}

/// Decode a key back into a `(zone, ReBACTuple)`.  Returns `None`
/// for any malformed key — the graph-cache rebuild treats a malformed
/// key as a soft skip (the tuple is invisible to the graph, but
/// other tuples in the zone continue to load), NOT a hard error, so
/// one bad row can't wedge the whole zone's permission plane.
///
/// Malformed = fewer than 6 or more than 7 segments.  A caller that
/// wants to audit malformed keys should walk `store.list()`
/// separately and check for `decode() == None` rows.
pub fn decode(key: &str) -> Option<(String, ReBACTuple)> {
    let parts: Vec<&str> = key.split(SEP).collect();
    let (subject_relation, subject_id_idx) = match parts.len() {
        6 => (None, 5),
        7 => (Some(parts[6].to_string()), 5),
        _ => return None,
    };
    // Reject empty required segments — Zanzibar entities are never
    // "" and a "" segment nearly always means the caller mis-built
    // the key.  Same soft-skip posture as segment-count mismatch.
    if parts[..6].iter().any(|s| s.is_empty()) {
        return None;
    }
    Some((
        parts[0].to_string(),
        ReBACTuple {
            object_type: parts[1].to_string(),
            object_id: parts[2].to_string(),
            relation: parts[3].to_string(),
            subject_type: parts[4].to_string(),
            subject_id: parts[subject_id_idx].to_string(),
            subject_relation,
        },
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn t(
        obj_type: &str,
        obj_id: &str,
        rel: &str,
        subj_type: &str,
        subj_id: &str,
        subj_rel: Option<&str>,
    ) -> ReBACTuple {
        ReBACTuple {
            object_type: obj_type.to_string(),
            object_id: obj_id.to_string(),
            relation: rel.to_string(),
            subject_type: subj_type.to_string(),
            subject_id: subj_id.to_string(),
            subject_relation: subj_rel.map(str::to_string),
        }
    }

    #[test]
    fn encode_direct_tuple_produces_six_segment_key() {
        let key = encode("root", &t("doc", "a", "reader", "user", "alice", None)).expect("encode");
        assert_eq!(key, "root|doc|a|reader|user|alice");
    }

    #[test]
    fn encode_userset_tuple_appends_subject_relation_segment() {
        let key = encode(
            "root",
            &t("doc", "a", "reader", "group", "eng", Some("member")),
        )
        .expect("encode");
        assert_eq!(key, "root|doc|a|reader|group|eng|member");
    }

    #[test]
    fn encode_rejects_segment_containing_delimiter() {
        // A pipe in any segment would ambiguate the split; a silent
        // escape would drop the tuple at decode time.  Better to
        // surface the bug at the write site.
        let bad = t("doc", "a|b", "reader", "user", "alice", None);
        assert!(encode("root", &bad).is_err(), "must reject pipe in id");

        let bad_zone_result = encode("root|other", &t("doc", "a", "r", "u", "alice", None));
        assert!(bad_zone_result.is_err(), "must reject pipe in zone");
    }

    #[test]
    fn decode_direct_tuple_roundtrips() {
        let original = t("doc", "a", "reader", "user", "alice", None);
        let key = encode("root", &original).expect("encode");
        let (zone, decoded) = decode(&key).expect("decode ok");
        assert_eq!(zone, "root");
        assert_eq!(decoded.object_type, original.object_type);
        assert_eq!(decoded.object_id, original.object_id);
        assert_eq!(decoded.relation, original.relation);
        assert_eq!(decoded.subject_type, original.subject_type);
        assert_eq!(decoded.subject_id, original.subject_id);
        assert_eq!(decoded.subject_relation, original.subject_relation);
    }

    #[test]
    fn decode_userset_tuple_roundtrips() {
        let original = t("doc", "a", "reader", "group", "eng", Some("member"));
        let key = encode("root", &original).expect("encode");
        let (zone, decoded) = decode(&key).expect("decode ok");
        assert_eq!(zone, "root");
        assert_eq!(decoded.subject_relation.as_deref(), Some("member"));
    }

    #[test]
    fn decode_returns_none_for_short_key() {
        // Soft-skip posture — a malformed key is invisible to the
        // graph rebuild, other tuples in the zone still load.
        assert!(decode("root|doc|a|reader|user").is_none());
    }

    #[test]
    fn decode_returns_none_for_too_many_segments() {
        assert!(decode("root|doc|a|reader|user|alice|member|extra").is_none());
    }

    #[test]
    fn decode_returns_none_for_empty_required_segment() {
        // An empty segment nearly always means the caller mis-built
        // the key; same soft-skip as segment-count mismatch.
        assert!(decode("root||a|reader|user|alice").is_none());
    }

    #[test]
    fn zone_of_extracts_first_segment_without_full_decode() {
        assert_eq!(
            zone_of("root|doc|a|reader|user|alice"),
            Some("root"),
            "extract zone in O(prefix) without allocating",
        );
        assert_eq!(
            zone_of("shared|folder|home|writer|user|bob|member"),
            Some("shared"),
            "userset-tuple zone extraction is the same shape",
        );
    }

    #[test]
    fn zone_of_returns_none_for_key_without_delimiter() {
        assert_eq!(zone_of("no_delimiter"), None);
    }

    #[test]
    fn zone_of_and_decode_agree_on_the_zone() {
        // Regression pin: a future encoding change must keep the
        // fast-path `zone_of` in sync with the full decode.  The
        // graph-cache rebuild walks `list()` and filters via
        // `zone_of` — a mismatch would drop tuples silently.
        let key =
            encode("shared", &t("doc", "a", "reader", "user", "alice", None)).expect("encode");
        let (decoded_zone, _) = decode(&key).expect("decode");
        assert_eq!(zone_of(&key), Some(decoded_zone.as_str()));
    }
}
