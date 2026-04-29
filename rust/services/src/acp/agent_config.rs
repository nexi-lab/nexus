//! `AgentConfig` — Rust mirror of `nexus.services.acp.agents.AgentConfig`.
//!
//! Lives as JSON in VFS at `/{zone}/agents/{id}/agent.json`. The Rust
//! `AcpService` reads + parses on every `call_agent` invocation; no
//! caching layer (the file is small and reads are cheap on the kernel
//! VFS path).
//!
//! Field shapes match the Python dataclass 1:1 so a single agent.json
//! file is shared between the Python tooling that authors it and the
//! Rust service that consumes it.

use std::collections::HashMap;

use serde::{Deserialize, Serialize};

/// Configuration for a coding agent CLI (parsed from VFS JSON).
///
/// Defaults match `agents.AgentConfig`: `prompt_flag = "-p"`,
/// `acp_args = ["--experimental-acp"]`, `enabled = true`.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub(crate) struct AgentConfig {
    pub agent_id: String,
    pub name: String,
    pub command: String,
    #[serde(default = "default_prompt_flag")]
    pub prompt_flag: String,
    #[serde(default)]
    pub default_system_prompt: Option<String>,
    #[serde(default)]
    pub extra_args: Vec<String>,
    #[serde(default)]
    pub env: HashMap<String, String>,
    #[serde(default)]
    pub npx_package: Option<String>,
    #[serde(default = "default_acp_args")]
    pub acp_args: Vec<String>,
    #[serde(default = "default_enabled")]
    pub enabled: bool,
}

fn default_prompt_flag() -> String {
    "-p".to_string()
}

fn default_acp_args() -> Vec<String> {
    vec!["--experimental-acp".to_string()]
}

fn default_enabled() -> bool {
    true
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn parses_minimal_payload_with_defaults() {
        let payload = json!({
            "agent_id": "claude",
            "name": "Claude",
            "command": "claude",
        })
        .to_string();
        let cfg: AgentConfig = serde_json::from_str(&payload).unwrap();
        assert_eq!(cfg.agent_id, "claude");
        assert_eq!(cfg.name, "Claude");
        assert_eq!(cfg.command, "claude");
        assert_eq!(cfg.prompt_flag, "-p");
        assert_eq!(cfg.default_system_prompt, None);
        assert!(cfg.extra_args.is_empty());
        assert!(cfg.env.is_empty());
        assert_eq!(cfg.npx_package, None);
        assert_eq!(cfg.acp_args, vec!["--experimental-acp".to_string()]);
        assert!(cfg.enabled);
    }

    #[test]
    fn parses_full_payload() {
        let payload = json!({
            "agent_id": "codex",
            "name": "Codex",
            "command": "codex",
            "prompt_flag": "--prompt",
            "default_system_prompt": "you are codex",
            "extra_args": ["--verbose"],
            "env": {"CODEX_KEY": "secret"},
            "npx_package": "@openai/codex",
            "acp_args": ["--experimental-acp", "--json"],
            "enabled": false,
        })
        .to_string();
        let cfg: AgentConfig = serde_json::from_str(&payload).unwrap();
        assert_eq!(cfg.prompt_flag, "--prompt");
        assert_eq!(cfg.default_system_prompt.as_deref(), Some("you are codex"));
        assert_eq!(cfg.extra_args, vec!["--verbose".to_string()]);
        assert_eq!(cfg.env.get("CODEX_KEY"), Some(&"secret".to_string()));
        assert_eq!(cfg.npx_package.as_deref(), Some("@openai/codex"));
        assert_eq!(
            cfg.acp_args,
            vec!["--experimental-acp".to_string(), "--json".to_string()]
        );
        assert!(!cfg.enabled);
    }

    #[test]
    fn round_trips_through_json() {
        let cfg = AgentConfig {
            agent_id: "claude".to_string(),
            name: "Claude".to_string(),
            command: "claude".to_string(),
            prompt_flag: "-p".to_string(),
            default_system_prompt: Some("hi".to_string()),
            extra_args: vec!["--x".to_string()],
            env: HashMap::from([("K".to_string(), "V".to_string())]),
            npx_package: None,
            acp_args: vec!["--experimental-acp".to_string()],
            enabled: true,
        };
        let bytes = serde_json::to_vec(&cfg).unwrap();
        let back: AgentConfig = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(back.agent_id, cfg.agent_id);
        assert_eq!(back.default_system_prompt, cfg.default_system_prompt);
        assert_eq!(back.env, cfg.env);
    }

    #[test]
    fn rejects_payload_missing_required_field() {
        let payload = json!({"agent_id": "claude", "name": "Claude"}).to_string();
        let err = serde_json::from_str::<AgentConfig>(&payload).unwrap_err();
        assert!(err.to_string().contains("command"));
    }
}
