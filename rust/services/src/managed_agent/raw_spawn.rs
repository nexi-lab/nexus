//! Raw ACP-subprocess control-plane spawner (frozen contract 2026-08-01).
//!
//! When `start_session` is given a `spawn_spec`, nexus launches that
//! subprocess and gives the client a **raw-byte tunnel** to its stdio via
//! three node-local `io_profile="memory"` `DT_STREAM`s at
//! `/proc/{pid}/fd/{0,1,2}`:
//!
//!   * `fd/1` (stdout) / `fd/2` (stderr) — a nexus PUMP reads the child's
//!     handle with tokio async I/O (EAGAIN-correct via the reactor) and
//!     `stream_write_nowait`s the raw bytes; the client reads with
//!     `read_at(offset)` (byte chunks; `WouldBlock`→poll; `Closed`→eof).
//!   * `fd/0` (stdin) — the client appends bytes; a nexus pump reads the
//!     stream and writes the child's stdin.
//!
//! nexus NEVER frames or parses ACP — NDJSON/LSP framing + all protocol
//! logic stay client-side. The stream primitive is the same offset-based,
//! non-destructive log the A2A mailboxes use, so `read_at(offset)` cleanly
//! separates "no data yet" (`WouldBlock`) from "producer gone" (`Closed`
//! → eof), which a raw stdio pipe read cannot.
//!
//! Kernel-concrete: the tunnel drives Kernel-INHERENT stream ops
//! (`stream_write_nowait` / `stream_read_at` / `close_stream` /
//! `destroy_stream`) that are deliberately NOT on the `KernelSyscall`
//! trait (the v2 audit keeps kernel-internal accessors off the
//! service-facing surface — same reason `install` reaches
//! `kernel.agent_registry()` Kernel-specifically). So this is injected
//! into `ManagedAgentService` at install (Kernel-specific) and called by
//! the generic `start_session` through `dyn RawSpawn` — mirroring the
//! `SpawnTask` DI. No syscall-ABI change.

#![cfg(all(unix, feature = "subprocess-host"))]

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use dashmap::DashMap;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::process::ChildStdin;
use tokio::sync::oneshot;

use kernel::core::agents::registry::{AgentRegistry, AgentState};
use kernel::kernel::{Kernel, OperationContext};

use super::{RawSpawn, SpawnHandle, SpawnSpec};
use crate::subprocess::HostedSubprocess;

/// DT_STREAM entry type (mirrors `kernel::meta_store::DT_STREAM`), typed
/// `i32` to match the `sys_setattr` `entry_type` parameter.
const DT_STREAM: i32 = 4;
/// Per-stream ring capacity. ACP traffic is low-volume; 1 MiB is ample.
const STREAM_CAP: usize = 1 << 20;
/// stdin-pump poll interval when the stream has no pending bytes. Bounds
/// client→agent latency without busy-spinning (the stdout path is
/// event-driven, so only stdin polls).
const STDIN_POLL_MS: u64 = 40;
/// Grace after `close_stream` before `destroy_stream`, so the client can
/// drain the last stdout/stderr bytes and observe eof before the streams
/// vanish.
const DRAIN_GRACE: Duration = Duration::from_secs(5);
/// Pump read buffer.
const PUMP_BUF: usize = 8192;

fn system_ctx() -> OperationContext {
    OperationContext::new("system", "root", true, None, true)
}

fn fd_path(pid: &str, n: u8) -> String {
    format!("/proc/{pid}/fd/{n}")
}

/// Register a node-local in-memory `DT_STREAM` at `path`.
fn register_memory_stream(kernel: &Kernel, path: &str) -> Result<(), String> {
    kernel
        .sys_setattr(
            path, /* entry_type   */ DT_STREAM, /* backend_name */ "",
            /* backend      */ None, /* metastore    */ None,
            /* raft_backend */ None, /* io_profile   */ "memory",
            /* zone_id      */ "root", /* is_external  */ false,
            /* capacity     */ STREAM_CAP, /* read_fd      */ None,
            /* write_fd     */ None, /* mime_type       */ None,
            /* modified_at_ms  */ None, /* content_id      */ None,
            /* size            */ None, /* version         */ None,
            /* created_at_ms   */ None, /* link_target     */ None,
            /* source          */ None, /* remote_metastore*/ None,
        )
        .map(|_| ())
        .map_err(|e| format!("{e:?}"))
}

