//! BRICK_NOSTR_RELAY — NIP-01 Nostr relay embedded in nexusd.
//!
//! Events are stored as JSON files at `/nostr/events/{event_id}.json` via
//! the nexus kernel VFS. Signature verification uses secp256k1 Schnorr
//! (BIP-340) via `k256`. New events are broadcast to active WebSocket
//! subscribers via a single global `sys_watch` task + tokio broadcast channel.
//!
//! # Wire protocol (NIP-01)
//!
//! ```text
//! CLIENT → RELAY  ["EVENT", <event>]
//!                 ["REQ",   <sub_id>, <filter>+]
//!                 ["CLOSE", <sub_id>]
//!
//! RELAY  → CLIENT ["EVENT",  <sub_id>, <event>]
//!                 ["EOSE",   <sub_id>]
//!                 ["NOTICE", <message>]
//!                 ["OK",     <event_id>, <bool>, <reason>]
//! ```
//!
//! # VFS layout
//!
//! ```text
//! /nostr/events/{event_id}.json   ← one file per event
//! ```
//!
//! The AuditHook fires automatically on every sys_write, so event ingestion
//! is auditable for free.
//!
//! # Usage
//!
//! ```no_run
//! let handle = start_nostr_relay(Arc::clone(&kernel), "0.0.0.0:7777").await?;
//! println!("Nostr relay listening on {}", handle.addr);
//! // Drop handle to stop the relay.
//! ```

use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::Arc;

use futures_util::{SinkExt, StreamExt};
use k256::schnorr::signature::Verifier;
use k256::schnorr::{Signature, VerifyingKey};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::broadcast;
use tokio_tungstenite::tungstenite::Message;

use kernel::kernel::Kernel;
use kernel::kernel::{KernelError, OperationContext};

// ── Constants ────────────────────────────────────────────────────────────────

const EVENTS_ROOT: &str = "/nostr/events";
/// Broadcast channel capacity — at ~1 KB per event this is ~4 MB worst case.
const BROADCAST_CAP: usize = 4096;
/// Default historical-query limit when the client omits `limit` in REQ.
const DEFAULT_LIMIT: usize = 500;
/// DT_REG file entry type returned by kernel.readdir().
const DT_REG: u8 = 8;

// ── Nostr event ──────────────────────────────────────────────────────────────

/// NIP-01 Nostr event (wire format).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct NostrEvent {
    /// SHA-256 of the canonical serialisation (32-byte hex).
    pub id: String,
    /// x-only secp256k1 public key (32-byte hex).
    pub pubkey: String,
    pub created_at: i64,
    pub kind: u64,
    pub tags: Vec<Vec<String>>,
    pub content: String,
    /// BIP-340 Schnorr signature over `id` (64-byte hex).
    pub sig: String,
}

impl NostrEvent {
    /// Canonical serialisation array used to compute the event ID.
    fn canonical_array(&self) -> serde_json::Value {
        serde_json::json!([
            0,
            self.pubkey,
            self.created_at,
            self.kind,
            self.tags,
            self.content
        ])
    }

    /// Validate: id == sha256(canonical_json) and sig is a valid Schnorr signature.
    pub fn verify(&self) -> Result<(), String> {
        // 1. Verify event ID.
        let id_bytes = hex_decode_32(&self.id).map_err(|e| format!("id: {e}"))?;
        let canonical = serde_json::to_string(&self.canonical_array())
            .map_err(|e| format!("serialise: {e}"))?;
        let computed: [u8; 32] = Sha256::digest(canonical.as_bytes()).into();
        if id_bytes != computed {
            return Err("id mismatch".into());
        }

        // 2. Verify Schnorr signature (BIP-340).
        let pubkey_bytes = hex_decode_32(&self.pubkey).map_err(|e| format!("pubkey: {e}"))?;
        let sig_bytes = hex_decode_64(&self.sig).map_err(|e| format!("sig: {e}"))?;

        let vk =
            VerifyingKey::from_bytes(&pubkey_bytes).map_err(|e| format!("pubkey parse: {e}"))?;
        let sig =
            Signature::try_from(sig_bytes.as_slice()).map_err(|e| format!("sig parse: {e}"))?;

        // k256 Schnorr verify: message bytes are passed directly into the
        // BIP-340 challenge hash, not re-hashed. So passing the 32-byte
        // event id is correct for Nostr.
        vk.verify(&id_bytes, &sig)
            .map_err(|_| "signature invalid".to_string())
    }
}

