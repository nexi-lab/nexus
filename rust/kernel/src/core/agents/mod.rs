//! Kernel agent registry — agent table SSOT.
//!
//! [`table::AgentTable`] holds the per-PID agent descriptors (name,
//! kind, state, owner) that the kernel mutates on agent lifecycle
//! events.  It's a pure-Rust DashMap registry — no PyO3, no I/O —
//! shared across syscall threads via `Arc` and read by service-tier
//! views like `services::agents::status_resolver::AgentStatusResolver`.
//!
//! Linux analogue: this is the kernel-owned `task_struct` ↔ pid_hash
//! pairing.  Kernel constructs + mutates the table; service-tier
//! procfs views (`fs/proc/`) read it through shared references.
//!
//! Phase 3: moved here from `rust/services/src/agent_table.rs`.  The
//! original placement was wrong — it predated the parallel-layers
//! crate split and put a kernel-owned SSOT field's type in services,
//! which forced kernel to depend on services and made the post-Phase-3
//! `services -> kernel` dependency cyclic.  The fix slots it where it
//! actually belongs: kernel owns the data, services owns the views.

pub mod table;
