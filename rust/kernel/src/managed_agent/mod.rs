//! Managed Agent service — host for `AgentKind::MANAGED` agents that
//! run in-process inside nexusd (linked-in Rust crates, no subprocess /
//! stdio JSON-RPC). Parallel of `nexus.services.acp` (which hosts
//! `AgentKind::UNMANAGED` external ACP backends like claude / codex).
//!
//! Today's first consumer is the `sudo-code` runtime crate (separate
//! repo, linked into nexusd via Cargo dep at build time). Future
//! managed agents (password-agent, browser-ai, …) can plug into the
//! same surface — the service is generic over `AgentKind::MANAGED`
//! agents, not sudo-code-specific.
//!
//! Layout (mirrors PR #3932's `rust/services/src/<service>/` layout —
//! files physically live in `rust/kernel/src/managed_agent/` until
//! the `services → kernel` dep flip lands; post-flip this folder
//! moves wholesale to `rust/services/src/managed_agent/`):
//!
//!   * `mailbox_stamping_hook` — registers a `NativeInterceptHook` that
//!     rewrites the `from` field on `*/chat-with-me` writes
//!   * `mailbox_stamping_policy` — pure rewrite logic the hook delegates
//!     to (envelope JSON shape, identity guarantee)
//!   * `workspace_boundary_hook` — registers a `NativeInterceptHook` that
//!     rejects cross-owner writes into `/proc/{pid}/workspace/`
//!
//! The two hooks register themselves through `ManagedAgentService::start()`
//! when (in a follow-up commit) the service itself lands; until then
//! `Kernel::new()` does the registration so the hooks stay live during
//! this transition. The hooks are generic to ALL managed agents — any
//! workspace, any chat-with-me — they're not specific to any single
//! consumer.

pub(crate) mod mailbox_stamping_hook;
pub(crate) mod mailbox_stamping_policy;
pub(crate) mod workspace_boundary_hook;
