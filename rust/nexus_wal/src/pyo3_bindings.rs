//! PyO3 bindings for the WAL engine.
//!
//! Exposes `PyWAL` as the primary Python-visible class.
//! All I/O is synchronous (Rust mutex) — Python wraps with async.

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;

use crate::wal::{SyncMode, WalEngine, WalError};

/// Convert WalError to Python RuntimeError.
fn to_py_err(e: WalError) -> PyErr {
    PyRuntimeError::new_err(format!("{e}"))
}

/// Rust-backed Write-Ahead Log exposed to Python.
///
/// Thread-safe: all methods take `&self`. Internal mutex handles concurrency.
#[pyclass(frozen, name = "PyWAL")]
pub struct PyWAL {
    engine: WalEngine,
}

#[pymethods]
impl PyWAL {
    /// Create or open a WAL at `wal_dir`.
    ///
    /// Args:
    ///     wal_dir: Directory path for WAL segment files.
    ///     segment_size: Max bytes per segment before rotation (default 4MB).
    ///     sync_mode: "every" (fsync per write) or "none" (OS-buffered).
    #[new]
    #[pyo3(signature = (wal_dir, segment_size = 4194304, sync_mode = "every"))]
    fn new(wal_dir: &str, segment_size: u64, sync_mode: &str) -> PyResult<Self> {
        let mode = SyncMode::parse(sync_mode).ok_or_else(|| {
            PyRuntimeError::new_err(format!(
                "invalid sync_mode: '{sync_mode}' (expected 'every' or 'none')"
            ))
        })?;
        let engine = WalEngine::open(std::path::Path::new(wal_dir), segment_size, mode)
            .map_err(to_py_err)?;
        Ok(Self { engine })
    }

    /// Append a single record. Returns the sequence number.
    fn append(&self, zone_id: &[u8], payload: &[u8]) -> PyResult<u64> {
        self.engine.append(zone_id, payload).map_err(to_py_err)
    }

    /// Append a batch of (zone_id, payload) records. Returns sequence numbers.
    fn append_batch(&self, events: Vec<(Vec<u8>, Vec<u8>)>) -> PyResult<Vec<u64>> {
        self.engine.append_batch(&events).map_err(to_py_err)
    }

    /// Read records from `seq` up to `limit`, with optional zone filter.
    ///
    /// Returns: list of (seq, zone_id, payload) tuples.
    #[pyo3(signature = (seq, limit, zone_id = None))]
    #[allow(clippy::type_complexity)]
    fn read_from(
        &self,
        seq: u64,
        limit: usize,
        zone_id: Option<&[u8]>,
    ) -> PyResult<Vec<(u64, Vec<u8>, Vec<u8>)>> {
        let records = self
            .engine
            .read_from(seq, limit, zone_id)
            .map_err(to_py_err)?;
        Ok(records
            .into_iter()
            .map(|r| (r.seq, r.zone_id, r.payload))
            .collect())
    }

    /// Delete segments with all records < before_seq. Returns records deleted.
    fn truncate(&self, before_seq: u64) -> PyResult<u64> {
        self.engine.truncate(before_seq).map_err(to_py_err)
    }

    /// Force fsync on the active segment.
    fn sync_wal(&self) -> PyResult<()> {
        self.engine.sync().map_err(to_py_err)
    }

    /// Close the WAL (fsync + release).
    fn close(&self) -> PyResult<()> {
        self.engine.close().map_err(to_py_err)
    }

    /// Most recently assigned sequence number (0 if empty).
    fn current_sequence(&self) -> u64 {
        self.engine.current_sequence()
    }

    /// True if the WAL is open and writable.
    fn health_check(&self) -> bool {
        self.engine.health_check()
    }
}
