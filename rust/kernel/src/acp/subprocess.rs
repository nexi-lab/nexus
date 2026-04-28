//! `AcpSubprocess` — owns a coding-agent CLI subprocess and the three
//! DT_PIPE registrations that surface its stdio inside VFS.
//!
//! Lifecycle (success path):
//!
//!   1. `AcpSubprocess::spawn(cfg, cwd, kernel, zone, pid)` — build
//!      argv + clean env, launch the CLI with all three stdio fds
//!      piped, take ownership of the parent-side OwnedFds, dup each
//!      and hand the duplicate to the kernel as a stdio-backed
//!      DT_PIPE at `/{zone}/proc/{pid}/fd/{0,1,2}`.
//!   2. ACP traffic flows through the DT_PIPE (kernel-side fds).
//!   3. `unregister_pipes(kernel)` — `sys_unlink` each path so the
//!      kernel-side `StdioPipeBackend` drops + closes its dup'd fd,
//!      then drop the parent-side OwnedFds so the OS pipe collapses
//!      and the subprocess sees EOF on stdin / read returns 0 on
//!      stdout/stderr.
//!   4. `wait()` — block until the child exits; returns the exit code.
//!   5. `kill()` — best-effort SIGKILL on the child if it didn't exit.
//!
//! Owned-fd contract: every parent-side stdio fd has exactly two live
//! handles — the `OwnedFd` this struct holds and the `StdioPipeBackend`
//! the kernel holds (created from a `dup`). Both close independently;
//! the OS pipe only collapses when both are gone, which is how we
//! deliver EOF to the subprocess.
//!
//! Unix-only — the entire module is gated `#[cfg(unix)]` (matches
//! `stdio_pipe.rs`). Windows port lives somewhere else when it's needed.

#![cfg(unix)]
#![allow(dead_code)]

use std::collections::HashMap;
use std::os::fd::{AsRawFd, FromRawFd, IntoRawFd, OwnedFd};
use std::path::Path;
use std::process::Stdio;

use tokio::process::{Child, Command};

use super::agent_config::AgentConfig;
use super::paths;
use crate::kernel::{Kernel, KernelError, OperationContext};

const PIPE_CAPACITY: usize = 1 << 20;

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

/// Owned subprocess + the parent-side OwnedFds the kernel got dup'd
/// copies of. Drop closes everything still open.
pub(crate) struct AcpSubprocess {
    child: Child,
    /// Parent-side write end of the subprocess stdin pipe. `Some`
    /// until `unregister_pipes` runs (then dropped to deliver EOF).
    stdin_fd: Option<OwnedFd>,
    /// Parent-side read end of the subprocess stdout pipe.
    stdout_fd: Option<OwnedFd>,
    /// Parent-side read end of the subprocess stderr pipe.
    stderr_fd: Option<OwnedFd>,
    /// VFS paths the kernel registered the dup'd fds at.
    stdin_path: String,
    stdout_path: String,
    stderr_path: String,
}

#[derive(Debug)]
pub(crate) enum SubprocessError {
    Spawn(String),
    Register(String),
    Io(String),
}

impl std::fmt::Display for SubprocessError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Spawn(m) => write!(f, "spawn: {m}"),
            Self::Register(m) => write!(f, "register pipe: {m}"),
            Self::Io(m) => write!(f, "io: {m}"),
        }
    }
}

impl std::error::Error for SubprocessError {}

impl AcpSubprocess {
    /// Spawn the agent CLI for `cfg` under `cwd`, register all three
    /// stdio fds as DT_PIPEs at `/{zone}/proc/{pid}/fd/{0,1,2}`, and
    /// return the live handle.
    ///
    /// Failure modes:
    ///   * spawn fails — returns `SubprocessError::Spawn`. No DT_PIPEs
    ///     created. `agent_registry.kill(pid, 127)` is the caller's
    ///     responsibility.
    ///   * register fails partway through — already-registered pipes
    ///     are unlinked before returning so we don't leak DT_PIPE
    ///     entries on the failure path.
    pub(crate) async fn spawn(
        cfg: &AgentConfig,
        cwd: &Path,
        kernel: &Kernel,
        zone: &str,
        pid: &str,
    ) -> Result<Self, SubprocessError> {
        let argv = build_argv(cfg);
        let env = prepare_clean_env(&cfg.env);

        let mut cmd = Command::new(&argv[0]);
        cmd.args(&argv[1..])
            .env_clear()
            .envs(env)
            .current_dir(cwd)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true);

        let mut child = cmd
            .spawn()
            .map_err(|e| SubprocessError::Spawn(e.to_string()))?;

        // Take the parent-side stdio handles. tokio's ChildStdin /
        // ChildStdout / ChildStderr each own a unique pipe fd; we
        // convert them to OwnedFd so we can dup them for the kernel.
        let stdin_fd = take_owned_fd(child.stdin.take())?;
        let stdout_fd = take_owned_fd(child.stdout.take())?;
        let stderr_fd = take_owned_fd(child.stderr.take())?;

        let stdin_path = paths::proc_fd(zone, pid, 0);
        let stdout_path = paths::proc_fd(zone, pid, 1);
        let stderr_path = paths::proc_fd(zone, pid, 2);

