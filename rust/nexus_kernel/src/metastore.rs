//! Metastore pillar — Rust kernel metadata contract.
//!
//! Rust equivalent of Python `MetastoreABC` (one of the Four Storage Pillars).
//! Provides ordered key-value storage for file metadata (inodes, config, topology).
//!
//! Local impl: RedbMetastore (redb crate, ~5μs reads).
//! Remote impl: gRPC client (existing network boundary).
//!
//! Issue #1868: PR 7b — PyMetastoreAdapter wraps Python MetastoreABC → Rust Metastore trait.

use pyo3::prelude::*;

/// Metadata record for a single file/directory.
///
/// Mirrors the Python `FileMetadata` fields needed by the Rust kernel.
#[derive(Clone, Debug)]
#[allow(dead_code)]
pub(crate) struct FileMetadata {
    pub(crate) path: String,
    pub(crate) backend_name: String,
    pub(crate) physical_path: String,
    pub(crate) size: u64,
    pub(crate) etag: Option<String>,
    pub(crate) version: u32,
    pub(crate) entry_type: u8,
    pub(crate) zone_id: Option<String>,
    pub(crate) mime_type: Option<String>,
}

/// Error type for Metastore operations.
#[derive(Debug)]
#[allow(dead_code)]
pub(crate) enum MetastoreError {
    /// Key not found.
    NotFound(String),
    /// Underlying I/O or storage error.
    IOError(String),
}

/// Metastore pillar — kernel metadata contract.
///
/// Rust equivalent of Python `MetastoreABC`.
/// Local impls (redb) implement directly; remote impls go through
/// existing gRPC network boundaries.
///
/// 5 abstract methods matching the Python ABC:
///   - get, put, delete, list, exists
#[allow(dead_code)]
pub(crate) trait Metastore: Send + Sync {
    /// Get metadata for a path. Returns None if not found.
    fn get(&self, path: &str) -> Result<Option<FileMetadata>, MetastoreError>;

    /// Put metadata at a path (insert or update).
    fn put(&self, path: &str, metadata: FileMetadata) -> Result<(), MetastoreError>;

    /// Delete metadata at a path. Returns true if it existed.
    fn delete(&self, path: &str) -> Result<bool, MetastoreError>;

    /// List all metadata entries under a prefix.
    fn list(&self, prefix: &str) -> Result<Vec<FileMetadata>, MetastoreError>;

    /// Check if a path exists in the metastore.
    fn exists(&self, path: &str) -> Result<bool, MetastoreError>;
}

// ── PyMetastoreAdapter ─────────────────────────────────────────────────

/// Wraps a Python `MetastoreABC` instance → implements Rust `Metastore` trait.
///
/// Same pattern as syscall adapters: Rust trait is the ABI, PyO3 adapter
/// wraps Python impl. GIL acquired only on dcache-miss (cold path).
///
/// Issue #1868 PR 7b: Eliminates `_read_via_dlc()` in Python wrapper.
pub(crate) struct PyMetastoreAdapter {
    inner: Py<PyAny>,
}

// Safety: PyMetastoreAdapter holds a Py<PyAny> which is Send + Sync
// when accessed only under GIL (which we guarantee in every method).
unsafe impl Send for PyMetastoreAdapter {}
unsafe impl Sync for PyMetastoreAdapter {}

impl PyMetastoreAdapter {
    pub(crate) fn new(inner: Py<PyAny>) -> Self {
        Self { inner }
    }
}

/// Extract FileMetadata fields from a Python FileMetadata object.
///
/// Reads: path, backend_name, physical_path, size, etag, version,
///        entry_type, zone_id, mime_type.
fn extract_metadata(
    py: Python<'_>,
    obj: &Bound<'_, PyAny>,
) -> Result<FileMetadata, MetastoreError> {
    let get_str = |name: &str| -> Result<String, MetastoreError> {
        obj.getattr(name)
            .and_then(|v| v.extract::<String>())
            .map_err(|e| MetastoreError::IOError(format!("field {name}: {e}")))
    };
    let get_opt_str = |name: &str| -> Result<Option<String>, MetastoreError> {
        match obj.getattr(name) {
            Ok(v) if v.is_none() => Ok(None),
            Ok(v) => v
                .extract::<String>()
                .map(Some)
                .map_err(|e| MetastoreError::IOError(format!("field {name}: {e}"))),
            Err(e) => Err(MetastoreError::IOError(format!("field {name}: {e}"))),
        }
    };

    let _ = py; // used for type inference only
    Ok(FileMetadata {
        path: get_str("path")?,
        backend_name: get_str("backend_name")?,
        physical_path: get_str("physical_path")?,
        size: obj
            .getattr("size")
            .and_then(|v| v.extract::<u64>())
            .map_err(|e| MetastoreError::IOError(format!("field size: {e}")))?,
        etag: get_opt_str("etag")?,
        version: obj
            .getattr("version")
            .and_then(|v| v.extract::<u32>())
            .map_err(|e| MetastoreError::IOError(format!("field version: {e}")))?,
        entry_type: obj
            .getattr("entry_type")
            .and_then(|v| v.extract::<u8>())
            .map_err(|e| MetastoreError::IOError(format!("field entry_type: {e}")))?,
        zone_id: get_opt_str("zone_id")?,
        mime_type: get_opt_str("mime_type")?,
    })
}

impl Metastore for PyMetastoreAdapter {
    fn get(&self, path: &str) -> Result<Option<FileMetadata>, MetastoreError> {
        Python::attach(|py| {
            let obj = self.inner.bind(py);
            let result = obj
                .call_method1("get", (path,))
                .map_err(|e| MetastoreError::IOError(format!("metastore.get({path}): {e}")))?;
            if result.is_none() {
                return Ok(None);
            }
            extract_metadata(py, &result).map(Some)
        })
    }

    fn put(&self, path: &str, metadata: FileMetadata) -> Result<(), MetastoreError> {
        // Not used in PR 7b — metadata.put stays in Python wrapper.
        // Stub implementation for trait completeness.
        let _ = (path, metadata);
        Err(MetastoreError::IOError(
            "put not implemented via PyMetastoreAdapter — use Python wrapper".to_string(),
        ))
    }

    fn delete(&self, path: &str) -> Result<bool, MetastoreError> {
        let _ = path;
        Err(MetastoreError::IOError(
            "delete not implemented via PyMetastoreAdapter — use Python wrapper".to_string(),
        ))
    }

    fn list(&self, prefix: &str) -> Result<Vec<FileMetadata>, MetastoreError> {
        let _ = prefix;
        Err(MetastoreError::IOError(
            "list not implemented via PyMetastoreAdapter — use Python wrapper".to_string(),
        ))
    }

    fn exists(&self, path: &str) -> Result<bool, MetastoreError> {
        Python::attach(|py| {
            let obj = self.inner.bind(py);
            let result = obj
                .call_method1("exists", (path,))
                .map_err(|e| MetastoreError::IOError(format!("metastore.exists({path}): {e}")))?;
            result
                .extract::<bool>()
                .map_err(|e| MetastoreError::IOError(format!("metastore.exists result: {e}")))
        })
    }
}
