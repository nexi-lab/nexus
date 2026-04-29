//! IPC syscalls — pipe + stream manager delegation.
//!
//! Every method stays a member of [`Kernel`] via this submodule's
//! `impl Kernel { ... }` block.

use crate::dcache::{DT_PIPE, DT_STREAM};

use super::{pipe_mgr_err, stream_mgr_err, Kernel, KernelError};

impl Kernel {
    // ── IPC Registry — Pipe methods (delegates to PipeManager) ──────────

    /// Create a pipe buffer in the IPC registry.
    ///
    /// PipeManager owns the buffer; Kernel persists DT_PIPE inode to
    /// metastore + dcache so sys_read/sys_write dispatch to IPC fast-path.
    pub fn create_pipe(&self, path: &str, capacity: usize) -> Result<(), KernelError> {
        self.pipe_manager
            .create(path, capacity)
            .map_err(pipe_mgr_err)?;

        // Persist DT_PIPE inode (best-effort — metastore may not be wired in tests).
        let mount_point = self.resolve_mount_point(path, contracts::ROOT_ZONE_ID);
        let meta = self.build_metadata(
            path,
            contracts::ROOT_ZONE_ID,
            DT_PIPE,
            capacity as u64,
            None,
            1,
            None,
            None,
            None,
        );
        self.commit_metadata(path, &mount_point, meta)?;

        Ok(())
    }

    /// Destroy a pipe buffer.
    pub fn destroy_pipe(&self, path: &str) -> Result<(), KernelError> {
        self.pipe_manager.destroy(path).map_err(pipe_mgr_err)?;

        // Atomic delete — metastore (raft) first, dcache eviction on
        // success.
        let mount_point = self.resolve_mount_point(path, contracts::ROOT_ZONE_ID);
        self.commit_delete(path, &mount_point)?;

        Ok(())
    }

    /// Close a pipe (signal close, keep in registry for drain).
    pub fn close_pipe(&self, path: &str) -> Result<(), KernelError> {
        self.pipe_manager.close(path).map_err(pipe_mgr_err)
    }

    /// Check if a pipe exists.
    pub fn has_pipe(&self, path: &str) -> bool {
        self.pipe_manager.has(path)
    }

    /// Queued bytes pending in a DT_PIPE.
    ///
    /// Returns `KernelError::FileNotFound` if no pipe is registered at
    /// `path`. `Ok(0)` means the pipe exists but has nothing to pop.
    /// Kernel-internal helper — no PyO3 wrapper, no Python syscall.
    pub fn pipe_size(&self, path: &str) -> Result<usize, KernelError> {
        self.pipe_manager
            .size(path)
            .ok_or_else(|| KernelError::FileNotFound(path.to_string()))
    }

    /// Non-blocking write to a pipe. Returns bytes written.
    pub fn pipe_write_nowait(&self, path: &str, data: &[u8]) -> Result<usize, KernelError> {
        self.pipe_manager
            .write_nowait(path, data)
            .map_err(pipe_mgr_err)
    }

    /// Non-blocking read from a pipe. Returns data or None if empty.
    pub fn pipe_read_nowait(&self, path: &str) -> Result<Option<Vec<u8>>, KernelError> {
        self.pipe_manager.read_nowait(path).map_err(pipe_mgr_err)
    }

    /// List all pipes with their paths.
    pub fn list_pipes(&self) -> Vec<String> {
        self.pipe_manager.list()
    }

    /// Blocking read — Condvar wait (GIL-free via py.allow_threads).
    /// Called from generated_pyo3.rs PyKernel wrapper.
    #[allow(dead_code)]
    pub fn pipe_read_blocking(&self, path: &str, timeout_ms: u64) -> Result<Vec<u8>, KernelError> {
        self.pipe_manager
            .read_blocking(path, timeout_ms)
            .map_err(pipe_mgr_err)
    }

    /// Close all pipes (shutdown).
    pub fn close_all_pipes(&self) {
        self.pipe_manager.close_all();
    }

    // ── IPC Registry — Stream methods (delegates to StreamManager) ────

