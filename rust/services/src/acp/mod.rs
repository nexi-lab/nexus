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
//!   * [`subprocess`] (unix) — `AcpSubprocess` owns the agent CLI +
//!     three stdio DT_PIPE registrations.
//!   * [`jsonrpc`] — newline-delimited JSON-RPC 2.0 client.
//!   * [`observer`] — accumulator for `session/update` notifications.
//!   * [`connection`] — ACP-specific request / notification routing.
//!   * [`service`] — the registered Rust service: `call_agent`,
//!     admin RPCs, registry / on-terminate plumbing.
//!
//! Module placement: lives at `rust/kernel/src/acp/` today because the
//! `services` -> `kernel` dep flip (PR #3932) hasn't merged. Once it
//! does, the whole module moves to `rust/services/src/acp/` next to
//! `agent_table` (same migration as `managed_agent/`).

#![allow(dead_code)]

pub(crate) mod agent_config;
pub(crate) mod connection;
pub(crate) mod jsonrpc;
pub(crate) mod observer;
pub(crate) mod paths;
pub(crate) mod pyo3;
pub(crate) mod service;
#[cfg(unix)]
pub(crate) mod subprocess;

#[allow(unused_imports)] // commit 21 wires AcpService into the boot path
pub(crate) use service::{AcpService, AgentRegistry};
