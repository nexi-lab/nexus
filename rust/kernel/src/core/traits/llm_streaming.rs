//! LlmStreamingBackend pillar — object-safe trait the connector backends
//! opt into so the kernel's `PyKernel::llm_start_streaming` syscall can
//! drive any protocol-specific SSE pipeline (OpenAI, Anthropic, …).
//!
//! Phase D rationale: the trait declaration must live in `kernel/` so
//! `ObjectStore::as_llm_streaming() -> Option<&dyn LlmStreamingBackend>`
//! does not pull a concrete `backends/` type across the kernel boundary.
//! Every protocol-specific impl (`OpenAIBackend`, future `AnthropicBackend`)
//! lives in `backends/src/transports/ai/*` and depends on this trait.

use std::sync::Arc;

use crate::core::stream::manager::StreamManager;

/// Streaming-capable LLM backend — object-safe trait so `ObjectStore` impls
/// can opt in to `PyKernel::llm_start_streaming` without every backend
/// learning every protocol's SSE shape.
pub trait LlmStreamingBackend: Send + Sync {
    /// Run a streaming chat completion to completion. Writes token deltas
    /// into `stream_path`, persists the session via CAS, closes the stream.
    /// Blocks the calling thread — caller is expected to have released the
    /// GIL and be running on a worker thread.
    #[allow(private_interfaces)]
    fn run_streaming(
        &self,
        request_bytes: &[u8],
        stream_path: &str,
        stream_manager: &Arc<StreamManager>,
    ) -> Result<(), String>;
}
