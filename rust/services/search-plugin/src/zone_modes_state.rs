//! Global zone-indexing-mode registry (Phase 8 of the Python-parity
//! roadmap; see `PARITY_ROADMAP.md`).
//!
//! # What this stores
//!
//! Per-zone "indexing mode" — one of `on`, `off`, `sandbox`.
//! Operators toggle it via SetZoneIndexingMode to gate a zone
//! on/off during rebuild windows.  Mirrors Python's per-zone
//! mode dict.
//!
//! # Placement
//!
//! Unlike parked_state / indexed_dirs_state (per-zone), this
//! sidecar is **cluster-wide** — one JSON at
//! `<data_root>/plugins/search/zone_modes.json` covering every
//! zone.  Rationale: `ListZoneIndexingModes` returns every zone
//! in one call, and per-zone sidecars would force a directory
//! scan.  A single file is O(1) to read.
//!
//! # SSOT + persistence posture
//!
//! Per-node — operators drive the registry via the plugin RPC.
//! Dropping the file resets every zone to `on` (the default).

use std::collections::HashMap;
use std::path::PathBuf;

use parking_lot::RwLock;
use serde::{Deserialize, Serialize};

const MODES_FILE: &str = "zone_modes.json";
const MODES_VERSION: u32 = 1;

fn modes_version_default() -> u32 {
    MODES_VERSION
}

/// Server-side default when a zone hasn't been explicitly set —
/// indexing is on unless an operator opted out.  Matches Python.
pub const DEFAULT_MODE: &str = "on";

/// Recognised modes.  Anything else in a stored file is treated
/// as invalid + reset to the default at load time.
const VALID_MODES: &[&str] = &["on", "off", "sandbox"];

pub fn is_valid_mode(mode: &str) -> bool {
    VALID_MODES.contains(&mode)
}

#[derive(Debug, Serialize, Deserialize, Default)]
struct Persisted {
    #[serde(default = "modes_version_default")]
    version: u32,
    #[serde(default)]
    modes: HashMap<String, String>,
}

/// Cluster-scoped zone-indexing-mode registry.  One instance per
/// plugin, sitting on top of the IndexManager root.
pub struct ZoneModesRegistry {
    dir: PathBuf,
    inner: RwLock<HashMap<String, String>>,
}

impl ZoneModesRegistry {
    /// Open (or create empty) at `<root>/zone_modes.json`.  Invalid
    /// modes in the stored file are silently reset to `DEFAULT_MODE`
    /// so a hand-edited file with a typo doesn't lock a zone out.
    pub fn open_or_create(root: PathBuf) -> Result<Self, ModesError> {
        std::fs::create_dir_all(&root)
            .map_err(|e| ModesError::CreateDir(root.display().to_string(), e.to_string()))?;
        let path = root.join(MODES_FILE);
        let mut map: HashMap<String, String> = HashMap::new();
        if path.exists() {
            let bytes = std::fs::read(&path)
                .map_err(|e| ModesError::Read(path.display().to_string(), e.to_string()))?;
            let persisted: Persisted = serde_json::from_slice(&bytes)
                .map_err(|e| ModesError::Parse(path.display().to_string(), e.to_string()))?;
            if persisted.version != MODES_VERSION {
                return Err(ModesError::Parse(
                    path.display().to_string(),
                    format!(
                        "zone_modes version {} != expected {}",
                        persisted.version, MODES_VERSION
                    ),
                ));
            }
            for (zone, mode) in persisted.modes {
                let mode = if is_valid_mode(&mode) {
                    mode
                } else {
                    tracing::warn!(
                        zone = %zone,
                        mode = %mode,
                        "zone_modes: invalid mode in stored file — resetting to default",
                    );
                    DEFAULT_MODE.to_string()
                };
                map.insert(zone, mode);
            }
        }
        Ok(Self {
            dir: root,
            inner: RwLock::new(map),
        })
    }

    /// Look up a zone's mode.  Falls back to `DEFAULT_MODE` for
    /// zones that were never explicitly set.
    pub fn get(&self, zone: &str) -> String {
        self.inner
            .read()
            .get(zone)
            .cloned()
            .unwrap_or_else(|| DEFAULT_MODE.to_string())
    }

    /// Set (or update) a zone's mode.  Empty / unknown mode resets
    /// to `DEFAULT_MODE`.  Returns Err on invalid explicit mode so
    /// operators see a typo instead of silently getting default.
    pub fn set(&self, zone: &str, mode: &str) -> Result<(), ModesError> {
        let normalised = if mode.is_empty() {
            DEFAULT_MODE.to_string()
        } else if is_valid_mode(mode) {
            mode.to_string()
        } else {
            return Err(ModesError::InvalidMode(mode.to_string()));
        };
        self.inner.write().insert(zone.to_string(), normalised);
        Ok(())
    }

