//! Upstream `nexus.search.v1.SearchService` client — one long-lived
//! `tonic::transport::Channel` per backend address, dialed lazily
//! on first use and cached for the process lifetime.
//!
//! # Why cache
//!
//! Every request through [`SearchBackend::client`] would otherwise
//! open a fresh TCP + gRPC handshake — the search-plugin cluster is
//! a stable set of upstreams and a per-request dial makes p99 dive
//! for no benefit.  The sibling `nexus-search-plugin::peer_fanout`
//! uses the same DashMap-per-address shape for its own peer client
//! cache; we mirror the pattern.
//!
//! # Race posture
//!
//! Two concurrent misses on the same address can both dial before
//! either publishes — DashMap's `entry().or_insert()` picks a
//! winner atomically and the loser's Channel gets dropped.  We
//! accept the extra dial (bounded by tonic's connect timeout) to
//! avoid holding a shard write-lock across `.await`, which would
//! starve every sibling address hashing to the same shard.  Same
//! rationale documented on `peer_fanout::PeerFanoutDispatcher::get_or_dial`.

use std::sync::Arc;

use dashmap::DashMap;
use tonic::transport::{Channel, Endpoint};

use crate::search_proto::search_service_client::SearchServiceClient;

/// Errors surfaced by [`SearchBackend`] when the upstream cannot be
/// reached.  Kept small — every variant maps 1:1 to an HTTP status
/// at the handler boundary via `axum::response::IntoResponse` (see
/// `handlers/search.rs`).
#[derive(Debug, thiserror::Error)]
pub enum BackendError {
    /// The target string is not a valid gRPC endpoint (bad scheme,
    /// bad host:port, tonic could not parse).  Config bug — the
    /// operator gave us a garbage address.
    #[error("invalid backend endpoint {target}: {source}")]
    InvalidEndpoint {
        target: String,
        #[source]
        source: tonic::transport::Error,
    },

    /// Dial failed.  Backend down, DNS blackholed, network partition
    /// — any transport error hitting the wire before we send the RPC.
    #[error("connect to backend {target} failed: {source}")]
    ConnectFailed {
        target: String,
        #[source]
        source: tonic::transport::Error,
    },
}

/// Live upstream registry — one entry per gRPC backend address.
/// Cheap to construct; the dial happens on first [`Self::client`]
/// call and the Channel is cached for the process lifetime.
///
/// Cloneable via `Arc` so axum's `State` extractor can hand every
/// handler a shared reference to the same cache.
#[derive(Clone)]
pub struct SearchBackend {
    default_target: Arc<str>,
    channels: Arc<DashMap<Arc<str>, Channel>>,
}

impl SearchBackend {
    /// Construct a backend that routes every request to `default_target`.
    /// `default_target` is a full gRPC endpoint (e.g. `http://127.0.0.1:2126`);
    /// the address is stored as `Arc<str>` so cache lookups avoid a
    /// per-request `String` clone.
    pub fn new(default_target: impl Into<Arc<str>>) -> Self {
        Self {
            default_target: default_target.into(),
            channels: Arc::new(DashMap::new()),
        }
    }

    /// Return a `SearchServiceClient` wired to the default target,
    /// dialing lazily on first call.  Cheap on subsequent calls —
    /// the Channel is cached and `SearchServiceClient::new` is a
    /// no-op wrapper around the shared Channel handle.
    pub async fn client(&self) -> Result<SearchServiceClient<Channel>, BackendError> {
        let channel = self.get_or_dial(Arc::clone(&self.default_target)).await?;
        // 64 MiB matches the sibling `peer_fanout::fan_out` cap so
        // an operator upgrading either side of the wire doesn't hit
        // an asymmetric decode ceiling.
        Ok(SearchServiceClient::new(channel).max_decoding_message_size(64 * 1024 * 1024))
    }

    async fn get_or_dial(&self, target: Arc<str>) -> Result<Channel, BackendError> {
        if let Some(c) = self.channels.get(&target) {
            return Ok(c.clone());
        }
        let endpoint = Endpoint::from_shared(target.as_ref().to_string()).map_err(|e| {
            BackendError::InvalidEndpoint {
                target: target.as_ref().to_string(),
                source: e,
            }
        })?;
        let channel = endpoint.connect_lazy();
        // `connect_lazy` never fails — the first RPC dial is where
        // errors surface (as `tonic::Status`, handled by the
        // handler).  Cache immediately so concurrent requesters
        // share the Channel.
        Ok(self.channels.entry(target).or_insert(channel).clone())
    }
}
