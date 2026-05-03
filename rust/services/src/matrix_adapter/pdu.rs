//! PDU envelope ↔ chat envelope translator.
//!
//! Per integration doc §4.2 the adapter translates between Matrix
//! Persistent Data Units (the wire shape Matrix clients send and
//! receive over `/send` and `/sync`) and the chat envelope JSON the
//! kernel persists in chat-with-me DT_STREAMs. Both sides are JSON
//! and the field mapping is small enough to do field-by-field on the
//! hot path:
//!
//! | PDU field            | chat-envelope field            | notes |
//! |----------------------|--------------------------------|-------|
//! | `sender`             | `from`                         | stamped by MailboxStampingHook from OperationContext — adapter cannot forge |
//! | `content.body`       | `body`                         | `m.room.message` text; pass-through |
//! | `content.msgtype`    | `msgtype`                      | `m.text`, `m.image`, ... |
//! | `origin_server_ts`   | `ts_ms`                        | unix ms |
//! | `event_id`           | derived from DT_STREAM offset  | `$offset_{n}:{server_name}` so it's stable cross-restart |
//! | `room_id`            | derived from path              | `room_id::encode_room_id(path, server_name)` |
//! | `unsigned.age`       | computed at send time          | not persisted; computed on read |
//!
//! The adapter NEVER persists `sender` from the PDU body — the kernel
//! re-stamps it through MailboxStampingHook at sys_write time. That
//! is the load-bearing identity guarantee: a Matrix client cannot
//! forge `sender` even if it lies in the JSON body.

use serde_json::{json, Value};

use crate::matrix_adapter::error::AdapterError;
use crate::matrix_adapter::room_id::encode_room_id;

/// Fields the adapter copies from a `PUT /send/...` PDU body into the
/// on-disk chat envelope. Returns the envelope JSON bytes ready to be
/// passed to `kernel.sys_write` — `from` is left absent so
/// MailboxStampingHook stamps it, mailbox_stamping_policy.rs §3.3.
pub fn pdu_send_to_chat_envelope(
    pdu_content: &Value,
    ts_ms: i64,
) -> Result<Vec<u8>, AdapterError> {
    let content_obj = pdu_content
        .as_object()
        .ok_or_else(|| AdapterError::BadJson("PDU `content` must be a JSON object".into()))?;

    let body = content_obj
        .get("body")
        .and_then(|v| v.as_str())
        .ok_or_else(|| AdapterError::BadJson("PDU `content.body` is required".into()))?;
    // Default to `m.text` so plain `{body: "..."}` payloads from
    // simple clients still land. Matrix spec leaves `msgtype`
    // technically optional inside `content`, defaulting to text.
    let msgtype = content_obj
        .get("msgtype")
        .and_then(|v| v.as_str())
        .unwrap_or("m.text");

    let envelope = json!({
        "body": body,
        "msgtype": msgtype,
        "ts_ms": ts_ms,
    });
    serde_json::to_vec(&envelope).map_err(|e| AdapterError::Internal(e.to_string()))
}

