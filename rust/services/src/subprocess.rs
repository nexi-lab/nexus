//! `HostedSubprocess` — a generic hosted OS subprocess whose three
//! stdio fds are surfaced inside VFS as stdio-backed `DT_PIPE`s.
//!
//! This is a service-tier PRIMITIVE, not tied to any one caller: the
//! ACP one-shot path ([`crate::acp`]) and the managed-agent raw
//! control-plane path ([`crate::managed_agent`]) both build on it. It
//! knows nothing about ACP framing, agent configs, or session
//! lifecycle — it launches an argv, wires the fds, and exposes the
//! handle. Caller-specific translation (an `AgentConfig` → argv, or an
//! embedder spawn-spec → argv) lives with the caller.
//!
//! Lifecycle (success path):
//!
//!   1. [`HostedSubprocess::spawn_from_argv`] — launch `argv[0]` with
//!      `argv[1..]` under `cwd`/`env`, all three stdio fds piped; take
//!      ownership of the parent-side handles, dup each and hand the
//!      duplicate to the kernel as a stdio-backed `DT_PIPE` at the
//!      caller-supplied `fd_paths`.
//!   2. Traffic flows through the `DT_PIPE` (kernel-side fds): a driver
//!      reads/writes the VFS paths, OR takes the parent-side handles via
//!      [`HostedSubprocess::take_stdio_for_connection`] to drive them
//!      directly through `AsyncRead` / `AsyncWrite`.
//!   3. [`HostedSubprocess::unregister_pipes`] — `sys_unlink` each path
//!      so the kernel-side `StdioPipeBackend` drops + closes its dup'd
//!      fd, then drop the parent-side handles so the OS pipe collapses
//!      and the subprocess sees EOF on stdin / read returns 0 on
//!      stdout/stderr.
//!   4. [`HostedSubprocess::wait`] — block until the child exits;
//!      returns the exit code.
//!   5. [`HostedSubprocess::kill`] — best-effort SIGKILL if it didn't
//!      exit.
//!
//! Owned-fd contract: every parent-side stdio fd has exactly two live
//! handles — the one this struct holds and the `StdioPipeBackend` the
//! kernel holds (created from a `dup`). Both close independently; the OS
//! pipe only collapses when both are gone, which is how we deliver EOF
//! to the subprocess.
//!
//! Unix-only (gated at the `mod` declaration): the module `dup(2)`s raw
//! fds and depends on `#[cfg(unix)]` stdio-pipe kernel support.

#![allow(dead_code)]

use std::collections::HashMap;
use std::os::fd::AsRawFd;
use std::path::Path;
use std::process::Stdio;

use tokio::process::{Child, ChildStderr, ChildStdin, ChildStdout, Command};

use kernel::kernel::syscall::KernelSyscall;
use kernel::kernel::{KernelError, OperationContext};

const PIPE_CAPACITY: usize = 1 << 20;

