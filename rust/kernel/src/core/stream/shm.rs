//! Cross-process append-only StreamBuffer via mmap + OS pipe notification (#1680).
//!
//! Same algorithms as `stream.rs` (StreamBufferCore) — push_inner/read_at_inner
//! — but buffer lives in a MAP_SHARED mmap region.  Single OS pipe provides
//! writer→reader(s) notification.
//!
//! Layout (384 bytes header + linear data):
//!
//! ```text
//! [0..128)    Slot 0 — immutable config
//!             magic: u32 = 0x4E585354 ("NXST")
//!             version: u32 = 1
//!             capacity: u32
//!
//! [128..256)  Slot 1 — writer-hot
//!             tail: AtomicUsize
//!             push_count: AtomicU64
//!             msg_count: AtomicUsize
//!
//! [256..384)  Slot 2 — shared flags
//!             closed: AtomicBool (stored as u8)
//!
//! [384..)     Linear data region (capacity bytes)
//! ```
//!
//! No `head` field — DT_STREAM readers maintain independent cursors externally.

use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const HEADER_SIZE: usize = 4; // 4-byte u32 LE length prefix
const MAGIC: u32 = 0x4E58_5354; // "NXST"
const VERSION: u32 = 1;
const SLOT_SIZE: usize = 128;
const DATA_OFFSET: usize = SLOT_SIZE * 3; // 384 bytes header

// Offsets within the mmap header
// Slot 0: immutable config
const OFF_MAGIC: usize = 0;
const OFF_VERSION: usize = 4;
const OFF_CAPACITY: usize = 8;
// Slot 1: writer-hot
const OFF_TAIL: usize = SLOT_SIZE;
const OFF_PUSH_COUNT: usize = SLOT_SIZE + 8;
const OFF_MSG_COUNT: usize = SLOT_SIZE + 16;
// Slot 2: flags
const OFF_CLOSED: usize = SLOT_SIZE * 2;

// Use shared StreamError from stream.rs
use crate::stream::StreamError;

// ---------------------------------------------------------------------------
// Header accessors (same pattern as shm_pipe.rs)
// ---------------------------------------------------------------------------

#[cfg(test)]
#[inline]
fn read_u32(base: *const u8, off: usize) -> u32 {
    unsafe { (base.add(off) as *const u32).read() }
}

#[inline]
fn write_u32(base: *mut u8, off: usize, val: u32) {
    unsafe { (base.add(off) as *mut u32).write(val) }
}

#[inline]
unsafe fn atomic_usize(base: *const u8, off: usize) -> &'static AtomicUsize {
    &*(base.add(off) as *const AtomicUsize)
}

#[inline]
unsafe fn atomic_u64(base: *const u8, off: usize) -> &'static AtomicU64 {
    &*(base.add(off) as *const AtomicU64)
}

#[inline]
unsafe fn atomic_bool(base: *const u8, off: usize) -> &'static AtomicBool {
    &*(base.add(off) as *const AtomicBool)
}

// ---------------------------------------------------------------------------
// SharedMemoryStreamBackend
// ---------------------------------------------------------------------------

/// Cross-process append-only buffer backed by mmap + OS pipe notification.
///
/// Kernel-internal primitive: constructed by the kernel inside
/// `sys_setattr` when `io_profile=shared_memory` is requested for a
/// DT_STREAM inode.  Python callers reach it via `sys_read` /
/// `sys_write`; there is no PyO3 surface on the type itself.
pub struct SharedMemoryStreamBackend {
    mmap: memmap2::MmapMut,
    capacity: usize,
    notify_data_wr: i32, // writer writes here after push
    // `is_creator` + `shm_path` are read only by the test-only `cleanup()`
    // helper; production kernel cleans up via tempfile drop semantics.
    // Allowed dead so `#[deny(warnings)]` in release builds doesn't trip.
    #[allow(dead_code)]
    is_creator: bool,
    #[allow(dead_code)]
    shm_path: String,
}

unsafe impl Send for SharedMemoryStreamBackend {}
unsafe impl Sync for SharedMemoryStreamBackend {}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

