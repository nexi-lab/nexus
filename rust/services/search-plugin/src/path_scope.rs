//! Query path scope — the union of a request's path prefixes.
//!
//! `QueryRequest.path_filter` plus `QueryRequest.path_filters`: a hit
//! is in scope when its path starts with ANY prefix.  An empty scope
//! is unscoped.  One query over several subtrees yields ONE fused
//! ranking; separate per-prefix queries cannot be merged by score,
//! because fused scores are normalised per result list.

/// Normalised set of path prefixes.  Duplicates and prefixes covered
/// by a shorter one are dropped, so the remaining prefixes are
/// disjoint and per-prefix counts can be summed.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct PathScope {
    prefixes: Vec<String>,
}

impl PathScope {
    /// Build from the wire fields.  Empty strings are ignored; an
    /// all-empty input is the unscoped scope.
    pub fn new<'a>(prefixes: impl IntoIterator<Item = &'a str>) -> Self {
        let mut all: Vec<&str> = prefixes.into_iter().filter(|p| !p.is_empty()).collect();
        // Shortest first, so a covering prefix is kept before the
        // longer prefixes it subsumes.
        all.sort_by(|a, b| a.len().cmp(&b.len()).then(a.cmp(b)));
        let mut kept: Vec<String> = Vec::with_capacity(all.len());
        for p in all {
            if !kept.iter().any(|k| p.starts_with(k.as_str())) {
                kept.push(p.to_string());
            }
        }
        kept.sort();
        Self { prefixes: kept }
    }

    /// Scope of a `QueryRequest`: `path_filter` OR any `path_filters`.
    pub fn from_request(path_filter: &str, path_filters: &[String]) -> Self {
        Self::new(std::iter::once(path_filter).chain(path_filters.iter().map(String::as_str)))
    }

    pub fn is_unscoped(&self) -> bool {
        self.prefixes.is_empty()
    }

    /// Disjoint prefixes, sorted.  Empty when unscoped.
    pub fn prefixes(&self) -> &[String] {
        &self.prefixes
    }

    /// The single prefix when exactly one applies.
    pub fn single(&self) -> Option<&str> {
        match self.prefixes.as_slice() {
            [only] => Some(only.as_str()),
            _ => None,
        }
    }

    pub fn matches(&self, path: &str) -> bool {
        self.prefixes.is_empty() || self.prefixes.iter().any(|p| path.starts_with(p.as_str()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_inputs_are_unscoped() {
        let s = PathScope::from_request("", &["".to_string()]);
        assert!(s.is_unscoped());
        assert!(s.matches("/anything"));
        assert_eq!(s.single(), None);
    }

    #[test]
    fn single_prefix_behaves_like_path_filter() {
        let s = PathScope::from_request("/ws/documents/", &[]);
        assert_eq!(s.single(), Some("/ws/documents/"));
        assert!(s.matches("/ws/documents/a.md"));
        assert!(!s.matches("/ws/notes/a.md"));
    }

    #[test]
    fn union_matches_any_prefix() {
        let s = PathScope::from_request(
            "",
            &["/ws/documents/".to_string(), "/ws/notes/".to_string()],
        );
        assert!(s.matches("/ws/documents/a.md"));
        assert!(s.matches("/ws/notes/b.md"));
        assert!(!s.matches("/ws/brief/c.md"));
        assert_eq!(s.single(), None);
    }

    #[test]
    fn covered_and_duplicate_prefixes_collapse() {
        let s = PathScope::from_request(
            "/ws/documents/",
            &[
                "/ws/".to_string(),
                "/ws/notes/".to_string(),
                "/ws/".to_string(),
                "/other/".to_string(),
            ],
        );
        assert_eq!(s.prefixes(), ["/other/", "/ws/"]);
    }
}