// ── Nostr filter ─────────────────────────────────────────────────────────────

/// NIP-01 subscription filter. All non-None fields are ANDed; values within a
/// single field are ORed (prefix-match for ids/authors, exact for kinds/tags).
#[derive(Debug, Clone, Default, Deserialize)]
pub struct NostrFilter {
    pub ids: Option<Vec<String>>,
    pub authors: Option<Vec<String>>,
    pub kinds: Option<Vec<u64>>,
    pub since: Option<i64>,
    pub until: Option<i64>,
    pub limit: Option<usize>,
    #[serde(rename = "#e")]
    pub e_tags: Option<Vec<String>>,
    #[serde(rename = "#p")]
    pub p_tags: Option<Vec<String>>,
}

impl NostrFilter {
    pub fn matches(&self, event: &NostrEvent) -> bool {
        if let Some(ids) = &self.ids {
            if !ids
                .iter()
                .any(|prefix| event.id.starts_with(prefix.as_str()))
            {
                return false;
            }
        }
        if let Some(authors) = &self.authors {
            if !authors
                .iter()
                .any(|prefix| event.pubkey.starts_with(prefix.as_str()))
            {
                return false;
            }
        }
        if let Some(kinds) = &self.kinds {
            if !kinds.contains(&event.kind) {
                return false;
            }
        }
        if let Some(since) = self.since {
            if event.created_at < since {
                return false;
            }
        }
        if let Some(until) = self.until {
            if event.created_at > until {
                return false;
            }
        }
        if let Some(e_tags) = &self.e_tags {
            let event_e: Vec<&str> = event
                .tags
                .iter()
                .filter(|t| t.first().map(|x| x == "e").unwrap_or(false))
                .filter_map(|t| t.get(1).map(String::as_str))
                .collect();
            if !e_tags.iter().any(|e| event_e.contains(&e.as_str())) {
                return false;
            }
        }
        if let Some(p_tags) = &self.p_tags {
            let event_p: Vec<&str> = event
                .tags
                .iter()
                .filter(|t| t.first().map(|x| x == "p").unwrap_or(false))
                .filter_map(|t| t.get(1).map(String::as_str))
                .collect();
            if !p_tags.iter().any(|p| event_p.contains(&p.as_str())) {
                return false;
            }
        }
        true
    }
}

// ── Error ─────────────────────────────────────────────────────────────────────

#[derive(Debug)]
pub enum NostrRelayError {
    Bind(std::io::Error),
    Kernel(KernelError),
}

impl std::fmt::Display for NostrRelayError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Bind(e) => write!(f, "bind: {e}"),
            Self::Kernel(e) => write!(f, "kernel: {e:?}"),
        }
    }
}

impl std::error::Error for NostrRelayError {}

// ── Handle ────────────────────────────────────────────────────────────────────

/// Returned by `start_nostr_relay`. Drop to stop the relay.
pub struct NostrRelayHandle {
    pub addr: SocketAddr,
    // Held so the relay loop detects drop via oneshot cancellation.
    _shutdown: tokio::sync::oneshot::Sender<()>,
}

// ── Public entry point ────────────────────────────────────────────────────────

