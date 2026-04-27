//! Agent service tier — procfs-style read views over the kernel's
//! agent table SSOT.
//!
//! Phase 3 moved the SSOT itself (`AgentTable` struct) into the
//! kernel crate at [`kernel::core::agents::table`] — that's where
//! the data actually lives (kernel owns the field, kernel mutates it
//! on agent lifecycle events).  This module owns only the *views*
//! that read the SSOT via shared `Arc`:
//!
//! * [`status_resolver`] — `/{zone}/proc/{pid}/status` virtual file,
//!   impls `kernel::core::dispatch::PathResolver`.

pub mod status_resolver;
