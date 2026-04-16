//! Cross-process SPSC RingBuffer via mmap + OS pipe notification (#1680).
//!
//! Same algorithms as `pipe.rs` (RingBufferCore) — push_inner/pop_position/
//! commit_pop — but buffer lives in a MAP_SHARED mmap region instead of
//! heap `Vec<u8>`.  OS pipes provide cross-process wakeup.
//!
//! Layout (512 bytes header + ring data):
//!
//! ```text
//! [0..128)    Slot 0 — immutable config (cache-line aligned)
//!             magic: u32 = 0x4E585049 ("NXPI")
//!             version: u32 = 1
//!             ring_cap: u32
//!             user_capacity: u32
//!
//! [128..256)  Slot 1 — writer-hot (separate cache line from reader)
//!             tail: AtomicUsize
//!             push_count: AtomicU64
//!             msg_count: AtomicUsize
//!             used_bytes: AtomicUsize
//!
//! [256..384)  Slot 2 — reader-hot
//!             head: AtomicUsize
//!             pop_count: AtomicU64
//!
//! [384..512)  Slot 3 — shared flags
//!             closed: AtomicBool (stored as u8)
//!
//! [512..)     Ring data region (ring_cap bytes)
//! ```
//!
//! 128-byte slot alignment covers x86 (64B) and Apple M-series (128B).

use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const HEADER_SIZE: usize = 4; // 4-byte u32 LE length prefix
const MAGIC: u32 = 0x4E58_5049; // "NXPI"
const VERSION: u32 = 1;
const SLOT_SIZE: usize = 128; // cache-line aligned slot
const DATA_OFFSET: usize = SLOT_SIZE * 4; // 512 bytes header

// Offsets within the mmap header (byte offsets from start)
// Slot 0: immutable config
const OFF_MAGIC: usize = 0;
const OFF_VERSION: usize = 4;
const OFF_RING_CAP: usize = 8;
const OFF_USER_CAP: usize = 12;
// Slot 1: writer-hot
const OFF_TAIL: usize = SLOT_SIZE;
const OFF_PUSH_COUNT: usize = SLOT_SIZE + 8;
const OFF_MSG_COUNT: usize = SLOT_SIZE + 16;
const OFF_USED_BYTES: usize = SLOT_SIZE + 24;
// Slot 2: reader-hot
const OFF_HEAD: usize = SLOT_SIZE * 2;
const OFF_POP_COUNT: usize = SLOT_SIZE * 2 + 8;
// Slot 3: flags
const OFF_CLOSED: usize = SLOT_SIZE * 3;

// Use shared PipeError from pipe.rs
use crate::pipe::PipeError;

// ---------------------------------------------------------------------------
// Shared-memory header accessors (safe wrappers over raw mmap)
// ---------------------------------------------------------------------------

/// Read a u32 from mmap at the given offset.
#[inline]
fn read_u32(base: *const u8, off: usize) -> u32 {
    unsafe {
        let ptr = base.add(off) as *const u32;
        ptr.read()
    }
}

/// Write a u32 to mmap at the given offset.
#[inline]
fn write_u32(base: *mut u8, off: usize, val: u32) {
    unsafe {
        let ptr = base.add(off) as *mut u32;
        ptr.write(val);
    }
}

/// Get an AtomicUsize reference from mmap at the given offset.
///
/// SAFETY: The caller must ensure the pointer is valid and 8-byte aligned,
/// and lives for the duration of the returned reference.
#[inline]
unsafe fn atomic_usize(base: *const u8, off: usize) -> &'static AtomicUsize {
    &*(base.add(off) as *const AtomicUsize)
}

/// Get an AtomicU64 reference from mmap at the given offset.
#[inline]
unsafe fn atomic_u64(base: *const u8, off: usize) -> &'static AtomicU64 {
    &*(base.add(off) as *const AtomicU64)
}

