//! In-memory zone-scoped query result cache (Phase 7 of the
//! Python-parity roadmap; see `PARITY_ROADMAP.md`).
//!
//! # What this stores
//!
//! `zone_id → { hash(request) → (cached results, inserted_at) }`.
//! Serves Query calls whose full parameter tuple hits an entry
//! younger than `TTL` — bypassing FTS + ANN + fusion + scoring +
//! pooling entirely.
//!
//! # Auth boundary
//!
//! The plugin never sees subject identity (D5 read-gating stays
//! kernel-tier).  `zone_id` IS the auth boundary — two callers who
//! hit the same query in the same zone are equivalent from the
//! plugin's perspective, so sharing a cache entry between them is
//! safe.  The kernel-side router already checked read permission
//! before dialing the plugin.
//!
//! # Cache key
//!
//! Hash of every QueryRequest field that changes the result:
//! `q`, `zone_id`, `limit`, `path_filter`, `query_type`, `alpha`,
//! `fusion_method`, `rrf_k`, `chunks_per_page`, `expand`,
//! `recency_mode`, `recency_weight`, `recency_half_life_days`,
//! `path_prefix_boosts`.  Explicitly EXCLUDES `auth_token` (per
//! above — zone is the boundary).  `path_prefix_boosts` is folded
//! in via a sorted-key traversal so map ordering doesn't flap the
//! hash.
//!
//! # Adversarial hardening (#4559)
//!
//! - `path_filter="/foo/"` vs `"/foo"` MUST key distinctly —
//!   different filter semantics.  The hash sees the raw string.
//! - Alpha 0.0 vs 0.001 MUST key distinctly — floats hash by bit
//!   pattern (`f32::to_bits`) rather than value equality that would
//!   collapse NaN vs NaN or ±0.
//! - Wire fields ADDED in a future phase MUST land in the hash
//!   even if the caller doesn't set them (they'd share the default
//!   value, which is fine — the key stays stable per that default).
//!
//! # Eviction
//!
//! - TTL: entries older than [`DEFAULT_TTL`] on read return None
//!   and get dropped lazily on next insert to the same zone.
//! - Per-zone capacity: [`MAX_ENTRIES_PER_ZONE`] — on insert past
//!   the cap the OLDEST entry is evicted (FIFO by inserted_at).
//!   LRU would be marginally better but FIFO is simpler and the
//!   difference at 128 entries is inside measurement noise.
//! - Zone-wide invalidate: `invalidate_zone(zone)` drops the whole
//!   zone's cache.  Called by Index + Refresh so callers who just
//!   changed the corpus don't see the stale prior response.
//!
//! # Concurrency
//!
//! `parking_lot::Mutex<HashMap<zone, ZoneEntry>>` at the top level.
//! Lock held for the map lookup + one HashMap operation on the
//! zone entry; both are ns-scale.  Concurrent Query calls to
//! different zones don't serialise beyond that.

use std::collections::HashMap;
use std::hash::{Hash, Hasher};
use std::sync::Arc;
use std::time::{Duration, Instant};

use parking_lot::Mutex;

use crate::search_proto::{QueryRequest, QueryResult};

/// Default entry TTL.  5 minutes matches the Python
/// SearchDaemon's `result_cache_ttl_seconds` default.
pub const DEFAULT_TTL: Duration = Duration::from_secs(300);

/// Per-zone entry cap.  Past this on insert, the OLDEST entry
/// (smallest `inserted_at`) is evicted.  128 keeps memory bounded
/// on a busy zone (typical entry = a few KB of QueryResult) while
/// still absorbing burst query patterns.
pub const MAX_ENTRIES_PER_ZONE: usize = 128;

/// One cached response entry.
#[derive(Debug, Clone)]
struct Entry {
    results: Vec<QueryResult>,
    inserted_at: Instant,
}

/// One zone's cache slice.  Nested inside the top-level Mutex map
/// so per-zone reads/writes stay cheap and don't need their own
/// lock — the top-level Mutex serialises accesses across all
/// zones, but each access is ns-scale.
type ZoneEntries = HashMap<u64, Entry>;

/// Zone-scoped query result cache.  Cheap to clone via Arc.
pub struct QueryCache {
    zones: Mutex<HashMap<String, ZoneEntries>>,
    ttl: Duration,
}

impl QueryCache {
    /// New cache with the default 5-minute TTL.
    pub fn new() -> Self {
        Self::with_ttl(DEFAULT_TTL)
    }