/// Owned subprocess + the parent-side stdio handles the kernel got
/// dup'd copies of. The tokio types are kept here (rather than raw
/// `OwnedFd`) so a driver that takes them via
/// [`Self::take_stdio_for_connection`] can drive them through
/// `AsyncRead` / `AsyncWrite` directly. Drop closes everything still
/// open; tokio's `kill_on_drop(true)` reaps the child process itself.
pub(crate) struct HostedSubprocess {
    child: Child,
    /// Parent-side write end of the subprocess stdin pipe. `Some` until
    /// `take_stdio_for_connection` hands it off (or `unregister_pipes`
    /// drops it to deliver EOF).
    stdin: Option<ChildStdin>,
    /// Parent-side read end of the subprocess stdout pipe.
    stdout: Option<ChildStdout>,
    /// Parent-side read end of the subprocess stderr pipe.
    stderr: Option<ChildStderr>,
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

impl HostedSubprocess {
    /// Launch `argv[0]` with `argv[1..]` under `cwd`, register the three
    /// stdio fds as `DT_PIPE`s at `fd_paths` (`[stdin, stdout, stderr]`),
    /// and return the live handle.
    ///
    /// The caller owns the path scheme: acp uses
    /// `/{zone}/proc/{pid}/fd/{n}`; the managed-agent raw path uses the
    /// session's zone-free `/proc/{pid}/fd/{n}` subtree.
    ///
    /// `env` is used VERBATIM under `env_clear()` — only these vars reach
    /// the child, so the caller must include everything the binary needs
    /// (notably `PATH` for a bare command name).
    ///
    /// Failure modes:
    ///   * spawn fails — `SubprocessError::Spawn`. No `DT_PIPE`s created.
    ///   * register fails partway — already-registered pipes are unlinked
    ///     before returning so we don't leak `DT_PIPE` entries.
    pub(crate) async fn spawn_from_argv<K: KernelSyscall>(
        argv: Vec<String>,
        env: HashMap<String, String>,
        cwd: &Path,
        kernel: &K,
        fd_paths: [String; 3],
    ) -> Result<Self, SubprocessError> {
        let [stdin_path, stdout_path, stderr_path] = fd_paths;
        // Parent-side handles are kept so a driver can drive them through
        // AsyncRead / AsyncWrite; we dup the raw fds for the kernel's
        // stdio-backed DT_PIPE below.
        let (child, stdin, stdout, stderr) = spawn_child_piped(argv, env, cwd)?;

        // Register stdin (kernel writes into subprocess stdin).
        if let Err(e) = register_stdio_pipe(
            kernel,
            &stdin_path,
            /* read_fd */ -1,
            dup_raw(stdin.as_raw_fd())?,
        ) {
            return Err(SubprocessError::Register(e));
        }
        // Register stdout (kernel reads from subprocess stdout).
        if let Err(e) = register_stdio_pipe(
            kernel,
            &stdout_path,
            dup_raw(stdout.as_raw_fd())?,
            /* write_fd */ -1,
        ) {
            let _ = unlink_quiet(kernel, &stdin_path);
            return Err(SubprocessError::Register(e));
        }
        // Register stderr.
        if let Err(e) = register_stdio_pipe(
            kernel,
            &stderr_path,
            dup_raw(stderr.as_raw_fd())?,
            /* write_fd */ -1,
        ) {
            let _ = unlink_quiet(kernel, &stdin_path);
            let _ = unlink_quiet(kernel, &stdout_path);
            return Err(SubprocessError::Register(e));
        }

        Ok(Self {
            child,
            stdin: Some(stdin),
            stdout: Some(stdout),
            stderr: Some(stderr),
            stdin_path,
            stdout_path,
            stderr_path,
        })
    }

    /// Spawn `argv[0]` with `argv[1..]` under `cwd`/`env`, all three
    /// stdio piped, WITHOUT registering any VFS `DT_PIPE`s. The caller
    /// drives the parent-side handles directly (via
    /// [`Self::take_stdio_for_connection`]) — e.g. the managed-agent
    /// control plane pumps them into node-local memory `DT_STREAM`s for a
    /// raw-byte tunnel. `unregister_pipes` is a no-op here (no paths).
    ///
    /// `env` is used VERBATIM under `env_clear()` (see
    /// [`Self::spawn_from_argv`]).
    pub(crate) async fn spawn_no_pipes(
        argv: Vec<String>,
        env: HashMap<String, String>,
        cwd: &Path,
    ) -> Result<Self, SubprocessError> {
        let (child, stdin, stdout, stderr) = spawn_child_piped(argv, env, cwd)?;
        Ok(Self {
            child,
            stdin: Some(stdin),
            stdout: Some(stdout),
            stderr: Some(stderr),
            stdin_path: String::new(),
            stdout_path: String::new(),
            stderr_path: String::new(),
        })
    }

    /// The child's OS pid, or `None` once it has been reaped. The
    /// managed-agent raw-spawn control plane surfaces this to the
    /// embedder (pid-bound auth-proxy bookkeeping) while the durable
    /// session handle stays the synthetic AgentRegistry pid — the two
    /// identities are deliberately separate (#195 aligned the *agent*
    /// pid with the OS host_pid, but the managed *session* id is its
    /// own thing).
    pub(crate) fn os_pid(&self) -> Option<u32> {
        self.child.id()
    }