/// Get an AtomicBool reference from mmap at the given offset.
#[inline]
unsafe fn atomic_bool(base: *const u8, off: usize) -> &'static AtomicBool {
    &*(base.add(off) as *const AtomicBool)
}

// ---------------------------------------------------------------------------
// SharedMemoryPipeBackend
// ---------------------------------------------------------------------------

/// Cross-process SPSC ring buffer backed by mmap + OS pipe notification.
///
/// `create()` allocates a temp file + mmap + two OS pipes.
/// `attach()` opens the same file + mmap from another process.
///
/// Python side uses `loop.add_reader(fd, callback)` on the notification
/// pipe fds for zero-cost asyncio integration.
#[pyclass]
pub struct SharedMemoryPipeBackend {
    mmap: memmap2::MmapMut,
    ring_cap: usize,
    user_capacity: usize,
    // Notification pipe write-ends (creator holds both, attacher holds neither —
    // they are inherited as readable fds)
    notify_data_wr: i32,  // writer writes here after push (wakes reader)
    notify_space_wr: i32, // reader writes here after pop (wakes writer)
    is_creator: bool,
    shm_path: String,
}

// SAFETY: SPSC by design — single producer + single consumer across processes.
unsafe impl Send for SharedMemoryPipeBackend {}
unsafe impl Sync for SharedMemoryPipeBackend {}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

impl SharedMemoryPipeBackend {
    /// Pointer to the start of the mmap region.
    #[inline]
    fn base(&self) -> *const u8 {
        self.mmap.as_ptr()
    }

    /// Mutable pointer to the start of the mmap region.
    #[inline]
    fn base_mut(&self) -> *mut u8 {
        self.mmap.as_ptr() as *mut u8
    }

    /// Pointer to the ring data region.
    #[inline]
    fn ring_ptr(&self) -> *mut u8 {
        unsafe { self.base_mut().add(DATA_OFFSET) }
    }

    /// Ring data as a mutable slice.
    ///
    /// SAFETY: SPSC design — writer and reader access disjoint regions
    /// of the mmap'd ring buffer, guarded by atomic head/tail.
    #[allow(clippy::mut_from_ref)]
    #[inline]
    fn ring_slice(&self) -> &mut [u8] {
        unsafe { std::slice::from_raw_parts_mut(self.ring_ptr(), self.ring_cap) }
    }

    fn head(&self) -> &AtomicUsize {
        unsafe { atomic_usize(self.base(), OFF_HEAD) }
    }

    fn tail(&self) -> &AtomicUsize {
        unsafe { atomic_usize(self.base(), OFF_TAIL) }
    }

    fn push_count(&self) -> &AtomicU64 {
        unsafe { atomic_u64(self.base(), OFF_PUSH_COUNT) }
    }

    fn pop_count(&self) -> &AtomicU64 {
        unsafe { atomic_u64(self.base(), OFF_POP_COUNT) }
    }

    fn msg_count(&self) -> &AtomicUsize {
        unsafe { atomic_usize(self.base(), OFF_MSG_COUNT) }
    }

    fn used_bytes(&self) -> &AtomicUsize {
        unsafe { atomic_usize(self.base(), OFF_USED_BYTES) }
    }

    fn closed_flag(&self) -> &AtomicBool {
        unsafe { atomic_bool(self.base(), OFF_CLOSED) }
    }

    /// Notify the reader that new data is available.
    fn notify_data(&self) {
        if self.notify_data_wr >= 0 {
            unsafe {
                libc::write(
                    self.notify_data_wr,
                    [1u8].as_ptr() as *const libc::c_void,
                    1,
                );
            }
        }
    }

    /// Notify the writer that space has been freed.
    fn notify_space(&self) {
        if self.notify_space_wr >= 0 {
            unsafe {
                libc::write(
                    self.notify_space_wr,
                    [1u8].as_ptr() as *const libc::c_void,
                    1,
                );
            }
        }
    }