    /// Create a stream buffer in the IPC registry.
    pub fn create_stream(&self, path: &str, capacity: usize) -> Result<(), KernelError> {
        self.stream_manager
            .create(path, capacity)
            .map_err(stream_mgr_err)?;

        let mount_point = self.resolve_mount_point(path, contracts::ROOT_ZONE_ID);
        let meta = self.build_metadata(
            path,
            contracts::ROOT_ZONE_ID,
            DT_STREAM,
            capacity as u64,
            None,
            1,
            None,
            None,
            None,
        );
        self.commit_metadata(path, &mount_point, meta)?;

        Ok(())
    }

    /// Destroy a stream buffer.
    pub fn destroy_stream(&self, path: &str) -> Result<(), KernelError> {
        self.stream_manager.destroy(path).map_err(stream_mgr_err)?;

        // Atomic delete — metastore (raft) first, dcache eviction on
        // success.
        let mount_point = self.resolve_mount_point(path, contracts::ROOT_ZONE_ID);
        self.commit_delete(path, &mount_point)?;

        Ok(())
    }

    /// Close a stream (signal close, keep in registry for drain).
    pub fn close_stream(&self, path: &str) -> Result<(), KernelError> {
        self.stream_manager.close(path).map_err(stream_mgr_err)
    }

    /// Check if a stream exists.
    pub fn has_stream(&self, path: &str) -> bool {
        self.stream_manager.has(path)
    }

    /// Current tail (write offset) of a DT_STREAM.
    ///
    /// Returns `KernelError::FileNotFound` if no stream is registered at
    /// `path`. Callers use this for the seek-to-end pattern: read the
    /// tail, then pass it as the offset to `stream_read_at_blocking`
    /// to skip history and block until new data arrives.  Kernel-internal
    /// helper — no PyO3 wrapper, no Python syscall.
    pub fn stream_tail(&self, path: &str) -> Result<usize, KernelError> {
        self.stream_manager
            .tail(path)
            .ok_or_else(|| KernelError::FileNotFound(path.to_string()))
    }

    /// Non-blocking write to a stream. Returns byte offset.
    pub fn stream_write_nowait(&self, path: &str, data: &[u8]) -> Result<usize, KernelError> {
        self.stream_manager
            .write_nowait(path, data)
            .map_err(stream_mgr_err)
    }

    /// Read one message at byte offset. Returns (data, next_offset) or None if empty.
    pub fn stream_read_at(
        &self,
        path: &str,
        offset: usize,
    ) -> Result<Option<(Vec<u8>, usize)>, KernelError> {
        self.stream_manager
            .read_at(path, offset)
            .map_err(stream_mgr_err)
    }

    /// Read up to `count` messages starting from byte offset.
    pub fn stream_read_batch(
        &self,
        path: &str,
        offset: usize,
        count: usize,
    ) -> Result<(Vec<Vec<u8>>, usize), KernelError> {
        self.stream_manager
            .read_batch(path, offset, count)
            .map_err(stream_mgr_err)
    }

    /// Collect all stream payloads from offset 0, concatenated.
    ///
    /// Replaces the manual `read_at` loop in Python LLM backends.
    /// Single Rust call → no per-frame PyO3 round-trip.
    pub fn stream_collect_all(&self, path: &str) -> Result<Vec<u8>, KernelError> {
        self.stream_manager
            .collect_all_payloads(path)
            .map_err(stream_mgr_err)
    }

    /// List all streams with their paths.
    pub fn list_streams(&self) -> Vec<String> {
        self.stream_manager.list()
    }

    /// Blocking read at offset — Condvar wait (GIL-free via py.allow_threads).
    /// Called from generated_pyo3.rs PyKernel wrapper.
    #[allow(dead_code)]
    pub fn stream_read_at_blocking(
        &self,
        path: &str,
        offset: usize,
        timeout_ms: u64,
    ) -> Result<(Vec<u8>, usize), KernelError> {
        self.stream_manager
            .read_at_blocking(path, offset, timeout_ms)
            .map_err(stream_mgr_err)
    }

    /// Close all streams (shutdown).
    pub fn close_all_streams(&self) {
        self.stream_manager.close_all();
    }
}
