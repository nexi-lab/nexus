//! nexus-ipc: Agent-to-agent messaging over the VFS (IPC-over-VFS pattern).
//!
//! Messages are VFS files written to canonical agent inbox paths. Delivery
//! notification uses DT_PIPE writes. Blocking receipt uses `sys_watch`.
//! No separate transport — all I/O goes through the same kernel syscalls as
//! any other VFS operation, so audit, federation, and permission hooks fire
//! automatically.
//!
//! # Path layout
//!
//! ```text
//! /agents/{agent_id}/
//!   inbox/          ← incoming MessageEnvelope JSON files
//!   outbox/         ← sent messages (audit trail)
//!   processed/      ← successfully handled messages
//!   dead_letter/    ← failed messages
//!   notify          ← DT_PIPE: wakeup signal (one byte per new message)
//! ```
//!
//! # Usage
//!
//! ```no_run
//! use nexus_kernel::ipc::{send_message, wait_for_message, MessageEnvelope};
//!
//! // Send
//! let envelope = MessageEnvelope::new("agent:sender", "agent:reviewer", "task", payload);
//! send_message(&kernel, &ctx, &envelope)?;
//!
//! // Receive (blocks up to timeout_ms)
//! if let Some(msg) = wait_for_message(&kernel, &ctx, "agent:analyst", 30_000)? {
//!     // process msg.payload, then it's already renamed to processed/
//! }
//! ```

use chrono::SecondsFormat;
use serde::{Deserialize, Serialize};
use uuid::Uuid;

use kernel::kernel::Kernel;
use kernel::kernel::{KernelError, OperationContext};

// ── Path conventions (mirrors bricks/ipc/conventions.py) ────────────────────

const AGENTS_ROOT: &str = "/agents";

fn inbox_path(agent_id: &str) -> String {
    format!("{AGENTS_ROOT}/{agent_id}/inbox")
}

fn outbox_path(agent_id: &str) -> String {
    format!("{AGENTS_ROOT}/{agent_id}/outbox")
}

fn processed_path(agent_id: &str) -> String {
    format!("{AGENTS_ROOT}/{agent_id}/processed")
}

fn dead_letter_path(agent_id: &str) -> String {
    format!("{AGENTS_ROOT}/{agent_id}/dead_letter")
}

fn notify_pipe_path(agent_id: &str) -> String {
    format!("{AGENTS_ROOT}/{agent_id}/notify")
}

fn message_filename(msg_id: &str, ts: &str) -> String {
    // {ISO_ts_compact}_{msg_id}.json — sortable, unique.
    // ts is already an ISO-8601 string; strip colons/dashes for filename safety.
    let ts_compact = ts.replace(['-', ':', '.'], "");
    format!("{ts_compact}_{msg_id}.json")
}

// ── MessageEnvelope wire format ──────────────────────────────────────────────

/// JSON wire format for agent-to-agent messages. Fields match
/// `bricks/ipc/envelope.py` so Python and Rust agents interoperate.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MessageEnvelope {
    /// Wire protocol version ("1.0").
    pub nexus_message: String,
    /// Unique message ID (UUID v4 string, prefixed "msg_").
    pub id: String,
    /// Sender agent ID (e.g. "agent:analyst").
    pub from: String,
    /// Recipient agent ID.
    pub to: String,
    /// Message type (e.g. "task", "reply", "event").
    #[serde(rename = "type")]
    pub msg_type: String,
    /// Optional correlation ID for request-reply matching.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub correlation_id: Option<String>,
    /// ISO-8601 creation timestamp.
    pub timestamp: String,
    /// Message TTL in seconds (0 = no expiry).
    pub ttl_seconds: u64,
    /// Arbitrary payload — caller's responsibility to define schema.
    pub payload: serde_json::Value,
}

