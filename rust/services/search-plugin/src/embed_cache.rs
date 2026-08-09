//! Bounded query-embedding cache (Issue #4610).
//!
//! # Why
//!
//! Path-scoped fan-out callers (Koodle's cross-workspace search,
//! DeepBuildAI/koodle#2176) send the SAME query text across many
//! `path_filter` variants — ~3 sub-paths × ~60 workspaces per
//! keystroke.  Every one of those used to pay a full query embed, and
//! `FastEmbedder` serialises embeds on a single ort-session mutex, so
//! repeated embeds of identical text were not just wasted CPU — they
//! were wasted time on the plugin's ONE embedding lane.  A hit here
//! skips both.
//!
//! The P7 result cache (`query_cache`) cannot absorb this: its key
//! includes `path_filter`, so the fan-out's queries are all distinct
//! entries there even though their embedding is identical.
//!
//! # Semantics
//!
//! - Keyed by `(embedder tag, dim, query text)` — a model swap changes
//!   the tag (see `Embedder::tag`), so stale vectors from a previous
//!   model can never be served.  Dim is in the key for the same reason
//!   the AnnIndex pins it at open time: belt-and-suspenders against a
//!   tag collision across dims.
//! - Embeddings are pure functions of (model, text) — there is no
//!   corpus dependency, so Index / Refresh do NOT need to invalidate
//!   this cache (unlike the result cache).
//! - Failures are never cached; the caller retries on the next miss.
//! - FIFO eviction, deliberately not LRU: the dominant pattern is a
//!   burst of identical texts inside one fan-out window, where FIFO
//!   and LRU behave identically, and FIFO keeps every operation O(1)
//!   without an extra recency structure.
//!
//! # Sizing
//!
//! `NEXUS_SEARCH_QUERY_EMBED_CACHE_SIZE` overrides the default 512
//! entries; `0` disables caching entirely (kill switch).  At the
//! mE5-small 384-dim default an entry is ~1.5 KB, so the default cap
//! is under a megabyte.

use std::collections::{HashMap, VecDeque};

use parking_lot::Mutex;

use crate::embedder::Embedder;

/// Env var overriding the cache capacity.  `0` disables the cache.
pub const CACHE_SIZE_ENV: &str = "NEXUS_SEARCH_QUERY_EMBED_CACHE_SIZE";

/// Default capacity when the env var is unset or unparseable.
pub const DEFAULT_CACHE_SIZE: usize = 512;

type CacheKey = (String, usize, String);

#[derive(Default)]
struct Inner {
    map: HashMap<CacheKey, Vec<f32>>,
    order: VecDeque<CacheKey>,
}

/// Bounded FIFO cache of query-text embeddings.  Thread-safe; the
/// lock is only held for map operations, never across an embed call.
pub struct QueryEmbedCache {
    inner: Mutex<Inner>,
    capacity: usize,
}

impl QueryEmbedCache {
    /// Cache with an explicit capacity.  `0` = disabled (every `get`
    /// misses, every `put` is a no-op).
    pub fn with_capacity(capacity: usize) -> Self {
        Self {
            inner: Mutex::new(Inner::default()),
            capacity,
        }
    }

    /// Production constructor — capacity from
    /// [`CACHE_SIZE_ENV`], falling back to [`DEFAULT_CACHE_SIZE`].
    pub fn from_env() -> Self {
        let capacity = std::env::var(CACHE_SIZE_ENV)
            .ok()
            .and_then(|v| v.trim().parse::<usize>().ok())
            .unwrap_or(DEFAULT_CACHE_SIZE);
        Self::with_capacity(capacity)
    }

    pub fn get(&self, tag: &str, dim: usize, q: &str) -> Option<Vec<f32>> {
        if self.capacity == 0 {
            return None;
        }
        let inner = self.inner.lock();
        inner
            .map
            .get(&(tag.to_string(), dim, q.to_string()))
            .cloned()
    }

    pub fn put(&self, tag: &str, dim: usize, q: &str, vec: Vec<f32>) {
        if self.capacity == 0 {
            return;
        }
        let key: CacheKey = (tag.to_string(), dim, q.to_string());
        let mut inner = self.inner.lock();
        if inner.map.insert(key.clone(), vec).is_none() {
            inner.order.push_back(key);
            while inner.order.len() > self.capacity {
                if let Some(evict) = inner.order.pop_front() {
                    inner.map.remove(&evict);
                }
            }
        }
    }

