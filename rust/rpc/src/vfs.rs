//! VFS gRPC client — re-export of `kernel::rpc_transport`.
//!
//! Trait declaration and implementation live in the kernel crate
//! because the kernel-internal `RemoteMetaStore` / `RemotePipeBackend`
//! / `RemoteStreamBackend` wrappers also wrap a concrete `RpcTransport`.
//! This module re-exports the public surface so peer crates name a
//! single canonical path (`rpc::vfs::RpcTransport`) consistent with
//! the rest of the rpc client modules (peer_blob, federation).

pub use kernel::rpc_transport::{RpcTransport, TlsConfig};
