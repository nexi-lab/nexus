//! Agent service tier — procfs-style read views over the kernel's
//! agent registry SSOT.
//!
//! The SSOT itself (`AgentRegistry` struct) lives in the kernel crate
//! at [`kernel::core::agents::registry`] — that's where the data
//! actually lives (kernel owns the field, kernel mutates it on agent
//! lifecycle events).  This module owns only the *views* that read
//! the SSOT via shared `Arc`:
//!
//! * [`status_resolver`] — `/{zone}/proc/{pid}/status` virtual file,
//!   impls `kernel::core::dispatch::PathResolver`.
//!
//! `mailbox_stamping` used to live here; it moved to
//! `a2a::mailbox_stamping_policy` because the `from` guarantee belongs to
//! the A2A substrate every message write converges on — it is not a
//! generic agent-registry concern.

pub mod status_resolver;
