use reqwest::header::{HeaderValue, AUTHORIZATION};
use reqwest::{Client, StatusCode, Url};
use serde::Deserialize;
use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant};

const LOOKUP_TIMEOUT: Duration = Duration::from_secs(2);
const WORKER_CACHE_TTL: Duration = Duration::from_secs(5);
const CACHE_CAPACITY: usize = 10_000;

#[derive(Debug, PartialEq, Eq, thiserror::Error)]
pub enum ConfigurationError {
    #[error("Moss membership endpoint must be an HTTP URL without userinfo or a fragment")]
    InvalidEndpoint,
    #[error("Moss membership credential is missing or invalid")]
    InvalidCredential,
    #[error("cannot construct the Moss membership client")]
    ClientUnavailable,
}

#[derive(Debug, PartialEq, Eq, thiserror::Error)]
#[error("Moss membership lookup unavailable")]
pub struct MembershipUnavailable;

#[derive(Clone, Deserialize)]
struct Snapshot {
    status: String,
    revision: i64,
}

struct CachedSnapshot {
    snapshot: Option<Snapshot>,
    expires_at: Instant,
}

struct WorkerCache {
    entries: Mutex<HashMap<(String, String), CachedSnapshot>>,
    now: Box<dyn Fn() -> Instant + Send + Sync>,
}

impl WorkerCache {
    fn new() -> Self {
        Self {
            entries: Mutex::new(HashMap::new()),
            now: Box::new(Instant::now),
        }
    }

    fn get(
        &self,
        key: &(String, String),
        now: Instant,
    ) -> Result<Option<Option<Snapshot>>, MembershipUnavailable> {
        let entries = self.entries.lock().map_err(|_| MembershipUnavailable)?;
        Ok(entries
            .get(key)
            .filter(|entry| entry.expires_at > now)
            .map(|entry| entry.snapshot.clone()))
    }

    fn insert(
        &self,
        key: (String, String),
        snapshot: Option<Snapshot>,
        started: Instant,
    ) -> Result<(), MembershipUnavailable> {
        let mut entries = self.entries.lock().map_err(|_| MembershipUnavailable)?;
        let expires_at = started + WORKER_CACHE_TTL;
        // Concurrent lookups can finish out of order. An older lookup must
        // not replace a snapshot fetched by a later lookup.
        if entries
            .get(&key)
            .is_some_and(|entry| entry.expires_at > expires_at)
        {
            return Ok(());
        }
        if entries.len() >= CACHE_CAPACITY && !entries.contains_key(&key) {
            let now = (self.now)();
            entries.retain(|_, entry| entry.expires_at > now);
            if entries.len() >= CACHE_CAPACITY {
                let oldest = entries
                    .iter()
                    .min_by_key(|(_, entry)| entry.expires_at)
                    .map(|(key, _)| key.clone());
                if let Some(oldest) = oldest {
                    entries.remove(&oldest);
                }
            }
        }
        entries.insert(
            key,
            CachedSnapshot {
                snapshot,
                expires_at,
            },
        );
        Ok(())
    }
}

/// Moss owns membership. Access checks always fetch fresh state; only the
/// recovery worker may use a bounded, process-local five-second cache.
pub struct MossMembershipVerifier {
    client: Client,
    endpoint: Url,
    authorization: HeaderValue,
    worker_cache: Option<WorkerCache>,
}

impl MossMembershipVerifier {
    pub fn new(endpoint: &str, token: &str) -> Result<Self, ConfigurationError> {
        let endpoint = Url::parse(endpoint).map_err(|_| ConfigurationError::InvalidEndpoint)?;
        if !matches!(endpoint.scheme(), "http" | "https")
            || endpoint.host_str().is_none()
            || !endpoint.username().is_empty()
            || endpoint.password().is_some()
            || endpoint.fragment().is_some()
        {
            return Err(ConfigurationError::InvalidEndpoint);
        }
        let token = token.trim();
        if token.is_empty() {
            return Err(ConfigurationError::InvalidCredential);
        }
        let mut authorization = HeaderValue::from_str(&format!("Bearer {token}"))
            .map_err(|_| ConfigurationError::InvalidCredential)?;
        authorization.set_sensitive(true);
        let client = Client::builder()
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(LOOKUP_TIMEOUT)
            .build()
            .map_err(|_| ConfigurationError::ClientUnavailable)?;
        Ok(Self {
            client,
            endpoint,
            authorization,
            worker_cache: None,
        })
    }

    pub fn with_worker_cache(mut self) -> Self {
        self.worker_cache = Some(WorkerCache::new());
        self
    }

    /// A false result is an authoritative denial. An error means freshness
    /// could not be established and must remain distinct from that denial.
    pub async fn check(
        &self,
        user_id: &str,
        org_id: &str,
        membership_version: &str,
    ) -> Result<bool, MembershipUnavailable> {
        let snapshot = self.lookup(user_id, org_id).await?;
        Ok(snapshot.is_some_and(|snapshot| {
            snapshot.status == "active" && membership_version == format!("r{}", snapshot.revision)
        }))
    }

    async fn lookup(
        &self,
        user_id: &str,
        org_id: &str,
    ) -> Result<Option<Snapshot>, MembershipUnavailable> {
        let cache_lookup = if let Some(cache) = &self.worker_cache {
            let key = (user_id.to_owned(), org_id.to_owned());
            let started = (cache.now)();
            if let Some(snapshot) = cache.get(&key, started)? {
                return Ok(snapshot);
            }
            Some((key, started))
        } else {
            None
        };
        let response = self
            .client
            .get(self.endpoint.clone())
            .header(AUTHORIZATION, self.authorization.clone())
            .query(&[("user_id", user_id), ("org_id", org_id)])
            .send()
            .await
            .map_err(|_| MembershipUnavailable)?;
        let snapshot = if response.status() == StatusCode::NOT_FOUND {
            None
        } else {
            if !response.status().is_success() {
                return Err(MembershipUnavailable);
            }
            Some(
                response
                    .json::<Snapshot>()
                    .await
                    .map_err(|_| MembershipUnavailable)?,
            )
        };
        if let (Some(cache), Some((key, started))) = (&self.worker_cache, cache_lookup) {
            cache.insert(key, snapshot.clone(), started)?;
        }
        Ok(snapshot)
    }
}

#[cfg(test)]
mod tests;
