//! Static Day-1 federation topology — env-var parsers.
//!
//! The cluster binary (rust/cluster/) reads these at startup and
//! feeds the result into [`crate::ZoneManager::bootstrap_static`] +
//! [`crate::ZoneManager::apply_topology`].
//!
//! Mirrors the contract Python `nexus.raft.federation` used to expose
//! before deletion: every node in the federation reads the same
//! environment and converges on the same topology.

use std::collections::BTreeMap;

/// Variable name carrying the comma-separated list of non-root zone
/// ids to create (e.g. `"corp,corp-eng,family"`).
pub const ENV_FEDERATION_ZONES: &str = "NEXUS_FEDERATION_ZONES";

/// Variable name carrying the global path → zone id mount map
/// (e.g. `"/corp=corp,/corp/eng=corp-eng,/home/family=family"`).
pub const ENV_FEDERATION_MOUNTS: &str = "NEXUS_FEDERATION_MOUNTS";

/// Parse `NEXUS_FEDERATION_ZONES` into a deduplicated, order-preserving
/// list of zone ids. Empty / unset → empty list.
pub fn parse_zones_env() -> Vec<String> {
    parse_zones_str(&std::env::var(ENV_FEDERATION_ZONES).unwrap_or_default())
}

fn parse_zones_str(csv: &str) -> Vec<String> {
    let mut seen = std::collections::HashSet::new();
    csv.split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .filter(|s| seen.insert(s.to_string()))
        .map(str::to_string)
        .collect()
}

/// Parse `NEXUS_FEDERATION_MOUNTS` into a `path -> zone_id` map.
///
/// Accepted format: comma-separated `<global_path>=<zone_id>` pairs.
/// Whitespace around segments is trimmed. Entries without `=`, with
/// an empty path, or with an empty zone id are silently dropped (the
/// cluster binary logs a warning at startup if the parsed map is
/// shorter than the raw input).
pub fn parse_mounts_env() -> BTreeMap<String, String> {
    parse_mounts_str(&std::env::var(ENV_FEDERATION_MOUNTS).unwrap_or_default())
}

fn parse_mounts_str(csv: &str) -> BTreeMap<String, String> {
    let mut out = BTreeMap::new();
    for entry in csv.split(',').map(str::trim).filter(|s| !s.is_empty()) {
        let Some((path, zone)) = entry.split_once('=') else {
            continue;
        };
        let path = path.trim();
        let zone = zone.trim();
        if path.is_empty() || zone.is_empty() || !path.starts_with('/') {
            continue;
        }
        out.insert(path.to_string(), zone.to_string());
    }
    out
}

/// Convenience: read both env vars in one call.
pub fn parse_federation_env() -> (Vec<String>, BTreeMap<String, String>) {
    (parse_zones_env(), parse_mounts_env())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn zones_parses_comma_separated() {
        assert_eq!(parse_zones_str("corp,family"), vec!["corp", "family"]);
    }

    #[test]
    fn zones_trims_whitespace_and_dedupes() {
        assert_eq!(
            parse_zones_str("  corp , corp ,family,  "),
            vec!["corp", "family"]
        );
    }

    #[test]
    fn zones_empty_input_returns_empty() {
        assert!(parse_zones_str("").is_empty());
        assert!(parse_zones_str(",,").is_empty());
    }

    #[test]
    fn mounts_parses_path_zone_pairs() {
        let m = parse_mounts_str("/corp=corp,/family=family");
        assert_eq!(m.get("/corp"), Some(&"corp".to_string()));
        assert_eq!(m.get("/family"), Some(&"family".to_string()));
        assert_eq!(m.len(), 2);
    }

    #[test]
    fn mounts_drops_invalid_entries() {
        // No '=', empty path, empty zone, non-absolute path
        let m = parse_mounts_str("/ok=z,broken,=z,/p=,relative=z");
        assert_eq!(m.len(), 1);
        assert_eq!(m.get("/ok"), Some(&"z".to_string()));
    }

    #[test]
    fn mounts_btreemap_orders_by_path() {
        let m = parse_mounts_str("/corp/eng=eng,/corp=corp,/family=family");
        let keys: Vec<&str> = m.keys().map(String::as_str).collect();
        assert_eq!(keys, vec!["/corp", "/corp/eng", "/family"]);
    }

    #[test]
    fn mounts_trims_whitespace() {
        let m = parse_mounts_str("  /corp = corp ,  /eng = eng  ");
        assert_eq!(m.get("/corp"), Some(&"corp".to_string()));
        assert_eq!(m.get("/eng"), Some(&"eng".to_string()));
    }
}