    /// Move the parent-side stdio handles out so a driver can wrap them
    /// as `AsyncRead` / `AsyncWrite`. After this call the kernel-side
    /// `DT_PIPE`s (created in `spawn_from_argv`) remain registered;
    /// `unregister_pipes` is still required for teardown.
    pub(crate) fn take_stdio_for_connection(
        &mut self,
    ) -> Result<(ChildStdin, ChildStdout, ChildStderr), SubprocessError> {
        let stdin = self
            .stdin
            .take()
            .ok_or_else(|| SubprocessError::Io("stdin already taken".into()))?;
        let stdout = self
            .stdout
            .take()
            .ok_or_else(|| SubprocessError::Io("stdout already taken".into()))?;
        let stderr = self
            .stderr
            .take()
            .ok_or_else(|| SubprocessError::Io("stderr already taken".into()))?;
        Ok((stdin, stdout, stderr))
    }

    /// Unlink the three `DT_PIPE` entries (closing the kernel-side dup'd
    /// fds) and drop the parent-side stdio handles still held. After
    /// this call the OS pipes collapse and the subprocess sees EOF on
    /// stdin / read returns 0 on stdout / stderr — provided any driver
    /// that took ownership via `take_stdio_for_connection` has also
    /// dropped. Idempotent: subsequent calls are no-ops.
    pub(crate) fn unregister_pipes<K: KernelSyscall>(&mut self, kernel: &K) {
        let _ = unlink_quiet(kernel, &self.stdin_path);
        let _ = unlink_quiet(kernel, &self.stdout_path);
        let _ = unlink_quiet(kernel, &self.stderr_path);
        // Drop any remaining parent-side handles so EOF reaches the
        // child even if take_stdio_for_connection wasn't called.
        self.stdin.take();
        self.stdout.take();
        self.stderr.take();
    }

    /// Best-effort SIGKILL on the child. Safe to call even if the child
    /// has already exited.
    pub(crate) async fn kill(&mut self) {
        let _ = self.child.kill().await;
    }

    /// Wait for the child to exit. Returns the exit code (or 0 on
    /// signal / unknown status, matching the Python service's "no code"
    /// fallback).
    pub(crate) async fn wait(&mut self) -> i32 {
        match self.child.wait().await {
            Ok(status) => status.code().unwrap_or(0),
            Err(_) => -1,
        }
    }
}

// ── Internal helpers ───────────────────────────────────────────────────

/// Launch `argv[0]` with `argv[1..]` under `cwd`/`env`, all three stdio
/// piped with `kill_on_drop(true)`, and take the parent-side handles.
/// Shared spawn core for [`HostedSubprocess::spawn_from_argv`] (which then
/// dups the fds into DT_PIPEs) and [`HostedSubprocess::spawn_no_pipes`]
/// (which pumps the handles into DT_STREAMs). `env` is used VERBATIM under
/// `env_clear()` — the caller must include everything the child needs.
///
/// Must be called from within a tokio runtime context (tokio `Command`).
#[allow(clippy::type_complexity)]
fn spawn_child_piped(
    argv: Vec<String>,
    env: HashMap<String, String>,
    cwd: &Path,
) -> Result<(Child, ChildStdin, ChildStdout, ChildStderr), SubprocessError> {
    if argv.is_empty() {
        return Err(SubprocessError::Spawn("empty argv".into()));
    }
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
    let stdin = child
        .stdin
        .take()
        .ok_or_else(|| SubprocessError::Io("subprocess stdin missing".into()))?;
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| SubprocessError::Io("subprocess stdout missing".into()))?;
    let stderr = child
        .stderr
        .take()
        .ok_or_else(|| SubprocessError::Io("subprocess stderr missing".into()))?;
    Ok((child, stdin, stdout, stderr))
}

