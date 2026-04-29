//! Mailbox envelope stamping — overwrites the `from` field of a message
//! envelope with the caller's `agent_id` before the write reaches the
//! backend.
//!
//! Lives in the services rlib because the policy (which paths count as
//! mailboxes, what the envelope schema looks like, the identity
//! guarantee) is service-tier behavior layered on top of the kernel
//! write primitive. The kernel calls into this from `sys_write` so the
//! rewrite runs before any backend touches the bytes; that integration
//! site is the only kernel awareness of mailbox semantics.
//!
//! Path policy: any `sys_write` whose target ends in `/chat-with-me`
//! (the canonical mailbox path documented in the sudowork integration
//! design `docs/tech/nexus-integration-architecture.md` §3.3) is
//! parsed as a JSON envelope; the `from` field is stamped with
//! `caller_agent_id` regardless of what the LLM authored. Receivers
//! see who actually wrote the message, not who claimed to.
//!
//! Non-mailbox paths and writes without a caller agent_id short-circuit
//! at the path test, so the steady-state cost on the hot path is one
//! `str::ends_with` call.

use std::borrow::Cow;

const CHAT_WITH_ME_SUFFIX: &str = "/chat-with-me";

/// Rewrite the envelope's `from` field to the caller's `agent_id` when
/// the write target is a mailbox path. Returns the rewritten bytes, or
/// `None` if no rewrite was needed (non-mailbox path, no caller agent,
/// non-JSON content, or the existing `from` already matches).
///
/// JSON parsing failures are treated as "leave it alone" rather than
/// rejected — the kernel does not police the envelope schema, only the
/// `from` field. A non-JSON payload is forwarded to the backend
/// untouched and the receiver decides whether to accept it.
pub fn maybe_stamp_chat_envelope<'a>(
    path: &str,
    caller_agent_id: Option<&str>,
    content: &'a [u8],
) -> Option<Cow<'a, [u8]>> {
    if !path.ends_with(CHAT_WITH_ME_SUFFIX) {
        return None;
    }
    let caller = caller_agent_id?;
    if caller.is_empty() {
        return None;
    }

    let mut value: serde_json::Value = serde_json::from_slice(content).ok()?;
    let obj = value.as_object_mut()?;

    // No-op if the field is already correct — preserves the borrow path
    // even when the caller already wrote `from` themselves with the
    // right value (rare, but cheap to check).
    if let Some(existing) = obj.get("from").and_then(|v| v.as_str()) {
        if existing == caller {
            return None;
        }
    }

    obj.insert(
        "from".to_string(),
        serde_json::Value::String(caller.to_string()),
    );
    let rewritten = serde_json::to_vec(&value).ok()?;
    Some(Cow::Owned(rewritten))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(bytes: &[u8]) -> serde_json::Value {
        serde_json::from_slice(bytes).expect("rewritten content must be valid JSON")
    }

    #[test]
    fn stamps_from_field_on_chat_with_me_write() {
        let original = br#"{"to":"agent-b","body":"hi"}"#;
        let out =
            maybe_stamp_chat_envelope("/proc/p1/chat-with-me", Some("agent-a"), original).unwrap();
        let v = parse(&out);
        assert_eq!(v["from"], "agent-a");
        assert_eq!(v["to"], "agent-b");
        assert_eq!(v["body"], "hi");
    }

    #[test]
    fn overwrites_caller_supplied_from_field() {
        // LLM tries to spoof a from field; the kernel overwrites it.
        let original = br#"{"from":"agent-fake","to":"agent-b","body":"x"}"#;
        let out = maybe_stamp_chat_envelope(
            "/proc/p1/workspace/chat-with-me",
            Some("agent-real"),
            original,
        )
        .unwrap();
        let v = parse(&out);
        assert_eq!(v["from"], "agent-real");
    }

    #[test]
    fn passes_through_when_caller_already_correct() {
        let original = br#"{"from":"agent-a","to":"agent-b"}"#;
        let out = maybe_stamp_chat_envelope("/proc/p1/chat-with-me", Some("agent-a"), original);
        assert!(out.is_none(), "no rewrite when from field already matches");
    }

    #[test]
    fn ignores_non_mailbox_paths() {
        let original = br#"{"from":"liar","body":"x"}"#;
        let out = maybe_stamp_chat_envelope("/workspace/notes.md", Some("agent-a"), original);
        assert!(
            out.is_none(),
            "rewriter must not touch ordinary file writes"
        );
    }

    #[test]
    fn ignores_when_caller_unset() {
        let original = br#"{"to":"agent-b"}"#;
        let out = maybe_stamp_chat_envelope("/proc/p1/chat-with-me", None, original);
        assert!(
            out.is_none(),
            "kernel-internal writes (no agent_id) walk through unmodified"
        );
    }

    #[test]
    fn ignores_when_caller_empty_string() {
        let original = br#"{"to":"agent-b"}"#;
        let out = maybe_stamp_chat_envelope("/proc/p1/chat-with-me", Some(""), original);
        assert!(out.is_none());
    }

    #[test]
    fn ignores_non_json_content() {
        let original = b"plain text body, not an envelope";
        let out = maybe_stamp_chat_envelope("/proc/p1/chat-with-me", Some("agent-a"), original);
        assert!(
            out.is_none(),
            "non-JSON content is forwarded untouched — receiver decides"
        );
    }

    #[test]
    fn ignores_json_array_top_level() {
        // Stamping is defined for envelope objects; anything else is left
        // alone so the kernel doesn't accidentally corrupt valid wire
        // formats it doesn't know about.
        let original = br#"["msg1","msg2"]"#;
        let out = maybe_stamp_chat_envelope("/proc/p1/chat-with-me", Some("agent-a"), original);
        assert!(out.is_none());
    }
}
