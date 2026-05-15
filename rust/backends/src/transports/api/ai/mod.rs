//! LLM connector backends — SSE → DT_STREAM → CAS pipeline.

#[cfg(feature = "driver-anthropic")]
pub mod anthropic;
#[cfg(feature = "driver-openai")]
pub mod openai;