impl SharedMemoryStreamBackend {
    #[inline]
    fn base(&self) -> *const u8 {
        self.mmap.as_ptr()
    }

    #[inline]
    fn base_mut(&self) -> *mut u8 {
        self.mmap.as_ptr() as *mut u8
    }

    #[inline]
    fn data_ptr(&self) -> *mut u8 {
        unsafe { self.base_mut().add(DATA_OFFSET) }
    }

    /// SAFETY: Single-writer design — only the creator pushes, readers
    /// access committed (immutable) regions behind the atomic tail.
    #[allow(clippy::mut_from_ref)]
    #[inline]
    fn data_slice(&self) -> &mut [u8] {
        unsafe { std::slice::from_raw_parts_mut(self.data_ptr(), self.capacity) }
    }

    fn tail_atomic(&self) -> &AtomicUsize {
        unsafe { atomic_usize(self.base(), OFF_TAIL) }
    }

    fn push_count(&self) -> &AtomicU64 {
        unsafe { atomic_u64(self.base(), OFF_PUSH_COUNT) }
    }

    fn msg_count(&self) -> &AtomicUsize {
        unsafe { atomic_usize(self.base(), OFF_MSG_COUNT) }
    }

    fn closed_flag(&self) -> &AtomicBool {
        unsafe { atomic_bool(self.base(), OFF_CLOSED) }
    }

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

    /// Push raw bytes — same algorithm as StreamBufferCore::push_inner.
    fn push_inner(&self, data: &[u8]) -> Result<usize, StreamError> {
        if self.closed_flag().load(Ordering::Acquire) {
            return Err(StreamError::Closed("write to closed stream"));
        }
        let payload_len = data.len();
        if payload_len == 0 {
            return Ok(self.tail_atomic().load(Ordering::Relaxed));
        }
        if payload_len > self.capacity {
            return Err(StreamError::Oversized(payload_len, self.capacity));
        }

        let frame_len = HEADER_SIZE + payload_len;
        let tail = self.tail_atomic().load(Ordering::Relaxed);

        if tail + frame_len > self.capacity {
            return Err(StreamError::Full(tail, self.capacity));
        }

        let buf = self.data_slice();

        // Write frame: [4B len][payload]
        let header = (payload_len as u32).to_le_bytes();
        buf[tail..tail + HEADER_SIZE].copy_from_slice(&header);
        buf[tail + HEADER_SIZE..tail + HEADER_SIZE + payload_len].copy_from_slice(data);

        let msg_offset = tail;

        // Update tail
        self.tail_atomic()
            .store(tail + frame_len, Ordering::Release);

        // Update counters
        self.msg_count().fetch_add(1, Ordering::Relaxed);
        self.push_count().fetch_add(1, Ordering::Relaxed);

        Ok(msg_offset)
    }

    /// Read one message at byte offset — same as StreamBufferCore::read_at_inner.
    fn read_at_inner(&self, byte_offset: usize) -> Result<(usize, usize, usize), StreamError> {
        let tail = self.tail_atomic().load(Ordering::Acquire);

        if byte_offset >= tail {
            return if self.closed_flag().load(Ordering::Acquire) {
                Err(StreamError::ClosedEmpty)
            } else {
                Err(StreamError::Empty)
            };
        }

        if byte_offset + HEADER_SIZE > tail {
            return Err(StreamError::InvalidOffset(byte_offset, tail));
        }

        let buf = self.data_slice();

        let mut hdr = [0u8; HEADER_SIZE];
        hdr.copy_from_slice(&buf[byte_offset..byte_offset + HEADER_SIZE]);
        let payload_len = u32::from_le_bytes(hdr) as usize;

        let payload_start = byte_offset + HEADER_SIZE;
        let next_offset = payload_start + payload_len;

        if next_offset > tail {
            return Err(StreamError::InvalidOffset(byte_offset, tail));
        }

        Ok((payload_start, payload_len, next_offset))
    }
}

// ---------------------------------------------------------------------------
// StreamBackend trait impl
// ---------------------------------------------------------------------------