    /// Push raw bytes — same algorithm as MemoryPipeBackend::push.
    fn push_inner(&self, data: &[u8]) -> Result<usize, PipeError> {
        if self.closed_flag().load(Ordering::Acquire) {
            return Err(PipeError::Closed("write to closed pipe"));
        }
        let payload_len = data.len();
        if payload_len == 0 {
            return Ok(0);
        }
        if payload_len > self.user_capacity {
            return Err(PipeError::Oversized(payload_len, self.user_capacity));
        }

        let used = self.used_bytes().load(Ordering::Relaxed);
        if used + payload_len > self.user_capacity {
            return Err(PipeError::Full(used, self.user_capacity));
        }

        let frame_len = HEADER_SIZE + payload_len;
        let tail_val = self.tail().load(Ordering::Relaxed);
        let head_val = self.head().load(Ordering::Acquire);
        let tail_idx = tail_val % self.ring_cap;
        let contiguous = self.ring_cap - tail_idx;

        // Physical ring-space check (includes header + potential sentinel).
        // `used_bytes` is payload-only, which can under-count true ring fill
        // for tiny payloads where header overhead (HEADER_SIZE=4) plus wrap
        // padding is larger than the slack `ring_cap - user_capacity`.
        let need = if frame_len > contiguous {
            contiguous + frame_len
        } else {
            frame_len
        };
        if tail_val.saturating_sub(head_val) + need > self.ring_cap {
            return Err(PipeError::Full(used, self.user_capacity));
        }

        let ring = self.ring_slice();

        // Sentinel + wrap if frame doesn't fit contiguously
        let write_idx = if frame_len > contiguous {
            let sentinel = 0u32.to_le_bytes();
            ring[tail_idx..tail_idx + HEADER_SIZE].copy_from_slice(&sentinel);
            let new_tail = tail_val + contiguous;
            self.tail().store(new_tail, Ordering::Release);
            0
        } else {
            tail_idx
        };

        // Write frame: [4B len][payload]
        let header = (payload_len as u32).to_le_bytes();
        ring[write_idx..write_idx + HEADER_SIZE].copy_from_slice(&header);
        ring[write_idx + HEADER_SIZE..write_idx + HEADER_SIZE + payload_len].copy_from_slice(data);

        // Update tail
        let current_tail = self.tail().load(Ordering::Relaxed);
        self.tail()
            .store(current_tail + frame_len, Ordering::Release);

        // Update counters
        self.msg_count().fetch_add(1, Ordering::Relaxed);
        self.used_bytes().fetch_add(payload_len, Ordering::Relaxed);
        self.push_count().fetch_add(1, Ordering::Relaxed);

        Ok(payload_len)
    }

    /// Find the next message position — same algorithm as MemoryPipeBackend::pop_position.
    fn pop_position(&self) -> Result<(usize, usize, usize), PipeError> {
        let mut head_val = self.head().load(Ordering::Acquire);
        let tail_val = self.tail().load(Ordering::Acquire);

        loop {
            if head_val == tail_val {
                return if self.closed_flag().load(Ordering::Acquire) {
                    Err(PipeError::ClosedEmpty)
                } else {
                    Err(PipeError::Empty)
                };
            }

            let head_idx = head_val % self.ring_cap;
            let ring = self.ring_slice();

            let mut hdr = [0u8; HEADER_SIZE];
            hdr.copy_from_slice(&ring[head_idx..head_idx + HEADER_SIZE]);
            let payload_len = u32::from_le_bytes(hdr) as usize;

            if payload_len == 0 {
                // Sentinel — skip waste to ring start
                let waste = self.ring_cap - head_idx;
                head_val += waste;
                self.head().store(head_val, Ordering::Release);
                continue;
            }

            let payload_start = head_idx + HEADER_SIZE;
            let total_advance = HEADER_SIZE + payload_len;
            return Ok((payload_start, payload_len, total_advance));
        }
    }