impl MessageEnvelope {
    /// Create a new envelope with a generated ID and current timestamp.
    pub fn new(
        from: impl Into<String>,
        to: impl Into<String>,
        msg_type: impl Into<String>,
        payload: serde_json::Value,
    ) -> Self {
        let now = chrono::Utc::now().to_rfc3339_opts(SecondsFormat::Millis, true);
        Self {
            nexus_message: "1.0".into(),
            id: format!("msg_{}", Uuid::new_v4().simple()),
            from: from.into(),
            to: to.into(),
            msg_type: msg_type.into(),
            correlation_id: None,
            timestamp: now,
            ttl_seconds: 3600,
            payload,
        }
    }
}

// ── IPC error type ────────────────────────────────────────────────────────────

/// Errors returned by IPC operations.
#[derive(Debug)]
pub enum IpcError {
    /// Underlying kernel syscall failed.
    Kernel(KernelError),
    /// Could not serialise or deserialise an envelope.
    Json(serde_json::Error),
    /// No message arrived within the timeout.
    Timeout,
}

impl std::fmt::Display for IpcError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Kernel(e) => write!(f, "kernel: {e:?}"),
            Self::Json(e) => write!(f, "json: {e}"),
            Self::Timeout => write!(f, "timeout"),
        }
    }
}

impl std::error::Error for IpcError {}

impl From<KernelError> for IpcError {
    fn from(e: KernelError) -> Self {
        Self::Kernel(e)
    }
}

impl From<serde_json::Error> for IpcError {
    fn from(e: serde_json::Error) -> Self {
        Self::Json(e)
    }
}

// ── Public API ────────────────────────────────────────────────────────────────

/// Send a message to `envelope.to`.
///
/// Steps:
/// 1. Serialise envelope to JSON.
/// 2. `sys_write` to `/agents/{to}/inbox/{ts}_{id}.json`.
/// 3. `sys_write` b"\x01" to `/agents/{to}/notify` (DT_PIPE wakeup) —
///    best-effort, ignored if the pipe does not exist.
/// 4. Optionally copy to sender's outbox (audit trail).
///
/// Returns the inbox path the message was written to.
pub fn send_message(
    kernel: &Kernel,
    ctx: &OperationContext,
    envelope: &MessageEnvelope,
) -> Result<String, IpcError> {
    let json = serde_json::to_vec(envelope)?;
    let filename = message_filename(&envelope.id, &envelope.timestamp);

    // 1. Write to recipient inbox.
    let inbox = format!("{}/{filename}", inbox_path(&envelope.to));
    let wr = kernel.sys_write(&inbox, ctx, &json, 0)?;
    if !wr.hit {
        return Err(IpcError::Kernel(KernelError::IOError(format!(
            "sys_write missed on inbox path {inbox}"
        ))));
    }

    // 2. DT_PIPE wakeup — best-effort (no pipe = silent no-op).
    let notify = notify_pipe_path(&envelope.to);
    let _ = kernel.sys_write(&notify, ctx, b"\x01", 0);

    // 3. Outbox copy (audit trail) — best-effort.
    let outbox = format!("{}/{filename}", outbox_path(&envelope.from));
    let _ = kernel.sys_write(&outbox, ctx, &json, 0);

    Ok(inbox)
}