    /// New cache with an explicit TTL — used by tests that need
    /// deterministic expiration windows.
    pub fn with_ttl(ttl: Duration) -> Self {
        Self {
            zones: Mutex::new(HashMap::new()),
            ttl,
        }
    }

    /// Fetch the cached response for `(zone_id, hash(req, title_arm))`
    /// if it exists AND hasn't expired.  Returns a clone of the
    /// results (callers get their own copy; the cache retains
    /// ownership).
    ///
    /// `title_arm` is the EFFECTIVE title-arm state for this request
    /// (arm enabled AND the request is hybrid) — it is server config,
    /// not a wire field, but it changes hybrid rankings, so it must
    /// be part of the cache identity or flipping the
    /// NEXUS_SEARCH_TITLE_ARM kill-switch would keep serving
    /// pre-flip rankings for the TTL (review R1 of #4628).
    pub fn get(&self, req: &QueryRequest, title_arm: bool) -> Option<Vec<QueryResult>> {
        let key = hash_request(req, title_arm);
        let zones = self.zones.lock();
        let zone = zones.get(&req.zone_id)?;
        let entry = zone.get(&key)?;
        if entry.inserted_at.elapsed() >= self.ttl {
            // Lazy TTL — leave the stale entry for the next insert
            // to sweep, but don't hand it back to this caller.
            return None;
        }
        Some(entry.results.clone())
    }

    /// Insert `results` under the cache key derived from
    /// `(req, title_arm)` — see [`Self::get`] for why the arm state
    /// is part of the identity.  Evicts stale entries + the oldest
    /// entry if this zone is at [`MAX_ENTRIES_PER_ZONE`].
    pub fn insert(&self, req: &QueryRequest, title_arm: bool, results: Vec<QueryResult>) {
        let key = hash_request(req, title_arm);
        let now = Instant::now();
        let mut zones = self.zones.lock();
        let zone = zones.entry(req.zone_id.clone()).or_default();

        // Lazy TTL sweep — walk the whole per-zone map dropping
        // anything past TTL.  With 128-entry cap the sweep cost is
        // negligible; kept simple.
        zone.retain(|_, e| e.inserted_at.elapsed() < self.ttl);

        // Capacity eviction — if inserting this key would push past
        // the cap, drop the oldest entry first.  Skipped when we're
        // overwriting an existing key (already-counted).
        if !zone.contains_key(&key) && zone.len() >= MAX_ENTRIES_PER_ZONE {
            if let Some(oldest_key) = zone
                .iter()
                .min_by_key(|(_, e)| e.inserted_at)
                .map(|(k, _)| *k)
            {
                zone.remove(&oldest_key);
            }
        }

        zone.insert(
            key,
            Entry {
                results,
                inserted_at: now,
            },
        );
    }

    /// Drop every cache entry for `zone_id`.  Called by Index +
    /// Refresh whenever the underlying corpus for a zone changes,
    /// so the next Query sees fresh results rather than the stale
    /// pre-change response.
    pub fn invalidate_zone(&self, zone_id: &str) {
        self.zones.lock().remove(zone_id);
    }

    /// Number of live entries across all zones — used by tests +
    /// operator diagnostics.
    pub fn total_entries(&self) -> usize {
        self.zones.lock().values().map(|z| z.len()).sum()
    }

    /// Number of live entries in one zone.
    pub fn zone_entries(&self, zone_id: &str) -> usize {
        self.zones.lock().get(zone_id).map(|z| z.len()).unwrap_or(0)
    }
}

impl Default for QueryCache {
    fn default() -> Self {
        Self::new()
    }
}