    /// Entry count — test + stats visibility.
    pub fn len(&self) -> usize {
        self.inner.lock().map.len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

/// Embed `q` through `cache`: serve a hit without touching the
/// embedder (and its session mutex), populate on miss.  Error text
/// matches the previous inline `do_semantic_query` embed so callers'
/// error surfaces are unchanged.
pub fn embed_query_cached(
    embedder: &dyn Embedder,
    cache: &QueryEmbedCache,
    q: &str,
) -> Result<Vec<f32>, String> {
    if let Some(v) = cache.get(embedder.tag(), embedder.dim(), q) {
        return Ok(v);
    }
    let vec = embedder
        .embed_batch(&[q])
        .map_err(|e| format!("embed query: {e}"))?
        .into_iter()
        .next()
        .ok_or_else(|| "embed query: empty result".to_string())?;
    cache.put(embedder.tag(), embedder.dim(), q, vec.clone());
    Ok(vec)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::embedder::MockEmbedder;
    use std::sync::atomic::{AtomicUsize, Ordering};

    /// MockEmbedder wrapper that counts embed_batch calls.
    struct Counting {
        inner: MockEmbedder,
        calls: AtomicUsize,
    }

    impl Counting {
        fn new() -> Self {
            Self {
                inner: MockEmbedder::with_dim(8),
                calls: AtomicUsize::new(0),
            }
        }
    }

    impl Embedder for Counting {
        fn embed_batch(
            &self,
            texts: &[&str],
        ) -> Result<Vec<Vec<f32>>, crate::embedder::EmbedError> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            self.inner.embed_batch(texts)
        }
        fn dim(&self) -> usize {
            self.inner.dim()
        }
        fn tag(&self) -> &str {
            "counting"
        }
    }

    #[test]
    fn hit_skips_embedder() {
        let e = Counting::new();
        let cache = QueryEmbedCache::with_capacity(4);
        let v1 = embed_query_cached(&e, &cache, "hello").unwrap();
        let v2 = embed_query_cached(&e, &cache, "hello").unwrap();
        assert_eq!(v1, v2);
        assert_eq!(e.calls.load(Ordering::SeqCst), 1, "second call must hit");
    }

    #[test]
    fn distinct_texts_miss_independently() {
        let e = Counting::new();
        let cache = QueryEmbedCache::with_capacity(4);
        let _ = embed_query_cached(&e, &cache, "a").unwrap();
        let _ = embed_query_cached(&e, &cache, "b").unwrap();
        assert_eq!(e.calls.load(Ordering::SeqCst), 2);
        assert_eq!(cache.len(), 2);
    }

    #[test]
    fn tag_and_dim_isolate_entries() {
        let cache = QueryEmbedCache::with_capacity(4);
        cache.put("model-a", 8, "q", vec![1.0]);
        assert!(cache.get("model-b", 8, "q").is_none(), "tag must isolate");
        assert!(cache.get("model-a", 16, "q").is_none(), "dim must isolate");
        assert_eq!(cache.get("model-a", 8, "q"), Some(vec![1.0]));
    }

    #[test]
    fn capacity_bound_evicts_oldest() {
        let cache = QueryEmbedCache::with_capacity(2);
        cache.put("t", 8, "one", vec![1.0]);
        cache.put("t", 8, "two", vec![2.0]);
        cache.put("t", 8, "three", vec![3.0]);
        assert_eq!(cache.len(), 2);
        assert!(cache.get("t", 8, "one").is_none(), "oldest must evict");
        assert_eq!(cache.get("t", 8, "three"), Some(vec![3.0]));
    }

    #[test]
    fn re_put_same_key_does_not_grow_order() {
        let cache = QueryEmbedCache::with_capacity(2);
        for _ in 0..10 {
            cache.put("t", 8, "same", vec![1.0]);
        }
        cache.put("t", 8, "other", vec![2.0]);
        assert_eq!(
            cache.len(),
            2,
            "duplicate puts must not evict via order growth"
        );
        assert!(cache.get("t", 8, "same").is_some());
    }

    #[test]
    fn zero_capacity_disables() {
        let e = Counting::new();
        let cache = QueryEmbedCache::with_capacity(0);
        let _ = embed_query_cached(&e, &cache, "hello").unwrap();
        let _ = embed_query_cached(&e, &cache, "hello").unwrap();
        assert_eq!(e.calls.load(Ordering::SeqCst), 2, "capacity 0 must bypass");
        assert!(cache.is_empty());
    }

    #[test]
    fn failures_are_not_cached() {
        struct Failing;
        impl Embedder for Failing {
            fn embed_batch(
                &self,
                _texts: &[&str],
            ) -> Result<Vec<Vec<f32>>, crate::embedder::EmbedError> {
                Err(crate::embedder::EmbedError::Runtime("boom".into()))
            }
            fn dim(&self) -> usize {
                8
            }
            fn tag(&self) -> &str {
                "failing"
            }
        }
        let cache = QueryEmbedCache::with_capacity(4);
        assert!(embed_query_cached(&Failing, &cache, "q").is_err());
        assert!(cache.is_empty(), "a failed embed must not be cached");
    }
}
