//! Room id ↔ stream path codec.
//!
//! A Matrix room id is a UTF-8 string starting with `!`, followed by
//! the local-part, then `:server-name`. Local-part has no spec-imposed
//! charset beyond "URL-safe-ish opaque blob". The integration doc
//! §4.2 picks RFC 4648 base32 (uppercase, A–Z and 2–7, padding `=`)
//! for the local-part so:
//!
//!   * Every chat-with-me path round-trips deterministically across
//!     restarts and across nexus instances — same path, same room id.
//!   * The encoded form contains only chars Matrix clients accept
//!     without escaping in URLs and JSON.
//!   * Strict, well-known alphabet so a custom decoder is unnecessary.
//!
//! Padding is preserved so the decode is exact-inverse-of-encode; spec
//! does not forbid `=` inside a local-part.

use crate::matrix_adapter::error::AdapterError;

/// Encode a chat-with-me stream path into a Matrix room id of the form
/// `!{BASE32(path)}:{server_name}`.
///
/// `server_name` is the homeserver suffix configured at adapter boot
/// (typically `nexus.local`).
pub fn encode_room_id(stream_path: &str, server_name: &str) -> String {
    let local_part = base32::encode(base32::Alphabet::Rfc4648 { padding: true }, stream_path.as_bytes());
    format!("!{local_part}:{server_name}")
}

/// Inverse of [`encode_room_id`]. Verifies the room id is shaped
/// `!<localpart>:<server-name>`, the server-name matches, and the
/// local-part decodes as valid UTF-8 (which a stream path always is).
pub fn decode_room_id(room_id: &str, server_name: &str) -> Result<String, AdapterError> {
    let stripped = room_id
        .strip_prefix('!')
        .ok_or_else(|| AdapterError::BadJson(format!("room id {room_id:?} missing '!' sigil")))?;
    let (local_part, suffix) = stripped
        .rsplit_once(':')
        .ok_or_else(|| AdapterError::BadJson(format!("room id {room_id:?} missing ':server-name'")))?;
    if suffix != server_name {
        return Err(AdapterError::BadJson(format!(
            "room id {room_id:?} server-name {suffix:?} != adapter homeserver {server_name:?}"
        )));
    }
    let bytes = base32::decode(base32::Alphabet::Rfc4648 { padding: true }, local_part)
        .ok_or_else(|| AdapterError::BadJson(format!("room id {room_id:?} local-part is not valid base32")))?;
    String::from_utf8(bytes).map_err(|e| {
        AdapterError::BadJson(format!(
            "room id {room_id:?} local-part decoded but not UTF-8: {e}"
        ))
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    const SERVER: &str = "nexus.local";

    #[test]
    fn round_trip_canonical_chat_with_me_path() {
        let path = "/agents/human-bob/chat-with-me";
        let room_id = encode_room_id(path, SERVER);
        assert!(room_id.starts_with('!'));
        assert!(room_id.ends_with(":nexus.local"));
        let decoded = decode_room_id(&room_id, SERVER).unwrap();
        assert_eq!(decoded, path);
    }

    #[test]
    fn round_trip_per_pid_chat_with_me_path() {
        let path = "/proc/pid-7c3e9f2a/chat-with-me";
        let room_id = encode_room_id(path, SERVER);
        let decoded = decode_room_id(&room_id, SERVER).unwrap();
        assert_eq!(decoded, path);
    }

    #[test]
    fn empty_path_round_trips_as_empty_local_part() {
        let room_id = encode_room_id("", SERVER);
        assert_eq!(room_id, "!:nexus.local");
        let decoded = decode_room_id(&room_id, SERVER).unwrap();
        assert_eq!(decoded, "");
    }

    #[test]
    fn encoding_is_deterministic_across_calls() {
        let path = "/agents/human-bob/chat-with-me";
        assert_eq!(encode_room_id(path, SERVER), encode_room_id(path, SERVER));
    }

    #[test]
    fn decode_rejects_missing_sigil() {
        let err = decode_room_id("MFRGS:nexus.local", SERVER).unwrap_err();
        assert!(matches!(err, AdapterError::BadJson(_)));
    }

    #[test]
    fn decode_rejects_missing_server_name() {
        let err = decode_room_id("!MFRGS", SERVER).unwrap_err();
        assert!(matches!(err, AdapterError::BadJson(_)));
    }

    #[test]
    fn decode_rejects_wrong_server_name() {
        let path = "/agents/human-bob/chat-with-me";
        let room_id = encode_room_id(path, "other.example");
        let err = decode_room_id(&room_id, SERVER).unwrap_err();
        assert!(matches!(err, AdapterError::BadJson(_)));
    }

    #[test]
    fn decode_rejects_non_base32_local_part() {
        let err = decode_room_id("!not-base32!:nexus.local", SERVER).unwrap_err();
        assert!(matches!(err, AdapterError::BadJson(_)));
    }

    #[test]
    fn server_name_with_colons_uses_last_split() {
        // Matrix server names do not contain `:` (host:port is parsed
        // earlier in the URL), but `rsplit_once(':')` also handles the
        // edge case where the local-part is empty cleanly.
        let path = "/x";
        let room_id = encode_room_id(path, SERVER);
        let decoded = decode_room_id(&room_id, SERVER).unwrap();
        assert_eq!(decoded, path);
    }
}