/// Roll back a partial spawn: destroy any created streams + reap the
/// half-planted session so a launch failure surfaces as a clean error
/// rather than leaking a stream or a zombie descriptor.
fn rollback(kernel: &Kernel, registry: &AgentRegistry, paths: &[String], pid: &str) {
    for p in paths {
        let _ = kernel.destroy_stream(p);
    }
    let _ = registry.kill(pid, 127);
}

/// [`SpawnHandle`] for a raw stream tunnel — holds the supervisor's abort
/// signal. `abort()` is idempotent (the oneshot is taken on first fire).
struct RawStreamHandle {
    cancel: parking_lot::Mutex<Option<oneshot::Sender<()>>>,
}

impl SpawnHandle for RawStreamHandle {
    fn abort(&self) {
        if let Some(tx) = self.cancel.lock().take() {
            let _ = tx.send(());
        }
    }
}

/// Kernel-concrete [`RawSpawn`] provider. Constructed at
/// `install_returning` with the same `AgentRegistry` + `spawn_handles`
/// the service and its `on_terminate` observer share.
pub(crate) struct KernelRawSpawn {
    kernel: Arc<Kernel>,
    agent_registry: Arc<AgentRegistry>,
    spawn_handles: Arc<DashMap<String, Box<dyn SpawnHandle>>>,
}

impl KernelRawSpawn {
    pub(crate) fn new(
        kernel: Arc<Kernel>,
        agent_registry: Arc<AgentRegistry>,
        spawn_handles: Arc<DashMap<String, Box<dyn SpawnHandle>>>,
    ) -> Self {
        Self {
            kernel,
            agent_registry,
            spawn_handles,
        }
    }
}

impl RawSpawn for KernelRawSpawn {
    fn spawn(&self, pid: &str, spec: SpawnSpec) -> Result<Option<u32>, String> {
        if spec.cmd.is_empty() {
            let _ = self.agent_registry.kill(pid, 127);
            return Err("spawn_spec.cmd is required".into());
        }
        let mut argv = Vec::with_capacity(1 + spec.args.len());
        argv.push(spec.cmd);
        argv.extend(spec.args);
        let cwd = if spec.cwd.is_empty() {
            PathBuf::from(".")
        } else {
            PathBuf::from(&spec.cwd)
        };

        let stdin_path = fd_path(pid, 0); // client → agent
        let stdout_path = fd_path(pid, 1); // agent → client
        let stderr_path = fd_path(pid, 2); // agent → client
        let paths = [stdin_path.clone(), stdout_path.clone(), stderr_path.clone()];
        let kernel = self.kernel.as_ref();
        let registry = self.agent_registry.as_ref();

        for p in &paths {
            if let Err(e) = register_memory_stream(kernel, p) {
                rollback(kernel, registry, &paths, pid);
                return Err(format!("register stream {p}: {e}"));
            }
        }

        let rt = tokio::runtime::Handle::try_current().map_err(|_| {
            rollback(kernel, registry, &paths, pid);
            "start_session with spawn_spec requires an active tokio runtime".to_string()
        })?;

        // Spawn the subprocess with NO fd-pipes; we pump the parent-side
        // handles into the memory streams.
        let mut sub = rt
            .block_on(HostedSubprocess::spawn_no_pipes(argv, spec.env, &cwd))
            .map_err(|e| {
                rollback(kernel, registry, &paths, pid);
                format!("spawn_spec subprocess launch failed: {e}")
            })?;
        let os_pid = sub.os_pid();
        let (stdin, stdout, stderr) = sub.take_stdio_for_connection().map_err(|e| {
            rollback(kernel, registry, &paths, pid);
            format!("take subprocess stdio: {e}")
        })?;

        // Client can drive the tunnel immediately (no in-process loop).
        if let Err(e) = self.agent_registry.update_state(pid, AgentState::Ready) {
            tracing::warn!(pid = %pid, error = %e, "raw spawn: READY transition rejected");
        }

        // stdout / stderr pumps: child handle → memory stream (async read,
        // EAGAIN-correct). Each closes its OWN stream on EOF (see
        // `pump_out`), so drain-complete → client eof needs no timeout
        // here. stdin pump: memory stream → child stdin.
        rt.spawn(pump_out(
            stdout,
            Arc::clone(&self.kernel),
            stdout_path.clone(),
        ));
        rt.spawn(pump_out(
            stderr,
            Arc::clone(&self.kernel),
            stderr_path.clone(),
        ));
        let (stdin_stop_tx, stdin_stop_rx) = oneshot::channel::<()>();
        rt.spawn(pump_in(
            stdin,
            Arc::clone(&self.kernel),
            stdin_path.clone(),
            stdin_stop_rx,
        ));

        // Supervisor: race the child's exit against the abort signal.
        let (cancel_tx, cancel_rx) = oneshot::channel::<()>();
        let kernel = Arc::clone(&self.kernel);
        let registry = Arc::clone(&self.agent_registry);
        let pid_owned = pid.to_string();
        rt.spawn(async move {
            let mut sub = sub;
            let self_exit = {
                let waited = sub.wait();
                tokio::pin!(waited);
                tokio::select! {
                    code = &mut waited => Some(code),
                    _ = cancel_rx => None,
                }
            };
            let exit_code = match self_exit {
                Some(code) => code,
                None => {
                    sub.kill().await;
                    sub.wait().await
                }
            };
            // Child is dead ⇒ its stdout/stderr write-ends are closed, so
            // each output pump reads to EOF, flushes every last byte to
            // its stream, and closes it (eof) — no timing cutoff here, so
            // the client never loses the tail (e.g. the final ACP
            // response). Stop the stdin pump and reap the session
            // (Terminated → on_terminate tears down the procfs subtree).
            let _ = stdin_stop_tx.send(());
            let _ = registry.kill(&pid_owned, exit_code);
            // Give the client a bounded window to drain the closed streams,
            // then remove them so they don't leak in the registry. (This
            // also unwedges a pump if a grandchild kept a write-end open,
            // so the client still disconnects.)
            tokio::time::sleep(DRAIN_GRACE).await;
            for p in &paths {
                let _ = kernel.destroy_stream(p);
            }
            tracing::info!(pid = %pid_owned, exit_code, "raw ACP stream tunnel closed");
        });

        self.spawn_handles.insert(
            pid.to_string(),
            Box::new(RawStreamHandle {
                cancel: parking_lot::Mutex::new(Some(cancel_tx)),
            }),
        );

        Ok(os_pid)
    }
}

