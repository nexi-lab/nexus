//! pyo3 bridge exposing `PyPrefetchEngine` to Python.  Compiled only
//! under the `python` feature.  The engine takes a Python callable
//! `(key: str, offset: int, size: int) -> bytes` as its RangeReader,
//! so the Python `ReadaheadManager` shim can forward to
//! `read_range_from_backend` without round-tripping through Rust
//! backend dispatch.

#![cfg(feature = "python")]

use crate::engine::DetectorKind;
use crate::{EngineConfig, PrefetchEngine, PrefetchError, RangeReader};
use bytes::Bytes;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use std::sync::Arc;

struct PyCallableReader {
    callable: Py<PyAny>,
}

impl RangeReader for PyCallableReader {
    fn read(&self, key: &str, offset: u64, size: u32) -> Result<Bytes, PrefetchError> {
        Python::attach(|py| -> Result<Bytes, PrefetchError> {
            let res = self
                .callable
                .call1(py, (key, offset, size))
                .map_err(|e| PrefetchError::Backend(format!("python read failed: {e}")))?;
            // pyo3 0.28: extract Vec<u8> for an owning copy. Zero-copy via
            // downcast is possible but the engine takes ownership of Bytes
            // anyway, so the copy is unavoidable on the boundary.
            let v: Vec<u8> = res
                .extract(py)
                .map_err(|e| PrefetchError::Backend(format!("expected bytes from py read: {e}")))?;
            Ok(Bytes::from(v))
        })
    }
}

#[pyclass(name = "PrefetchEngine", module = "nexus_runtime")]
pub struct PyPrefetchEngine {
    inner: Option<PrefetchEngine>,
}

#[pymethods]
impl PyPrefetchEngine {
    #[new]
    #[pyo3(signature = (
        read_callable,
        block_size,
        initial_window,
        max_window,
        max_workers,
        queue_capacity,
        max_blocks_per_trigger,
        sequential_tolerance,
        min_sequential_count,
        detector="sequential",
        shutdown_timeout_ms=2000,
        max_buffer_bytes=128 * 1024 * 1024,
    ))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        read_callable: Py<PyAny>,
        block_size: u32,
        initial_window: u64,
        max_window: u64,
        max_workers: usize,
        queue_capacity: usize,
        max_blocks_per_trigger: u32,
        sequential_tolerance: u64,
        min_sequential_count: u32,
        detector: &str,
        shutdown_timeout_ms: u64,
        max_buffer_bytes: u64,
    ) -> PyResult<Self> {
        // Normalize before building the tokio runtime — passing
        // worker_threads(0) panics (round 6 finding #1).  Clamp +
        // normalize are idempotent, so `with_detector` re-applying
        // them is harmless.
        let cfg = EngineConfig {
            block_size,
            initial_window,
            max_window,
            max_workers,
            queue_capacity,
            max_blocks_per_trigger,
            sequential_tolerance,
            min_sequential_count,
            shutdown_timeout_ms,
            max_buffer_bytes,
        }
        .clamp()
        .normalize();
        let detector_kind = match detector.to_ascii_lowercase().as_str() {
            "sequential" => DetectorKind::Sequential,
            "stride" => DetectorKind::Stride,
            "trend" | "majority_trend" | "majority-trend" => DetectorKind::MajorityTrend,
            other => {
                return Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "unknown detector kind {other:?} (expected sequential|stride|trend)"
                )));
            }
        };
        let reader: Arc<dyn RangeReader> = Arc::new(PyCallableReader {
            callable: read_callable,
        });
        let rt = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(cfg.max_workers)
            .enable_all()
            .build()
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("rt build: {e}")))?;
        let engine = PrefetchEngine::with_detector(cfg, reader, Some(rt), detector_kind);
        Ok(Self {
            inner: Some(engine),
        })
    }

    fn on_open(&self, fh: u64, path: &str, file_size: Option<u64>) {
        if let Some(e) = self.inner.as_ref() {
            e.on_open(fh, path, file_size);
        }
    }

    fn on_read<'py>(
        &self,
        py: Python<'py>,
        fh: u64,
        offset: u64,
        size: u32,
    ) -> Option<Bound<'py, PyBytes>> {
        let e = self.inner.as_ref()?;
        let b = e.on_read(fh, offset, size)?;
        Some(PyBytes::new(py, &b))
    }

    fn on_release(&self, fh: u64) {
        if let Some(e) = self.inner.as_ref() {
            e.on_release(fh);
        }
    }

    /// Drop all prefetched data + pending work for a single open fh.
    /// Wired into `ReadaheadManager.invalidate_path` so writes/deletes
    /// can never serve stale prefetched bytes.
    fn invalidate_fh(&self, fh: u64) {
        if let Some(e) = self.inner.as_ref() {
            e.invalidate_fh(fh);
        }
    }

    /// Same as `invalidate_fh` but addresses every fh currently open
    /// against `path`.  Used by the FUSE invalidation hook.
    fn invalidate_path(&self, path: &str) {
        if let Some(e) = self.inner.as_ref() {
            e.invalidate_path(path);
        }
    }

    fn metrics(&self) -> PyResult<(u64, u64, u64, u64, u64)> {
        let e = self
            .inner
            .as_ref()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("engine shut down"))?;
        let s = e.metrics();
        Ok((
            s.hits,
            s.misses,
            s.prefetched_bytes,
            s.dropped_backpressure,
            s.resets,
        ))
    }

    fn shutdown(&mut self, py: Python<'_>) {
        if let Some(e) = self.inner.take() {
            // Release the GIL while the tokio runtime drains.  Worker
            // tasks may be mid-`spawn_blocking` calling back into Python
            // via `Python::attach` — holding the GIL here would deadlock
            // the runtime-drop barrier waiting for workers blocked on
            // GIL acquisition.  Use the engine's explicit `shutdown`
            // path (round 3 finding #3) which closes the queue, aborts
            // workers, and calls `Runtime::shutdown_timeout` for a
            // bounded teardown even when a Python read_callable is hung.
            py.detach(|| e.shutdown());
        }
    }
}

// Drop runs without a Python<'_> argument; pyo3 invokes it during GC
// while the GIL may or may not be held.  Mirror the explicit-shutdown
// GIL-release dance so implicit drops (e.g. the Python __del__ path)
// cannot deadlock the runtime drop.
impl Drop for PyPrefetchEngine {
    fn drop(&mut self) {
        if let Some(e) = self.inner.take() {
            Python::attach(|py| py.detach(|| e.shutdown()));
        }
    }
}
