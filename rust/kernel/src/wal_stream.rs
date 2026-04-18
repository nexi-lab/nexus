//! Durable DT_STREAM backed by Raft-replicated metastore entries.
//!
//! 1:1 behavior port of the Python `WALStreamBackend` (src/nexus/core/wal_stream.py).
//! Each `write_nowait(data)` stores data under `/__wal_stream__/<stream_id>/<seq>`
//! as a `FileMetadata` entry, with the raw bytes hex-encoded in `physical_path`.
//! The raft transport replicates entries to peers; readers decode by sequence.
//!
//! Same caveats as the Python original (noted for future R19.1b' improvement):
//!   - Entries are stored as metadata records (hex-encoded payload in
//!     `physical_path`). A dedicated `Command::AppendStreamEntry` raft variant
//!     would be cleaner but is out of scope for this port.
//!   - Sequential read only (no random access within a message).
//!
//! Python wiring: `from nexus_kernel import WalStreamBackend;
//! WalStreamBackend(zone_handle, "stream-id")`.

use pyo3::exceptions::{PyRuntimeError, PyStopIteration};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;

use crate::metastore::{FileMetadata, Metastore};
use crate::raft_metastore::ZoneMetastore;

/// Rust core — wraps an `Arc<dyn Metastore>` and tracks per-stream state.
///
/// The PyO3 class below delegates here. Splitting keeps the Python binding
/// thin and unit-testable with any `Metastore` impl (not just `ZoneMetastore`).
pub struct WalStreamCore {
    metastore: Arc<dyn Metastore>,
    stream_id: String,
    prefix: String,
    next_seq: AtomicU64,
    closed: AtomicBool,
}

impl WalStreamCore {
    pub fn new(metastore: Arc<dyn Metastore>, stream_id: String) -> Self {
        let prefix = format!("/__wal_stream__/{stream_id}/");
        Self {
            metastore,
            stream_id,
            prefix,
            next_seq: AtomicU64::new(0),
            closed: AtomicBool::new(false),
        }
    }

    fn key(&self, seq: u64) -> String {
        format!("{}{seq}", self.prefix)
    }

    pub fn write_nowait(&self, data: &[u8]) -> Result<u64, String> {
        if self.closed.load(Ordering::Acquire) {
            return Err(format!("WAL stream {} is closed", self.stream_id));
        }
        // Atomic fetch_add: if two concurrent writers race, each gets a
        // unique seq. The raft metastore put is single-writer per key
        // (no overwrite race because seqs differ).
        let seq = self.next_seq.fetch_add(1, Ordering::AcqRel);
        let key = self.key(seq);
        let meta = FileMetadata {
            path: key.clone(),
            backend_name: "wal_stream".to_string(),
            physical_path: hex_encode(data),
            size: data.len() as u64,
            etag: Some(key.clone()),
            version: 0,
            entry_type: 0,
            zone_id: None,
            mime_type: None,
            created_at_ms: None,
            modified_at_ms: None,
        };
        self.metastore
            .put(&key, meta)
            .map_err(|e| format!("WAL put({key}): {e:?}"))?;
        Ok(seq)
    }

    /// Read entry at `seq`. Returns `Ok(Some(bytes))` if present,
    /// `Ok(None)` if not yet written (non-terminal), or `Err` if the
    /// stream is closed and no more data will arrive at this offset.
    pub fn read_at(&self, seq: u64) -> Result<Option<Vec<u8>>, String> {
        let key = self.key(seq);
        let meta = self
            .metastore
            .get(&key)
            .map_err(|e| format!("WAL get({key}): {e:?}"))?;
        match meta {
            Some(m) => hex_decode(&m.physical_path)
                .map(Some)
                .map_err(|e| format!("WAL hex decode({key}): {e}")),
            None => {
                if self.closed.load(Ordering::Acquire) {
                    Err(format!("WAL stream {} closed at seq {seq}", self.stream_id))
                } else {
                    Ok(None)
                }
            }
        }
    }

