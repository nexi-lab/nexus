//! Rust-side scatter-gather for chunked cross-node CAS reads.
//!
//! When a follower node has Raft-replicated *metadata* for a chunked file
//! but the *content* replication window hasn't closed yet, the chunks that
//! the manifest points to may still live only on the writer's local CAS.
//! This module wraps `PeerBlobClient` with fan-out semantics so a local
//! chunk miss transparently becomes a bounded parallel fetch against the
//! file's `backend_name.origins` set with first-success-wins semantics.
//!
//! Design highlights:
//!   - **Bounded fan-out**: only the file's origin set is contacted, not
//!     the whole zone. The candidate set is naturally bounded by the
//!     replication factor (≤5 typical).
//!   - **First-success-wins**: CAS identity guarantees the bytes returned
//!     by any origin hash to the same content. The first OK response wins
//!     and pending futures are abandoned (their permits drop).
//!   - **Hash-verify**: every response is BLAKE3-verified before we return
//!     it. A compromised or misbehaving peer cannot poison the local CAS.
//!   - **Loop-back guard**: the caller's own `self_address` is filtered
//!     out of the origin list — we never issue an RPC to ourselves.
//!   - **Deferred to issue #3799**: no per-node Bloom / gossip routing.
//!
//! Parent module: `peer_blob_client`. This sits above it — the client
//! owns connections and semaphores, the fetcher owns the scatter-gather
//! policy.

use std::sync::Arc;

use futures::stream::{FuturesUnordered, StreamExt};

use crate::peer_blob_client::PeerBlobClient;

/// Trait implemented by `GrpcChunkFetcher` (prod) and mocks (tests).
///
/// `origins` is the candidate peer-address set for a particular file —
/// typically parsed from `backend_name = "cas-local@host1:port,host2:port"`.
/// Empty = local-only (caller should not even construct this, but we return
/// `None` defensively).
pub(crate) trait RemoteChunkFetcher: Send + Sync {
    /// Fetch a chunk by hash. Returns `Some(bytes)` on success, `None` when
    /// no origin has the chunk (caller maps to `CASError::NotFound`).
    ///
    /// Hash-verification is performed inside the fetcher — callers receive
    /// only bytes that match `chunk_hash`.
    fn fetch_chunk(&self, chunk_hash: &str, origins: &[String]) -> Option<Vec<u8>>;
}

/// Production fetcher — gRPC `ReadBlob` scatter-gather over a shared
/// `PeerBlobClient` channel pool.
pub(crate) struct GrpcChunkFetcher {
    client: Arc<PeerBlobClient>,
    self_address: Option<String>,
}

#[allow(dead_code)]
impl GrpcChunkFetcher {
    pub(crate) fn new(client: Arc<PeerBlobClient>, self_address: Option<String>) -> Self {
        Self {
            client,
            self_address,
        }
    }

    /// Filter origins: drop empties and self-address, de-dup.
    fn candidate_origins(&self, origins: &[String]) -> Vec<String> {
        let mut seen: Vec<String> = Vec::with_capacity(origins.len());
        for raw in origins {
            let trimmed = raw.trim();
            if trimmed.is_empty() {
                continue;
            }
            if self
                .self_address
                .as_deref()
                .is_some_and(|addr| addr == trimmed)
            {
                continue;
            }
            let s = trimmed.to_string();
            if !seen.contains(&s) {
                seen.push(s);
            }
        }
        seen
    }
}

impl RemoteChunkFetcher for GrpcChunkFetcher {
    fn fetch_chunk(&self, chunk_hash: &str, origins: &[String]) -> Option<Vec<u8>> {
        let candidates = self.candidate_origins(origins);
        if candidates.is_empty() {
            return None;
        }

        let hash_owned = chunk_hash.to_string();
        let client = Arc::clone(&self.client);
        let runtime = Arc::clone(client.runtime());

        runtime.block_on(async move {
            let mut futs = FuturesUnordered::new();
            for addr in candidates {
                let c = Arc::clone(&client);
                let h = hash_owned.clone();
                let a = addr.clone();
                futs.push(async move {
                    let r = c.fetch_blob_async(&a, &h).await;
                    (a, r)
                });
            }

            while let Some((addr, result)) = futs.next().await {
                match result {
                    Ok(bytes) => {
                        let actual = lib::hash::hash_content(&bytes);
                        if actual != hash_owned {
                            tracing::warn!(
                                target = "cas_remote",
                                origin = %addr,
                                expected = %hash_owned,
                                got = %actual,
                                "peer returned chunk with bad hash; discarding",
                            );
                            continue;
                        }
                        return Some(bytes);
                    }
                    Err(e) => {
                        tracing::debug!(
                            target = "cas_remote",
                            origin = %addr,
                            hash = %hash_owned,
                            error = %e,
                            "peer returned error; trying next origin",
                        );
                    }
                }
            }
            None
        })
    }
}

