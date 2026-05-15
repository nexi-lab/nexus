//! RemoteStreamBackend — proxies DT_STREAM operations to a remote nexus node.
//!
//! Resurrected from Python `core/remote_stream.py` (deleted R19.1e, commit 0723e7e99).
//! Implements the StreamBackend trait so StreamManager treats it identically to
//! local backends (MemoryStreamBackend, WalStreamCore).
//!
//! Wire protocol:
//!   push  → Call("sys_write", {path, content: base64}) → {"offset": N}
//!   read  → Call("sys_read",  {path, offset: N})       → {"content": base64, "next_offset": N}
//!
//! The typed Write RPC is intentionally NOT used for push because the proto
//! WriteResponse carries file-semantics fields (content_id, size) and no stream
//! byte-offset. The Call RPC routes through the server's Python dispatch layer
//! which understands stream-path semantics.

use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::Arc;

use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine as _;

use crate::rpc_transport::RpcTransport;
use crate::stream::{StreamBackend, StreamError};

// Stream frame header size — must match MemoryStreamBackend ([4B payload_len][payload]).
#[allow(dead_code)]
const HEADER_SIZE: usize = 4;

#[allow(dead_code)]
pub(crate) struct RemoteStreamBackend {
    path: String,
    transport: Arc<RpcTransport>,
    closed: AtomicBool,
    /// Local approximation of remote tail offset. Accurate when this backend
    /// is the sole writer to the remote path; may lag with concurrent writers.
    tail: AtomicUsize,
    msg_count: AtomicUsize,
}

impl RemoteStreamBackend {
    #[allow(dead_code)]
    pub(crate) fn new(path: impl Into<String>, transport: Arc<RpcTransport>) -> Self {
        Self {
            path: path.into(),
            transport,
            closed: AtomicBool::new(false),
            tail: AtomicUsize::new(0),
            msg_count: AtomicUsize::new(0),
        }
    }
}

impl StreamBackend for RemoteStreamBackend {
    fn push(&self, data: &[u8]) -> Result<usize, StreamError> {
        if self.closed.load(Ordering::Acquire) {
            return Err(StreamError::Closed("write to closed remote stream"));
        }

        let payload = serde_json::json!({
            "path": self.path,
            "content": B64.encode(data),
        });
        let payload_bytes = serde_json::to_vec(&payload)
            .map_err(|_| StreamError::Closed("remote stream: payload serialization failed"))?;

        let (resp_bytes, is_error) = self
            .transport
            .call("sys_write", &payload_bytes)
            .map_err(|_| StreamError::Closed("remote stream: sys_write RPC failed"))?;

        if is_error {
            return Err(StreamError::Closed(
                "remote stream: sys_write returned error",
            ));
        }

        // Parse byte offset from {"offset": N}. Fall back to current tail so
        // the local approximation stays monotonic even on unexpected responses.
        let offset = serde_json::from_slice::<serde_json::Value>(&resp_bytes)
            .ok()
            .and_then(|v| v.get("offset").and_then(|o| o.as_u64()))
            .map(|o| o as usize)
            .unwrap_or_else(|| self.tail.load(Ordering::Relaxed));

        self.tail
            .store(offset + HEADER_SIZE + data.len(), Ordering::Release);
        self.msg_count.fetch_add(1, Ordering::Relaxed);

        Ok(offset)
    }

    fn read_at(&self, offset: usize) -> Result<(Vec<u8>, usize), StreamError> {
        let payload = serde_json::json!({
            "path": self.path,
            "offset": offset,
        });
        let payload_bytes =
            serde_json::to_vec(&payload).map_err(|_| StreamError::InvalidOffset(offset, 0))?;

        let (resp_bytes, is_error) = self
            .transport
            .call("sys_read", &payload_bytes)
            .map_err(|_| StreamError::Empty)?;

        if is_error {
            return Err(StreamError::Empty);
        }

        let resp: serde_json::Value = serde_json::from_slice(&resp_bytes)
            .map_err(|_| StreamError::InvalidOffset(offset, 0))?;

        let content_b64 = resp
            .get("content")
            .and_then(|v| v.as_str())
            .ok_or(StreamError::InvalidOffset(offset, 0))?;

        let content = B64
            .decode(content_b64)
            .map_err(|_| StreamError::InvalidOffset(offset, 0))?;

        let next_offset = resp
            .get("next_offset")
            .and_then(|v| v.as_u64())
            .map(|n| n as usize)
            .unwrap_or(offset + HEADER_SIZE + content.len());

        Ok((content, next_offset))
    }

    fn read_batch(
        &self,
        offset: usize,
        count: usize,
    ) -> Result<(Vec<Vec<u8>>, usize), StreamError> {
        let mut results = Vec::with_capacity(count);
        let mut cur = offset;

        for _ in 0..count {
            match self.read_at(cur) {
                Ok((data, next)) => {
                    cur = next;
                    results.push(data);
                }
                Err(StreamError::Empty | StreamError::ClosedEmpty) => break,
                Err(e) => return Err(e),
            }
        }

        if results.is_empty() {
            Err(StreamError::Empty)
        } else {
            Ok((results, cur))
        }
    }

    fn close(&self) {
        self.closed.store(true, Ordering::Release);
    }

    fn is_closed(&self) -> bool {
        self.closed.load(Ordering::Acquire)
    }

    fn tail_offset(&self) -> usize {
        self.tail.load(Ordering::Acquire)
    }

    fn msg_count(&self) -> usize {
        self.msg_count.load(Ordering::Relaxed)
    }
}
