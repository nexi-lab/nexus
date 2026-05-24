//! RemoteStreamBackend — proxies DT_STREAM operations to a remote nexus node.
//!
//! Resurrected from Python `core/remote_stream.py` (deleted R19.1e, commit 0723e7e99).
//! Implements the StreamBackend trait so StreamManager treats it identically to
//! local backends (MemoryStreamBackend, WalStreamCore).
//!
//! Wire protocol:
//!   push  → typed StreamWriteNowait(path, data)  → offset
//!   read  → typed StreamReadAt(path, offset, …) → (data, next_offset, eof)
//!
//! Both are stream-shaped typed RPCs: native bytes (no base64 tax) and
//! native byte-offsets. The file-semantics Write RPC was the wrong target
//! because its response carries content_id / size, not stream offsets.

use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::Arc;

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

        let offset = match self.transport.stream_write_nowait(&self.path, data) {
            Ok(offset) => offset as usize,
            Err(_) => {
                return Err(StreamError::Closed(
                    "remote stream: StreamWriteNowait failed",
                ))
            }
        };

        self.tail
            .store(offset + HEADER_SIZE + data.len(), Ordering::Release);
        self.msg_count.fetch_add(1, Ordering::Relaxed);

        Ok(offset)
    }

    fn read_at(&self, offset: usize) -> Result<(Vec<u8>, usize), StreamError> {
        let result = self
            .transport
            .stream_read_at(&self.path, offset as u64, false, 0)
            .map_err(|_| StreamError::Empty)?;

        if result.eof {
            return Err(StreamError::Empty);
        }

        Ok((result.data, result.next_offset as usize))
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
