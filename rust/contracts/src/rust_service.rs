//! Rust-flavoured service contract — shared between the kernel
//! crate (which owns `ServiceRegistry`) and any out-of-kernel crate
//! that wants to register a `RustService` impl.
//!
//! Lifted out of `rust/kernel/src/service_registry.rs` so service
//! implementations don't have to live inside the kernel crate to
//! pull in this trait. The kernel re-exports both types so existing
//! `crate::service_registry::{RustService, RustCallError}` import
//! sites keep compiling.

use std::error::Error;
use std::fmt;

/// Error returned by `RustService::dispatch` and surfaced through
/// `Kernel::dispatch_rust_call`. Maps onto JSON-RPC-shaped wire error
/// codes by the gRPC `Call` handler.
#[derive(Debug)]
pub enum RustCallError {
    /// Method name is not handled by this service. The default
    /// `RustService::dispatch` impl returns this so existing services
    /// compile without an explicit override.
    NotFound,
    /// Payload could not be parsed, or its fields are out of range.
    InvalidArgument(String),
    /// Service-internal failure (state corruption, downstream IO error).
    Internal(String),
}

impl fmt::Display for RustCallError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotFound => write!(f, "method not found"),
            Self::InvalidArgument(m) => write!(f, "invalid argument: {m}"),
            Self::Internal(m) => write!(f, "internal: {m}"),
        }
    }
}

impl Error for RustCallError {}

/// Surface a Rust-implemented service exposes to the kernel's
/// `ServiceRegistry`.
///
/// Mirrors the Python `BackgroundService` protocol but with
/// synchronous Rust signatures. `start` / `stop` are called by the
/// registry's lifecycle hooks; `name` is the canonical service name
/// used for `nx.service("…")` lookups; `dispatch` is the per-call
/// entry the gRPC `Call` handler routes through.
///
/// Implementors must be `Send + Sync` so the registry can hand
/// `Arc<dyn RustService>` to multiple consumers.
pub trait RustService: Send + Sync {
    fn name(&self) -> &str;

    /// Start the service. Called once at bootstrap (or at enlist
    /// time for services registered post-bootstrap). Blocking is
    /// fine — the Rust path does not run on the asyncio loop.
    fn start(&self) -> Result<(), String> {
        Ok(())
    }

    /// Stop the service. Called once at shutdown, in reverse
    /// registration order.
    fn stop(&self) -> Result<(), String> {
        Ok(())
    }

    /// Dispatch a JSON-encoded RPC. The gRPC `Call` handler routes
    /// `NexusVFSService.Call(method, payload)` requests to a Rust
    /// service first via `Kernel::dispatch_rust_call`; on `NotFound`
    /// the handler falls through to the Python `dispatch_method`
    /// path, preserving compatibility with `@rpc_expose` services.
    ///
    /// `method` is the bare method name (no service prefix);
    /// `payload` is the raw JSON request body. Implementations parse
    /// and encode with `serde_json` and surface decode failures as
    /// `RustCallError::InvalidArgument`.
    ///
    /// Default impl returns `NotFound` so services that do not yet
    /// expose any RPCs continue to compile.
    fn dispatch(&self, _method: &str, _payload: &[u8]) -> Result<Vec<u8>, RustCallError> {
        Err(RustCallError::NotFound)
    }
}