/// Pump a child output handle (stdout/stderr) into `path` as raw bytes.
/// Event-driven via the tokio reactor. On child-side EOF (all bytes
/// drained) it CLOSES the stream, so the client reads every byte and
/// then observes a clean eof — with no timeout/cutoff on the teardown
/// path. Exits on error too (e.g. the stream was destroyed).
async fn pump_out<R>(mut rd: R, kernel: Arc<Kernel>, path: String)
where
    R: AsyncReadExt + Unpin + Send + 'static,
{
    let ctx = system_ctx();
    let mut buf = vec![0u8; PUMP_BUF];
    loop {
        match rd.read(&mut buf).await {
            Ok(0) => break, // child closed this stream — drain complete
            Ok(n) => {
                if kernel.stream_write_nowait(&path, &buf[..n], &ctx).is_err() {
                    return; // stream destroyed / gone — nothing to close
                }
            }
            Err(_) => break,
        }
    }
    // Drain complete: signal eof to the client (read_at → Closed once the
    // client has drained the buffered tail).
    let _ = kernel.close_stream(&path);
}

/// Pump the stdin stream into the child's stdin. Polls `read_at` (nowait)
/// so the async worker is never blocked; stops on the supervisor's signal
/// or a broken child stdin.
async fn pump_in(
    mut wr: ChildStdin,
    kernel: Arc<Kernel>,
    path: String,
    mut stop: oneshot::Receiver<()>,
) {
    let mut offset = 0usize;
    loop {
        match kernel.stream_read_at(&path, offset) {
            Ok(Some((data, next))) => {
                if wr.write_all(&data).await.is_err() || wr.flush().await.is_err() {
                    break; // child stdin closed
                }
                offset = next;
            }
            Ok(None) => {
                tokio::select! {
                    _ = &mut stop => break,
                    _ = tokio::time::sleep(Duration::from_millis(STDIN_POLL_MS)) => {}
                }
            }
            Err(_) => break, // stream closed / destroyed
        }
    }
}
