//! StdioPipeBackend — PipeBackend over OS subprocess file descriptors.
//!
//! Wraps raw fds (from subprocess stdin/stdout/stderr) as a `PipeBackend`
//! so unmodified CLIs can communicate via the DT_PIPE kernel primitive.
//!
//! Newline-framed: each `push` appends `\n`, each `pop` returns one line
//! (matching JSON-lines ACP/IPC protocol).
//!
//! Unix-only (`#[cfg(unix)]`).

use crate::pipe::PipeError;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::Mutex;

/// PipeBackend wrapping subprocess OS file descriptors.
///
/// For unmanaged agents (3rd-party CLIs) that speak raw stdio.
/// Newline-framed: each push appends `\n`, each pop returns one line.
pub(crate) struct StdioPipeBackend {
    /// Read fd (stdout/stderr of subprocess). -1 = write-only.
    read_fd: i32,
    /// Write fd (stdin of subprocess). -1 = read-only.
    write_fd: i32,
    closed: AtomicBool,
    push_count: AtomicUsize,
    pop_count: AtomicUsize,
    /// Internal line buffer for `pop()` — partial reads until `\n`.
    line_buf: Mutex<Vec<u8>>,
}

unsafe impl Send for StdioPipeBackend {}
unsafe impl Sync for StdioPipeBackend {}

impl StdioPipeBackend {
    /// Create a new StdioPipeBackend from raw fds.
    ///
    /// - `read_fd`: fd to read from (-1 for write-only pipes, e.g. stdin)
    /// - `write_fd`: fd to write to (-1 for read-only pipes, e.g. stdout)
    pub(crate) fn new(read_fd: i32, write_fd: i32) -> Self {
        Self {
            read_fd,
            write_fd,
            closed: AtomicBool::new(false),
            push_count: AtomicUsize::new(0),
            pop_count: AtomicUsize::new(0),
            line_buf: Mutex::new(Vec::with_capacity(4096)),
        }
    }
}

impl crate::pipe::PipeBackend for StdioPipeBackend {
    fn push(&self, data: &[u8]) -> Result<usize, PipeError> {
        if self.closed.load(Ordering::Acquire) {
            return Err(PipeError::Closed("write to closed stdio pipe"));
        }
        if self.write_fd < 0 {
            return Err(PipeError::Closed("no writer (read-only pipe)"));
        }

        // Append newline if not present
        let needs_nl = !data.ends_with(b"\n");
        let written = unsafe {
            let n = libc::write(self.write_fd, data.as_ptr() as *const _, data.len());
            if n < 0 {
                return Err(PipeError::Closed("write failed"));
            }
            if needs_nl {
                let nl = b"\n";
                libc::write(self.write_fd, nl.as_ptr() as *const _, 1);
            }
            n as usize + if needs_nl { 1 } else { 0 }
        };

        self.push_count.fetch_add(1, Ordering::Relaxed);
        Ok(written)
    }

    fn pop(&self) -> Result<Vec<u8>, PipeError> {
        if self.read_fd < 0 {
            return Err(PipeError::Closed("no reader (write-only pipe)"));
        }

        let mut buf = self.line_buf.lock().unwrap();

        // Check if we already have a complete line in the buffer
        if let Some(nl_pos) = buf.iter().position(|&b| b == b'\n') {
            let line: Vec<u8> = buf.drain(..=nl_pos).collect();
            self.pop_count.fetch_add(1, Ordering::Relaxed);
            return Ok(line);
        }

        // Read until we get a newline
        let mut read_buf = [0u8; 4096];
        loop {
            if self.closed.load(Ordering::Acquire) {
                // Drain remaining buffer
                if !buf.is_empty() {
                    let line = std::mem::take(&mut *buf);
                    self.pop_count.fetch_add(1, Ordering::Relaxed);
                    return Ok(line);
                }
                return Err(PipeError::ClosedEmpty);
            }

            let n = unsafe {
                libc::read(
                    self.read_fd,
                    read_buf.as_mut_ptr() as *mut _,
                    read_buf.len(),
                )
            };
            if n <= 0 {
                self.closed.store(true, Ordering::Release);
                if !buf.is_empty() {
                    let line = std::mem::take(&mut *buf);
                    self.pop_count.fetch_add(1, Ordering::Relaxed);
                    return Ok(line);
                }
                return Err(PipeError::ClosedEmpty);
            }

            buf.extend_from_slice(&read_buf[..n as usize]);

            // Check for newline in the newly appended data
            if let Some(nl_pos) = buf.iter().position(|&b| b == b'\n') {
                let line: Vec<u8> = buf.drain(..=nl_pos).collect();
                self.pop_count.fetch_add(1, Ordering::Relaxed);
                return Ok(line);
            }
        }
    }

    fn close(&self) {
        if self.closed.swap(true, Ordering::AcqRel) {
            return; // already closed
        }
        // Close write fd to signal EOF to the subprocess
        if self.write_fd >= 0 {
            unsafe {
                libc::close(self.write_fd);
            }
        }
        // Don't close read fd — let it drain naturally
    }

    fn is_closed(&self) -> bool {
        self.closed.load(Ordering::Acquire)
    }

    fn is_empty(&self) -> bool {
        let buf = self.line_buf.lock().unwrap();
        buf.is_empty()
    }

    fn size(&self) -> usize {
        let buf = self.line_buf.lock().unwrap();
        buf.len()
    }

    fn msg_count(&self) -> usize {
        self.pop_count.load(Ordering::Relaxed)
    }
}

impl Drop for StdioPipeBackend {
    fn drop(&mut self) {
        if !self.closed.load(Ordering::Acquire) {
            if self.write_fd >= 0 {
                unsafe {
                    libc::close(self.write_fd);
                }
            }
            if self.read_fd >= 0 {
                unsafe {
                    libc::close(self.read_fd);
                }
            }
        }
    }
}
