use crate::model::PrincipalRef;
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::fmt::Write;

#[derive(Debug, PartialEq, Eq, thiserror::Error)]
#[error("normalized product mutations must contain integer numbers")]
pub struct NonIntegerMutation;

/// Hash a normalized mutation, including its explicit optional defaults.
/// Current product mutations contain strings, integers, booleans and nulls.
/// Reject floats rather than silently changing an existing request's hash.
pub fn request_hash(normalized_payload: &Value) -> Result<String, NonIntegerMutation> {
    let mut canonical = Vec::new();
    write_canonical(normalized_payload, &mut canonical)?;
    Ok(format!("{:x}", Sha256::digest(canonical)))
}

fn write_canonical(value: &Value, output: &mut Vec<u8>) -> Result<(), NonIntegerMutation> {
    match value {
        Value::Object(object) => {
            output.push(b'{');
            let mut keys: Vec<_> = object.keys().collect();
            keys.sort_unstable();
            for (index, key) in keys.into_iter().enumerate() {
                if index != 0 {
                    output.push(b',');
                }
                serde_json::to_writer(&mut *output, key).expect("serialize a JSON object key");
                output.push(b':');
                write_canonical(&object[key], output)?;
            }
            output.push(b'}');
        }
        Value::Array(items) => {
            output.push(b'[');
            for (index, item) in items.iter().enumerate() {
                if index != 0 {
                    output.push(b',');
                }
                write_canonical(item, output)?;
            }
            output.push(b']');
        }
        Value::Number(number) if !number.is_i64() && !number.is_u64() => {
            return Err(NonIntegerMutation);
        }
        _ => serde_json::to_writer(output, value).expect("serialize a JSON scalar"),
    }
    Ok(())
}

/// Keep the durable scope encoding stable and qualify every owner by type.
pub fn scope(subject: &PrincipalRef, action: &str, targets: &[&str]) -> String {
    let owner = format!("{}:{}", subject.subject_type.as_str(), subject.subject_id);
    let values = std::iter::once(owner.as_str())
        .chain(std::iter::once(action))
        .chain(targets.iter().copied());
    let mut encoded = String::from("[");
    for (index, value) in values.enumerate() {
        if index != 0 {
            encoded.push_str(", ");
        }
        let quoted = serde_json::to_string(value).expect("serialize a string");
        for character in quoted.chars() {
            if character <= '\u{7e}' {
                encoded.push(character);
            } else {
                let mut units = [0; 2];
                for unit in character.encode_utf16(&mut units) {
                    write!(&mut encoded, "\\u{unit:04x}").expect("append to a string");
                }
            }
        }
    }
    encoded.push(']');
    encoded
}