/// `dup(2)` the raw fd so the kernel-side StdioPipeBackend holds an
/// independently-closable handle. Original tokio handle keeps its own
/// fd number; both close on Drop without colliding.
fn dup_raw(raw: i32) -> Result<i32, SubprocessError> {
    // SAFETY: libc::dup is the canonical way to duplicate a file
    // descriptor; the returned fd is independently closable.
    let dup = unsafe { libc::dup(raw) };
    if dup < 0 {
        return Err(SubprocessError::Io(format!(
            "dup({raw}): {}",
            std::io::Error::last_os_error()
        )));
    }
    Ok(dup)
}

fn register_stdio_pipe<K: KernelSyscall>(
    kernel: &K,
    path: &str,
    read_fd: i32,
    write_fd: i32,
) -> Result<(), String> {
    // DT_PIPE create via the generic sys_setattr matrix: entry_type=3
    // (DT_PIPE), the "stdio" io_profile, and the subprocess's dup'd
    // read/write fds. The DT_PIPE arm of sys_setattr accepts exactly
    // these params — no dedicated setattr_pipe syscall needed.
    kernel
        .sys_setattr(
            path,
            /* entry_type   */ 3, // DT_PIPE
            /* backend_name */ "",
            /* backend      */ None,
            /* metastore    */ None,
            /* raft_backend */ None,
            /* io_profile   */ "stdio",
            /* zone_id      */ "root",
            /* is_external  */ false,
            /* capacity     */ PIPE_CAPACITY,
            /* read_fd      */ Some(read_fd),
            /* write_fd     */ Some(write_fd),
            /* mime_type       */ None,
            /* modified_at_ms  */ None,
            /* content_id      */ None,
            /* size            */ None,
            /* version         */ None,
            /* created_at_ms   */ None,
            /* link_target     */ None,
            /* source          */ None,
            /* remote_metastore*/ None,
        )
        .map(|_| ())
        .map_err(|e: KernelError| format!("{e:?}"))
}

fn unlink_quiet<K: KernelSyscall>(kernel: &K, path: &str) -> Result<(), KernelError> {
    let ctx = OperationContext::new(
        /* user_id */ "system", /* zone_id */ "root", /* is_admin */ true,
        /* agent_id */ None, /* is_system */ true,
    );
    kernel.sys_unlink(path, &ctx, false).map(|_| ())
}

// Drop semantics: tokio's ChildStdin / ChildStdout / ChildStderr each
// close their own fd, so dropping this struct closes the parent-side OS
// pipe handles still held. The kernel-side StdioPipeBackend keeps its
// dup'd fd alive until `unregister_pipes` runs; if the caller forgot,
// the DT_PIPE entry leaks into the metastore. tokio Command's
// `kill_on_drop(true)` ensures the child process itself is reaped.

#[cfg(test)]
mod tests {
    use super::*;
    use kernel::kernel::Kernel;
    use std::sync::Arc;
    use std::time::Duration;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    fn cat_on_path() -> bool {
        std::process::Command::new("cat")
            .arg("--version")
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .status()
            .map(|s| s.success())
            .unwrap_or(false)
    }

    /// argv + env for spawning POSIX `cat` (echoes stdin→stdout until
    /// EOF). `env` carries PATH so the bare `cat` name resolves under
    /// `env_clear()`.
    fn cat_argv_env() -> (Vec<String>, HashMap<String, String>) {
        let env = HashMap::from([(
            "PATH".to_string(),
            std::env::var("PATH").unwrap_or_else(|_| "/usr/bin:/bin".to_string()),
        )]);
        (vec!["cat".to_string()], env)
    }

    fn fd_paths_for(pid: &str) -> [String; 3] {
        [
            format!("/root/proc/{pid}/fd/0"),
            format!("/root/proc/{pid}/fd/1"),
            format!("/root/proc/{pid}/fd/2"),
        ]
    }

