//! `MailboxStampingHook` — INTERCEPT pre-write hook that rewrites the
//! envelope's `from` field on chat-with-me writes.
//!
//! The kernel side is intentionally thin: this struct exists so the
//! dispatch system has a registered `NativeInterceptHook`, and its
//! `mutating_path_suffix` declaration drives the content-clone bypass
//! at the sys_write call site (only `*/chat-with-me` writes pay the
//! clone). The actual rewriting policy lives in the sibling
//! `mailbox_stamping_policy::maybe_stamp_chat_envelope` — kernel owns
//! "how to be a hook" (dispatch wiring), policy owns "what to rewrite"
//! (envelope schema, identity guarantee).

use super::mailbox_stamping_policy;
use kernel::core::dispatch::{HookContext, HookOutcome, NativeInterceptHook};

/// Path suffix the dispatcher consults to decide when to clone write
/// content into `WriteHookCtx`. Kept as a constant so the suffix
/// declared by the trait method matches the one mailbox stamping
/// itself recognises.
const CHAT_WITH_ME_SUFFIX: &str = "/chat-with-me";

#[allow(dead_code)] // wired up by Kernel::new at boot
pub(crate) struct MailboxStampingHook;

impl MailboxStampingHook {
    #[allow(dead_code)]
    pub(crate) fn new() -> Self {
        Self
    }
}

impl NativeInterceptHook for MailboxStampingHook {
    fn name(&self) -> &str {
        "mailbox_stamping"
    }

    fn mutating_path_suffix(&self) -> Option<&'static str> {
        Some(CHAT_WITH_ME_SUFFIX)
    }

    fn on_pre(&self, ctx: &HookContext) -> Result<HookOutcome, String> {
        let HookContext::Write(c) = ctx else {
            // Non-write contexts never carry mailbox content; ignore.
            return Ok(HookOutcome::Pass);
        };
        // Other accept/reject hooks declared no mutating suffix, so the
        // dispatcher keeps `c.content = vec![]` for them. We only see
        // real bytes when the dispatcher matched OUR suffix and cloned.
        // Defensive empty-content check covers the case where the path
        // matched some other future mutating hook's suffix but not ours.
        if c.content.is_empty() {
            return Ok(HookOutcome::Pass);
        }
        let caller = if c.identity.agent_id.is_empty() {
            None
        } else {
            Some(c.identity.agent_id.as_str())
        };
        match mailbox_stamping_policy::maybe_stamp_chat_envelope(&c.path, caller, &c.content) {
            Some(rewritten) => Ok(HookOutcome::Replace(rewritten.into_owned())),
            None => Ok(HookOutcome::Pass),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use kernel::core::dispatch::{HookIdentity, ReadHookCtx, WriteHookCtx};

    fn write_ctx(path: &str, agent_id: &str, content: Vec<u8>) -> HookContext {
        HookContext::Write(WriteHookCtx {
            path: path.to_string(),
            identity: HookIdentity {
                user_id: "user1".to_string(),
                zone_id: "root".to_string(),
                agent_id: agent_id.to_string(),
                is_admin: false,
            },
            content,
            is_new_file: false,
            content_id: None,
            new_version: 0,
            size_bytes: None,
        })
    }

    #[test]
    fn declares_chat_with_me_suffix() {
        let h = MailboxStampingHook::new();
        assert_eq!(h.mutating_path_suffix(), Some("/chat-with-me"));
    }

    #[test]
    fn replaces_envelope_when_chat_path_with_real_content() {
        let h = MailboxStampingHook::new();
        let ctx = write_ctx(
            "/proc/p1/chat-with-me",
            "agent-real",
            br#"{"to":"agent-b","body":"hi"}"#.to_vec(),
        );
        let outcome = h.on_pre(&ctx).expect("hook must accept");
        match outcome {
            HookOutcome::Replace(bytes) => {
                let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
                assert_eq!(v["from"], "agent-real");
            }
            HookOutcome::Pass => panic!("expected Replace, got Pass"),
        }
    }

    #[test]
    fn passes_when_content_empty_even_on_chat_path() {
        // Defensive: dispatcher can in principle match a different
        // mutating hook's suffix and skip cloning for ours. We must
        // not blow up — empty content means "not for us".
        let h = MailboxStampingHook::new();
        let ctx = write_ctx("/proc/p1/chat-with-me", "agent-a", Vec::new());
        let outcome = h.on_pre(&ctx).unwrap();
        assert!(matches!(outcome, HookOutcome::Pass));
    }

    #[test]
    fn passes_when_caller_agent_id_empty() {
        let h = MailboxStampingHook::new();
        let ctx = write_ctx("/proc/p1/chat-with-me", "", br#"{"to":"agent-b"}"#.to_vec());
        let outcome = h.on_pre(&ctx).unwrap();
        assert!(matches!(outcome, HookOutcome::Pass));
    }

    #[test]
    fn passes_for_non_write_contexts() {
        let h = MailboxStampingHook::new();
        let ctx = HookContext::Read(ReadHookCtx {
            path: "/proc/p1/chat-with-me".to_string(),
            identity: HookIdentity {
                user_id: "user".to_string(),
                zone_id: "root".to_string(),
                agent_id: "agent-a".to_string(),
                is_admin: false,
            },
            content: None,
            content_id: None,
        });
        assert!(matches!(h.on_pre(&ctx).unwrap(), HookOutcome::Pass));
    }

    #[test]
    fn passes_for_non_chat_path_with_content() {
        // The dispatcher only clones when our suffix matches, so this
        // shape shouldn't occur in practice; but if it does, the policy
        // function sees the non-chat path and returns None, which we
        // surface as Pass.
        let h = MailboxStampingHook::new();
        let ctx = write_ctx(
            "/workspace/notes.md",
            "agent-a",
            br#"{"to":"agent-b"}"#.to_vec(),
        );
        let outcome = h.on_pre(&ctx).unwrap();
        assert!(matches!(outcome, HookOutcome::Pass));
    }
}
