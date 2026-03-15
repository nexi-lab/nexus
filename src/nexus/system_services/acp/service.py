"""AcpService — stateless coding agent caller via ACP JSON-RPC protocol.

Calls coding agent CLIs (Claude Code, Gemini CLI, Codex, etc.) over the
ACP JSON-RPC 2.0 protocol (stdin/stdout).  Each call is a one-shot
session: spawn → initialize → session/new → session/prompt → disconnect.

Results are persisted to the VFS at ``/{zone}/proc/{pid}/result`` for audit.

Bidirectional communication (file reads, permission requests) is handled
automatically by ``AcpConnection``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.metadata import DT_REG, FileMetadata
from nexus.contracts.process_types import ProcessDescriptor, ProcessKind, ProcessState

from .agents import BUILTIN_AGENTS, AgentConfig
from .connection import AcpConnection, AcpPromptResult, AcpRpcError

logger = logging.getLogger(__name__)

# Custom metadata keys for VFS storage
ACP_RESULT_KEY = "__acp_result__"
ACP_SYSTEM_PROMPT_KEY = "__acp_system_prompt__"
ACP_ENABLED_SKILLS_KEY = "__acp_enabled_skills__"


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


class AcpService:
    """Stateless coding agent caller via ACP JSON-RPC — Tier 1 system service.

    Lifecycle:
        1. Look up ``AgentConfig`` by ``agent_id``
        2. Inject system prompt (VFS override > config default)
        3. Build ACP command (binary + acp_args, no prompt in argv)
        4. Register external process in ``ProcessTable``
        5. Spawn ``AcpConnection`` → initialize → session/new → session/prompt
        6. Disconnect and transition process to ZOMBIE
        7. Map ``AcpPromptResult`` → ``AcpResult`` with unified metadata
        8. Persist result to VFS at ``/{zone}/proc/{pid}/result``
    """

    def __init__(
        self,
        process_table: Any,
        metastore: Any,
        zone_id: str = ROOT_ZONE_ID,
    ) -> None:
        self._process_table = process_table
        self._metastore = metastore
        self._zone_id = zone_id
        self._default_zone_id = zone_id  # expose for RPC layer
        self._agents: dict[str, AgentConfig] = {
            k: v for k, v in BUILTIN_AGENTS.items() if v.enabled
        }
        self._connections: dict[str, AcpConnection] = {}

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
        config = self._agents.get(agent_id)
        if config is None:
            raise ValueError(f"Unknown agent_id: {agent_id!r}")

        user_prompt = prompt  # preserve original for history

        # Inject system prompt as first-message prefix (mirrors AionUi)
        system_prompt = self.get_system_prompt(agent_id, zone_id) or config.default_system_prompt
        enabled_skills = self.get_enabled_skills(agent_id, zone_id)

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

        # Register in ProcessTable (CREATED state)
        connection_id = uuid.uuid4().hex
        desc = self._process_table.register_external(
            name=f"acp:{config.name}",
            owner_id=owner_id,
            zone_id=zone_id,
            connection_id=connection_id,
            host_pid=None,
            protocol="acp",
            labels=merged_labels,
        )
        pid = desc.pid

        # Transition CREATED → RUNNING (direct _transition — kernel-level)
        self._process_table._transition(desc, ProcessState.RUNNING)

        # Run ACP session
        timed_out = False
        exit_code = -1
        response_text = ""
        stderr_text = ""
        metadata: dict[str, Any] = {}
        conn = AcpConnection()

        try:
            await conn.spawn(cmd, cwd=cwd, env=env)
            self._connections[pid] = conn

            try:
                await conn.initialize(timeout=30.0)
                if session_id:
                    if not conn._load_session:
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
            if conn.stderr_output and not stderr_text:
                stderr_text = conn.stderr_output
            await conn.disconnect()
            self._connections.pop(pid, None)

        # Kill in ProcessTable (→ ZOMBIE)
        try:
            self._process_table.kill(pid, exit_code=exit_code)
        except Exception:
            logger.debug("ProcessTable.kill(%s) failed (may already be reaped)", pid)

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
        self._persist_result(result, zone_id, prompt=user_prompt)

        return result

    def kill_agent(self, pid: str) -> ProcessDescriptor:
        """Kill a running agent connection and mark ZOMBIE in ProcessTable."""
        conn = self._connections.pop(pid, None)
        if conn is not None:
            # Schedule disconnect in the background (fire-and-forget)
            asyncio.ensure_future(conn.disconnect())

        desc: ProcessDescriptor = self._process_table.kill(pid, exit_code=-9)
        return desc

    def list_agents(
        self,
        *,
        zone_id: str | None = None,
        owner_id: str | None = None,
    ) -> list[ProcessDescriptor]:
        """List ACP-managed processes from the ProcessTable."""
        procs = self._process_table.list_processes(
            kind=ProcessKind.EXTERNAL,
            zone_id=zone_id,
            owner_id=owner_id,
        )
        return [p for p in procs if p.labels.get("service") == "acp"]

    def register_agent(self, config: AgentConfig) -> None:
        """Register a custom agent config at runtime."""
        self._agents[config.agent_id] = config
        logger.debug("ACP agent registered: %s (%s)", config.agent_id, config.command)

    # ------------------------------------------------------------------
    # System prompt management (VFS-backed)
    # ------------------------------------------------------------------

    def _system_prompt_path(self, agent_id: str, zone_id: str) -> str:
        return f"/{zone_id}/acp/{agent_id}/SYSTEM.md"

    def set_system_prompt(
        self,
        agent_id: str,
        content: str,
        zone_id: str | None = None,
    ) -> None:
        """Write a system prompt for *agent_id* to the VFS."""
        zone_id = zone_id or self._zone_id
        path = self._system_prompt_path(agent_id, zone_id)

        existing = self._metastore.get(path)
        if existing is None:
            meta = FileMetadata(
                path=path,
                backend_name="acp",
                physical_path=f"acp://{agent_id}/SYSTEM.md",
                size=len(content.encode()),
                entry_type=DT_REG,
                zone_id=zone_id,
                owner_id="system",
            )
            self._metastore.put(meta)

        self._metastore.set_file_metadata(path, ACP_SYSTEM_PROMPT_KEY, content)
        logger.debug("ACP system prompt set for %s (%d chars)", agent_id, len(content))

    def get_system_prompt(
        self,
        agent_id: str,
        zone_id: str | None = None,
    ) -> str | None:
        """Read the system prompt for *agent_id* from the VFS."""
        zone_id = zone_id or self._zone_id
        path = self._system_prompt_path(agent_id, zone_id)
        result: str | None = self._metastore.get_file_metadata(path, ACP_SYSTEM_PROMPT_KEY)
        return result

    def delete_system_prompt(
        self,
        agent_id: str,
        zone_id: str | None = None,
    ) -> None:
        """Remove the system prompt for *agent_id* from the VFS."""
        zone_id = zone_id or self._zone_id
        path = self._system_prompt_path(agent_id, zone_id)
        with contextlib.suppress(Exception):
            self._metastore.delete(path)

    # ------------------------------------------------------------------
    # Enabled skills management (VFS-backed)
    # ------------------------------------------------------------------

    def _config_path(self, agent_id: str, zone_id: str) -> str:
        return f"/{zone_id}/acp/{agent_id}/config"

    def set_enabled_skills(
        self,
        agent_id: str,
        skills: list[dict],
        zone_id: str | None = None,
    ) -> None:
        """Store the enabled skills list for *agent_id* in the VFS.

        Each skill is a dict with keys: name, description, path.
        """
        zone_id = zone_id or self._zone_id
        path = self._config_path(agent_id, zone_id)

        existing = self._metastore.get(path)
        if existing is None:
            meta = FileMetadata(
                path=path,
                backend_name="acp",
                physical_path=f"acp://{agent_id}/config",
                size=0,
                entry_type=DT_REG,
                zone_id=zone_id,
                owner_id="system",
            )
            self._metastore.put(meta)

        self._metastore.set_file_metadata(path, ACP_ENABLED_SKILLS_KEY, skills)
        logger.debug("ACP enabled_skills set for %s: %s", agent_id, skills)

    def get_enabled_skills(
        self,
        agent_id: str,
        zone_id: str | None = None,
    ) -> list[dict] | None:
        """Read the enabled skills list for *agent_id* from the VFS."""
        zone_id = zone_id or self._zone_id
        path = self._config_path(agent_id, zone_id)
        result: list[dict] | None = self._metastore.get_file_metadata(path, ACP_ENABLED_SKILLS_KEY)
        return result

    def get_call_history(
        self,
        zone_id: str | None = None,
        *,
        limit: int = 50,
    ) -> list[dict]:
        """Return past ACP call results from the VFS metastore (newest first)."""
        zone_id = zone_id or self._zone_id
        prefix = f"/{zone_id}/proc/"
        results: list[dict] = []
        try:
            entries = self._metastore.list(prefix)
            for entry in entries:
                if not entry.path.endswith("/result"):
                    continue
                payload = self._metastore.get_file_metadata(entry.path, ACP_RESULT_KEY)
                if payload and isinstance(payload, dict):
                    results.append(payload)
        except Exception as exc:
            logger.debug("get_call_history failed: %s", exc)
        # Newest first
        results.sort(key=lambda r: r.get("created_at", 0), reverse=True)
        return results[:limit]

    def close_all(self) -> None:
        """Disconnect all active ACP connections."""
        for pid, conn in list(self._connections.items()):
            asyncio.ensure_future(conn.disconnect())
            logger.debug("ACP disconnecting pid=%s", pid)
        self._connections.clear()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _build_acp_command(config: AgentConfig) -> list[str]:
        """Build the subprocess command for ACP JSON-RPC mode.

        No prompt in argv — the prompt is sent via JSON-RPC ``session/prompt``.
        When ``npx_package`` is configured, always use npx — the package is
        typically an ACP wrapper that differs from the local binary.
        Falls back to the local binary only when no npx_package is set.
        """
        binary = config.command
        # Prefer npx when npx_package is configured — the npx package is
        # typically an ACP wrapper (e.g. @zed-industries/claude-agent-acp)
        # that differs from the local binary which may not support ACP.
        if config.npx_package is not None:
            return [
                "npx",
                "--yes",
                "--prefer-offline",
                config.npx_package,
                *config.acp_args,
            ]

        return [binary, *config.acp_args]

    def _persist_result(self, result: AcpResult, zone_id: str, *, prompt: str = "") -> None:
        """Write result JSON to metastore at /{zone}/proc/{pid}/result."""
        path = f"/{zone_id}/proc/{result.pid}/result"

        try:
            existing = self._metastore.get(path)
            if existing is None:
                meta = FileMetadata(
                    path=path,
                    backend_name="proc",
                    physical_path=f"proc://{result.pid}/result",
                    size=0,
                    entry_type=DT_REG,
                    zone_id=zone_id,
                    owner_id="system",
                )
                self._metastore.put(meta)

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
            self._metastore.set_file_metadata(path, ACP_RESULT_KEY, payload)
        except Exception as exc:
            logger.warning("ACP result persistence failed for pid=%s: %s", result.pid, exc)


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
    """Extract standardised metadata from an ACP prompt result.

    Combines data from the prompt response itself and accumulated
    session/update notifications into a unified metadata dict.

    Standardised keys:
        model, cost_usd, input_tokens, output_tokens,
        duration_api_ms, num_turns, session_id
    """
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