/// Block until a message arrives in `agent_id`'s inbox or `timeout_ms` elapses.
///
/// On success:
/// - Reads the envelope JSON from the file named in the FileEvent.
/// - Renames the file to `processed/` (marks as handled).
/// - Returns the parsed `MessageEnvelope`.
///
/// On parse or read error:
/// - Renames the file to `dead_letter/` instead.
/// - Returns `Err(IpcError::Json(...))`.
///
/// On timeout: returns `Ok(None)`.
pub fn wait_for_message(
    kernel: &Kernel,
    ctx: &OperationContext,
    agent_id: &str,
    timeout_ms: u64,
) -> Result<Option<MessageEnvelope>, IpcError> {
    let watch_pattern = format!("{}/{{*}}", inbox_path(agent_id));
    let event = match kernel.sys_watch(&watch_pattern, timeout_ms) {
        Some(e) => e,
        None => return Ok(None),
    };

    let msg_path = event.path().to_string();
    let msg_path = &msg_path;

    // Read the envelope.
    let read_result = kernel.sys_read(msg_path, ctx)?;
    let data = match read_result.data {
        Some(d) => d,
        None => {
            let _ = rename_to_dead_letter(kernel, ctx, msg_path, agent_id);
            return Err(IpcError::Kernel(KernelError::FileNotFound(
                msg_path.clone(),
            )));
        }
    };

    // Parse — on failure move to dead_letter.
    match serde_json::from_slice::<MessageEnvelope>(&data) {
        Ok(envelope) => {
            rename_to_processed(kernel, ctx, msg_path, agent_id);
            Ok(Some(envelope))
        }
        Err(e) => {
            let _ = rename_to_dead_letter(kernel, ctx, msg_path, agent_id);
            Err(IpcError::Json(e))
        }
    }
}

// ── Helpers ───────────────────────────────────────────────────────────────────

fn rename_to_processed(kernel: &Kernel, ctx: &OperationContext, path: &str, agent_id: &str) {
    let filename = path.rsplit('/').next().unwrap_or("unknown");
    let dst = format!("{}/{filename}", processed_path(agent_id));
    let _ = kernel.sys_rename(path, &dst, ctx);
}

fn rename_to_dead_letter(
    kernel: &Kernel,
    ctx: &OperationContext,
    path: &str,
    agent_id: &str,
) -> Result<(), KernelError> {
    let filename = path.rsplit('/').next().unwrap_or("unknown");
    let dst = format!("{}/{filename}", dead_letter_path(agent_id));
    kernel.sys_rename(path, &dst, ctx).map(|_| ())
}

// ── Unit tests ────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn message_envelope_roundtrip() {
        let env = MessageEnvelope::new(
            "agent:sender",
            "agent:receiver",
            "task",
            serde_json::json!({"action": "review", "path": "/docs/spec.md"}),
        );
        let json = serde_json::to_string(&env).unwrap();
        let decoded: MessageEnvelope = serde_json::from_str(&json).unwrap();
        assert_eq!(decoded.from, "agent:sender");
        assert_eq!(decoded.to, "agent:receiver");
        assert_eq!(decoded.msg_type, "task");
        assert_eq!(decoded.nexus_message, "1.0");
        assert!(decoded.id.starts_with("msg_"));
        assert_eq!(decoded.payload["action"], "review");
    }

    #[test]
    fn message_filename_is_sortable_and_unique() {
        let ts = "2026-04-25T10:00:00.123Z";
        let id = "msg_abc123";
        let name = message_filename(id, ts);
        assert!(name.ends_with(".json"));
        assert!(name.contains("msg_abc123"));
        // Two messages at the same timestamp get different filenames if IDs differ.
        let name2 = message_filename("msg_def456", ts);
        assert_ne!(name, name2);
    }

    #[test]
    fn path_conventions_match_python() {
        assert_eq!(inbox_path("agent:foo"), "/agents/agent:foo/inbox");
        assert_eq!(outbox_path("agent:foo"), "/agents/agent:foo/outbox");
        assert_eq!(processed_path("agent:foo"), "/agents/agent:foo/processed");
        assert_eq!(
            dead_letter_path("agent:foo"),
            "/agents/agent:foo/dead_letter"
        );
        assert_eq!(notify_pipe_path("agent:foo"), "/agents/agent:foo/notify");
    }

    #[test]
    fn envelope_new_unique_ids() {
        use std::collections::HashSet;
        let ids: HashSet<String> = (0..50)
            .map(|_| MessageEnvelope::new("a", "b", "t", serde_json::Value::Null).id)
            .collect();
        assert_eq!(ids.len(), 50, "IDs must be unique");
    }
}
