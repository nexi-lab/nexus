//! Revision token — read-your-writes fence across nodes (Issue #4737).
//!
//! A mutation returns a *revision token* a later read can wait on:
//!
//! ```text
//! <anchor>@<index>
//! ```
//!
//! * **Path-anchored** — anchor is the written path, index is its
//!   `gen` after the write (e.g. `/ws/a.txt@7`).  Raft applies log
//!   entries strictly in order, so a node whose `sys_stat` shows the
//!   path at `gen >= 7` has applied every earlier entry of that zone
//!   as well.  A path-anchored fence therefore fences directory
//!   listings, glob, grep and search for that zone — no kernel
//!   change and no zone-wide counter needed.
//! * **Zone-anchored** — `root@1234` where the index is the zone's
//!   raft `applied_index` on the serving node.  Emitted only when
//!   the kernel stamps `zone_id` / `applied_index` on the mutation
//!   response; honoured only when the kernel serves the
//!   `federation_cluster_info` Call.  A bare integer is a zone
//!   token for `root`.  The pinned `nexusd-cluster` does not expose
//!   `federation_cluster_info` — the fence answers 501 on those
//!   tokens until it does.
//!
//! Contract: `docs/architecture/consistency-contract.md`.
//!
//! # Layout
//!
//! The token type + parse/format live here (small, no I/O, cheap to
//! use from both write handlers and the fence extractor); the wait
//! loop + HTTP extractor live in `middleware::revision`.

use std::fmt;

/// Response header carrying the revision observed / stamped.
pub const REVISION_HEADER: &str = "X-Nexus-Revision";
/// Request header naming the minimum revision the caller wants observed
/// before the read is served.
pub const MIN_REVISION_HEADER: &str = "X-Nexus-Min-Revision";
/// Request header overriding the fence timeout (ms).
pub const REVISION_TIMEOUT_HEADER: &str = "X-Nexus-Revision-Timeout-Ms";
/// Query-param equivalent of `X-Nexus-Min-Revision` (headers take
/// precedence when both are present).
pub const MIN_REVISION_PARAM: &str = "min_revision";
/// Query-param equivalent of `X-Nexus-Revision-Timeout-Ms`.
pub const REVISION_TIMEOUT_PARAM: &str = "revision_timeout_ms";

/// Fence default: how long to wait for the observed revision to reach
/// the required one before returning 412.
pub const DEFAULT_REVISION_TIMEOUT_MS: u64 = 5_000;
/// Fence hard cap: a caller cannot ask us to hold a connection open
/// longer than this while polling.  30 s bounds tail latency of a
/// misconfigured client without cutting short a legitimate wait for a
/// slow-applying zone.
pub const MAX_REVISION_TIMEOUT_MS: u64 = 30_000;

/// Kernel Call method that returns the zone's raft `applied_index`.
/// The pinned `nexusd-cluster` does not expose it — fences on
/// zone-anchored tokens answer 501.
pub const CLUSTER_INFO_METHOD: &str = "federation_cluster_info";

/// A parsed revision token.  Either path-anchored (anchor starts with
/// `/`) or zone-anchored (anchor is a zone id).
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct RevisionToken {
    pub anchor: String,
    pub index: u64,
}

impl RevisionToken {
    /// True when this token is path-anchored (anchor is an absolute
    /// path).  Zone-anchored otherwise.
    pub fn is_path(&self) -> bool {
        self.anchor.starts_with('/')
    }

