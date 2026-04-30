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
//! Kernel owns the data; services owns the views (preserves the
//! one-way `services -> kernel` dependency).

pub mod table;
