//! Agent service tier — procfs-style read views over the kernel's
//! agent table SSOT.
//!
//! The SSOT itself (`AgentTable` struct) lives in the kernel crate at
//! [`kernel::core::agents::table`] — that's where the data actually
//! lives (kernel owns the field, kernel mutates it on agent lifecycle
//! events).  This module owns only the *views* that read the SSOT via
//! shared `Arc`:
//!
//! * [`status_resolver`] — `/{zone}/proc/{pid}/status` virtual file,
//!   impls `kernel::core::dispatch::PathResolver`.
//!
//! `mailbox_stamping` used to live here; it moved to
//! `kernel::managed_agent::mailbox_stamping_policy` because the policy
//! belongs alongside the `ManagedAgentService` that owns the
//! chat-with-me mailbox surface — it's a managed-agent concern, not
//! a generic agent-table concern.

pub mod agent_table;
pub mod status_resolver;
