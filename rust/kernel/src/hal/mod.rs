//! Kernel HAL — kernel-defined extension interfaces.
//!
//! Linux analogue: `security_operations` (LSM hooks) and similar
//! extension surfaces. These traits are NOT §3 ABC pillars (those live
//! in `crate::abc::*`); they're additional contracts the kernel
//! exposes for parallel-crate impls to plug into.
//!
//! Current members:
//!
//! * [`llm_streaming`] — extension over `ObjectStore` for connector
//!   backends that want a chunked LLM response stream materialised
//!   into the CAS pillar (the AI connector path).
//! * [`peer`] — abstract peer-blob fetch trait. Kernel code holds an
//!   `Arc<dyn PeerBlobClient>` so the concrete `transport::blob::
//!   peer_client::PeerBlobClient` impl can move into the `transport`
//!   crate (Phase 4) without dragging the kernel ↔ transport edge
//!   across the workspace twice.
//!
//! Phase 1 introduced this directory alongside `abc/`. The two are
//! intentionally separate: `abc/` is the §3 invariant set (3 pillars,
//! period), `hal/` is the open-ended extension namespace.

pub mod llm_streaming;
pub mod peer;