/// Start the Nostr relay, listening on `bind_addr` (e.g. `"0.0.0.0:7777"`).
///
/// Spawns:
/// - A TCP accept loop (tokio task).
/// - A global VFS watch task that monitors `/nostr/events/**` and broadcasts
///   newly stored events to all active subscriber connections.
///
/// Returns a handle; drop the handle to stop the relay.
pub async fn start_nostr_relay(
    kernel: Arc<Kernel>,
    bind_addr: &str,
) -> Result<NostrRelayHandle, NostrRelayError> {
    let listener = TcpListener::bind(bind_addr)
        .await
        .map_err(NostrRelayError::Bind)?;
    let addr = listener.local_addr().map_err(NostrRelayError::Bind)?;

    let (shutdown_tx, mut shutdown_rx) = tokio::sync::oneshot::channel::<()>();
    let (broadcast_tx, _) = broadcast::channel::<Arc<NostrEvent>>(BROADCAST_CAP);

    // Global watch task: monitors /nostr/events/** and broadcasts new events.
    {
        let watch_kernel = Arc::clone(&kernel);
        let watch_tx = broadcast_tx.clone();
        tokio::spawn(global_watch_loop(watch_kernel, watch_tx));
    }

    // Accept loop.
    {
        let accept_tx = broadcast_tx.clone();
        tokio::spawn(async move {
            loop {
                tokio::select! {
                    _ = &mut shutdown_rx => break,
                    result = listener.accept() => {
                        match result {
                            Ok((stream, _peer)) => {
                                let conn_kernel = Arc::clone(&kernel);
                                let conn_rx = accept_tx.subscribe();
                                tokio::spawn(handle_connection(stream, conn_kernel, conn_rx));
                            }
                            Err(e) => tracing::warn!(error = %e, "nostr relay: accept failed"),
                        }
                    }
                }
            }
        });
    }

    tracing::info!(addr = %addr, "nostr relay started");
    Ok(NostrRelayHandle {
        addr,
        _shutdown: shutdown_tx,
    })
}

// ── Global watch loop ─────────────────────────────────────────────────────────

/// Runs forever: blocks on `sys_watch("/nostr/events/**")`, reads newly stored
/// event files, and broadcasts parsed `NostrEvent`s to all connections.
async fn global_watch_loop(kernel: Arc<Kernel>, tx: broadcast::Sender<Arc<NostrEvent>>) {
    let pattern = format!("{EVENTS_ROOT}/{{*}}");
    loop {
        let k = Arc::clone(&kernel);
        let p = pattern.clone();
        let maybe_event = tokio::task::spawn_blocking(move || k.sys_watch(&p, 30_000)).await;

        match maybe_event {
            Ok(Some(file_event)) => {
                let ctx = relay_ctx();
                match kernel.sys_read(file_event.path(), &ctx) {
                    Ok(result) => {
                        if let Some(data) = result.data {
                            match serde_json::from_slice::<NostrEvent>(&data) {
                                Ok(ev) => {
                                    // Ignore send error — no active subscribers is fine.
                                    let _ = tx.send(Arc::new(ev));
                                }
                                Err(e) => {
                                    tracing::warn!(
                                        path = %file_event.path(),
                                        error = %e,
                                        "nostr watch: event parse failed"
                                    );
                                }
                            }
                        }
                    }
                    Err(e) => {
                        tracing::warn!(
                            path = %file_event.path(),
                            error = ?e,
                            "nostr watch: sys_read failed"
                        );
                    }
                }
            }
            Ok(None) => {} // timeout, loop again
            Err(e) => tracing::warn!(error = ?e, "nostr watch: spawn_blocking panicked"),
        }
    }
}

// ── Per-connection handler ────────────────────────────────────────────────────

