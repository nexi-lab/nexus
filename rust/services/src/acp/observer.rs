//! `AgentObserver` — accumulator for ACP `session/update` notifications.
//!
//! 1:1 port of `nexus.services.agent_runtime.observer.AgentObserver`.
//! AcpConnection feeds every `session/update` here; on `finish_turn`
//! the observer hands back an `AgentTurnResult` with the joined text,
//! merged usage dict, captured tool calls, and any thinking-stream
//! content.
//!
//! Thread-safety: a single `Mutex<Inner>` guards all state. Designed
//! for one observer per session — the Mutex is only contended between
//! the JsonRpcClient reader task (notification handler) and the
//! application task driving the prompt; both are short critical
//! sections.

#![allow(dead_code)]

use std::collections::HashMap;
use std::sync::Mutex;

use serde_json::{Map, Value};

#[derive(Debug, Default, Clone)]
pub(crate) struct AgentTurnResult {
    pub text: String,
    pub stop_reason: Option<String>,
    pub model: Option<String>,
    pub usage: Map<String, Value>,
    pub num_turns: u32,
    pub tool_calls: Vec<Value>,
    pub thinking: Option<String>,
}

#[derive(Default)]
struct Inner {
    accumulated_text: Vec<String>,
    accumulated_thinking: Vec<String>,
    accumulated_usage: Map<String, Value>,
    num_turns: u32,
    model_name: Option<String>,
    tool_calls: Vec<Value>,
    prompt_active: bool,
}

pub(crate) struct AgentObserver {
    inner: Mutex<Inner>,
}

impl AgentObserver {
    pub(crate) fn new() -> Self {
        Self {
            inner: Mutex::new(Inner::default()),
        }
    }

    /// Reset per-turn accumulators. Call before each `session/prompt`.
    pub(crate) fn reset_turn(&self) {
        let mut s = self.inner.lock().unwrap();
        s.accumulated_text.clear();
        s.accumulated_thinking.clear();
        s.tool_calls.clear();
        s.prompt_active = true;
    }

    /// Finalize the current turn and return the accumulated result.
    pub(crate) fn finish_turn(&self, stop_reason: Option<String>) -> AgentTurnResult {
        let mut s = self.inner.lock().unwrap();
        s.prompt_active = false;
        let text: String = s.accumulated_text.concat();
        let thinking = if s.accumulated_thinking.is_empty() {
            None
        } else {
            Some(s.accumulated_thinking.concat())
        };
        // Python: model = accumulated_usage.pop("model") or self._model_name.
        let model = match s.accumulated_usage.remove("model") {
            Some(Value::String(m)) => Some(m),
            _ => s.model_name.clone(),
        };
        AgentTurnResult {
            text,
            stop_reason,
            model,
            usage: s.accumulated_usage.clone(),
            num_turns: s.num_turns,
            tool_calls: s.tool_calls.clone(),
            thinking,
        }
    }

    /// Process a single `session/update` notification. `update_type`
    /// is the `sessionUpdate` field; `update` is the full notification
    /// payload (so handlers can pull `content`, `usage`, etc.).
    pub(crate) fn observe_update(&self, update_type: &str, update: &Value) {
        let mut s = self.inner.lock().unwrap();
        match update_type {
            "agent_message_chunk" => {
                if !s.prompt_active {
                    return;
                }
                let content = update.get("content").and_then(Value::as_object);
                if let Some(c) = content {
                    if c.get("type").and_then(Value::as_str) == Some("text") {
                        if let Some(t) = c.get("text").and_then(Value::as_str) {
                            s.accumulated_text.push(t.to_string());
                        }
                    }
                }
            }
            "usage_update" => {
                if let Some(usage) = update.get("usage").and_then(Value::as_object) {
                    for (key, val) in usage {
                        match val {
                            Value::Number(n) => {
                                let entry = s
                                    .accumulated_usage
                                    .entry(key.clone())
                                    .or_insert_with(|| Value::Number(0.into()));
                                *entry = sum_numbers(entry, n);
                            }
                            other => {
                                s.accumulated_usage.insert(key.clone(), other.clone());
                            }
                        }
                    }
                }
            }
            "thinking" => {
                if !s.prompt_active {
                    return;
                }
                if let Some(c) = update.get("content").and_then(Value::as_str) {
                    s.accumulated_thinking.push(c.to_string());
                }
            }
            "tool_call" => {
                s.num_turns += 1;
                s.tool_calls.push(update.clone());
            }
            "user_message_chunk" => {
                // History replay during an active prompt — drop the
                // accumulated model text so only the post-replay
                // response survives. Mirror of Python observer.
                if s.prompt_active {
                    s.accumulated_text.clear();
                }
            }
            _ => {}
        }
    }

    pub(crate) fn collected_text(&self) -> String {
        self.inner.lock().unwrap().accumulated_text.concat()
    }

    pub(crate) fn num_turns(&self) -> u32 {
        self.inner.lock().unwrap().num_turns
    }

    pub(crate) fn model_name(&self) -> Option<String> {
        self.inner.lock().unwrap().model_name.clone()
    }

    pub(crate) fn set_model_name(&self, value: Option<String>) {
        self.inner.lock().unwrap().model_name = value;
    }

    /// Reset every field to defaults — matches Python's
    /// `self._observer = AgentObserver()` swap. Used by
    /// `AcpConnection::session_load` after the 200ms replay drain so
    /// `send_prompt` starts on a clean accumulator.
    pub(crate) fn reset_all(&self) {
        let mut s = self.inner.lock().unwrap();
        *s = Inner::default();
    }
}