    pub fn read_batch(&self, start_seq: u64, count: usize) -> Result<(Vec<Vec<u8>>, u64), String> {
        let mut items = Vec::with_capacity(count);
        let mut seq = start_seq;
        for _ in 0..count {
            match self.read_at(seq) {
                Ok(Some(data)) => {
                    items.push(data);
                    seq += 1;
                }
                Ok(None) => break,
                Err(_) if !items.is_empty() => break,
                Err(e) => return Err(e),
            }
        }
        Ok((items, seq))
    }

    pub fn close(&self) {
        self.closed.store(true, Ordering::Release);
    }

    pub fn is_closed(&self) -> bool {
        self.closed.load(Ordering::Acquire)
    }

    pub fn tail(&self) -> u64 {
        self.next_seq.load(Ordering::Acquire)
    }

    pub fn stream_id(&self) -> &str {
        &self.stream_id
    }
}

// ---------------------------------------------------------------------------
// Hex encoding (match Python's bytes.hex() / bytes.fromhex()).
// ---------------------------------------------------------------------------

fn hex_encode(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut s = String::with_capacity(bytes.len() * 2);
    for &b in bytes {
        s.push(HEX[(b >> 4) as usize] as char);
        s.push(HEX[(b & 0x0f) as usize] as char);
    }
    s
}

fn hex_decode(s: &str) -> Result<Vec<u8>, &'static str> {
    if !s.len().is_multiple_of(2) {
        return Err("odd-length hex string");
    }
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(s.len() / 2);
    for chunk in bytes.chunks_exact(2) {
        let hi = hex_nibble(chunk[0])?;
        let lo = hex_nibble(chunk[1])?;
        out.push((hi << 4) | lo);
    }
    Ok(out)
}

fn hex_nibble(c: u8) -> Result<u8, &'static str> {
    match c {
        b'0'..=b'9' => Ok(c - b'0'),
        b'a'..=b'f' => Ok(c - b'a' + 10),
        b'A'..=b'F' => Ok(c - b'A' + 10),
        _ => Err("invalid hex char"),
    }
}

// ---------------------------------------------------------------------------
// PyO3 binding: WalStreamBackend
// ---------------------------------------------------------------------------

/// Durable StreamBackend backed by Raft WAL.
///
/// Constructed with a ZoneHandle + stream_id. All writes go through Raft
/// consensus (SC); reads are local state-machine lookups.
#[pyclass(name = "WalStreamBackend")]
pub struct WalStreamBackend {
    core: Arc<WalStreamCore>,
}

#[pymethods]
impl WalStreamBackend {
    /// Create a new WAL stream backend bound to a raft zone.
    ///
    /// Args:
    ///     zone_handle: A `nexus_kernel.ZoneHandle` instance.
    ///     stream_id: Unique identifier for this stream (used as key prefix).
    #[new]
    fn new(py: Python<'_>, zone_handle: Bound<'_, PyAny>, stream_id: String) -> PyResult<Self> {
        let _ = py;
        let zh_ref = zone_handle
            .cast::<nexus_raft::pyo3_bindings::PyZoneHandle>()
            .map_err(|e| {
                pyo3::exceptions::PyTypeError::new_err(format!("expected ZoneHandle, got: {e}"))
            })?;
        let zh = zh_ref.borrow();
        let consensus = zh.consensus_node();
        let handle = zh.runtime_handle();
        let metastore: Arc<dyn Metastore> = ZoneMetastore::new_arc(consensus, handle);
        Ok(Self {
            core: Arc::new(WalStreamCore::new(metastore, stream_id)),
        })
    }

    /// Append `data` to the WAL. Returns the sequence number (offset).
    fn write_nowait(&self, py: Python<'_>, data: &[u8]) -> PyResult<u64> {
        let core = self.core.clone();
        let data = data.to_vec();
        py.detach(|| core.write_nowait(&data).map_err(PyRuntimeError::new_err))
    }