/// Upgrades a raw TCP stream to WebSocket, then drives the NIP-01 message loop.
async fn handle_connection(
    stream: TcpStream,
    kernel: Arc<Kernel>,
    mut broadcast_rx: broadcast::Receiver<Arc<NostrEvent>>,
) {
    let ws_stream = match tokio_tungstenite::accept_async(stream).await {
        Ok(ws) => ws,
        Err(e) => {
            tracing::warn!(error = %e, "nostr relay: WebSocket handshake failed");
            return;
        }
    };

    let (mut ws_sink, mut ws_source) = ws_stream.split();
    // sub_id → filters for this connection.
    let mut subscriptions: HashMap<String, Vec<NostrFilter>> = HashMap::new();

    loop {
        tokio::select! {
            msg = ws_source.next() => {
                match msg {
                    Some(Ok(Message::Text(text))) => {
                        let replies = handle_client_message(
                            text.as_str(), &kernel, &mut subscriptions
                        );
                        for r in replies {
                            if ws_sink.send(Message::Text(r.into())).await.is_err() {
                                return;
                            }
                        }
                    }
                    Some(Ok(Message::Close(_))) | None => return,
                    Some(Ok(Message::Ping(data))) => {
                        let _ = ws_sink.send(Message::Pong(data)).await;
                    }
                    _ => {}
                }
            }
            ev = broadcast_rx.recv() => {
                match ev {
                    Ok(event) => {
                        for (sub_id, filters) in &subscriptions {
                            if filters.iter().any(|f| f.matches(&event)) {
                                let msg = serde_json::json!(["EVENT", sub_id, &*event]).to_string();
                                if ws_sink.send(Message::Text(msg.into())).await.is_err() {
                                    return;
                                }
                            }
                        }
                    }
                    Err(broadcast::error::RecvError::Lagged(n)) => {
                        tracing::warn!(dropped = n, "nostr relay: broadcast lag on connection");
                    }
                    Err(broadcast::error::RecvError::Closed) => return,
                }
            }
        }
    }
}

// ── NIP-01 message dispatcher ─────────────────────────────────────────────────

/// Parse and dispatch one client message. Returns zero or more reply strings.
fn handle_client_message(
    text: &str,
    kernel: &Arc<Kernel>,
    subscriptions: &mut HashMap<String, Vec<NostrFilter>>,
) -> Vec<String> {
    let msg: serde_json::Value = match serde_json::from_str(text) {
        Ok(v) => v,
        Err(_) => {
            return vec![notice("invalid JSON")];
        }
    };
    let arr = match msg.as_array() {
        Some(a) if !a.is_empty() => a,
        _ => return vec![notice("expected JSON array")],
    };
    let verb = match arr[0].as_str() {
        Some(v) => v,
        None => return vec![notice("first element must be a string")],
    };

    match verb {
        "EVENT" => handle_event(arr, kernel),
        "REQ" => handle_req(arr, kernel, subscriptions),
        "CLOSE" => handle_close(arr, subscriptions),
        other => vec![notice(&format!("unknown verb: {other}"))],
    }
}

fn handle_event(arr: &[serde_json::Value], kernel: &Arc<Kernel>) -> Vec<String> {
    let event: NostrEvent = match arr
        .get(1)
        .and_then(|v| serde_json::from_value(v.clone()).ok())
    {
        Some(e) => e,
        None => return vec![notice("EVENT: invalid event object")],
    };
    let event_id = event.id.clone();

    if let Err(reason) = event.verify() {
        return vec![ok_msg(&event_id, false, &reason)];
    }

    let path = format!("{EVENTS_ROOT}/{}.json", event.id);
    let ctx = relay_ctx();
    let json = match serde_json::to_vec(&event) {
        Ok(j) => j,
        Err(e) => return vec![ok_msg(&event_id, false, &format!("serialise: {e}"))],
    };

    match kernel.sys_write(&path, &ctx, &json, 0) {
        Ok(_) => vec![ok_msg(&event_id, true, "")],
        Err(e) => vec![ok_msg(&event_id, false, &format!("storage: {e:?}"))],
    }
}