impl Default for AgentObserver {
    fn default() -> Self {
        Self::new()
    }
}

/// Merge two JSON numbers preserving the integer/float distinction the
/// Python observer maintains via `dict.get(key, 0) + val`.
fn sum_numbers(existing: &Value, addend: &serde_json::Number) -> Value {
    let cur_i = existing.as_i64();
    let cur_f = existing.as_f64();
    let add_i = addend.as_i64();
    let add_f = addend.as_f64();
    match (cur_i, add_i) {
        (Some(a), Some(b)) => Value::Number((a + b).into()),
        _ => {
            let a = cur_f.unwrap_or(0.0);
            let b = add_f.unwrap_or(0.0);
            // serde_json::Number::from_f64 returns None for NaN/Inf —
            // fall back to 0 to mirror Python's silent coercion.
            Value::Number(serde_json::Number::from_f64(a + b).unwrap_or_else(|| 0.into()))
        }
    }
}

// Suppress unused warning for the optional helper.
#[allow(dead_code)]
fn _ensure_hashmap_compiles(_: HashMap<String, Value>) {}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn agent_message_chunk_appends_text_during_prompt() {
        let obs = AgentObserver::new();
        obs.reset_turn();
        obs.observe_update(
            "agent_message_chunk",
            &json!({"content": {"type":"text","text":"hello "}}),
        );
        obs.observe_update(
            "agent_message_chunk",
            &json!({"content": {"type":"text","text":"world"}}),
        );
        let r = obs.finish_turn(Some("end_turn".into()));
        assert_eq!(r.text, "hello world");
        assert_eq!(r.stop_reason.as_deref(), Some("end_turn"));
    }

    #[test]
    fn agent_message_chunk_outside_prompt_is_ignored() {
        let obs = AgentObserver::new();
        // No reset_turn — prompt_active stays false.
        obs.observe_update(
            "agent_message_chunk",
            &json!({"content": {"type":"text","text":"hi"}}),
        );
        let r = obs.finish_turn(None);
        assert_eq!(r.text, "");
    }

    #[test]
    fn usage_update_sums_integer_metrics() {
        let obs = AgentObserver::new();
        obs.reset_turn();
        obs.observe_update("usage_update", &json!({"usage":{"input_tokens": 10}}));
        obs.observe_update(
            "usage_update",
            &json!({"usage":{"input_tokens": 5, "output_tokens": 7}}),
        );
        let r = obs.finish_turn(None);
        assert_eq!(r.usage.get("input_tokens"), Some(&json!(15)));
        assert_eq!(r.usage.get("output_tokens"), Some(&json!(7)));
    }

    #[test]
    fn usage_update_replaces_non_numeric_values() {
        let obs = AgentObserver::new();
        obs.reset_turn();
        obs.observe_update(
            "usage_update",
            &json!({"usage":{"model":"claude-sonnet-4-6"}}),
        );
        // model in usage flows into AgentTurnResult.model (Python parity).
        let r = obs.finish_turn(None);
        assert_eq!(r.model.as_deref(), Some("claude-sonnet-4-6"));
        assert!(r.usage.get("model").is_none());
    }

    #[test]
    fn tool_call_increments_num_turns_and_collects_payload() {
        let obs = AgentObserver::new();
        obs.reset_turn();
        let payload = json!({"sessionUpdate":"tool_call","toolName":"Read","args":{"path":"/x"}});
        obs.observe_update("tool_call", &payload);
        obs.observe_update("tool_call", &payload);
        let r = obs.finish_turn(None);
        assert_eq!(r.num_turns, 2);
        assert_eq!(r.tool_calls.len(), 2);
    }

    #[test]
    fn thinking_appends_string_content() {
        let obs = AgentObserver::new();
        obs.reset_turn();
        obs.observe_update("thinking", &json!({"content":"reasoning..."}));
        obs.observe_update("thinking", &json!({"content":" continued"}));
        let r = obs.finish_turn(None);
        assert_eq!(r.thinking.as_deref(), Some("reasoning... continued"));
    }

    #[test]
    fn user_message_chunk_during_prompt_clears_replay_text() {
        let obs = AgentObserver::new();
        obs.reset_turn();
        // Replay history first.
        obs.observe_update(
            "agent_message_chunk",
            &json!({"content": {"type":"text","text":"old reply"}}),
        );
        obs.observe_update("user_message_chunk", &json!({"content":"new prompt"}));
        // Then real model output.
        obs.observe_update(
            "agent_message_chunk",
            &json!({"content": {"type":"text","text":"new reply"}}),
        );
        let r = obs.finish_turn(None);
        assert_eq!(r.text, "new reply");
    }

    #[test]
    fn unknown_update_type_is_silently_dropped() {
        let obs = AgentObserver::new();
        obs.reset_turn();
        obs.observe_update("never_heard_of_this", &json!({"x":1}));
        let r = obs.finish_turn(None);
        assert_eq!(r.text, "");
        assert_eq!(r.num_turns, 0);
    }

    #[test]
    fn model_name_setter_is_used_when_usage_lacks_model() {
        let obs = AgentObserver::new();
        obs.set_model_name(Some("claude-haiku-4-5".to_string()));
        obs.reset_turn();
        let r = obs.finish_turn(None);
        assert_eq!(r.model.as_deref(), Some("claude-haiku-4-5"));
    }
}