    /// Advance head after data has been copied out.
    fn commit_pop(&self, total_advance: usize, payload_len: usize) {
        let head_val = self.head().load(Ordering::Relaxed);
        self.head()
            .store(head_val + total_advance, Ordering::Release);
        self.msg_count().fetch_sub(1, Ordering::Relaxed);
        self.used_bytes().fetch_sub(payload_len, Ordering::Relaxed);
        self.pop_count().fetch_add(1, Ordering::Relaxed);
    }
}

// ---------------------------------------------------------------------------
// PipeBackend trait impl
// ---------------------------------------------------------------------------

impl crate::pipe::PipeBackend for SharedMemoryPipeBackend {
    fn push(&self, data: &[u8]) -> Result<usize, PipeError> {
        let n = self.push_inner(data)?;
        self.notify_data();
        Ok(n)
    }
    fn pop(&self) -> Result<Vec<u8>, PipeError> {
        let (start, len, advance) = self.pop_position()?;
        let ring = self.ring_slice();
        let data = ring[start..start + len].to_vec();
        self.commit_pop(advance, len);
        self.notify_space();
        Ok(data)
    }
    fn close(&self) {
        self.closed_flag().store(true, Ordering::Release);
        self.notify_data();
        self.notify_space();
    }
    fn is_closed(&self) -> bool {
        self.closed_flag().load(Ordering::Acquire)
    }
    fn is_empty(&self) -> bool {
        self.msg_count().load(Ordering::Relaxed) == 0
    }
    fn size(&self) -> usize {
        self.used_bytes().load(Ordering::Relaxed)
    }
    fn msg_count(&self) -> usize {
        self.msg_count().load(Ordering::Relaxed)
    }
}

// ---------------------------------------------------------------------------
// PyO3 methods
// ---------------------------------------------------------------------------