impl crate::stream::StreamBackend for SharedMemoryStreamBackend {
    fn push(&self, data: &[u8]) -> Result<usize, StreamError> {
        let offset = self.push_inner(data)?;
        self.notify_data();
        Ok(offset)
    }
    fn read_at(&self, offset: usize) -> Result<(Vec<u8>, usize), StreamError> {
        let (start, len, next) = self.read_at_inner(offset)?;
        let buf = self.data_slice();
        let data = buf[start..start + len].to_vec();
        Ok((data, next))
    }
    fn read_batch(
        &self,
        offset: usize,
        count: usize,
    ) -> Result<(Vec<Vec<u8>>, usize), StreamError> {
        let mut results = Vec::with_capacity(count);
        let mut pos = offset;
        for _ in 0..count {
            match self.read_at_inner(pos) {
                Ok((start, len, next)) => {
                    let buf = self.data_slice();
                    results.push(buf[start..start + len].to_vec());
                    pos = next;
                }
                Err(StreamError::Empty) | Err(StreamError::ClosedEmpty) => break,
                Err(e) => return Err(e),
            }
        }
        Ok((results, pos))
    }
    fn close(&self) {
        self.closed_flag().store(true, Ordering::Release);
        self.notify_data();
    }
    fn is_closed(&self) -> bool {
        self.closed_flag().load(Ordering::Acquire)
    }
    fn tail_offset(&self) -> usize {
        self.tail_atomic().load(Ordering::Acquire)
    }
    fn msg_count(&self) -> usize {
        self.msg_count().load(Ordering::Relaxed)
    }
}

// ---------------------------------------------------------------------------
// Pure Rust constructors (no PyO3 dependency)
// ---------------------------------------------------------------------------

impl SharedMemoryStreamBackend {
    /// Pure Rust constructor — called by Kernel::setattr_stream.
    ///
    /// Returns `(self, shm_path, data_rd_fd)`.
    pub(crate) fn create_native(capacity: usize) -> Result<(Self, String, i32), std::io::Error> {
        if capacity == 0 {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "capacity must be > 0",
            ));
        }

        let total_size = DATA_OFFSET + capacity;

        let tmp = tempfile::NamedTempFile::new()?;
        let (file, path) = tmp
            .keep()
            .map_err(|e| std::io::Error::other(format!("{e}")))?;
        let shm_path = path.to_string_lossy().to_string();

        file.set_len(total_size as u64)?;

        let mut mmap = unsafe { memmap2::MmapOptions::new().len(total_size).map_mut(&file) }?;

        let base = mmap.as_mut_ptr();
        write_u32(base, OFF_MAGIC, MAGIC);
        write_u32(base, OFF_VERSION, VERSION);
        write_u32(base, OFF_CAPACITY, capacity as u32);

        unsafe {
            atomic_usize(base, OFF_TAIL).store(0, Ordering::Relaxed);
            atomic_u64(base, OFF_PUSH_COUNT).store(0, Ordering::Relaxed);
            atomic_usize(base, OFF_MSG_COUNT).store(0, Ordering::Relaxed);
            atomic_bool(base, OFF_CLOSED).store(false, Ordering::Relaxed);
        }

        let mut data_fds = [0i32; 2];
        unsafe {
            if libc::pipe(data_fds.as_mut_ptr()) != 0 {
                return Err(std::io::Error::last_os_error());
            }
            libc::fcntl(data_fds[0], libc::F_SETFL, libc::O_NONBLOCK);
            libc::fcntl(data_fds[1], libc::F_SETFL, libc::O_NONBLOCK);
        }

        let core = SharedMemoryStreamBackend {
            mmap,
            capacity,
            notify_data_wr: data_fds[1],
            is_creator: true,
            shm_path: shm_path.clone(),
        };

        Ok((core, shm_path, data_fds[0]))
    }

    /// Attach to an existing shared stream buffer (same-process tests).
    /// Pure Rust — no PyO3.
    #[cfg(test)]
    fn attach(shm_path: &str, notify_data_wr: i32) -> Result<Self, std::io::Error> {
        let file = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open(shm_path)?;

        let mmap = unsafe { memmap2::MmapOptions::new().map_mut(&file) }?;

        let base = mmap.as_ptr();
        let magic = read_u32(base, OFF_MAGIC);
        if magic != MAGIC {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                format!("bad magic: expected 0x{MAGIC:08X}, got 0x{magic:08X}"),
            ));
        }
        let version = read_u32(base, OFF_VERSION);
        if version != VERSION {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                format!("unsupported version: expected {VERSION}, got {version}"),
            ));
        }

        let capacity = read_u32(base, OFF_CAPACITY) as usize;

        Ok(SharedMemoryStreamBackend {
            mmap,
            capacity,
            notify_data_wr,
            is_creator: false,
            shm_path: shm_path.to_string(),
        })
    }

    /// Remove the shared memory file (creator only).
    #[cfg(test)]
    fn cleanup(&self) -> std::io::Result<()> {
        if self.is_creator {
            std::fs::remove_file(&self.shm_path)?;
        }
        Ok(())
    }
}

