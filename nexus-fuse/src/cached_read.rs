//! Shared read-through-cache flow for nexus-fuse mount and daemon reads.

use crate::cache::{CacheLookup, FileCache};
use crate::client::{NexusClient, ReadResponse};
use crate::error::NexusClientError;
use log::{debug, error};

#[derive(Debug)]
pub struct CachedReadResult {
    pub content: Vec<u8>,
    pub etag: Option<String>,
    pub tier: &'static str,
}

pub fn read_with_cache(
    client: &NexusClient,
    cache: Option<&FileCache>,
    path: &str,
    gen: u64,
) -> Result<CachedReadResult, NexusClientError> {
    if let Some(cache) = cache {
        match cache.get(path, gen) {
            CacheLookup::Hit(entry) => {
                debug!("Foyer cache hit for {}", path);
                return Ok(CachedReadResult {
                    content: entry.content,
                    etag: entry.etag,
                    tier: "cache",
                });
            }
            CacheLookup::NeedsRevalidation { etag } => {
                debug!("Revalidating cache for {} with etag {}", path, etag);
                match client.read_with_etag(path, Some(&etag)) {
                    Ok(ReadResponse::NotModified) => {
                        crate::metrics::record_cache_etag_revalidate("304");
                        crate::metrics::record_etag_check("304");
                        cache.touch(path);
                        if let Some(entry) = cache.get_stale(path) {
                            return Ok(CachedReadResult {
                                content: entry.content,
                                etag: entry.etag,
                                tier: "cache",
                            });
                        }
                        error!("Cache inconsistency after 304 for {}", path);
                    }
                    Ok(ReadResponse::Content { content, etag }) => {
                        crate::metrics::record_cache_etag_revalidate("updated");
                        crate::metrics::record_etag_check("updated");
                        cache.put(path, &content, etag.as_deref(), gen);
                        return Ok(CachedReadResult {
                            content,
                            etag,
                            tier: "backend",
                        });
                    }
                    Err(e) => {
                        if e.is_transient() {
                            debug!("Revalidation failed for {}: {}, using stale cache", path, e);
                            if let Some(entry) = cache.get_stale(path) {
                                crate::metrics::record_cache_etag_revalidate("fallback");
                                crate::metrics::record_etag_check("fallback");
                                return Ok(CachedReadResult {
                                    content: entry.content,
                                    etag: entry.etag,
                                    tier: "cache",
                                });
                            }
                        }
                        debug!(
                            "Revalidation failed permanently for {}, invalidating stale cache: {}",
                            path, e
                        );
                        crate::metrics::record_cache_etag_revalidate("error");
                        crate::metrics::record_etag_check("error");
                        cache.invalidate(path);
                        return Err(e);
                    }
                }
            }
            CacheLookup::Miss => {}
        }
    }

    match client.read_with_etag(path, None) {
        Ok(ReadResponse::Content { content, etag }) => {
            if let Some(cache) = cache {
                cache.put(path, &content, etag.as_deref(), gen);
            }
            Ok(CachedReadResult {
                content,
                etag,
                tier: "backend",
            })
        }
        Ok(ReadResponse::NotModified) => {
            crate::metrics::record_etag_check("unexpected_304");
            Err(NexusClientError::InvalidResponse(
                "Unexpected 304 response".to_string(),
            ))
        }
        Err(e) => Err(e),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cache::{CacheConfig, MAX_FILE_SIZE};
    use mockito::Server;

    fn test_cache(label: &str) -> FileCache {
        let dir = tempfile::tempdir().unwrap().keep();
        let config = CacheConfig::new(
            dir.join(label),
            4 * 1024 * 1024,
            64 * 1024 * 1024,
            MAX_FILE_SIZE,
        )
        .unwrap();
        FileCache::new_with_config(&format!("http://{label}.test"), "test", config).unwrap()
    }

    #[test]
    fn not_found_revalidation_does_not_return_stale_content() {
        let mut server = Server::new();
        let _mock = server
            .mock("POST", "/api/nfs/read")
            .match_header("if-none-match", "\"stale-etag\"")
            .with_status(404)
            .with_body("missing")
            .create();
        let client = NexusClient::new(&server.url(), "test-key", None).unwrap();
        let cache = test_cache("revalidation-not-found");
        cache.put("/gone.txt", b"stale", Some("stale-etag"), 0);
        cache.backdate_for_test("/gone.txt", 3601);

        let err = read_with_cache(&client, Some(&cache), "/gone.txt", 0).unwrap_err();

        assert!(matches!(err, NexusClientError::NotFound(_)));
        assert!(matches!(cache.get("/gone.txt", 0), CacheLookup::Miss));
    }

    #[test]
    fn transient_revalidation_error_returns_stale_content() {
        let mut server = Server::new();
        let _mock = server
            .mock("POST", "/api/nfs/read")
            .match_header("if-none-match", "\"stale-etag\"")
            .with_status(503)
            .with_body("temporarily unavailable")
            .create();
        let client = NexusClient::new(&server.url(), "test-key", None).unwrap();
        let cache = test_cache("revalidation-transient");
        cache.put("/cached.txt", b"stale", Some("stale-etag"), 0);
        cache.backdate_for_test("/cached.txt", 3601);

        let result = read_with_cache(&client, Some(&cache), "/cached.txt", 0).unwrap();

        assert_eq!(result.content, b"stale");
        assert_eq!(result.etag.as_deref(), Some("stale-etag"));
        assert_eq!(result.tier, "cache");
    }

    #[test]
    fn malformed_revalidation_response_does_not_return_stale_content() {
        let mut server = Server::new();
        let _mock = server
            .mock("POST", "/api/nfs/read")
            .match_header("if-none-match", "\"stale-etag\"")
            .with_status(200)
            .with_header("content-type", "application/json")
            .with_body("not json")
            .create();
        let client = NexusClient::new(&server.url(), "test-key", None).unwrap();
        let cache = test_cache("revalidation-malformed");
        cache.put("/bad.txt", b"stale", Some("stale-etag"), 0);
        cache.backdate_for_test("/bad.txt", 3601);

        let err = read_with_cache(&client, Some(&cache), "/bad.txt", 0).unwrap_err();

        assert!(matches!(err, NexusClientError::HttpError(_)));
        assert!(matches!(cache.get("/bad.txt", 0), CacheLookup::Miss));
    }
}