#[pymethods]
impl SharedMemoryPipeBackend {
    /// Pure Rust constructor — no PyO3 dependency.
    ///
    /// Returns `(self, shm_path, data_rd_fd, space_rd_fd)`.
    /// Used by `Kernel::setattr_pipe(io_profile="shared_memory")`.
    pub(crate) fn create_native(
        capacity: usize,
    ) -> Result<(Self, String, i32, i32), std::io::Error> {
        if capacity == 0 {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "capacity must be > 0",
            ));
        }

        let ring_cap = capacity * 2;
        let total_size = DATA_OFFSET + ring_cap;

        let tmp = tempfile::NamedTempFile::new()?;
        let (file, path) = tmp
            .keep()
            .map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, format!("{e}")))?;
        let shm_path = path.to_string_lossy().to_string();

        file.set_len(total_size as u64)?;

        let mut mmap = unsafe { memmap2::MmapOptions::new().len(total_size).map_mut(&file) }?;

        let base = mmap.as_mut_ptr();
        write_u32(base, OFF_MAGIC, MAGIC);
        write_u32(base, OFF_VERSION, VERSION);
        write_u32(base, OFF_RING_CAP, ring_cap as u32);
        write_u32(base, OFF_USER_CAP, capacity as u32);

        unsafe {
            atomic_usize(base, OFF_TAIL).store(0, Ordering::Relaxed);
            atomic_usize(base, OFF_HEAD).store(0, Ordering::Relaxed);
            atomic_u64(base, OFF_PUSH_COUNT).store(0, Ordering::Relaxed);
            atomic_u64(base, OFF_POP_COUNT).store(0, Ordering::Relaxed);
            atomic_usize(base, OFF_MSG_COUNT).store(0, Ordering::Relaxed);
            atomic_usize(base, OFF_USED_BYTES).store(0, Ordering::Relaxed);
            atomic_bool(base, OFF_CLOSED).store(false, Ordering::Relaxed);
        }

        let mut data_fds = [0i32; 2];
        let mut space_fds = [0i32; 2];
        unsafe {
            if libc::pipe(data_fds.as_mut_ptr()) != 0 {
                return Err(std::io::Error::last_os_error());
            }
            if libc::pipe(space_fds.as_mut_ptr()) != 0 {
                libc::close(data_fds[0]);
                libc::close(data_fds[1]);
                return Err(std::io::Error::last_os_error());
            }
            libc::fcntl(data_fds[0], libc::F_SETFL, libc::O_NONBLOCK);
            libc::fcntl(space_fds[0], libc::F_SETFL, libc::O_NONBLOCK);
            libc::fcntl(data_fds[1], libc::F_SETFL, libc::O_NONBLOCK);
            libc::fcntl(space_fds[1], libc::F_SETFL, libc::O_NONBLOCK);
        }

        // The creator is the writer only: it writes into `notify_data_wr`
        // to wake the reader and listens on `space_rd_fd` for reader-side
        // space notifications. It does NOT need `space_fds[1]` at all —
        // retaining that fd kept the space-pipe alive even after the
        // reader died, so `read(space_rd_fd)` would never see EOF and
        // graceful shutdown was impossible (§ review fix #13). Close it.
        unsafe {
            libc::close(space_fds[1]);
        }

        let core = SharedMemoryPipeBackend {
            mmap,
            ring_cap,
            user_capacity: capacity,
            notify_data_wr: data_fds[1],
            notify_space_wr: space_fds[1],
            is_creator: true,
            shm_path: shm_path.clone(),
        };

        Ok((core, shm_path, data_fds[0], space_fds[0]))
    }

    /// Create a new shared ring buffer (PyO3 wrapper over `create_native`).
    ///
    /// Returns `(self, shm_path, data_rd_fd, space_rd_fd)`.
    #[staticmethod]
    fn create(capacity: usize) -> PyResult<(Self, String, i32, i32)> {
        Self::create_native(capacity)
            .map_err(|e| pyo3::exceptions::PyOSError::new_err(e.to_string()))
    }

    /// Attach to an existing shared ring buffer from another process.
    ///
    /// Args:
    ///     shm_path: Path to the shared memory file.
    ///     data_wr_fd: Write-end of the data notification pipe (reader notifies writer — unused here,
    ///                 actually this is the data_rd_fd the reader uses to receive data notifications).
    ///     space_wr_fd: Write-end of the space notification pipe (reader writes here after pop).
    #[staticmethod]
    fn attach(shm_path: &str, notify_data_wr: i32, notify_space_wr: i32) -> PyResult<Self> {
        let file = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open(shm_path)
            .map_err(|e| pyo3::exceptions::PyOSError::new_err(format!("open: {e}")))?;

        let mmap = unsafe { memmap2::MmapOptions::new().map_mut(&file) }
            .map_err(|e| pyo3::exceptions::PyOSError::new_err(format!("mmap: {e}")))?;

        // Validate magic + version
        let base = mmap.as_ptr();
        let magic = read_u32(base, OFF_MAGIC);
        if magic != MAGIC {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "bad magic: expected 0x{MAGIC:08X}, got 0x{magic:08X}"
            )));
        }
        let version = read_u32(base, OFF_VERSION);
        if version != VERSION {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "unsupported version: expected {VERSION}, got {version}"
            )));
        }

        let ring_cap = read_u32(base, OFF_RING_CAP) as usize;
        let user_capacity = read_u32(base, OFF_USER_CAP) as usize;

        Ok(SharedMemoryPipeBackend {
            mmap,
            ring_cap,
            user_capacity,
            notify_data_wr,
            notify_space_wr,
            is_creator: false,
            shm_path: shm_path.to_string(),
        })
    }

    /// Push a message into the buffer. Returns bytes written.
    /// Sends a 1-byte notification on the data pipe after successful push.
    fn push(&self, _py: Python<'_>, data: &[u8]) -> PyResult<usize> {
        match self.push_inner(data) {
            Ok(n) => {
                self.notify_data();
                Ok(n)
            }
            Err(PipeError::Closed(msg)) => Err(pyo3::exceptions::PyRuntimeError::new_err(format!(
                "PipeClosed:{msg}"
            ))),
            Err(PipeError::Full(used, cap)) => Err(pyo3::exceptions::PyRuntimeError::new_err(
                format!("PipeFull:buffer full ({used}/{cap} bytes)"),
            )),
            Err(PipeError::Oversized(msg_len, cap)) => {
                Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "message size {msg_len} exceeds buffer capacity {cap}"
                )))
            }
            Err(PipeError::Empty | PipeError::ClosedEmpty) => unreachable!(),
        }
    }

    /// Pop the next message. Returns PyBytes.
    /// Sends a 1-byte notification on the space pipe after successful pop.
    fn pop<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyBytes>> {
        match self.pop_position() {
            Ok((start, len, advance)) => {
                let ring = self.ring_slice();
                let result = PyBytes::new(py, &ring[start..start + len]);
                self.commit_pop(advance, len);
                self.notify_space();
                Ok(result)
            }
            Err(PipeError::ClosedEmpty) => Err(pyo3::exceptions::PyRuntimeError::new_err(
                "PipeClosed:read from closed empty pipe",
            )),
            Err(PipeError::Empty) => Err(pyo3::exceptions::PyRuntimeError::new_err(
                "PipeEmpty:buffer empty",
            )),
            Err(PipeError::Closed(_) | PipeError::Full(_, _) | PipeError::Oversized(_, _)) => {
                unreachable!()
            }
        }
    }

    /// Push a u64 value (12-byte frame).
    fn push_u64(&self, _py: Python<'_>, val: u64) -> PyResult<()> {
        match self.push_inner(&val.to_le_bytes()) {
            Ok(_) => {
                self.notify_data();
                Ok(())
            }
            Err(PipeError::Closed(msg)) => Err(pyo3::exceptions::PyRuntimeError::new_err(format!(
                "PipeClosed:{msg}"
            ))),
            Err(PipeError::Full(used, cap)) => Err(pyo3::exceptions::PyRuntimeError::new_err(
                format!("PipeFull:buffer full ({used}/{cap} bytes)"),
            )),
            Err(PipeError::Oversized(msg_len, cap)) => {
                Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "message size {msg_len} exceeds buffer capacity {cap}"
                )))
            }
            Err(PipeError::Empty | PipeError::ClosedEmpty) => unreachable!(),
        }
    }

    /// Pop a u64 value.
    fn pop_u64(&self, _py: Python<'_>) -> PyResult<u64> {
        match self.pop_position() {
            Ok((start, len, advance)) => {
                if len != 8 {
                    return Err(pyo3::exceptions::PyValueError::new_err(format!(
                        "pop_u64 expects 8-byte payload, got {len}"
                    )));
                }
                let ring = self.ring_slice();
                let mut buf = [0u8; 8];
                buf.copy_from_slice(&ring[start..start + 8]);
                let val = u64::from_le_bytes(buf);
                self.commit_pop(advance, len);
                self.notify_space();
                Ok(val)
            }
            Err(PipeError::ClosedEmpty) => Err(pyo3::exceptions::PyRuntimeError::new_err(
                "PipeClosed:read from closed empty pipe",
            )),
            Err(PipeError::Empty) => Err(pyo3::exceptions::PyRuntimeError::new_err(
                "PipeEmpty:buffer empty",
            )),
            Err(PipeError::Closed(_) | PipeError::Full(_, _) | PipeError::Oversized(_, _)) => {
                unreachable!()
            }
        }
    }

    /// Close the buffer. Sends notification on both pipes.
    fn close(&self) {
        self.closed_flag().store(true, Ordering::Release);
        self.notify_data();
        self.notify_space();
    }

    fn is_empty(&self) -> bool {
        self.msg_count().load(Ordering::Relaxed) == 0
    }

    fn is_full(&self) -> bool {
        self.used_bytes().load(Ordering::Relaxed) >= self.user_capacity
    }

    #[getter]
    fn closed(&self) -> bool {
        self.closed_flag().load(Ordering::Acquire)
    }

    #[getter]
    fn size(&self) -> usize {
        self.used_bytes().load(Ordering::Relaxed)
    }

    #[getter]
    fn capacity(&self) -> usize {
        self.user_capacity
    }

    #[getter]
    fn msg_count_prop(&self) -> usize {
        self.msg_count().load(Ordering::Relaxed)
    }

    /// Buffer statistics as a dict.
    fn stats(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let dict = PyDict::new(py);
        dict.set_item("size", self.used_bytes().load(Ordering::Relaxed))?;
        dict.set_item("capacity", self.user_capacity)?;
        dict.set_item("msg_count", self.msg_count().load(Ordering::Relaxed))?;
        dict.set_item("closed", self.closed_flag().load(Ordering::Acquire))?;
        dict.set_item("push_count", self.push_count().load(Ordering::Relaxed))?;
        dict.set_item("pop_count", self.pop_count().load(Ordering::Relaxed))?;
        Ok(dict.into())
    }

    /// Remove the shared memory file. Only the creator should call this.
    fn cleanup(&self) -> PyResult<()> {
        if self.is_creator {
            std::fs::remove_file(&self.shm_path)
                .map_err(|e| pyo3::exceptions::PyOSError::new_err(format!("cleanup: {e}")))?;
        }
        Ok(())
    }

    /// Return the shared memory file path.
    #[getter]
    fn shm_path(&self) -> &str {
        &self.shm_path
    }
}