fn handle_req(
    arr: &[serde_json::Value],
    kernel: &Arc<Kernel>,
    subscriptions: &mut HashMap<String, Vec<NostrFilter>>,
) -> Vec<String> {
    let sub_id = match arr.get(1).and_then(|v| v.as_str()) {
        Some(s) => s.to_string(),
        None => return vec![notice("REQ: missing subscription id")],
    };
    if arr.len() < 3 {
        return vec![notice("REQ: at least one filter required")];
    }
    let filters: Vec<NostrFilter> = arr[2..]
        .iter()
        .filter_map(|v| serde_json::from_value(v.clone()).ok())
        .collect();
    if filters.is_empty() {
        return vec![notice("REQ: no valid filters")];
    }

    // Historical query — scan existing event files and return matching ones.
    let ctx = relay_ctx();
    let limit = filters
        .iter()
        .filter_map(|f| f.limit)
        .min()
        .unwrap_or(DEFAULT_LIMIT);
    let mut events: Vec<NostrEvent> = Vec::new();

    let children = kernel.readdir(EVENTS_ROOT, "root", true);
    'scan: for (child_path, entry_type) in children {
        if entry_type != DT_REG {
            continue;
        }
        if let Ok(result) = kernel.sys_read(&child_path, &ctx) {
            if let Some(data) = result.data {
                if let Ok(ev) = serde_json::from_slice::<NostrEvent>(&data) {
                    if filters.iter().any(|f| f.matches(&ev)) {
                        events.push(ev);
                        if events.len() >= limit {
                            break 'scan;
                        }
                    }
                }
            }
        }
    }

    // Sort newest-first (NIP-01 convention).
    events.sort_unstable_by(|a, b| b.created_at.cmp(&a.created_at));

    let mut replies: Vec<String> = events
        .into_iter()
        .map(|ev| serde_json::json!(["EVENT", sub_id, ev]).to_string())
        .collect();
    replies.push(serde_json::json!(["EOSE", sub_id]).to_string());

    // Register subscription so the connection handler forwards live events.
    subscriptions.insert(sub_id, filters);

    replies
}

fn handle_close(
    arr: &[serde_json::Value],
    subscriptions: &mut HashMap<String, Vec<NostrFilter>>,
) -> Vec<String> {
    if let Some(sub_id) = arr.get(1).and_then(|v| v.as_str()) {
        subscriptions.remove(sub_id);
    }
    vec![]
}

// ── Helpers ───────────────────────────────────────────────────────────────────

fn relay_ctx() -> OperationContext {
    OperationContext::new("system", "root", true, Some("service:nostr-relay"), true)
}

fn ok_msg(event_id: &str, accepted: bool, reason: &str) -> String {
    serde_json::json!(["OK", event_id, accepted, reason]).to_string()
}

fn notice(msg: &str) -> String {
    serde_json::json!(["NOTICE", msg]).to_string()
}

/// Decode a 64-hex-char string into exactly 32 bytes.
fn hex_decode_32(hex: &str) -> Result<[u8; 32], String> {
    let bytes = hex_decode(hex)?;
    bytes
        .try_into()
        .map_err(|_| format!("expected 32 bytes (64 hex chars), got {}", hex.len() / 2))
}

/// Decode a 128-hex-char string into exactly 64 bytes.
fn hex_decode_64(hex: &str) -> Result<[u8; 64], String> {
    let bytes = hex_decode(hex)?;
    bytes
        .try_into()
        .map_err(|_| format!("expected 64 bytes (128 hex chars), got {}", hex.len() / 2))
}

fn hex_decode(hex: &str) -> Result<Vec<u8>, String> {
    if !hex.len().is_multiple_of(2) {
        return Err("odd hex length".into());
    }
    hex.as_bytes()
        .chunks(2)
        .map(|chunk| {
            let s = std::str::from_utf8(chunk).map_err(|e| e.to_string())?;
            u8::from_str_radix(s, 16).map_err(|e| e.to_string())
        })
        .collect()
}

