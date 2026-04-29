//! `LlmStreamingBackend` — object-safe extension hook on §3.A.2
//! `ObjectStore`. Connector backends opt in so the kernel's
//! `PyKernel::llm_start_streaming` syscall drives any protocol-specific
//! SSE pipeline (OpenAI, Anthropic, …).
//!
//! Distinct from §3.B Control-Plane HAL traits: those are runtime DI
//! surfaces the kernel reaches through trait dispatch
//! (`DistributedCoordinator`, `ObjectStoreProvider`); this is a
//! sub-capability ObjectStore impls expose through
//! [`crate::abc::object_store::ObjectStore::as_llm_streaming`].
//!
//! Trait declaration lives in the kernel because the
//! `ObjectStore::as_llm_streaming() -> Option<&dyn LlmStreamingBackend>`
//! method signature references it. Concrete protocol-specific impls
//! (`OpenAIBackend`, `AnthropicBackend`) live in
//! `backends/src/transports/api/ai/*`.

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
