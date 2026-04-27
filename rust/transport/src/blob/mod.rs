//! Peer blob fetch — sub-module of `transport`.
//!
//! Phase 4: lifted out of `kernel/src/{peer_blob_client,blob_fetcher}.rs`.
//! `peer_client::PeerBlobClient` impls
//! `kernel::hal::peer::PeerBlobClient` (HAL trait declared in kernel
//! since Phase 1) so kernel callers hold an `Arc<dyn PeerBlobClient>`
//! and never touch the concrete type — the cycle break that lets the
//! impl live here even though kernel uses the trait.

pub mod fetcher;
pub mod peer_client;