/// Build the read-side PDU event the adapter returns through
/// `/messages` / `/sync` for one stream entry. Carries `event_id`
/// derived from the stream offset for cross-restart stability and
/// `room_id` derived from the stream path.
pub fn chat_envelope_to_pdu_event(
    stream_path: &str,
    server_name: &str,
    offset: u64,
    envelope_bytes: &[u8],
) -> Result<Value, AdapterError> {
    let envelope: Value = serde_json::from_slice(envelope_bytes).map_err(|e| {
        AdapterError::Internal(format!(
            "stored chat envelope at offset {offset} is not valid JSON: {e}"
        ))
    })?;
    let envelope_obj = envelope
        .as_object()
        .ok_or_else(|| AdapterError::Internal("stored envelope is not a JSON object".into()))?;

    // `from` is the kernel-stamped identity; bubble it up as
    // `sender`. Empty/missing `from` falls back to a placeholder so
    // older entries written before MailboxStampingHook landed still
    // render with a valid PDU shape.
    let sender = envelope_obj
        .get("from")
        .and_then(|v| v.as_str())
        .unwrap_or("@unknown:unknown");
    let body = envelope_obj
        .get("body")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    let msgtype = envelope_obj
        .get("msgtype")
        .and_then(|v| v.as_str())
        .unwrap_or("m.text");
    let ts_ms = envelope_obj.get("ts_ms").and_then(|v| v.as_i64()).unwrap_or(0);

    let room_id = encode_room_id(stream_path, server_name);
    let event_id = format!("$offset_{offset}:{server_name}");

    Ok(json!({
        "event_id": event_id,
        "room_id": room_id,
        "sender": sender,
        "type": "m.room.message",
        "origin_server_ts": ts_ms,
        "content": {
            "body": body,
            "msgtype": msgtype,
        },
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    const SERVER: &str = "nexus.local";

    #[test]
    fn pdu_send_strips_sender_so_kernel_can_stamp() {
        // Even if the client lies about `sender` in the request body
        // (Matrix `/send` payload is just `content`, but a simple
        // client might smuggle one in), the adapter never lifts it
        // into the on-disk envelope.
        let content = json!({"body": "hi", "msgtype": "m.text"});
        let bytes = pdu_send_to_chat_envelope(&content, 1_700_000_000_000).unwrap();
        let envelope: Value = serde_json::from_slice(&bytes).unwrap();
        assert!(envelope.get("from").is_none(), "from must be absent so kernel hook stamps it");
        assert_eq!(envelope["body"], "hi");
        assert_eq!(envelope["msgtype"], "m.text");
        assert_eq!(envelope["ts_ms"], 1_700_000_000_000_i64);
    }

    #[test]
    fn pdu_send_defaults_missing_msgtype_to_text() {
        let content = json!({"body": "plain"});
        let bytes = pdu_send_to_chat_envelope(&content, 0).unwrap();
        let envelope: Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(envelope["msgtype"], "m.text");
    }

    #[test]
    fn pdu_send_rejects_missing_body() {
        let content = json!({"msgtype": "m.text"});
        let err = pdu_send_to_chat_envelope(&content, 0).unwrap_err();
        assert!(matches!(err, AdapterError::BadJson(_)));
    }

    #[test]
    fn pdu_send_rejects_non_object_content() {
        let content = json!("not an object");
        let err = pdu_send_to_chat_envelope(&content, 0).unwrap_err();
        assert!(matches!(err, AdapterError::BadJson(_)));
    }

    #[test]
    fn read_side_event_carries_offset_derived_event_id() {
        let envelope = serde_json::to_vec(&json!({
            "from": "@ethan:nexus.local",
            "body": "hi",
            "msgtype": "m.text",
            "ts_ms": 17,
        }))
        .unwrap();
        let event = chat_envelope_to_pdu_event(
            "/agents/human-bob/chat-with-me",
            SERVER,
            42,
            &envelope,
        )
        .unwrap();
        assert_eq!(event["event_id"], "$offset_42:nexus.local");
        assert_eq!(event["sender"], "@ethan:nexus.local");
        assert_eq!(event["type"], "m.room.message");
        assert_eq!(event["origin_server_ts"], 17_i64);
        assert_eq!(event["content"]["body"], "hi");
        assert_eq!(event["content"]["msgtype"], "m.text");
        assert_eq!(
            event["room_id"].as_str().unwrap(),
            encode_room_id("/agents/human-bob/chat-with-me", SERVER)
        );
    }

    #[test]
    fn read_side_event_renders_missing_fields_with_safe_defaults() {
        // Older entries pre-stamping have no `from`. Hook is the SSOT
        // for sender once it lands; until then the adapter still
        // renders a valid PDU shape so clients don't crash on
        // historical messages.
        let envelope = serde_json::to_vec(&json!({"body": "older"})).unwrap();
        let event = chat_envelope_to_pdu_event("/x", SERVER, 0, &envelope).unwrap();
        assert_eq!(event["sender"], "@unknown:unknown");
        assert_eq!(event["content"]["body"], "older");
        assert_eq!(event["content"]["msgtype"], "m.text");
        assert_eq!(event["origin_server_ts"], 0_i64);
    }

    #[test]
    fn read_side_event_rejects_corrupt_envelope() {
        let err =
            chat_envelope_to_pdu_event("/x", SERVER, 0, b"not json at all").unwrap_err();
        assert!(matches!(err, AdapterError::Internal(_)));
    }

    #[test]
    fn round_trip_send_then_read_preserves_body_and_msgtype() {
        // The kernel stamps `from` between write and read; emulate
        // that by injecting it into the stored envelope.
        let pdu_content = json!({"body": "ping", "msgtype": "m.text"});
        let mut stored: Value =
            serde_json::from_slice(&pdu_send_to_chat_envelope(&pdu_content, 99).unwrap())
                .unwrap();
        stored["from"] = Value::String("@ethan:nexus.local".into());
        let stored_bytes = serde_json::to_vec(&stored).unwrap();

        let event =
            chat_envelope_to_pdu_event("/proc/p_1/chat-with-me", SERVER, 7, &stored_bytes)
                .unwrap();
        assert_eq!(event["content"]["body"], "ping");
        assert_eq!(event["content"]["msgtype"], "m.text");
        assert_eq!(event["sender"], "@ethan:nexus.local");
        assert_eq!(event["origin_server_ts"], 99_i64);
    }
}
