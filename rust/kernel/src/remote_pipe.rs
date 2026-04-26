//! RemotePipeBackend — proxies DT_PIPE push/pop to a remote nexus node.
//!
//! Resurrected from Python `core/remote_pipe.py` (deleted R19.1e, commit 0723e7e99).
//! Implements the PipeBackend trait so PipeManager treats it identically to
//! local backends (MemoryPipeBackend, SharedMemoryPipeBackend).
//!
//! Wire protocol (JSON-RPC via RpcTransport::call):
//!   push → Call("pipe_write_nowait", {path, data: base64}) → {"written": true}
//!   pop  → Call("pipe_read_nowait",  {path})               → {"data": base64} | null
//!
//! Local msg_count is a producer-side approximation (accurate when this backend
//! is the sole writer to the remote path; pop() adjusts on successful read).

use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::Arc;

use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine as _;

use crate::pipe::{PipeBackend, PipeError};
use crate::rpc_transport::RpcTransport;

#[allow(dead_code)]
pub(crate) struct RemotePipeBackend {
    path: String,
    transport: Arc<RpcTransport>,
    closed: AtomicBool,
    /// Approximation of queued message count. Incremented on push, decremented
    /// on successful pop. May desync with concurrent remote writers/readers.
    msg_count: AtomicUsize,
}

impl RemotePipeBackend {
    #[allow(dead_code)]
    pub(crate) fn new(path: impl Into<String>, transport: Arc<RpcTransport>) -> Self {
        Self {
            path: path.into(),
            transport,
            closed: AtomicBool::new(false),
            msg_count: AtomicUsize::new(0),
        }
    }
}

impl PipeBackend for RemotePipeBackend {
    fn push(&self, data: &[u8]) -> Result<usize, PipeError> {
        if self.closed.load(Ordering::Acquire) {
            return Err(PipeError::Closed("write to closed remote pipe"));
        }

        let payload = serde_json::json!({
            "path": self.path,
            "data": B64.encode(data),
        });
        let payload_bytes = serde_json::to_vec(&payload)
            .map_err(|_| PipeError::Closed("remote pipe: payload serialization failed"))?;

        let (resp_bytes, is_error) = self
            .transport
            .call("pipe_write_nowait", &payload_bytes)
            .map_err(|_| PipeError::Closed("remote pipe: pipe_write_nowait RPC failed"))?;

        if is_error {
            let msg = String::from_utf8_lossy(&resp_bytes);
            if msg.contains("Full") {
                return Err(PipeError::Full(0, 0));
            }
            return Err(PipeError::Closed("remote pipe: push returned error"));
        }

        self.msg_count.fetch_add(1, Ordering::Relaxed);
        Ok(data.len())
    }

    fn pop(&self) -> Result<Vec<u8>, PipeError> {
        if self.closed.load(Ordering::Acquire) {
            return Err(PipeError::ClosedEmpty);
        }

        let payload = serde_json::json!({ "path": self.path });
        let payload_bytes = serde_json::to_vec(&payload).map_err(|_| PipeError::Empty)?;

        let (resp_bytes, is_error) = self
            .transport
            .call("pipe_read_nowait", &payload_bytes)
            .map_err(|_| PipeError::Empty)?;

        if is_error || resp_bytes == b"null" || resp_bytes.is_empty() {
            return Err(PipeError::Empty);
        }

        let resp: serde_json::Value =
            serde_json::from_slice(&resp_bytes).map_err(|_| PipeError::Empty)?;

        let data_b64 = resp
            .get("data")
            .and_then(|v| v.as_str())
            .ok_or(PipeError::Empty)?;

        let data = B64.decode(data_b64).map_err(|_| PipeError::Empty)?;

        self.msg_count
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |c| {
                Some(c.saturating_sub(1))
            })
            .ok();

        Ok(data)
    }

    fn close(&self) {
        self.closed.store(true, Ordering::Release);
    }

    fn is_closed(&self) -> bool {
        self.closed.load(Ordering::Acquire)
    }

    fn is_empty(&self) -> bool {
        self.msg_count.load(Ordering::Relaxed) == 0
    }

    fn size(&self) -> usize {
        0 // Remote byte-size not tracked locally
    }

    fn msg_count(&self) -> usize {
        self.msg_count.load(Ordering::Relaxed)
    }
}