    /// Smoke: spawn cat, write a line to its stdin, read it back from
    /// stdout, drop the connection so the subprocess sees EOF, reap.
    ///
    /// `#[ignore]` because a bare-kernel test environment doesn't have a
    /// metastore mount at `/{zone}/proc/...`, so `unregister_pipes`
    /// can't reach the kernel-side StdioPipeBackend to close its dup'd
    /// fd, the subprocess never sees EOF on stdin, and `wait` hangs. Run
    /// against a fully-wired kernel:
    ///   cargo test subprocess::tests::cat_roundtrip -- --ignored
    /// The roundtrip portion (write -> read -> assert echoed bytes) does
    /// pass; only the EOF / wait teardown trips on the missing mount.
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    #[ignore]
    async fn cat_roundtrip_through_hosted_subprocess() {
        if !cat_on_path() {
            eprintln!("cat not on PATH -- skipping");
            return;
        }
        let kernel = Arc::new(Kernel::new());
        let cwd = std::env::temp_dir();
        let (argv, env) = cat_argv_env();
        let mut sub = HostedSubprocess::spawn_from_argv(
            argv,
            env,
            &cwd,
            kernel.as_ref(),
            fd_paths_for("pid-cat-roundtrip"),
        )
        .await
        .expect("spawn cat");

        let (mut stdin, mut stdout, _stderr) = sub.take_stdio_for_connection().expect("take stdio");

        stdin
            .write_all(b"hello acp\n")
            .await
            .expect("write to cat stdin");
        stdin.flush().await.expect("flush");

        let mut buf = vec![0u8; 64];
        let n = tokio::time::timeout(Duration::from_secs(5), stdout.read(&mut buf))
            .await
            .expect("stdout read timed out")
            .expect("stdout read");
        assert!(n > 0, "expected echoed bytes");
        let echoed = std::str::from_utf8(&buf[..n]).unwrap();
        assert!(echoed.starts_with("hello acp"), "got {echoed:?}");

        drop(stdin);
        drop(stdout);

        sub.unregister_pipes(kernel.as_ref());
        let exit = tokio::time::timeout(Duration::from_secs(5), sub.wait())
            .await
            .expect("wait timed out");
        assert_eq!(exit, 0, "cat should exit 0 on EOF");
    }

    /// Stress the spawn / register / write / read / kill path 10x to
    /// shake out fd leaks + register/unregister ordering bugs. Same
    /// `#[ignore]` rationale as `cat_roundtrip_through_hosted_subprocess`.
    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    #[ignore]
    async fn cat_roundtrip_stress_10x() {
        if !cat_on_path() {
            eprintln!("cat not on PATH -- skipping");
            return;
        }
        let kernel = Arc::new(Kernel::new());
        let cwd = std::env::temp_dir();
        for i in 0..10 {
            let pid = format!("pid-stress-{i}");
            let (argv, env) = cat_argv_env();
            let mut sub = HostedSubprocess::spawn_from_argv(
                argv,
                env,
                &cwd,
                kernel.as_ref(),
                fd_paths_for(&pid),
            )
            .await
            .unwrap_or_else(|e| panic!("spawn iter {i}: {e}"));
            let (mut stdin, mut stdout, _stderr) = sub.take_stdio_for_connection().unwrap();
            let line = format!("iter {i}\n");
            stdin.write_all(line.as_bytes()).await.unwrap();
            stdin.flush().await.unwrap();
            let mut buf = vec![0u8; 64];
            let n = tokio::time::timeout(Duration::from_secs(5), stdout.read(&mut buf))
                .await
                .unwrap_or_else(|_| panic!("read iter {i} timed out"))
                .unwrap();
            let echoed = std::str::from_utf8(&buf[..n]).unwrap();
            assert!(
                echoed.starts_with(&format!("iter {i}")),
                "iter {i}: got {echoed:?}"
            );
            drop(stdin);
            drop(stdout);
            sub.unregister_pipes(kernel.as_ref());
            sub.kill().await;
            let _ = tokio::time::timeout(Duration::from_secs(5), sub.wait()).await;
        }
    }
}
