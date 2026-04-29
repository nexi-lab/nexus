//! Peer blob fetch — sub-module of `transport`.
//!
//! `peer_client::PeerBlobClient` impls
//! `transport_primitives::PeerBlobClient` (trait lives in the shared
//! crate so raft + rpc can both name it without depending on each
//! other). Kernel callers hold an `Arc<dyn PeerBlobClient>` and reach
//! the concrete impl through the slot wired at cdylib boot.
//!
//! The server-side `KernelBlobFetcher` handler lives in
//! `nexus_raft::blob_fetcher_handler` — co-located with the raft
//! gRPC server that owns the `ReadBlob` wire format.

pub mod peer_client;
