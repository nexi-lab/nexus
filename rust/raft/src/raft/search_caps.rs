//! Per-zone search capabilities.
//!
//! Stored in `{base_path}/{zone_id}/search_caps.json`. Python search daemon
//! writes the file at startup; the Rust `GetSearchCapabilities` gRPC handler
//! reads it on each RPC. File-SSOT — no in-memory cache in the registry.
//!
//! Rationale: search capabilities are *search-service* state, not raft state.
//! Keeping them on the zone dir couples their lifecycle to the zone
//! directory (`remove_dir_all` during zone removal drops the caps
//! with no extra bookkeeping) and keeps the SSOT single.

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

/// Per-zone search capabilities. Mirrors the proto `SearchCapabilities`
/// message minus `zone_id` (derivable from the file path).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SearchCapabilitiesInfo {
    pub device_tier: String,
    pub search_modes: Vec<String>,
    pub embedding_model: String,
    pub embedding_dimensions: i32,
    pub has_graph: bool,
}

impl Default for SearchCapabilitiesInfo {
    fn default() -> Self {
        Self {
            device_tier: "server".to_string(),
            search_modes: vec!["keyword".to_string()],
            embedding_model: String::new(),
            embedding_dimensions: 0,
            has_graph: false,
        }
    }
}

/// Path to the capabilities file for a zone.
pub fn search_caps_path(base_path: &Path, zone_id: &str) -> PathBuf {
    base_path.join(zone_id).join("search_caps.json")
}

/// Read capabilities for a zone from disk. Returns `None` if the file is
/// missing or malformed — callers should fall back to
/// `SearchCapabilitiesInfo::default()`.
pub fn read_search_caps(base_path: &Path, zone_id: &str) -> Option<SearchCapabilitiesInfo> {
    let path = search_caps_path(base_path, zone_id);
    let bytes = std::fs::read(&path).ok()?;
    match serde_json::from_slice::<SearchCapabilitiesInfo>(&bytes) {
        Ok(caps) => Some(caps),
        Err(e) => {
            tracing::warn!(
                zone = %zone_id,
                path = %path.display(),
                error = %e,
                "Failed to parse search_caps.json — using defaults",
            );
            None
        }
    }
}

/// Atomically write capabilities for a zone. Writes to `search_caps.json.tmp`
/// and renames, so a crash mid-write leaves either the old content or no file.
/// Zone dir must already exist (guaranteed during zone lifetime by
/// `ZonePersistence::create` / `open`).
pub fn write_search_caps(
    base_path: &Path,
    zone_id: &str,
    caps: &SearchCapabilitiesInfo,
) -> std::io::Result<()> {
    let final_path = search_caps_path(base_path, zone_id);
    let tmp_path = final_path.with_extension("json.tmp");
    if let Some(parent) = final_path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let bytes = serde_json::to_vec_pretty(caps).map_err(std::io::Error::other)?;
    std::fs::write(&tmp_path, bytes)?;
    std::fs::rename(&tmp_path, &final_path)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    #[test]
    fn test_round_trip() {
        let tmp = TempDir::new().unwrap();
        let base = tmp.path();
        std::fs::create_dir_all(base.join("z1")).unwrap();
        let caps = SearchCapabilitiesInfo {
            device_tier: "phone".into(),
            search_modes: vec!["keyword".into(), "semantic".into()],
            embedding_model: "all-MiniLM-L6-v2".into(),
            embedding_dimensions: 384,
            has_graph: true,
        };
        write_search_caps(base, "z1", &caps).unwrap();
        let got = read_search_caps(base, "z1").unwrap();
        assert_eq!(got.device_tier, "phone");
        assert_eq!(got.search_modes, vec!["keyword", "semantic"]);
        assert_eq!(got.embedding_model, "all-MiniLM-L6-v2");
        assert_eq!(got.embedding_dimensions, 384);
        assert!(got.has_graph);
    }

    #[test]
    fn test_missing_returns_none() {
        let tmp = TempDir::new().unwrap();
        assert!(read_search_caps(tmp.path(), "nonexistent").is_none());
    }

    #[test]
    fn test_malformed_returns_none() {
        let tmp = TempDir::new().unwrap();
        std::fs::create_dir_all(tmp.path().join("z1")).unwrap();
        std::fs::write(search_caps_path(tmp.path(), "z1"), b"{not valid json").unwrap();
        assert!(read_search_caps(tmp.path(), "z1").is_none());
    }
}
