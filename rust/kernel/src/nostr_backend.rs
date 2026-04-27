//! NostrBackend — bidirectional VFS storage driver over Nostr relays.
//!
//! Mounts an agent / user namespace onto a Nostr identity. Outbound writes
//! become NIP-04 encrypted DMs (kind 4) signed with the local identity key;
//! inbound DMs from the relay subscription are decrypted, written into the
//! local mirror, and emitted as `FileEvent` entries so `sys_watch` callers
//! see them through the same surface as any other VFS write.
//!
//! The backend is the "remote identity" leg of the chat-with-me A2A surface
//! described in the sudowork integration doc §3.5: when an agent name in
//! `/agents/{name}/chat-with-me` is mounted with `NostrBackend`, writes are
//! shipped over Nostr to the npub the mount carries, and the agent_chat
//! aggregator skips the local-broadcast / sys_read merge paths because the
//! relay handles delivery instead.
//!
//! All `ObjectStore` methods return `StorageError::NotSupported` until the
//! relay client lands. The mount-side wiring is intentionally not present —
//! the nexus repo's mount surface only adds the entry once the runtime
//! impl ships, so a misconfigured mount cannot reach this stub at runtime.
//!
//! See `OPEN-ITEMS.md / nostr-backend-driver` for the staged plan: relay
//! client + NIP-04 encrypt/decrypt + FileEvent emission + mount integration.

use crate::backend::{ObjectStore, StorageError, WriteResult};
use crate::kernel::OperationContext;

/// Nostr storage backend — mount carries `npub` (recipient pubkey) plus the
/// local identity key, and a list of relay URLs the client subscribes to.
///
/// Construction is `pub(crate)` because the kernel's `add_mount` surface is
/// the only legitimate entry point; tests live in this module so they can
/// exercise the stub without leaking the constructor.
#[allow(dead_code)] // wired up once relay client lands — keeps the trait shape committed
pub(crate) struct NostrBackend {
    backend_name: String,
    /// Recipient pubkey (`npub` bech32). Outbound DMs target this key.
    recipient_npub: String,
    /// Relay URLs the inbound subscription connects to. One inbound stream
    /// per relay; events are deduplicated by Nostr event id.
    relay_urls: Vec<String>,
}

impl NostrBackend {
    /// Construct the backend without opening relay connections.
    ///
    /// Returns the stub immediately so the mount registration codepath can
    /// remain synchronous; relay dial-out happens lazily on first read or
    /// write once the runtime impl lands.
    #[allow(dead_code)]
    pub(crate) fn new(name: &str, recipient_npub: &str, relay_urls: Vec<String>) -> Self {
        Self {
            backend_name: name.to_string(),
            recipient_npub: recipient_npub.to_string(),
            relay_urls,
        }
    }
}

impl ObjectStore for NostrBackend {
    fn name(&self) -> &str {
        &self.backend_name
    }

    /// Outbound DM publication — NIP-04 encrypt to `recipient_npub`, sign
    /// with the local identity key, broadcast to all `relay_urls`. Returns
    /// the Nostr event id as `content_id` so callers can correlate ACKs.
    ///
    /// Stubbed: returns `NotSupported` until the relay client lands.
    fn write_content(
        &self,
        content: &[u8],
        content_id: &str,
        _ctx: &OperationContext,
        offset: u64,
    ) -> Result<WriteResult, StorageError> {
        let _ = (content, content_id, offset);
        let _ = (&self.recipient_npub, &self.relay_urls);
        Err(StorageError::NotSupported(
            "NostrBackend.write_content awaits relay client — see OPEN-ITEMS / nostr-backend-driver",
        ))
    }

    /// Inbound DM lookup. The runtime impl reads from the local mirror that
    /// the relay subscription writes into — direct relay reads would race
    /// against `FileEvent` emission and starve `sys_watch` callers. Stubbed
    /// for now.
    fn read_content(
        &self,
        _content_id: &str,
        backend_path: &str,
        _ctx: &OperationContext,
    ) -> Result<Vec<u8>, StorageError> {
        let _ = backend_path;
        Err(StorageError::NotSupported(
            "NostrBackend.read_content awaits relay client — see OPEN-ITEMS / nostr-backend-driver",
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn backend() -> NostrBackend {
        NostrBackend::new(
            "nostr-test",
            "npub1exampleexampleexampleexample",
            vec!["wss://relay.damus.io".to_string()],
        )
    }

    #[test]
    fn name_returns_backend_name() {
        let b = backend();
        assert_eq!(b.name(), "nostr-test");
    }

    #[test]
    fn write_content_returns_not_supported_until_relay_wired() {
        let b = backend();
        let ctx = OperationContext::new("test", "root", false, None, false);
        let result = b.write_content(b"hi", "", &ctx, 0);
        let err = match result {
            Ok(_) => panic!("stub must surface NotSupported"),
            Err(e) => e,
        };
        match err {
            StorageError::NotSupported(msg) => {
                assert!(msg.contains("relay client"));
            }
            other => panic!("expected NotSupported, got {other:?}"),
        }
    }

    #[test]
    fn read_content_returns_not_supported_until_relay_wired() {
        let b = backend();
        let ctx = OperationContext::new("test", "root", false, None, false);
        let err = b
            .read_content("", "", &ctx)
            .expect_err("stub must surface NotSupported");
        match err {
            StorageError::NotSupported(msg) => {
                assert!(msg.contains("relay client"));
            }
            other => panic!("expected NotSupported, got {other:?}"),
        }
    }
}
