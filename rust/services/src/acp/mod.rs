//! `AcpService` — Rust port of the Python `nexus.services.acp` package.
//!
//! AcpService drives one-shot ACP (Agent Client Protocol) calls against
//! a coding-agent CLI binary (Claude / Codex / Gemini / …) defined in
//! VFS at `/{zone}/agents/{id}/agent.json`. Each `call_agent` invocation
//! spawns the CLI as a subprocess, opens an ACP session over stdio,
//! sends a single prompt, accumulates the streaming response into an
//! `AgentTurnResult`, persists it to `/{zone}/proc/{pid}/result`, and
//! reaps the subprocess.
//!
//! Layered:
//!
//!   * [`agent_config`] — `AgentConfig` serde struct mirroring the
//!     Python `AgentConfig` dataclass; reads from VFS `agent.json`.
//!   * [`paths`] — VFS path constructors mirroring
//!     `nexus.contracts.vfs_paths`. The Rust port keeps the same
//!     conventions so a Python and Rust caller addressing the same
//!     agent see the same files.
//!   * [`subprocess`] (unix) — `spawn_acp` bridge: `AgentConfig` → argv
//!     over the generic [`crate::subprocess::HostedSubprocess`] host.
//!   * [`jsonrpc`] — newline-delimited JSON-RPC 2.0 client.
//!   * [`observer`] — accumulator for `session/update` notifications.
//!   * [`connection`] — ACP-specific request / notification routing.
//!   * [`service`] — the registered Rust service: `call_agent`,
//!     admin RPCs, registry / on-terminate plumbing.
//!
//! Module placement: lives at `rust/kernel/src/acp/` today because the
//! `services` -> `kernel` dep flip (PR #3932) hasn't merged. Once it
//! does, the whole module moves to `rust/services/src/acp/` next to
//! `agent_registry` (same migration as `managed_agent/`).

#![allow(dead_code)]

pub(crate) mod agent_config;
pub(crate) mod connection;
pub(crate) mod jsonrpc;
pub(crate) mod observer;
pub(crate) mod paths;
pub(crate) mod service;
#[cfg(unix)]
pub(crate) mod subprocess;

#[allow(unused_imports)] // commit 21 wires AcpService into the boot path
pub(crate) use service::{AcpService, AgentRegistry};

/// The ACP one-shot service as a boot declaration for
/// [`kernel::kernel::Kernel::bring_up_services`] — the uniform path the
/// assembly uses to hand services to the kernel. Wraps
/// [`service::AcpService::install`]; `default_zone` is the deployment's
/// default zone (the assembly passes `"root"` for the node-local daemon).
pub fn service_decl(default_zone: String) -> kernel::kernel::ServiceDecl {
    kernel::kernel::ServiceDecl {
        name: "acp".to_string(),
        install: Box::new(move |kernel| service::AcpService::install(kernel, &default_zone)),
    }
}