/// Parse origins out of a `backend_name` of the form
/// `"type@host1:port1,host2:port2"`. Returns `Vec::new()` for local-only
/// backends (no `@`).
#[allow(dead_code)]
pub(crate) fn parse_origins(backend_name: &str) -> Vec<String> {
    match backend_name.split_once('@') {
        None => Vec::new(),
        Some((_, tail)) => tail
            .split(',')
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .collect(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct MockFetcher {
        response: Option<Vec<u8>>,
        calls: std::sync::Mutex<Vec<(String, Vec<String>)>>,
    }

    impl MockFetcher {
        fn new(response: Option<Vec<u8>>) -> Self {
            Self {
                response,
                calls: std::sync::Mutex::new(Vec::new()),
            }
        }
    }

    impl RemoteChunkFetcher for MockFetcher {
        fn fetch_chunk(&self, chunk_hash: &str, origins: &[String]) -> Option<Vec<u8>> {
            self.calls
                .lock()
                .unwrap()
                .push((chunk_hash.to_string(), origins.to_vec()));
            self.response.clone()
        }
    }

    #[test]
    fn test_parse_origins_empty_for_local_only() {
        assert!(parse_origins("cas-local").is_empty());
    }

    #[test]
    fn test_parse_origins_single_peer() {
        let v = parse_origins("cas-local@nexus-1:2126");
        assert_eq!(v, vec!["nexus-1:2126".to_string()]);
    }

    #[test]
    fn test_parse_origins_multi_peer_trims_whitespace() {
        let v = parse_origins("cas-local@ nexus-1:2126 , nexus-2:2126 ,nexus-3:2126");
        assert_eq!(
            v,
            vec![
                "nexus-1:2126".to_string(),
                "nexus-2:2126".to_string(),
                "nexus-3:2126".to_string(),
            ]
        );
    }

    #[test]
    fn test_parse_origins_skips_empty_between_commas() {
        let v = parse_origins("cas-local@,nexus-1:2126,,,nexus-2:2126,");
        assert_eq!(
            v,
            vec!["nexus-1:2126".to_string(), "nexus-2:2126".to_string()]
        );
    }

    #[test]
    fn test_candidate_origins_filters_self() {
        let rt = crate::peer_blob_client::build_kernel_runtime();
        let client = Arc::new(PeerBlobClient::new(rt));
        let fetcher = GrpcChunkFetcher::new(client, Some("nexus-self:2126".into()));
        let filtered = fetcher.candidate_origins(&[
            "nexus-self:2126".into(),
            "nexus-peer:2126".into(),
            "nexus-peer:2126".into(), // dedup
            "".into(),                // empty
        ]);
        assert_eq!(filtered, vec!["nexus-peer:2126".to_string()]);
    }

    #[test]
    fn test_grpc_fetcher_returns_none_for_empty_candidates() {
        let rt = crate::peer_blob_client::build_kernel_runtime();
        let client = Arc::new(PeerBlobClient::new(rt));
        let fetcher = GrpcChunkFetcher::new(client, Some("nexus-self:2126".into()));
        // Only candidate is self — filtered out.
        let out = fetcher.fetch_chunk(
            "0000000000000000000000000000000000000000000000000000000000000000",
            &["nexus-self:2126".to_string()],
        );
        assert!(out.is_none());
    }

    #[test]
    fn test_mock_fetcher_records_calls() {
        let fetcher = MockFetcher::new(Some(b"mock".to_vec()));
        let r = fetcher.fetch_chunk("abc", &["peer1".into(), "peer2".into()]);
        assert_eq!(r, Some(b"mock".to_vec()));
        let calls = fetcher.calls.lock().unwrap();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].0, "abc");
    }
}