    /// Async write — raft append is synchronous, so this just calls
    /// `write_nowait`. The `blocking` kwarg is accepted for StreamBackend
    /// protocol parity.
    #[pyo3(signature = (data, *, blocking=true))]
    fn write(&self, py: Python<'_>, data: &[u8], blocking: bool) -> PyResult<u64> {
        let _ = blocking;
        self.write_nowait(py, data)
    }

    /// Read entry at `byte_offset` (sequence number). Returns
    /// `(bytes, next_offset)`. Empty bytes means "no data yet" —
    /// caller polls or uses async `read()` for blocking semantics.
    fn read_at<'py>(
        &self,
        py: Python<'py>,
        byte_offset: u64,
    ) -> PyResult<(Bound<'py, PyBytes>, u64)> {
        let core = self.core.clone();
        let result = py.detach(|| core.read_at(byte_offset));
        match result {
            Ok(Some(data)) => Ok((PyBytes::new(py, &data), byte_offset + 1)),
            Ok(None) => Ok((PyBytes::new(py, b""), byte_offset)),
            Err(e) => Err(PyStopIteration::new_err(e)),
        }
    }

    /// Read up to `count` entries starting at `byte_offset`.
    #[pyo3(signature = (byte_offset=0, count=10))]
    fn read_batch<'py>(
        &self,
        py: Python<'py>,
        byte_offset: u64,
        count: usize,
    ) -> PyResult<(Bound<'py, PyList>, u64)> {
        let core = self.core.clone();
        let (items, next) = py
            .detach(|| core.read_batch(byte_offset, count))
            .map_err(PyRuntimeError::new_err)?;
        let list = PyList::empty(py);
        for item in items {
            list.append(PyBytes::new(py, &item))?;
        }
        Ok((list, next))
    }

    /// Mark the stream closed (readers past tail will get StopIteration).
    fn close(&self) {
        self.core.close();
    }

    /// True if `close()` has been called.
    #[getter]
    fn closed(&self) -> bool {
        self.core.is_closed()
    }

    /// Read-only stats dict. Shape matches the Python `WALStreamBackend.stats`
    /// property: `{stream_id, next_seq, closed, backend}`.
    #[getter]
    fn stats<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let dict = PyDict::new(py);
        dict.set_item("stream_id", self.core.stream_id())?;
        dict.set_item("next_seq", self.core.tail())?;
        dict.set_item("closed", self.core.is_closed())?;
        dict.set_item("backend", "wal")?;
        Ok(dict)
    }

    /// Current tail (== next seq to be written == number of entries).
    #[getter]
    fn tail(&self) -> u64 {
        self.core.tail()
    }
}

