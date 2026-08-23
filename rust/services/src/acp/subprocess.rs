//! ACP subprocess bridge — translates an [`AgentConfig`] into the argv /
//! env / fd-path layout the generic [`HostedSubprocess`] host expects.
//!
//! The generic subprocess host ([`subprocess`]) knows nothing
//! about ACP; this thin module owns the ACP-specific translation
//! (`AgentConfig` → argv, Electron/npm env sanitising, the acp
//! `/{zone}/proc/{pid}/fd/{n}` path scheme) and delegates the actual
//! launch + DT_PIPE wiring to [`HostedSubprocess::spawn_from_argv`].
//!
//! Unix-only (matches the generic host + the kernel stdio-pipe support).

#![allow(dead_code)]

use std::collections::HashMap;
use std::path::Path;

use super::agent_config::AgentConfig;
use super::paths;
use kernel::kernel::syscall::KernelSyscall;
use subprocess::{HostedSubprocess, SubprocessError};

/// Env vars stripped before spawning agents (mirrors AionUi
/// `prepareCleanEnv` and the Python `_ENV_STRIP_KEYS`). Prevents
/// Electron / npm pollution from leaking into the CLI.
const ENV_STRIP_KEYS: &[&str] = &["NODE_OPTIONS", "NODE_INSPECT", "NODE_DEBUG", "CLAUDECODE"];
const ENV_STRIP_PREFIXES: &[&str] = &["npm_"];

/// Build the subprocess argv for ACP mode.
///
/// `npx_package` wraps the binary in `npx --yes --prefer-offline`
/// (matches the Python `_build_acp_command`). Otherwise the binary is
/// `cfg.command` directly. `cfg.acp_args` follows. `cfg.extra_args`
/// is intentionally ignored — those are for the non-ACP one-shot
/// invocation path that doesn't apply here.
pub(crate) fn build_argv(cfg: &AgentConfig) -> Vec<String> {
    if let Some(pkg) = cfg.npx_package.as_deref() {
        let mut out = vec![
            "npx".to_string(),
            "--yes".to_string(),
            "--prefer-offline".to_string(),
            pkg.to_string(),
        ];
        out.extend(cfg.acp_args.iter().cloned());
        return out;
    }
    let mut out = vec![cfg.command.clone()];
    out.extend(cfg.acp_args.iter().cloned());
    out
}

/// Return a sanitised env (mirror of Python `_prepare_clean_env`).
/// Strips Electron / npm pollution from the inherited environment,
/// then overlays `extra` (per-agent overrides from `AgentConfig.env`).
pub(crate) fn prepare_clean_env(extra: &HashMap<String, String>) -> HashMap<String, String> {
    let mut env: HashMap<String, String> = std::env::vars()
        .filter(|(k, _)| {
            if ENV_STRIP_KEYS.contains(&k.as_str()) {
                return false;
            }
            !ENV_STRIP_PREFIXES.iter().any(|p| k.starts_with(p))
        })
        .collect();
    for (k, v) in extra {
        env.insert(k.clone(), v.clone());
    }
    env
}

/// Spawn the agent CLI for `cfg` under `cwd` and register its stdio as
/// DT_PIPEs at `/{zone}/proc/{pid}/fd/{0,1,2}`. Thin ACP-config wrapper
/// over [`HostedSubprocess::spawn_from_argv`]: it computes the argv
/// (`build_argv`), the sanitised inherited env (`prepare_clean_env` —
/// keeps PATH so a bare command name resolves), and the acp fd-path
/// scheme, then delegates the launch + fd wiring to the generic host.
pub(crate) async fn spawn_acp<K: KernelSyscall>(
    cfg: &AgentConfig,
    cwd: &Path,
    kernel: &K,
    zone: &str,
    pid: &str,
) -> Result<HostedSubprocess, SubprocessError> {
    let argv = build_argv(cfg);
    let env = prepare_clean_env(&cfg.env);
    let fd_paths = [
        paths::proc_fd(zone, pid, 0),
        paths::proc_fd(zone, pid, 1),
        paths::proc_fd(zone, pid, 2),
    ];
    HostedSubprocess::spawn_from_argv(argv, env, cwd, kernel, fd_paths).await
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cfg(npx: Option<&str>, env: &[(&str, &str)]) -> AgentConfig {
        AgentConfig {
            agent_id: "test".to_string(),
            name: "Test".to_string(),
            command: "claude".to_string(),
            prompt_flag: "-p".to_string(),
            default_system_prompt: None,
            extra_args: vec!["--ignored-by-acp-mode".to_string()],
            env: env
                .iter()
                .map(|(k, v)| (k.to_string(), v.to_string()))
                .collect(),
            npx_package: npx.map(str::to_string),
            acp_args: vec!["--experimental-acp".to_string(), "--json".to_string()],
            enabled: true,
        }
    }

    #[test]
    fn build_argv_uses_command_when_no_npx() {
        let v = build_argv(&cfg(None, &[]));
        assert_eq!(
            v,
            vec![
                "claude".to_string(),
                "--experimental-acp".to_string(),
                "--json".to_string(),
            ]
        );
    }

    #[test]
    fn build_argv_wraps_npx_package() {
        let v = build_argv(&cfg(Some("@anthropic-ai/claude-code"), &[]));
        assert_eq!(
            v,
            vec![
                "npx".to_string(),
                "--yes".to_string(),
                "--prefer-offline".to_string(),
                "@anthropic-ai/claude-code".to_string(),
                "--experimental-acp".to_string(),
                "--json".to_string(),
            ]
        );
    }

    #[test]
    fn build_argv_ignores_extra_args() {
        // ACP path uses acp_args only; extra_args belongs to the
        // legacy one-shot prompt path.
        let v = build_argv(&cfg(None, &[]));
        assert!(!v.contains(&"--ignored-by-acp-mode".to_string()));
    }

    #[test]
    fn prepare_clean_env_strips_electron_keys() {
        // SAFETY: tests run in-process; we restore the env after.
        let saved = std::env::var("NODE_OPTIONS").ok();
        unsafe {
            std::env::set_var("NODE_OPTIONS", "--inspect");
        }
        let env = prepare_clean_env(&HashMap::new());
        assert!(!env.contains_key("NODE_OPTIONS"));
        unsafe {
            match saved {
                Some(v) => std::env::set_var("NODE_OPTIONS", v),
                None => std::env::remove_var("NODE_OPTIONS"),
            }
        }
    }

    #[test]
    fn prepare_clean_env_strips_npm_prefix() {
        let saved = std::env::var("npm_config_loglevel").ok();
        unsafe {
            std::env::set_var("npm_config_loglevel", "info");
        }
        let env = prepare_clean_env(&HashMap::new());
        assert!(!env.contains_key("npm_config_loglevel"));
        unsafe {
            match saved {
                Some(v) => std::env::set_var("npm_config_loglevel", v),
                None => std::env::remove_var("npm_config_loglevel"),
            }
        }
    }

    #[test]
    fn prepare_clean_env_overlays_extras() {
        let extra = HashMap::from([
            ("ANTHROPIC_API_KEY".to_string(), "sk-test".to_string()),
            ("PATH".to_string(), "/agent/bin".to_string()),
        ]);
        let env = prepare_clean_env(&extra);
        assert_eq!(env.get("ANTHROPIC_API_KEY"), Some(&"sk-test".to_string()));
        // Overlay wins over inherited PATH.
        assert_eq!(env.get("PATH"), Some(&"/agent/bin".to_string()));
    }
}