// ── Unit tests ────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    fn make_event(kind: u64, pubkey: &str) -> NostrEvent {
        NostrEvent {
            id: "a".repeat(64),
            pubkey: pubkey.to_string(),
            created_at: 1_000_000,
            kind,
            tags: vec![],
            content: "hello".into(),
            sig: "b".repeat(128),
        }
    }

    #[test]
    fn filter_matches_kind() {
        let ev = make_event(1, "aa".repeat(32).as_str());
        let f = NostrFilter {
            kinds: Some(vec![1]),
            ..Default::default()
        };
        assert!(f.matches(&ev));
        let f2 = NostrFilter {
            kinds: Some(vec![2]),
            ..Default::default()
        };
        assert!(!f2.matches(&ev));
    }

    #[test]
    fn filter_matches_since_until() {
        let ev = make_event(1, "aa".repeat(32).as_str());
        let f_since = NostrFilter {
            since: Some(999_999),
            ..Default::default()
        };
        assert!(f_since.matches(&ev));
        let f_future = NostrFilter {
            since: Some(2_000_000),
            ..Default::default()
        };
        assert!(!f_future.matches(&ev));
        let f_until = NostrFilter {
            until: Some(2_000_000),
            ..Default::default()
        };
        assert!(f_until.matches(&ev));
        let f_past = NostrFilter {
            until: Some(500_000),
            ..Default::default()
        };
        assert!(!f_past.matches(&ev));
    }

    #[test]
    fn filter_matches_authors_prefix() {
        let pubkey = "abcd".repeat(16); // 64 chars
        let ev = make_event(1, &pubkey);
        let f = NostrFilter {
            authors: Some(vec!["abcd".into()]),
            ..Default::default()
        };
        assert!(f.matches(&ev));
        let f2 = NostrFilter {
            authors: Some(vec!["efgh".into()]),
            ..Default::default()
        };
        assert!(!f2.matches(&ev));
    }

    #[test]
    fn filter_matches_e_tag() {
        let mut ev = make_event(1, "aa".repeat(32).as_str());
        ev.tags = vec![vec!["e".into(), "eventref123".into()]];
        let f = NostrFilter {
            e_tags: Some(vec!["eventref123".into()]),
            ..Default::default()
        };
        assert!(f.matches(&ev));
        let f2 = NostrFilter {
            e_tags: Some(vec!["notpresent".into()]),
            ..Default::default()
        };
        assert!(!f2.matches(&ev));
    }

    #[test]
    fn hex_decode_round_trip() {
        let bytes = [0xde, 0xad, 0xbe, 0xef];
        let hex: String = bytes.iter().map(|b| format!("{b:02x}")).collect();
        let decoded = hex_decode(&hex).unwrap();
        assert_eq!(decoded, bytes);
    }

    #[test]
    fn hex_decode_32_wrong_length() {
        assert!(hex_decode_32("0102").is_err());
        assert!(hex_decode_32(&"ab".repeat(32)).is_ok());
    }

    #[test]
    fn ok_and_notice_format() {
        let ok = ok_msg("deadbeef", true, "");
        let v: serde_json::Value = serde_json::from_str(&ok).unwrap();
        assert_eq!(v[0], "OK");
        assert_eq!(v[2], true);

        let n = notice("test message");
        let v2: serde_json::Value = serde_json::from_str(&n).unwrap();
        assert_eq!(v2[0], "NOTICE");
        assert_eq!(v2[1], "test message");
    }

    #[test]
    fn canonical_array_field_order() {
        let ev = NostrEvent {
            id: "a".repeat(64),
            pubkey: "b".repeat(64),
            created_at: 1_700_000_000,
            kind: 1,
            tags: vec![vec!["e".into(), "ref".into()]],
            content: "hello".into(),
            sig: "c".repeat(128),
        };
        let arr = ev.canonical_array();
        assert_eq!(arr[0], 0);
        assert_eq!(arr[1].as_str().unwrap(), "b".repeat(64));
        assert_eq!(arr[2], 1_700_000_000i64);
        assert_eq!(arr[3], 1u64);
        assert_eq!(arr[5].as_str().unwrap(), "hello");
    }
}