impl Drop for SharedMemoryStreamBackend {
    fn drop(&mut self) {
        if self.notify_data_wr >= 0 {
            unsafe {
                libc::close(self.notify_data_wr);
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

    fn create_pair(cap: usize) -> (SharedMemoryStreamBackend, SharedMemoryStreamBackend) {
        let (creator, shm_path, data_rd_fd) =
            SharedMemoryStreamBackend::create_native(cap).unwrap();
        let attacher = SharedMemoryStreamBackend::attach(&shm_path, -1).unwrap();
        unsafe {
            libc::close(data_rd_fd);
        }
        (creator, attacher)
    }

    fn push(core: &SharedMemoryStreamBackend, data: &[u8]) -> usize {
        core.push_inner(data).expect("push failed")
    }

    fn read_at(core: &SharedMemoryStreamBackend, offset: usize) -> (Vec<u8>, usize) {
        let (start, len, next) = core.read_at_inner(offset).expect("read_at failed");
        let buf = core.data_slice();
        (buf[start..start + len].to_vec(), next)
    }

    #[test]
    fn test_create_returns_handles() {
        let (creator, shm_path, data_rd_fd) =
            SharedMemoryStreamBackend::create_native(1024).unwrap();
        assert!(!shm_path.is_empty());
        assert!(data_rd_fd >= 0);
        unsafe {
            libc::close(data_rd_fd);
        }
        creator.cleanup().unwrap();
    }

    #[test]
    fn test_write_read_at_roundtrip() {
        let (creator, attacher) = create_pair(1024);
        let offset = push(&creator, b"hello");
        assert_eq!(offset, 0);
        let (data, next) = read_at(&attacher, offset);
        assert_eq!(data, b"hello");
        assert_eq!(next, HEADER_SIZE + 5);
        creator.cleanup().unwrap();
    }

    #[test]
    fn test_multi_reader_independent_cursors() {
        let (creator, attacher) = create_pair(1024);
        push(&creator, b"msg1");
        push(&creator, b"msg2");

        // Reader A
        let (d1, n1) = read_at(&attacher, 0);
        let (d2, _) = read_at(&attacher, n1);
        assert_eq!(d1, b"msg1");
        assert_eq!(d2, b"msg2");

        // Reader B (re-read from 0 — non-destructive)
        let (d1b, _) = read_at(&attacher, 0);
        assert_eq!(d1b, b"msg1");

        creator.cleanup().unwrap();
    }

    #[test]
    fn test_tail_monotonic() {
        let (creator, _attacher) = create_pair(1024);
        let t0 = creator.tail_atomic().load(Ordering::Relaxed);
        push(&creator, b"a");
        let t1 = creator.tail_atomic().load(Ordering::Relaxed);
        push(&creator, b"b");
        let t2 = creator.tail_atomic().load(Ordering::Relaxed);
        assert!(t0 < t1);
        assert!(t1 < t2);
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
    fn test_cleanup_removes_file() {
        let (creator, _attacher) = create_pair(64);
        let path = creator.shm_path.clone();
        assert!(std::path::Path::new(&path).exists());
        creator.cleanup().unwrap();
        assert!(!std::path::Path::new(&path).exists());
    }
}
