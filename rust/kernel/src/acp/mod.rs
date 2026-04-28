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
//!   * [`AcpService`] — the registered Rust service, holding the
//!     default zone for new sessions. Subsequent commits add the
//!     subprocess + JSON-RPC + dispatch layers.
//!
//! Module placement: lives at `rust/kernel/src/acp/` today because the
//! `services` -> `kernel` dep flip (PR #3932) hasn't merged. Once it
//! does, the whole module moves to `rust/services/src/acp/` next to
//! `agent_table` (same migration as `managed_agent/`).

// Subprocess + connection layers land in follow-up commits; the
// dead-code allowance keeps the skeleton compiling while the surface
// fills out.
#![allow(dead_code)]

use std::sync::Arc;

use crate::service_registry::RustService;

pub(crate) mod agent_config;
pub(crate) mod jsonrpc;
pub(crate) mod paths;
#[cfg(unix)]
pub(crate) mod subprocess;

/// Rust-flavoured ACP service. Today's responsibilities are limited
/// to registration; `call_agent` and the admin RPCs land in commits
/// 20 and 21.
pub(crate) struct AcpService {
    default_zone: String,
}

impl AcpService {
    pub(crate) const NAME: &'static str = "acp";

    pub(crate) fn new(default_zone: String) -> Self {
        Self { default_zone }
    }

    /// Register the service into `ServiceRegistry`. Called from
    /// `Kernel::new()` after `ManagedAgentService::install`.
    ///
    /// Today this is a no-op past registration; future commits hang
    /// the dispatch table and call_agent surface off the registered
    /// instance.
    pub(crate) fn install(
        kernel: &crate::kernel::Kernel,
        default_zone: &str,
    ) -> Result<(), String> {
        let svc = Arc::new(Self::new(default_zone.to_string()));
        kernel.register_rust_service(Self::NAME, svc as Arc<dyn RustService>, Vec::new())
    }

    pub(crate) fn default_zone(&self) -> &str {
        &self.default_zone
    }
}

impl RustService for AcpService {
    fn name(&self) -> &str {
        Self::NAME
    }
}
