"""AcpService — stateless coding agent caller via ACP JSON-RPC protocol.

Calls coding agent CLIs (Claude Code, Gemini CLI, Codex, etc.) over the
ACP JSON-RPC 2.0 protocol (stdin/stdout).  Each call is a one-shot
session: spawn → initialize → session/new → session/prompt → disconnect.

AcpService **owns the subprocess** and wraps stdin/stdout in StdioPipeBackend
(kernel PipeBackend).  Agent pipes are registered as DT_PIPEs at
``/{zone}/proc/{pid}/fd/0`` (stdin) and ``fd/1`` (stdout) when PipeManager
is available.  AcpConnection is a pure protocol adapter — no subprocess.

Results are persisted to the VFS at ``/{zone}/proc/{pid}/result`` via
``sys_write``.  NexusFS **must** be bound for all I/O — no metastore fallback.

Bidirectional communication (file reads, permission requests) is handled
automatically by ``AcpConnection``.  File I/O from the agent is routed
through VFS syscalls when ``NexusFS`` is bound (``everything is a file``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.process_types import AgentDescriptor, AgentKind
from nexus.contracts.vfs_paths import agent as agent_paths
from nexus.contracts.vfs_paths import proc as proc_paths
from nexus.core.stdio_pipe import StdioPipeBackend

from .agents import AgentConfig
from .connection import AcpConnection, AcpPromptResult, AcpRpcError, FsReadFn, FsWriteFn

if TYPE_CHECKING:
    from nexus.contracts.types import VFSOperations

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AcpResult:
    """Unified result of a one-shot coding agent call."""

    pid: str
    agent_id: str
    exit_code: int
    response: str  # extracted answer text
    raw_stdout: str  # kept for backward compat (stderr in JSON-RPC mode)
    stderr: str
    timed_out: bool
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class _ActiveAgent:
    """Tracks active agent subprocess + connection for cleanup."""

    conn: AcpConnection
    proc: asyncio.subprocess.Process
    fd0_path: str  # /{zone}/proc/{pid}/fd/0  (stdin)
    fd1_path: str  # /{zone}/proc/{pid}/fd/1  (stdout)
    fd2_path: str  # /{zone}/proc/{pid}/fd/2  (stderr)


class AcpService:
    """Stateless coding agent caller via ACP JSON-RPC.

    Lifecycle:
        1. Look up ``AgentConfig`` by ``agent_id``
        2. Inject system prompt (VFS override > config default)
        3. Build ACP command (binary + acp_args, no prompt in argv)
        4. Register process in ``AgentRegistry`` (spawn → REGISTERED)
        5. Create subprocess, wrap in StdioPipeBackend, register DT_PIPEs
        6. ``AcpConnection`` → initialize → session/new → session/prompt
        7. Disconnect, kill subprocess, destroy DT_PIPEs, → TERMINATED
        8. Map ``AcpPromptResult`` → ``AcpResult`` with unified metadata
        9. Persist result to VFS at ``/{zone}/proc/{pid}/result``
    """

    def __init__(
        self,
        agent_registry: Any,
        zone_id: str = ROOT_ZONE_ID,
    ) -> None:
        self._agent_registry = agent_registry
        self._zone_id = zone_id
        self._nexus_fs: VFSOperations | None = None
        self._connections: dict[str, _ActiveAgent] = {}
        # Agent termination callbacks — invoked with agent_id on kill/disconnect
        # (Issue #3398 decision 2A: permission lease revocation on agent death)
        self._on_terminate_callbacks: list[tuple[str, Any]] = []

    # ------------------------------------------------------------------
    # Public properties (used by AcpRPCService — no private access)
    # ------------------------------------------------------------------

    @property
    def default_zone_id(self) -> str:
        """The default zone ID for this service instance."""
        return self._zone_id

    # ------------------------------------------------------------------
    # Late-binding NexusFS (``everything is a file``)
    # ------------------------------------------------------------------

    def bind_fs(self, nexus_fs: VFSOperations) -> None:
        """Bind NexusFS for VFS-routed file I/O and result persistence.

        Called after NexusFS construction (factory ``_wired.py`` phase).
        """
        self._nexus_fs = nexus_fs
        logger.debug("AcpService: NexusFS bound for VFS-backed file I/O")

    def register_on_terminate(self, callback_id: str, callback: Any) -> None:
        """Register a callback invoked with ``agent_id`` when an agent terminates.

        Used by permission lease table to revoke stale leases on agent
        death (Issue #3398 decision 2A).

        Args:
            callback_id: Unique identifier (for dedup / unregister).
            callback: ``Callable[[str], None]`` — receives the agent_id (pid).
        """
        for cid, _ in self._on_terminate_callbacks:
            if cid == callback_id:
                return
        self._on_terminate_callbacks.append((callback_id, callback))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def call_agent(
        self,
        agent_id: str,
        prompt: str,
        owner_id: str,
        zone_id: str,
        *,
        cwd: str = ".",
        timeout: float = 300.0,
        labels: dict[str, str] | None = None,
        session_id: str | None = None,
    ) -> AcpResult:
        """Call a coding agent via ACP JSON-RPC.

        If *session_id* is provided, resumes the previous session instead
        of creating a new one (``session/load`` vs ``session/new``).
        """
        config = await self._read_agent_config(agent_id, zone_id)
        if config is None:
            raise ValueError(f"Unknown agent_id: {agent_id!r}")

        user_prompt = prompt  # preserve original for history

        # Inject system prompt as first-message prefix (mirrors AionUi)
        system_prompt = (
            await self.get_system_prompt(agent_id, zone_id) or config.default_system_prompt
        )
        enabled_skills = await self.get_enabled_skills(agent_id, zone_id)

        if system_prompt or enabled_skills:
            rules_parts: list[str] = []
            if system_prompt:
                rules_parts.append(system_prompt)
            if enabled_skills:
                skill_lines = []
                for sk in enabled_skills:
                    skill_lines.append(
                        f'<skill name="{sk["name"]}" path="{sk["path"]}">'
                        f"{sk.get('description', '')}</skill>"
                    )
                rules_parts.append(
                    "<enabled-skills>\n" + "\n".join(skill_lines) + "\n</enabled-skills>"
                )
            prompt = (
                "[Assistant Rules - You MUST follow these instructions]\n"
                + "\n".join(rules_parts)
                + f"\n\n[User Request]\n{prompt}"
            )

        # Build ACP command — no prompt in argv, sent via JSON-RPC
        cmd = self._build_acp_command(config)

        # Build clean env (mirrors AionUi prepareCleanEnv)
        env = _prepare_clean_env(config.env)

        # Build labels
        merged_labels = {"agent_id": agent_id, "service": "acp"}
        if labels:
            merged_labels.update(labels)

        # Register in AgentRegistry via spawn (→ REGISTERED directly, #1691).
        desc = self._agent_registry.spawn(
            name=f"acp:{config.name}",
            owner_id=owner_id,
            zone_id=zone_id,
            kind=AgentKind.UNMANAGED,
            labels=merged_labels,
        )
        pid = desc.pid

        # Build VFS-backed file I/O callables when NexusFS is available.
        host_cwd = os.path.abspath(cwd)
        fs_read, fs_write = self._make_fs_callables(host_cwd, zone_id)

        # DT_PIPE paths for VFS visibility
        fd0_path = proc_paths.fd(zone_id, pid, 0)
        fd1_path = proc_paths.fd(zone_id, pid, 1)
        fd2_path = proc_paths.fd(zone_id, pid, 2)

        # Run ACP session
        timed_out = False
        exit_code = -1
        response_text = ""
        stderr_text = ""
        metadata: dict[str, Any] = {}
        proc: asyncio.subprocess.Process | None = None
        conn: AcpConnection | None = None

        try:
            # OS subprocess (was in AcpConnection.spawn())
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )

            # Register DT_PIPEs in kernel — Rust StdioPipeBackend wraps raw fds.
            # Python StdioPipeBackend kept for AcpConnection async I/O (readline).
            stdin_pipe = StdioPipeBackend(reader=None, writer=proc.stdin)
            stdout_pipe = StdioPipeBackend(reader=proc.stdout, writer=None)
            stderr_pipe = StdioPipeBackend(reader=proc.stderr, writer=None)

            if self._nexus_fs is not None:
                try:
                    from nexus.contracts.metadata import DT_PIPE

                    _nx: Any = self._nexus_fs

                    def _fd_from_transport(transport: Any) -> int:
                        """Extract raw fd from asyncio transport."""
                        pipe = transport.get_extra_info("pipe")
                        return pipe.fileno() if pipe is not None else -1

                    # stdin: write-only (parent → child)
                    stdin_wfd = _fd_from_transport(proc.stdin.transport) if proc.stdin else -1
                    # stdout/stderr: read-only (child → parent).
                    # StreamReader exposes ._transport at runtime; getattr avoids
                    # mypy's [attr-defined] since the Python typeshed lists it as private.
                    stdout_rfd = (
                        _fd_from_transport(getattr(proc.stdout, "_transport", None))
                        if proc.stdout
                        else -1
                    )
                    stderr_rfd = (
                        _fd_from_transport(getattr(proc.stderr, "_transport", None))
                        if proc.stderr
                        else -1
                    )

                    _nx.sys_setattr(
                        fd0_path, entry_type=DT_PIPE, io_profile="stdio", write_fd=stdin_wfd
                    )
                    _nx.sys_setattr(
                        fd1_path, entry_type=DT_PIPE, io_profile="stdio", read_fd=stdout_rfd
                    )
                    _nx.sys_setattr(
                        fd2_path, entry_type=DT_PIPE, io_profile="stdio", read_fd=stderr_rfd
                    )
                except Exception as exc:
                    logger.debug("DT_PIPE registration failed (degraded): %s", exc)

            # AcpConnection with PipeBackend (async I/O via readline)
            conn = AcpConnection(
                stdin_pipe=stdin_pipe,
                stdout_pipe=stdout_pipe,
                stderr_pipe=stderr_pipe,
                cwd=cwd,
                fs_read=fs_read,
                fs_write=fs_write,
            )
            conn.start()
            self._connections[pid] = _ActiveAgent(
                conn=conn,
                proc=proc,
                fd0_path=fd0_path,
                fd1_path=fd1_path,
                fd2_path=fd2_path,
            )

            try:
                await conn.initialize(timeout=30.0)
                if session_id:
                    if not conn.supports_load_session:
                        raise ValueError(
                            f"Agent {agent_id!r} does not support session resume "
                            f"(loadSession capability not advertised)"
                        )
                    await conn.session_load(session_id, cwd=cwd, timeout=30.0)
                else:
                    await conn.session_new(cwd=cwd, timeout=30.0)
                prompt_result = await conn.send_prompt(prompt, timeout=timeout)

                response_text = prompt_result.text
                metadata = _build_metadata(prompt_result, conn.num_turns)
                exit_code = 0

            except TimeoutError:
                timed_out = True
                exit_code = -1
                stderr_text = f"Agent timed out after {timeout}s"
                logger.warning(
                    "ACP agent %s (pid=%s) timed out after %.1fs",
                    agent_id,
                    pid,
                    timeout,
                )
            except AcpRpcError as exc:
                exit_code = 1
                stderr_text = f"ACP RPC error: {exc}"
                logger.error("ACP agent %s (pid=%s) RPC error: %s", agent_id, pid, exc)

        except FileNotFoundError:
            exit_code = 127
            stderr_text = f"Command not found: {config.command}"
            logger.error("ACP agent %s: command not found: %s", agent_id, config.command)
        except Exception as exc:
            exit_code = -1
            stderr_text = str(exc)
            logger.error("ACP agent %s (pid=%s) failed: %s", agent_id, pid, exc)
        finally:
            # Collect stderr before disconnect
            if conn is not None:
                if conn.stderr_output and not stderr_text:
                    stderr_text = conn.stderr_output
                await conn.disconnect()

            # Teardown subprocess + DT_PIPEs
            active = self._connections.pop(pid, None)
            if active is not None:
                self._teardown_agent(active)
                # Wait for process exit (best-effort)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(active.proc.wait(), timeout=5.0)
            elif proc is not None and proc.returncode is None:
                # Subprocess started but never registered (early failure)
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(proc.wait(), timeout=5.0)

        # Kill in AgentRegistry (→ TERMINATED)
        try:
            self._agent_registry.kill(pid, exit_code=exit_code)
        except Exception:
            logger.debug("AgentRegistry.kill(%s) failed (may already be reaped)", pid)

        result = AcpResult(
            pid=pid,
            agent_id=agent_id,
            exit_code=exit_code,
            response=response_text,
            raw_stdout=stderr_text,  # no raw stdout in JSON-RPC mode; store stderr here
            stderr=stderr_text,
            timed_out=timed_out,
            metadata=metadata,
        )

        # Persist to VFS (include original prompt for history)
        await self._persist_result(result, zone_id, prompt=user_prompt)

        return result

    def _teardown_agent(self, active: _ActiveAgent) -> None:
        """Kill subprocess and destroy DT_PIPEs for an active agent."""
        if active.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                active.proc.kill()
        if self._nexus_fs is not None:
            for fd_path in (active.fd0_path, active.fd1_path, active.fd2_path):
                with contextlib.suppress(Exception):
                    self._nexus_fs.pipe_destroy(fd_path)

    def kill_agent(self, pid: str) -> AgentDescriptor:
        """Kill a running agent connection and mark TERMINATED in AgentRegistry."""
        active = self._connections.pop(pid, None)
        if active is not None:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(active.conn.disconnect(), name=f"acp-disconnect-{pid}")
            except RuntimeError:
                pass
            self._teardown_agent(active)

        desc: AgentDescriptor = self._agent_registry.kill(pid, exit_code=-9)

        # Notify termination callbacks (Issue #3398: lease revocation on agent death)
        for callback_id, callback in self._on_terminate_callbacks:
            try:
                callback(pid)
            except Exception:
                logger.warning(
                    "AcpService: on_terminate callback %s failed for pid=%s",
                    callback_id,
                    pid,
                    exc_info=True,
                )

        return desc

    def list_agents(
        self,
        *,
        zone_id: str | None = None,
        owner_id: str | None = None,
    ) -> list[AgentDescriptor]:
        """List ACP-managed processes from the AgentRegistry."""
        procs = self._agent_registry.list_processes(
            kind=AgentKind.UNMANAGED,
            zone_id=zone_id,
            owner_id=owner_id,
        )
        return [p for p in procs if p.labels.get("service") == "acp"]

    async def _read_agent_config(self, agent_id: str, zone_id: str) -> AgentConfig | None:
        """Read agent config from VFS — single source of truth.

        Reads ``/{zone}/agents/{agent_id}/agent.json`` via ``sys_read``.
        Returns None if the file does not exist or NexusFS is not bound.
        """
        if self._nexus_fs is None:
            return None
        path = agent_paths.config(zone_id, agent_id)
        try:
            data: bytes = self._nexus_fs.sys_read(path)
            if not data:
                return None
            return AgentConfig.from_dict(json.loads(data.decode("utf-8")))
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, KeyError) as exc:
            logger.warning("Invalid agent config at %s: %s", path, exc)
            return None

    async def list_agent_configs(self, zone_id: str | None = None) -> list[dict]:
        """List agent configs from VFS — reads ``/{zone}/agents/*/agent.json``.

        Returns a list of parsed config dicts (agent_id, name, command, enabled).
        """
        if self._nexus_fs is None:
            return []
        zone_id = zone_id or self._zone_id
        agents_dir = f"/{zone_id}/agents"
        configs: list[dict] = []
        try:
            entries = self._nexus_fs.sys_readdir(agents_dir)
        except (FileNotFoundError, Exception):
            return []
        for entry in entries:
            entry_path = getattr(entry, "path", None) or str(entry)
            config_path = (
                f"{entry_path}/agent.json"
                if not entry_path.endswith("/")
                else f"{entry_path}agent.json"
            )
            try:
                data: bytes = self._nexus_fs.sys_read(config_path)
                if data:
                    cfg = json.loads(data.decode("utf-8"))
                    if isinstance(cfg, dict) and "agent_id" in cfg:
                        configs.append(cfg)
            except (FileNotFoundError, json.JSONDecodeError):
                continue
            except Exception:
                continue
        return configs

    # ------------------------------------------------------------------
    # System prompt management (VFS-backed via sys_write/sys_read)
    # ------------------------------------------------------------------

    def _system_prompt_path(self, agent_id: str, zone_id: str) -> str:
        return agent_paths.system_prompt(zone_id, agent_id)

    async def set_system_prompt(
        self,
        agent_id: str,
        content: str,
        zone_id: str | None = None,
    ) -> None:
        """Write a system prompt for *agent_id* via VFS syscall."""
        zone_id = zone_id or self._zone_id
        path = self._system_prompt_path(agent_id, zone_id)
        if self._nexus_fs is None:
            raise RuntimeError("NexusFS not bound — cannot set system prompt")
        self._nexus_fs.write(path, content.encode("utf-8"))
        logger.debug("ACP system prompt set for %s (%d chars)", agent_id, len(content))

    async def get_system_prompt(
        self,
        agent_id: str,
        zone_id: str | None = None,
    ) -> str | None:
        """Read the system prompt for *agent_id* via VFS syscall."""
        if self._nexus_fs is None:
            return None
        zone_id = zone_id or self._zone_id
        path = self._system_prompt_path(agent_id, zone_id)
        try:
            data: bytes = self._nexus_fs.sys_read(path)
            return data.decode("utf-8", errors="replace") if data else None
        except FileNotFoundError:
            return None
        except Exception:
            return None

    async def delete_system_prompt(
        self,
        agent_id: str,
        zone_id: str | None = None,
    ) -> None:
        """Remove the system prompt for *agent_id* via VFS syscall."""
        if self._nexus_fs is None:
            return
        zone_id = zone_id or self._zone_id
        path = self._system_prompt_path(agent_id, zone_id)
        with contextlib.suppress(Exception):
            self._nexus_fs.sys_unlink(path)

    # ------------------------------------------------------------------
    # Enabled skills management (VFS-backed via sys_write/sys_read)
    # ------------------------------------------------------------------

    def _config_path(self, agent_id: str, zone_id: str) -> str:
        return agent_paths.skills(zone_id, agent_id)

    async def set_enabled_skills(
        self,
        agent_id: str,
        skills: list[dict],
        zone_id: str | None = None,
    ) -> None:
        """Store the enabled skills list for *agent_id* via VFS syscall."""
        zone_id = zone_id or self._zone_id
        path = self._config_path(agent_id, zone_id)
        content = json.dumps(skills, ensure_ascii=False)
        if self._nexus_fs is None:
            raise RuntimeError("NexusFS not bound — cannot set enabled skills")
        self._nexus_fs.write(path, content.encode("utf-8"))
        logger.debug("ACP enabled_skills set for %s: %s", agent_id, skills)

    async def get_enabled_skills(
        self,
        agent_id: str,
        zone_id: str | None = None,
    ) -> list[dict] | None:
        """Read the enabled skills list for *agent_id* via VFS syscall."""
        if self._nexus_fs is None:
            return None
        zone_id = zone_id or self._zone_id
        path = self._config_path(agent_id, zone_id)
        try:
            data: bytes = self._nexus_fs.sys_read(path)
            return json.loads(data.decode("utf-8")) if data else None
        except FileNotFoundError:
            return None
        except Exception:
            return None

    async def get_call_history(
        self,
        zone_id: str | None = None,
        *,
        limit: int = 50,
    ) -> list[dict]:
        """Return past ACP call results from VFS (newest first).

        Reads ``/{zone}/proc/*/result`` files via ``sys_readdir`` + ``sys_read``.
        """
        if self._nexus_fs is None:
            return []
        zone_id = zone_id or self._zone_id
        proc_dir = f"/{zone_id}/proc"
        results: list[dict] = []
        try:
            entries = self._nexus_fs.sys_readdir(proc_dir)
            for entry in entries:
                result_path = getattr(entry, "path", None) or str(entry)
                try:
                    data: bytes = self._nexus_fs.sys_read(result_path)
                    if data:
                        payload = json.loads(data.decode("utf-8"))
                        if isinstance(payload, dict):
                            results.append(payload)
                except (FileNotFoundError, json.JSONDecodeError):
                    continue
                except Exception:
                    continue
        except Exception as exc:
            logger.debug("get_call_history failed: %s", exc)
        results.sort(key=lambda r: r.get("created_at", 0), reverse=True)
        return results[:limit]

    def close_all(self) -> None:
        """Disconnect all active ACP connections and kill subprocesses."""
        for pid, active in list(self._connections.items()):
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(active.conn.disconnect(), name=f"acp-disconnect-{pid}")
            except RuntimeError:
                pass
            self._teardown_agent(active)
            logger.debug("ACP disconnecting pid=%s", pid)
        self._connections.clear()

    # ------------------------------------------------------------------
    # VFS file I/O callables for AcpConnection
    # ------------------------------------------------------------------

    def _make_fs_callables(
        self,
        host_cwd: str,
        zone_id: str,
    ) -> tuple[FsReadFn | None, FsWriteFn | None]:
        """Create VFS-backed read/write callables for ``AcpConnection``.

        File paths from the agent (host paths) are mapped to VFS paths
        relative to the zone root and routed through ``sys_read`` /
        ``sys_write``.

        Returns ``(None, None)`` when NexusFS is not available — the
        connection will return JSON-RPC errors for file I/O requests.
        """
        nx = self._nexus_fs
        if nx is None:
            return None, None

        vfs_root = f"/{zone_id}"

        async def vfs_read(host_path: str) -> str:
            vfs_path = _host_to_vfs(host_path, host_cwd, vfs_root)
            data: bytes = nx.sys_read(vfs_path)
            return data.decode("utf-8", errors="replace")

        async def vfs_write(host_path: str, content: str) -> None:
            vfs_path = _host_to_vfs(host_path, host_cwd, vfs_root)
            nx.write(vfs_path, content.encode("utf-8"))

        return vfs_read, vfs_write

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _build_acp_command(config: AgentConfig) -> list[str]:
        """Build the subprocess command for ACP JSON-RPC mode."""
        if config.npx_package is not None:
            return [
                "npx",
                "--yes",
                "--prefer-offline",
                config.npx_package,
                *config.acp_args,
            ]
        return [config.command, *config.acp_args]

    async def _persist_result(self, result: AcpResult, zone_id: str, *, prompt: str = "") -> None:
        """Persist result to VFS at ``/{zone}/proc/{pid}/result`` via ``sys_write``."""
        if self._nexus_fs is None:
            logger.debug("ACP result not persisted (NexusFS not bound) for pid=%s", result.pid)
            return
        path = proc_paths.result(zone_id, result.pid)
        payload = {
            "pid": result.pid,
            "agent_id": result.agent_id,
            "prompt": prompt,
            "created_at": time.time(),
            "exit_code": result.exit_code,
            "response": result.response,
            "raw_stdout": result.raw_stdout,
            "stderr": result.stderr,
            "timed_out": result.timed_out,
            "metadata": result.metadata,
            "session_id": result.metadata.get("session_id"),
        }
        try:
            content = json.dumps(payload, ensure_ascii=False, indent=2)
            self._nexus_fs.write(path, content.encode("utf-8"))
        except Exception as exc:
            logger.warning("ACP result persistence failed for pid=%s: %s", result.pid, exc)


# ---------------------------------------------------------------------------
# Path mapping — host path → VFS path
# ---------------------------------------------------------------------------


def _host_to_vfs(host_path: str, host_cwd: str, vfs_root: str) -> str:
    """Map a host filesystem path to a VFS path.

    Relative paths are resolved against *host_cwd*, then the host_cwd
    prefix is replaced with *vfs_root*.

    Examples::

        _host_to_vfs("/home/user/project/src/main.py",
                      "/home/user/project", "/root/workspace")
        → "/root/workspace/src/main.py"

        _host_to_vfs("src/main.py",
                      "/home/user/project", "/root/workspace")
        → "/root/workspace/src/main.py"

    Paths outside *host_cwd* are placed under ``{vfs_root}/__external__/``
    as a containment boundary (prevents arbitrary VFS traversal).
    """
    # Resolve to absolute
    if not os.path.isabs(host_path):
        host_path = os.path.join(host_cwd, host_path)
    host_path = os.path.normpath(host_path)

    try:
        rel = os.path.relpath(host_path, host_cwd)
    except ValueError:
        # Windows cross-drive or other edge case
        rel = None

    if rel is not None and not rel.startswith(".."):
        return f"{vfs_root}/{rel}"

    # Outside workspace — contain under __external__ with full host path
    return f"{vfs_root}/__external__{host_path}"


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------

# Env vars stripped before spawning agents (mirrors AionUi prepareCleanEnv).
# Prevents Electron/npm pollution from leaking into agent subprocesses.
_ENV_STRIP_KEYS = frozenset(
    {
        "NODE_OPTIONS",
        "NODE_INSPECT",
        "NODE_DEBUG",
        "CLAUDECODE",  # prevents nested Claude Code detection
    }
)

_ENV_STRIP_PREFIXES = ("npm_",)


def _prepare_clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return a sanitised copy of ``os.environ`` with agent-specific extras."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in _ENV_STRIP_KEYS and not any(k.startswith(p) for p in _ENV_STRIP_PREFIXES)
    }
    if extra:
        env.update(extra)
    return env


# ---------------------------------------------------------------------------
# Metadata extraction — replaces per-agent parsers
# ---------------------------------------------------------------------------


def _build_metadata(
    prompt_result: AcpPromptResult,
    num_turns: int = 0,
) -> dict[str, Any]:
    """Extract standardised metadata from an ACP prompt result."""
    meta: dict[str, Any] = {}

    if prompt_result.model:
        meta["model"] = prompt_result.model
    if prompt_result.session_id:
        meta["session_id"] = prompt_result.session_id

    # Usage from prompt response — handle both snake_case and camelCase keys
    usage = prompt_result.usage
    in_tok = usage.get("input_tokens", 0) or usage.get("inputTokens", 0)
    cached_read = usage.get("cache_read_input_tokens", 0) or usage.get("cachedReadTokens", 0)
    cached_write = usage.get("cache_creation_input_tokens", 0) or usage.get("cachedWriteTokens", 0)
    out_tok = usage.get("output_tokens", 0) or usage.get("outputTokens", 0)

    total_in = in_tok + cached_read + cached_write
    if total_in:
        meta["input_tokens"] = total_in
    if out_tok:
        meta["output_tokens"] = out_tok

    # Accumulated usage from session/update notifications
    acc = prompt_result.accumulated_usage
    cost = acc.get("cost_usd") or acc.get("costUSD") or acc.get("totalCostUSD")
    if cost:
        meta["cost_usd"] = cost
    duration = acc.get("duration_api_ms") or acc.get("durationMs") or acc.get("apiDurationMs")
    if duration:
        meta["duration_api_ms"] = duration

    # Merge any accumulated token counts that weren't in the prompt response
    if "input_tokens" not in meta:
        acc_in = acc.get("input_tokens") or acc.get("inputTokens")
        if acc_in:
            meta["input_tokens"] = acc_in
    if "output_tokens" not in meta:
        acc_out = acc.get("output_tokens") or acc.get("outputTokens")
        if acc_out:
            meta["output_tokens"] = acc_out

    # Model from accumulated usage if not already set
    if "model" not in meta:
        m = acc.get("model")
        if m:
            meta["model"] = m

    if num_turns > 0:
        meta["num_turns"] = num_turns

    # Estimate cost from token counts if not provided by the agent
    if "cost_usd" not in meta and meta.get("model"):
        estimated = _estimate_cost(
            meta["model"],
            in_tok,
            cached_read,
            cached_write,
            out_tok,
        )
        if estimated is not None:
            meta["cost_usd"] = estimated

    return meta


# Per-million-token pricing (USD).  Cached-read is 90% cheaper than base input.
_PRICING: dict[str, tuple[float, float, float]] = {
    # (input_per_M, cached_read_per_M, output_per_M)
    "opus 4.6": (15.0, 1.50, 75.0),
    "sonnet 4.6": (3.0, 0.30, 15.0),
    "haiku 4.5": (0.8, 0.08, 4.0),
}


def _estimate_cost(
    model: str,
    input_tokens: int,
    cached_read_tokens: int,
    cached_write_tokens: int,
    output_tokens: int,
) -> float | None:
    """Estimate USD cost from token counts and model name. Returns None if unknown model."""
    key = model.lower().strip()
    pricing = _PRICING.get(key)
    if pricing is None:
        # Try partial match
        for k, v in _PRICING.items():
            if k in key or key in k:
                pricing = v
                break
    if pricing is None:
        return None
    inp_rate, cached_rate, out_rate = pricing
    cost = (
        (input_tokens / 1_000_000) * inp_rate
        + (cached_read_tokens / 1_000_000) * cached_rate
        + (cached_write_tokens / 1_000_000) * inp_rate  # cache writes billed at input rate
        + (output_tokens / 1_000_000) * out_rate
    )
    return round(cost, 6)
