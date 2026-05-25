//! PipeBackend pillar — uniform interface for DT_PIPE IPC backends.
//!
//! In-memory reference impl `MemoryPipeBackend` lives in `crate::pipe`;
//! SHM, stdio, and remote variants live in sibling files.

#[derive(Debug)]
pub(crate) enum PipeError {
    Closed(&'static str),
    Full(usize, usize),
    Empty,
    ClosedEmpty,
    Oversized(usize, usize),
}

/// Uniform interface for pipe backends (memory, shared memory, future gRPC).
///
/// Enables `DashMap<String, Arc<dyn PipeBackend>>` in PipeManager for
/// heterogeneous backend dispatch.
pub(crate) trait PipeBackend: Send + Sync {
    fn push(&self, data: &[u8]) -> Result<usize, PipeError>;
    fn pop(&self) -> Result<Vec<u8>, PipeError>;
    fn close(&self);
    /// Diagnostic accessors — no dyn-dispatch caller exercises these
    /// today; retained on the trait so future kernel introspection /
    /// admin probes can read them through any backend uniformly.
    #[allow(dead_code)]
    fn is_closed(&self) -> bool;
    #[allow(dead_code)]
    fn is_empty(&self) -> bool;
    fn size(&self) -> usize;
    #[allow(dead_code)]
    fn msg_count(&self) -> usize;
}