    /// Parse `<anchor>@<index>` or a bare `<index>` (zone token for
    /// `default_zone`).  Splits on the *last* `@` so path anchors may
    /// themselves contain `@`.
    pub fn parse(raw: &str, default_zone: &str) -> Result<Self, ParseRevisionError> {
        let text = raw.trim();
        if text.is_empty() {
            return Err(ParseRevisionError::Empty);
        }
        let (anchor, index_text) = match text.rfind('@') {
            Some(i) => (&text[..i], &text[i + 1..]),
            // Bare integer -> zone token for the default zone.
            None => (default_zone, text),
        };
        let anchor = anchor.trim();
        if anchor.is_empty() {
            return Err(ParseRevisionError::EmptyAnchor(raw.to_string()));
        }
        if !anchor.starts_with('/') && anchor.chars().any(|c| c.is_whitespace()) {
            return Err(ParseRevisionError::InvalidZone(raw.to_string()));
        }
        let index_text = index_text.trim();
        let index = index_text
            .parse::<u64>()
            .map_err(|_| ParseRevisionError::NonNegativeInteger(raw.to_string()))?;
        Ok(RevisionToken {
            anchor: anchor.to_string(),
            index,
        })
    }
}

impl fmt::Display for RevisionToken {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}@{}", self.anchor, self.index)
    }
}

/// Reasons a revision token failed to parse.  Every variant renders
/// as a stable, actionable message the fence extractor can hand back
/// verbatim as an HTTP 400 body.
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum ParseRevisionError {
    #[error("revision token is empty")]
    Empty,
    #[error("revision token has an empty anchor: {0:?}")]
    EmptyAnchor(String),
    #[error("revision token has an invalid zone id: {0:?}")]
    InvalidZone(String),
    #[error("revision token index must be a non-negative integer: {0:?}")]
    NonNegativeInteger(String),
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_path_token() {
        let t = RevisionToken::parse("/ws/a.txt@7", "root").unwrap();
        assert_eq!(t.anchor, "/ws/a.txt");
        assert_eq!(t.index, 7);
        assert!(t.is_path());
        assert_eq!(t.to_string(), "/ws/a.txt@7");
    }

    #[test]
    fn parse_zone_token() {
        let t = RevisionToken::parse("root@1234", "root").unwrap();
        assert_eq!(t.anchor, "root");
        assert_eq!(t.index, 1234);
        assert!(!t.is_path());
        assert_eq!(t.to_string(), "root@1234");
    }

    #[test]
    fn parse_bare_index_uses_default_zone() {
        let t = RevisionToken::parse("42", "eng").unwrap();
        assert_eq!(t.anchor, "eng");
        assert_eq!(t.index, 42);
        assert!(!t.is_path());
    }

    #[test]
    fn parse_path_with_at_in_it() {
        // Split on the LAST '@' so a path anchor may itself contain '@'.
        let t = RevisionToken::parse("/ws/a@b.txt@9", "root").unwrap();
        assert_eq!(t.anchor, "/ws/a@b.txt");
        assert_eq!(t.index, 9);
    }

    #[test]
    fn parse_trims_whitespace() {
        let t = RevisionToken::parse("  /ws/a.txt @ 3 ", "root").unwrap();
        assert_eq!(t.anchor, "/ws/a.txt");
        assert_eq!(t.index, 3);
    }

    #[test]
    fn parse_empty_rejects() {
        assert_eq!(
            RevisionToken::parse("", "root"),
            Err(ParseRevisionError::Empty)
        );
        assert_eq!(
            RevisionToken::parse("   ", "root"),
            Err(ParseRevisionError::Empty)
        );
    }

    #[test]
    fn parse_empty_anchor_rejects() {
        assert_eq!(
            RevisionToken::parse("@7", "root"),
            Err(ParseRevisionError::EmptyAnchor("@7".to_string()))
        );
    }

    #[test]
    fn parse_zone_with_whitespace_rejects() {
        assert_eq!(
            RevisionToken::parse("bad zone@1", "root"),
            Err(ParseRevisionError::InvalidZone("bad zone@1".to_string()))
        );
    }

    #[test]
    fn parse_non_integer_index_rejects() {
        assert!(matches!(
            RevisionToken::parse("/ws/a.txt@abc", "root"),
            Err(ParseRevisionError::NonNegativeInteger(_))
        ));
    }

    #[test]
    fn parse_negative_index_rejects() {
        // u64::parse fails on '-1'.
        assert!(matches!(
            RevisionToken::parse("/ws/a.txt@-1", "root"),
            Err(ParseRevisionError::NonNegativeInteger(_))
        ));
    }
}
