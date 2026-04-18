//! Debug-surface helpers for the ``FileMetadata`` proto boundary.
//!
//! Exposes ``proto_to_kernel`` / ``kernel_to_proto`` (from
//! ``raft_metastore``) to Python so cross-language round-trip tests
//! can verify that fields Python writes via ``MetadataMapper.to_proto``
//! survive Rust's proto decode path — most importantly
//! ``target_zone_id`` for DT_MOUNT entries (R16.1a).
//!
//! These are intentionally narrow ``pyfunction``s (not ``pyclass``),
//! since they exist purely to let Python tests assert what Rust sees.
//! Production metastore reads/writes go through ``ZoneMetastore``.
//!
//! Keep the surface minimal — if a caller needs full field access
//! in Python, use ``MetadataMapper.from_proto`` instead.
//!
//! Implementation note (R16.1a): these helpers live in their own
//! module rather than in ``raft_metastore.rs`` so adding them does
//! not drag a ``pyo3`` import into a module that currently has none.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};

use crate::raft_metastore::{kernel_to_proto, proto_to_kernel};

/// Decode ``FileMetadata`` proto bytes into a Python dict snapshot.
///
/// Returns the Rust-side view of every field the kernel tracks —
/// used by ``tests/unit/storage/test_metadata_mapper_rust_roundtrip.py``
/// to assert that Python-authored ``MetadataMapper.to_proto`` output
/// lands in Rust with the fields (notably ``target_zone_id``) intact.
#[pyfunction]
pub fn file_metadata_from_proto_bytes<'py>(
    py: Python<'py>,
    bytes: &[u8],
) -> PyResult<Bound<'py, PyDict>> {
    let meta = proto_to_kernel(bytes)
        .map_err(|e| PyValueError::new_err(format!("proto decode: {e:?}")))?;
    let dict = PyDict::new(py);
    dict.set_item("path", meta.path)?;
    dict.set_item("backend_name", meta.backend_name)?;
    dict.set_item("physical_path", meta.physical_path)?;
    dict.set_item("size", meta.size)?;
    dict.set_item("etag", meta.etag)?;
    dict.set_item("version", meta.version)?;
    dict.set_item("entry_type", meta.entry_type)?;
    dict.set_item("zone_id", meta.zone_id)?;
    dict.set_item("target_zone_id", meta.target_zone_id)?;
    dict.set_item("mime_type", meta.mime_type)?;
    Ok(dict)
}

/// Encode a minimal ``FileMetadata`` (fields the kernel tracks) into
/// proto bytes. Mirror of ``file_metadata_from_proto_bytes``.
#[pyfunction]
#[pyo3(signature = (
    path,
    backend_name,
    physical_path,
    size,
    version,
    entry_type,
    *,
    etag=None,
    zone_id=None,
    target_zone_id=None,
    mime_type=None,
))]
#[allow(clippy::too_many_arguments)]
pub fn file_metadata_to_proto_bytes<'py>(
    py: Python<'py>,
    path: String,
    backend_name: String,
    physical_path: String,
    size: u64,
    version: u32,
    entry_type: u8,
    etag: Option<String>,
    zone_id: Option<String>,
    target_zone_id: Option<String>,
    mime_type: Option<String>,
) -> Bound<'py, PyBytes> {
    let meta = crate::metastore::FileMetadata {
        path,
        backend_name,
        physical_path,
        size,
        etag,
        version,
        entry_type,
        zone_id,
        target_zone_id,
        mime_type,
        created_at_ms: None,
        modified_at_ms: None,
    };
    PyBytes::new(py, &kernel_to_proto(&meta))
}
