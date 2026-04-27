//! PipeBackend pillar — uniform interface for DT_PIPE IPC backends.
//!
//! In-memory reference impl `MemoryPipeBackend` lives in `crate::pipe`;
//! SHM, stdio, and remote variants live in sibling files (and Phase F
//! lifts the remote variant into `transport::pipe`).

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
#[allow(dead_code)] // Used via Arc<dyn PipeBackend> in PipeManager + generated_pyo3.rs
pub(crate) trait PipeBackend: Send + Sync {
    fn push(&self, data: &[u8]) -> Result<usize, PipeError>;
    fn pop(&self) -> Result<Vec<u8>, PipeError>;
    fn close(&self);
    fn is_closed(&self) -> bool;
    fn is_empty(&self) -> bool;
    fn size(&self) -> usize;
    fn msg_count(&self) -> usize;
}