    /// Every explicitly-set (zone, mode) pair.  Zones on default
    /// (never set) are NOT listed — the caller renders them as
    /// `on` at the UI layer if it needs a full view.
    pub fn list(&self) -> Vec<(String, String)> {
        let mut out: Vec<(String, String)> = self
            .inner
            .read()
            .iter()
            .map(|(z, m)| (z.clone(), m.clone()))
            .collect();
        // Sort by zone for deterministic ordering.
        out.sort_by(|a, b| a.0.cmp(&b.0));
        out
    }

    pub fn save(&self) -> Result<(), ModesError> {
        let path = self.dir.join(MODES_FILE);
        let persisted = Persisted {
            version: MODES_VERSION,
            modes: self.inner.read().clone(),
        };
        let bytes =
            serde_json::to_vec_pretty(&persisted).map_err(|e| ModesError::Encode(e.to_string()))?;
        let tmp = path.with_extension("json.tmp");
        std::fs::write(&tmp, &bytes)
            .map_err(|e| ModesError::Write(tmp.display().to_string(), e.to_string()))?;
        std::fs::rename(&tmp, &path)
            .map_err(|e| ModesError::Write(path.display().to_string(), e.to_string()))?;
        Ok(())
    }
}

#[derive(Debug, thiserror::Error)]
pub enum ModesError {
    #[error("create modes dir {0}: {1}")]
    CreateDir(String, String),
    #[error("read zone_modes {0}: {1}")]
    Read(String, String),
    #[error("parse zone_modes {0}: {1}")]
    Parse(String, String),
    #[error("encode zone_modes: {0}")]
    Encode(String),
    #[error("write zone_modes {0}: {1}")]
    Write(String, String),
    #[error("invalid mode {0:?}; must be one of on/off/sandbox")]
    InvalidMode(String),
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tempdir() -> PathBuf {
        tempfile::tempdir().expect("tempdir").keep()
    }

    #[test]
    fn get_unknown_zone_returns_default() {
        let r = ZoneModesRegistry::open_or_create(tempdir()).expect("open");
        assert_eq!(r.get("never-set-zone"), "on");
    }

    #[test]
    fn set_then_get_roundtrip() {
        let r = ZoneModesRegistry::open_or_create(tempdir()).expect("open");
        r.set("za", "off").expect("set");
        r.set("zb", "sandbox").expect("set");
        assert_eq!(r.get("za"), "off");
        assert_eq!(r.get("zb"), "sandbox");
        assert_eq!(r.get("zc"), "on", "unset zone stays on default");
    }

    #[test]
    fn set_empty_resets_to_default() {
        let r = ZoneModesRegistry::open_or_create(tempdir()).expect("open");
        r.set("za", "off").expect("set");
        r.set("za", "").expect("reset");
        assert_eq!(r.get("za"), "on");
    }

    #[test]
    fn set_invalid_mode_errors() {
        let r = ZoneModesRegistry::open_or_create(tempdir()).expect("open");
        assert!(matches!(
            r.set("za", "unknown"),
            Err(ModesError::InvalidMode(_))
        ));
    }

    #[test]
    fn list_sorted_by_zone() {
        let r = ZoneModesRegistry::open_or_create(tempdir()).expect("open");
        r.set("zc", "off").unwrap();
        r.set("za", "sandbox").unwrap();
        r.set("zb", "on").unwrap();
        let out = r.list();
        assert_eq!(out.len(), 3);
        assert_eq!(out[0].0, "za");
        assert_eq!(out[1].0, "zb");
        assert_eq!(out[2].0, "zc");
    }

    #[test]
    fn save_then_reopen_survives_restart() {
        let dir = tempdir();
        {
            let r = ZoneModesRegistry::open_or_create(dir.clone()).expect("open");
            r.set("za", "off").unwrap();
            r.set("zb", "sandbox").unwrap();
            r.save().expect("save");
        }
        let r2 = ZoneModesRegistry::open_or_create(dir).expect("reopen");
        assert_eq!(r2.get("za"), "off");
        assert_eq!(r2.get("zb"), "sandbox");
    }

    #[test]
    fn invalid_stored_mode_resets_to_default() {
        // Hand-edited file with a typo shouldn't lock a zone out.
        let dir = tempdir();
        let path = dir.join(MODES_FILE);
        std::fs::write(&path, r#"{"version":1,"modes":{"za":"typo-value"}}"#).unwrap();
        let r = ZoneModesRegistry::open_or_create(dir).expect("open");
        assert_eq!(r.get("za"), "on", "invalid stored mode should fall back");
    }
}