// ---------------------------------------------------------------------------
// Unit tests (in-memory Metastore stub — no raft runtime needed)
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::metastore::{MetastoreError, PutIfVersionResult};
    use std::sync::Mutex;

    /// Minimal in-memory Metastore for tests. Covers get/put/delete/list/exists.
    struct MemoryMetastore {
        inner: Mutex<std::collections::BTreeMap<String, FileMetadata>>,
    }

    impl MemoryMetastore {
        fn new() -> Self {
            Self {
                inner: Mutex::new(std::collections::BTreeMap::new()),
            }
        }
    }

    impl Metastore for MemoryMetastore {
        fn get(&self, path: &str) -> Result<Option<FileMetadata>, MetastoreError> {
            Ok(self.inner.lock().unwrap().get(path).cloned())
        }
        fn put(&self, path: &str, meta: FileMetadata) -> Result<(), MetastoreError> {
            self.inner.lock().unwrap().insert(path.to_string(), meta);
            Ok(())
        }
        fn delete(&self, path: &str) -> Result<bool, MetastoreError> {
            Ok(self.inner.lock().unwrap().remove(path).is_some())
        }
        fn list(&self, prefix: &str) -> Result<Vec<FileMetadata>, MetastoreError> {
            Ok(self
                .inner
                .lock()
                .unwrap()
                .range(prefix.to_string()..)
                .take_while(|(k, _)| k.starts_with(prefix))
                .map(|(_, v)| v.clone())
                .collect())
        }
        fn exists(&self, path: &str) -> Result<bool, MetastoreError> {
            Ok(self.inner.lock().unwrap().contains_key(path))
        }
        fn put_if_version(
            &self,
            metadata: FileMetadata,
            expected_version: u32,
        ) -> Result<PutIfVersionResult, MetastoreError> {
            let _ = (metadata, expected_version);
            Err(MetastoreError::IOError("not used".into()))
        }
    }

    fn core() -> WalStreamCore {
        WalStreamCore::new(Arc::new(MemoryMetastore::new()), "test".into())
    }

    #[test]
    fn hex_roundtrip() {
        for data in [
            b"".as_slice(),
            b"hello".as_slice(),
            &[0u8, 255, 1, 2, 3, 0, 0xde, 0xad, 0xbe, 0xef][..],
        ] {
            let encoded = hex_encode(data);
            assert_eq!(hex_decode(&encoded).unwrap(), data);
        }
    }

    #[test]
    fn write_then_read_single_entry() {
        let c = core();
        let seq = c.write_nowait(b"hello").unwrap();
        assert_eq!(seq, 0);
        let (data, _) = c.read_at(0).map(|o| (o.unwrap(), ())).unwrap();
        assert_eq!(data, b"hello");
        assert_eq!(c.tail(), 1);
    }

    #[test]
    fn write_many_preserves_order_and_seqs() {
        let c = core();
        for i in 0u8..10 {
            let seq = c.write_nowait(&[i, i + 1, i + 2]).unwrap();
            assert_eq!(seq, i as u64);
        }
        assert_eq!(c.tail(), 10);
        let (items, next) = c.read_batch(0, 100).unwrap();
        assert_eq!(items.len(), 10);
        assert_eq!(next, 10);
        for (i, item) in items.iter().enumerate() {
            assert_eq!(item, &[i as u8, i as u8 + 1, i as u8 + 2]);
        }
    }

    #[test]
    fn read_past_tail_returns_none_when_open() {
        let c = core();
        c.write_nowait(b"a").unwrap();
        assert_eq!(c.read_at(0).unwrap(), Some(b"a".to_vec()));
        assert_eq!(c.read_at(1).unwrap(), None);
    }

    #[test]
    fn read_past_tail_errors_when_closed() {
        let c = core();
        c.write_nowait(b"a").unwrap();
        c.close();
        assert!(c.read_at(1).is_err());
    }

    #[test]
    fn write_after_close_errors() {
        let c = core();
        c.close();
        assert!(c.write_nowait(b"x").is_err());
    }

    #[test]
    fn stats_reflect_tail_and_closed() {
        let c = core();
        assert_eq!(c.tail(), 0);
        assert!(!c.is_closed());
        c.write_nowait(b"x").unwrap();
        c.write_nowait(b"y").unwrap();
        assert_eq!(c.tail(), 2);
        c.close();
        assert!(c.is_closed());
    }

    #[test]
    fn read_batch_stops_at_tail() {
        let c = core();
        c.write_nowait(b"1").unwrap();
        c.write_nowait(b"2").unwrap();
        let (items, next) = c.read_batch(0, 100).unwrap();
        assert_eq!(items.len(), 2);
        assert_eq!(next, 2);
    }

    #[test]
    fn read_batch_from_middle() {
        let c = core();
        for i in 0u8..5 {
            c.write_nowait(&[i]).unwrap();
        }
        let (items, next) = c.read_batch(2, 10).unwrap();
        assert_eq!(items.len(), 3);
        assert_eq!(next, 5);
        assert_eq!(items[0], vec![2]);
        assert_eq!(items[2], vec![4]);
    }

    #[test]
    fn binary_data_roundtrip_with_nullbytes() {
        let c = core();
        let payload = vec![0u8, 1, 0, 2, 0, 3, 0xff, 0x00, 0xfe];
        c.write_nowait(&payload).unwrap();
        assert_eq!(c.read_at(0).unwrap(), Some(payload));
    }
}
