//! StreamBackend pillar — uniform interface for DT_STREAM IPC backends.
//!
//! In-memory reference impl `MemoryStreamBackend` lives in
//! `crate::stream`; SHM, stdio, WAL, and remote variants live in
//! sibling files.

#[derive(Debug)]
#[allow(dead_code)]
pub enum StreamError {
    Closed(&'static str),
    Full(usize, usize),
    Empty,
    ClosedEmpty,
    Oversized(usize, usize),
    InvalidOffset(usize, usize),
}

/// Uniform interface for stream backends (memory, shared memory, future gRPC).
///
/// Enables `DashMap<String, Arc<dyn StreamBackend>>` in StreamManager for
/// heterogeneous backend dispatch.
#[allow(dead_code)] // Used via Arc<dyn StreamBackend> in StreamManager + generated_pyo3.rs
pub trait StreamBackend: Send + Sync {
    fn push(&self, data: &[u8]) -> Result<usize, StreamError>;
    fn read_at(&self, offset: usize) -> Result<(Vec<u8>, usize), StreamError>;
    fn read_batch(&self, offset: usize, count: usize)
        -> Result<(Vec<Vec<u8>>, usize), StreamError>;
    fn close(&self);
    fn is_closed(&self) -> bool;
    fn tail_offset(&self) -> usize;
    fn msg_count(&self) -> usize;
}
