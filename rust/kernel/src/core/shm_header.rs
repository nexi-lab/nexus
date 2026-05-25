//! Safe wrappers over raw mmap pointers for SPSC SHM buffer headers.
//!
//! Shared between `core::pipe::shm::SharedMemoryPipeBackend` and
//! `core::stream::shm::SharedMemoryStreamBackend` — both lay out an
//! SPSC ring/linear buffer over an mmap region and need to read/write
//! u32 config fields plus atomic counters at fixed byte offsets.
//!
//! Cfg-gated to unix (mmap-backed SHM is unix-only — Windows uses a
//! different IPC path).

use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize};

/// Read a `u32` from mmap at `off`.
///
/// Only exercised by tests today (header reads in production read
/// through `atomic_*` helpers); kept `cfg(test)` so production
/// builds don't carry an unused helper.
#[cfg(test)]
#[inline]
pub(crate) fn read_u32(base: *const u8, off: usize) -> u32 {
    unsafe { (base.add(off) as *const u32).read() }
}

/// Write a `u32` to mmap at `off`.
#[inline]
pub(crate) fn write_u32(base: *mut u8, off: usize, val: u32) {
    unsafe { (base.add(off) as *mut u32).write(val) }
}

/// Borrow an `AtomicUsize` from mmap at `off`.
///
/// SAFETY: caller must ensure the pointer is valid and 8-byte
/// aligned, and that the mmap region lives for the duration of the
/// returned reference.
#[inline]
pub(crate) unsafe fn atomic_usize(base: *const u8, off: usize) -> &'static AtomicUsize {
    &*(base.add(off) as *const AtomicUsize)
}

/// Borrow an `AtomicU64` from mmap at `off`. Same safety contract as
/// [`atomic_usize`].
#[inline]
pub(crate) unsafe fn atomic_u64(base: *const u8, off: usize) -> &'static AtomicU64 {
    &*(base.add(off) as *const AtomicU64)
}

/// Borrow an `AtomicBool` from mmap at `off`. Same safety contract
/// as [`atomic_usize`].
#[inline]
pub(crate) unsafe fn atomic_bool(base: *const u8, off: usize) -> &'static AtomicBool {
    &*(base.add(off) as *const AtomicBool)
}