        // Register stdin (kernel writes into subprocess stdin).
        if let Err(e) = register_stdio_pipe(
            kernel,
            &stdin_path,
            /* read_fd */ -1,
            dup_fd(&stdin_fd)?,
        ) {
            let _ = unlink_quiet(kernel, &stdin_path);
            return Err(SubprocessError::Register(e));
        }
        // Register stdout (kernel reads from subprocess stdout).
        if let Err(e) = register_stdio_pipe(
            kernel,
            &stdout_path,
            dup_fd(&stdout_fd)?,
            /* write_fd */ -1,
        ) {
            let _ = unlink_quiet(kernel, &stdin_path);
            let _ = unlink_quiet(kernel, &stdout_path);
            return Err(SubprocessError::Register(e));
        }
        // Register stderr.
        if let Err(e) = register_stdio_pipe(
            kernel,
            &stderr_path,
            dup_fd(&stderr_fd)?,
            /* write_fd */ -1,
        ) {
            let _ = unlink_quiet(kernel, &stdin_path);
            let _ = unlink_quiet(kernel, &stdout_path);
            let _ = unlink_quiet(kernel, &stderr_path);
            return Err(SubprocessError::Register(e));
        }

        Ok(Self {
            child,
            stdin_fd: Some(stdin_fd),
            stdout_fd: Some(stdout_fd),
            stderr_fd: Some(stderr_fd),
            stdin_path,
            stdout_path,
            stderr_path,
        })
    }

    /// Unlink the three DT_PIPE entries (closing the kernel-side
    /// dup'd fds) and drop the parent-side OwnedFds. After this call
    /// the OS pipes collapse and the subprocess sees EOF on stdin
    /// (drained reads return 0 on stdout/stderr).
    ///
    /// Idempotent: subsequent calls are no-ops.
    pub(crate) fn unregister_pipes(&mut self, kernel: &Kernel) {
        let _ = unlink_quiet(kernel, &self.stdin_path);
        let _ = unlink_quiet(kernel, &self.stdout_path);
        let _ = unlink_quiet(kernel, &self.stderr_path);
        // Drop parent-side OwnedFds so EOF is delivered to the child.
        self.stdin_fd.take();
        self.stdout_fd.take();
        self.stderr_fd.take();
    }

    /// Best-effort SIGKILL on the child. Safe to call even if the
    /// child has already exited.
    pub(crate) async fn kill(&mut self) {
        let _ = self.child.kill().await;
    }

    /// Wait for the child to exit. Returns the exit code (or 0 on
    /// signal / unknown status, matching the Python service's
    /// "no code" fallback).
    pub(crate) async fn wait(&mut self) -> i32 {
        match self.child.wait().await {
            Ok(status) => status.code().unwrap_or(0),
            Err(_) => -1,
        }
    }
}

// ── Internal helpers ───────────────────────────────────────────────────

fn take_owned_fd<T>(handle: Option<T>) -> Result<OwnedFd, SubprocessError>
where
    T: AsRawFd + IntoRawFd,
{
    let h = handle.ok_or_else(|| SubprocessError::Io("subprocess stdio handle missing".into()))?;
    let raw = h.into_raw_fd();
    // SAFETY: into_raw_fd guarantees ownership transfer; the OwnedFd
    // assumes the only live reference to this fd (the subprocess
    // crate gave it up at into_raw_fd).
    Ok(unsafe { OwnedFd::from_raw_fd(raw) })
}

/// `dup(2)` the fd so we can hand a separate handle to the kernel.
/// The original `OwnedFd` keeps ownership of its number.
fn dup_fd(fd: &OwnedFd) -> Result<i32, SubprocessError> {
    // SAFETY: libc::dup is the canonical way to duplicate a file
    // descriptor; the returned fd is independently closable.
    let raw = unsafe { libc::dup(fd.as_raw_fd()) };
    if raw < 0 {
        return Err(SubprocessError::Io(format!(
            "dup({}): {}",
            fd.as_raw_fd(),
            std::io::Error::last_os_error()
        )));
    }
    Ok(raw)
}

fn register_stdio_pipe(
    kernel: &Kernel,
    path: &str,
    read_fd: i32,
    write_fd: i32,
) -> Result<(), String> {
    kernel
        .setattr_pipe(path, PIPE_CAPACITY, "stdio", Some(read_fd), Some(write_fd))
        .map(|_| ())
        .map_err(|e: KernelError| format!("{e:?}"))
}

fn unlink_quiet(kernel: &Kernel, path: &str) -> Result<(), KernelError> {
    let ctx = OperationContext::new(
        /* user_id */ "system", /* zone_id */ "root", /* is_admin */ true,
        /* agent_id */ None, /* is_system */ true,
    );
    kernel.sys_unlink(path, &ctx).map(|_| ())
}

impl Drop for AcpSubprocess {
    fn drop(&mut self) {
        // OwnedFds drop here — closes parent-side fds. The kernel-
        // side StdioPipeBackend keeps its dup'd fd alive until
        // `unregister_pipes` runs; if the caller forgot, the
        // DT_PIPE entry leaks into the metastore. tokio Command's
        // `kill_on_drop(true)` ensures the child process itself is
        // reaped.
    }
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