/// Hash every meaningful QueryRequest field — see the module doc
/// for the "meaningful" definition (all-fields-except-auth_token).
///
/// Floats hash by bit pattern (`to_bits`) so alpha=0.0 and
/// alpha=0.001 key distinctly (they'd Hash-equal under a naive
/// f32 impl that PartialEq'd 0.0 to -0.0, and both would collide
/// with sign-bit variants).  `path_prefix_boosts` sorts its keys
/// so map iteration order doesn't flap the hash.
fn hash_request(req: &QueryRequest, title_arm: bool) -> u64 {
    let mut hasher = std::collections::hash_map::DefaultHasher::new();
    // Server-side title-arm state (#4628): not a wire field, but it
    // changes hybrid rankings — see `QueryCache::get`.
    title_arm.hash(&mut hasher);
    req.q.hash(&mut hasher);
    req.zone_id.hash(&mut hasher);
    req.limit.hash(&mut hasher);
    req.path_filter.hash(&mut hasher);
    req.query_type.hash(&mut hasher);
    req.alpha.to_bits().hash(&mut hasher);
    req.fusion_method.hash(&mut hasher);
    req.rrf_k.hash(&mut hasher);
    req.chunks_per_page.hash(&mut hasher);
    req.expand.hash(&mut hasher);
    req.recency_mode.hash(&mut hasher);
    req.recency_weight.to_bits().hash(&mut hasher);
    req.recency_half_life_days.to_bits().hash(&mut hasher);
    // Sort by key so map iteration order doesn't flap.
    let mut boost_entries: Vec<(&String, &f32)> = req.path_prefix_boosts.iter().collect();
    boost_entries.sort_by(|a, b| a.0.cmp(b.0));
    for (k, v) in boost_entries {
        k.hash(&mut hasher);
        v.to_bits().hash(&mut hasher);
    }
    hasher.finish()
}

/// Shared handle — service.rs stores an `Arc<QueryCache>` in
/// SearchServiceImpl and Index/Refresh call `invalidate_zone`.
pub type SharedQueryCache = Arc<QueryCache>;

#[cfg(test)]
mod tests {
    use super::*;

    fn base_req(zone: &str, q: &str) -> QueryRequest {
        QueryRequest {
            q: q.to_string(),
            zone_id: zone.to_string(),
            limit: 10,
            path_filter: String::new(),
            query_type: 1,
            auth_token: String::new(),
            fusion_method: 0,
            alpha: 0.0,
            rrf_k: 0,
            chunks_per_page: 0,
            expand: String::new(),
            recency_mode: String::new(),
            recency_weight: 0.0,
            recency_half_life_days: 0.0,
            path_prefix_boosts: HashMap::new(),
        }
    }

    fn hit(path: &str, score: f32) -> QueryResult {
        QueryResult {
            path: path.to_string(),
            chunk_index: 0,
            chunk_text: String::new(),
            score,
            zone_id: "root".to_string(),
            mtime_ms: None,
            expanded_context: String::new(),
            title_score: None,
        }
    }

    #[test]
    fn title_arm_state_splits_the_cache_identity() {
        // Review R1 (#4628): flipping NEXUS_SEARCH_TITLE_ARM must not
        // serve rankings computed under the other state for the TTL.
        let c = QueryCache::new();
        let req = base_req("root", "atlas design doc");
        c.insert(&req, true, vec![hit("/designs/atlas.md", 1.0)]);
        assert!(
            c.get(&req, false).is_none(),
            "arm-off lookup must not see the arm-on entry"
        );
        assert!(c.get(&req, true).is_some(), "same-state lookup still hits");
    }

    #[test]
    fn get_returns_none_on_miss() {
        let c = QueryCache::new();
        assert!(c.get(&base_req("root", "widget"), false).is_none());
    }

    #[test]
    fn insert_then_get_returns_cached_results() {
        let c = QueryCache::new();
        let req = base_req("root", "widget");
        c.insert(&req, false, vec![hit("/a", 1.0)]);
        let got = c.get(&req, false).expect("should hit");
        assert_eq!(got.len(), 1);
        assert_eq!(got[0].path, "/a");
    }

    #[test]
    fn different_zones_dont_share() {
        let c = QueryCache::new();
        let r1 = base_req("root", "widget");
        let r2 = base_req("other", "widget");
        c.insert(&r1, false, vec![hit("/only-in-root", 1.0)]);
        assert!(c.get(&r1, false).is_some());
        assert!(c.get(&r2, false).is_none(), "zones must not share");
    }

    #[test]
    fn different_queries_dont_share() {
        let c = QueryCache::new();
        c.insert(&base_req("root", "alpha"), false, vec![hit("/a", 1.0)]);
        assert!(c.get(&base_req("root", "beta"), false).is_none());
    }

    #[test]
    fn auth_token_does_not_affect_key() {
        // Zone is the auth boundary; two callers with different
        // tokens in the same zone SHARE the cache entry.  Locks
        // the D5 posture.
        let c = QueryCache::new();
        let mut r1 = base_req("root", "widget");
        r1.auth_token = "token-a".into();
        let mut r2 = base_req("root", "widget");
        r2.auth_token = "token-b".into();
        c.insert(&r1, false, vec![hit("/x", 1.0)]);
        assert!(c.get(&r2, false).is_some(), "different tokens must share");
    }