impl Drop for SharedMemoryPipeBackend {
    fn drop(&mut self) {
        // Close notification pipe write-ends
        if self.notify_data_wr >= 0 {
            unsafe {
                libc::close(self.notify_data_wr);
            }
        }
        if self.notify_space_wr >= 0 {
            unsafe {
                libc::close(self.notify_space_wr);
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    /// Same-process create + attach test (valid because MAP_SHARED).
    fn create_pair(cap: usize) -> (SharedMemoryPipeBackend, SharedMemoryPipeBackend) {
        let (creator, shm_path, data_rd_fd, space_rd_fd) =
            SharedMemoryPipeBackend::create(cap).unwrap();
        // For same-process testing: attacher gets the write-ends for notifications
        // In real cross-process: fds would be inherited via subprocess
        // Here we pass notify_data_wr=-1, notify_space_wr=-1 since we don't need
        // cross-process notification in same-process tests.
        let attacher = SharedMemoryPipeBackend::attach(
            &shm_path, -1, // attacher doesn't need to notify data (it's the reader)
            -1, // attacher doesn't need to notify space in these tests
        )
        .unwrap();
        // Close unused read fds
        unsafe {
            libc::close(data_rd_fd);
            libc::close(space_rd_fd);
        }
        (creator, attacher)
    }

    /// Helper: push raw bytes via push_inner (bypass PyO3).
    fn push(core: &SharedMemoryPipeBackend, data: &[u8]) -> usize {
        core.push_inner(data).expect("push failed")
    }

    /// Helper: pop raw bytes via pop_position + commit_pop (bypass PyO3).
    fn pop(core: &SharedMemoryPipeBackend) -> Vec<u8> {
        let (start, len, advance) = core.pop_position().expect("pop failed");
        let ring = core.ring_slice();
        let data = ring[start..start + len].to_vec();
        core.commit_pop(advance, len);
        data
    }

    #[test]
    fn test_create_returns_valid_handles() {
        let (creator, shm_path, data_rd_fd, space_rd_fd) =
            SharedMemoryPipeBackend::create(1024).unwrap();
        assert!(!shm_path.is_empty());
        assert!(data_rd_fd >= 0);
        assert!(space_rd_fd >= 0);
        assert!(creator.is_creator);
        unsafe {
            libc::close(data_rd_fd);
            libc::close(space_rd_fd);
        }
        creator.cleanup().unwrap();
    }

    #[test]
    fn test_magic_version_validated() {
        let (creator, shm_path, dfd, sfd) = SharedMemoryPipeBackend::create(64).unwrap();
        let attacher = SharedMemoryPipeBackend::attach(&shm_path, -1, -1);
        assert!(attacher.is_ok());
        unsafe {
            libc::close(dfd);
            libc::close(sfd);
        }
        creator.cleanup().unwrap();
    }

    #[test]
    fn test_write_read_roundtrip() {
        let (creator, attacher) = create_pair(1024);
        push(&creator, b"hello");
        let out = pop(&attacher);
        assert_eq!(out, b"hello");
        creator.cleanup().unwrap();
    }

    #[test]
    fn test_fifo_ordering() {
        let (creator, attacher) = create_pair(1024);
        push(&creator, b"first");
        push(&creator, b"second");
        assert_eq!(pop(&attacher), b"first");
        assert_eq!(pop(&attacher), b"second");
        creator.cleanup().unwrap();
    }

    #[test]
    fn test_close_propagates() {
        let (creator, attacher) = create_pair(1024);
        assert!(!attacher.closed_flag().load(Ordering::Acquire));
        creator.closed_flag().store(true, Ordering::Release);
        assert!(attacher.closed_flag().load(Ordering::Acquire));
        creator.cleanup().unwrap();
    }

    #[test]
    fn test_wrap_around() {
        let (creator, attacher) = create_pair(64);
        for i in 0u8..20 {
            push(&creator, &[i; 50]);
            let out = pop(&attacher);
            assert_eq!(out, vec![i; 50]);
        }
        creator.cleanup().unwrap();
    }

    #[test]
    fn test_size_tracking() {
        let (creator, attacher) = create_pair(100);
        push(&creator, b"abcde");
        assert_eq!(creator.used_bytes().load(Ordering::Relaxed), 5);
        pop(&attacher);
        assert_eq!(attacher.used_bytes().load(Ordering::Relaxed), 0);
        creator.cleanup().unwrap();
    }

    #[test]
    fn test_cleanup_removes_file() {
        let (creator, _attacher) = create_pair(64);
        let path = creator.shm_path.clone();
        assert!(std::path::Path::new(&path).exists());
        creator.cleanup().unwrap();
        assert!(!std::path::Path::new(&path).exists());
    }

    #[test]
    fn test_many_tiny_pushes_cannot_overwrite_unread() {
        // Regression for review fix #1: tiny payloads + frame headers must
        // respect ring_cap, not only user_capacity.
        let (creator, attacher) = create_pair(32);
        let mut accepted = 0usize;
        for _ in 0..64 {
            match creator.push_inner(&[0xBBu8]) {
                Ok(_) => accepted += 1,
                Err(PipeError::Full(_, _)) => break,
                Err(e) => panic!("unexpected push error: {e:?}"),
            }
        }
        // floor(ring_cap / frame_len) = 64 / 5 = 12
        assert!(accepted <= 12, "overflow: accepted {accepted} pushes");
        for _ in 0..accepted {
            assert_eq!(pop(&attacher), vec![0xBBu8]);
        }
        creator.cleanup().unwrap();
    }

    #[test]
    fn test_u64_roundtrip() {
        let (creator, attacher) = create_pair(1024);
        creator.push_inner(&42u64.to_le_bytes()).unwrap();
        let (start, len, advance) = attacher.pop_position().unwrap();
        assert_eq!(len, 8);
        let ring = attacher.ring_slice();
        let mut buf = [0u8; 8];
        buf.copy_from_slice(&ring[start..start + 8]);
        assert_eq!(u64::from_le_bytes(buf), 42);
        attacher.commit_pop(advance, len);
        creator.cleanup().unwrap();
    }
}