    #[test]
    fn distinct_path_filter_suffix_keys_distinctly() {
        // Adversarial regression: "/foo/" and "/foo" have different
        // filter semantics.  Cache key must NOT collapse them.
        let c = QueryCache::new();
        let mut r1 = base_req("root", "q");
        r1.path_filter = "/foo/".into();
        let mut r2 = base_req("root", "q");
        r2.path_filter = "/foo".into();
        c.insert(&r1, false, vec![hit("/a", 1.0)]);
        assert!(c.get(&r2, false).is_none(), "path_filter suffix must matter");
    }

    #[test]
    fn distinct_prefix_boosts_key_distinctly() {
        let c = QueryCache::new();
        let mut r1 = base_req("root", "q");
        r1.path_prefix_boosts.insert("/docs/".to_string(), 1.5);
        let mut r2 = base_req("root", "q");
        r2.path_prefix_boosts.insert("/docs/".to_string(), 2.0);
        c.insert(&r1, false, vec![hit("/x", 1.0)]);
        assert!(c.get(&r2, false).is_none(), "boost value must key distinctly");
    }

    #[test]
    fn prefix_boost_key_order_stable() {
        // Insertion order into HashMap shouldn't affect the hash —
        // sorted-key traversal makes {a,b} == {b,a} on the wire.
        let c = QueryCache::new();
        let mut r1 = base_req("root", "q");
        r1.path_prefix_boosts.insert("/a/".to_string(), 1.0);
        r1.path_prefix_boosts.insert("/b/".to_string(), 2.0);
        let mut r2 = base_req("root", "q");
        r2.path_prefix_boosts.insert("/b/".to_string(), 2.0);
        r2.path_prefix_boosts.insert("/a/".to_string(), 1.0);
        c.insert(&r1, false, vec![hit("/x", 1.0)]);
        assert!(c.get(&r2, false).is_some(), "map-order should not flap the key");
    }

    #[test]
    fn ttl_expiry_returns_none() {
        let c = QueryCache::with_ttl(Duration::from_millis(1));
        c.insert(&base_req("root", "q"), false, vec![hit("/a", 1.0)]);
        std::thread::sleep(Duration::from_millis(10));
        assert!(c.get(&base_req("root", "q"), false).is_none(), "TTL not honoured");
    }

    #[test]
    fn invalidate_zone_drops_all_entries_for_zone() {
        let c = QueryCache::new();
        c.insert(&base_req("root", "q1"), false, vec![hit("/a", 1.0)]);
        c.insert(&base_req("root", "q2"), false, vec![hit("/b", 1.0)]);
        c.insert(&base_req("other", "q1"), false, vec![hit("/c", 1.0)]);
        assert_eq!(c.zone_entries("root"), 2);
        c.invalidate_zone("root");
        assert_eq!(c.zone_entries("root"), 0);
        assert_eq!(c.zone_entries("other"), 1, "unrelated zone untouched");
    }

    #[test]
    fn capacity_eviction_drops_oldest() {
        let c = QueryCache::new();
        // Fill past the cap with unique queries (each has a
        // different q so each hashes differently).
        for i in 0..MAX_ENTRIES_PER_ZONE {
            c.insert(
                &base_req("root", &format!("q{i}")),
                false,
                vec![hit(&format!("/p{i}"), 1.0)],
            );
        }
        assert_eq!(c.zone_entries("root"), MAX_ENTRIES_PER_ZONE);
        // The oldest entry (q0) should still be present.
        assert!(c.get(&base_req("root", "q0"), false).is_some());
        // One more insert — oldest gets evicted.
        c.insert(&base_req("root", "q_new"), false, vec![hit("/p_new", 1.0)]);
        assert_eq!(c.zone_entries("root"), MAX_ENTRIES_PER_ZONE);
        assert!(
            c.get(&base_req("root", "q0"), false).is_none(),
            "oldest not evicted"
        );
    }

    #[test]
    fn alpha_zero_vs_epsilon_key_distinctly() {
        // Adversarial: naive f32 hash could collapse 0.0 and
        // 0.001; to_bits() prevents it.
        let c = QueryCache::new();
        let mut r_zero = base_req("root", "q");
        r_zero.alpha = 0.0;
        let mut r_eps = base_req("root", "q");
        r_eps.alpha = 0.001;
        c.insert(&r_zero, false, vec![hit("/z", 1.0)]);
        assert!(
            c.get(&r_eps, false).is_none(),
            "alpha 0 vs epsilon must key distinctly"
        );
    }
}
