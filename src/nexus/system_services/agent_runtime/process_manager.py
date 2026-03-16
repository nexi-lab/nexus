"""ProcessManager — agent process lifecycle orchestrator.

Manages agent processes: spawn, terminate, signal, list.
In-memory process table for Phase 1 (single-node).

Design doc: docs/design/AGENT-PROCESS-ARCHITECTURE.md §5.2, §10.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from nexus.system_services.agent_runtime.session_store import SessionStore
from nexus.system_services.agent_runtime.types import (
    AgentProcess,
    AgentProcessConfig,
    AgentProcessState,
    AgentSignal,
)

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext
    from nexus.core.nexus_fs import NexusFS

logger = logging.getLogger(__name__)

# Default system prompt for agents
_DEFAULT_SYSTEM_PROMPT = (
    "You are an AI agent running on Nexus, an AI-native distributed filesystem. "
    "You have access to tools for reading/writing files, searching code, "
    "and executing commands. Use them to accomplish the user's request."
)


class ProcessManager:
    """Manages agent process lifecycle: spawn, terminate, signal.

    In-memory process table (single-node Phase 1).
    """

    def __init__(
        self,
        vfs: NexusFS,
        llm_provider: Any,
        *,
        agent_registry: Any | None = None,
        sandbox: Any | None = None,
        scheduler: Any | None = None,
    ) -> None:
        self._vfs = vfs
        self._llm = llm_provider
        self._agent_registry = agent_registry
        self._sandbox = sandbox
        self._scheduler = scheduler
        self._session_store = SessionStore(vfs)
        self._processes: dict[str, AgentProcess] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}  # running loop tasks per PID

    # ------------------------------------------------------------------
    # spawn
    # ------------------------------------------------------------------

    async def spawn(
        self,
        owner_id: str,
        zone_id: str,
        *,
        config: AgentProcessConfig,
        parent_pid: str | None = None,
    ) -> AgentProcess:
        """Create a new agent process.

        1. Generate PID (UUID).
        2. Create home dir: /{zone_id}/agents/{pid}/.
        3. Write SYSTEM.md if config.system_prompt.
        4. Register with AgentRegistry (if available).
        5. Store in in-memory process table.
        """
        pid = str(uuid.uuid4())
        cwd = config.cwd or f"/{zone_id}/agents/{pid}"
        root = f"/{zone_id}"

        ctx = _make_system_context(owner_id, zone_id)

        # Create home directory structure (sync: one-time setup, not hot path)
        system_prompt_path = f"{cwd}/SYSTEM.md"
        prompt_text = config.system_prompt or _DEFAULT_SYSTEM_PROMPT
        await self._vfs.sys_write(
            system_prompt_path,
            prompt_text.encode("utf-8"),
            context=ctx,
        )

        # Write settings.json
        import json

        settings = {
            "model": config.model,
            "tools": list(config.tools),
            "max_turns": config.max_turns,
            "agent_type": config.agent_type,
            "mode": config.mode,
        }
        await self._vfs.sys_write(
            f"{cwd}/settings.json",
            json.dumps(settings, indent=2).encode("utf-8"),
            context=ctx,
        )

        # Register with AgentRegistry if available
        if self._agent_registry is not None:
            try:
                await self._agent_registry.register(
                    agent_id=pid,
                    owner_id=owner_id,
                    zone_id=zone_id,
                    name=config.name,
                    metadata={"agent_type": config.agent_type, "model": config.model},
                )
            except Exception as exc:
                logger.warning("AgentRegistry.register failed for %s: %s", pid, exc)

        # Build AgentProcess
        process = AgentProcess(
            pid=pid,
            ppid=parent_pid,
            name=config.name,
            owner_id=owner_id,
            zone_id=zone_id,
            state=AgentProcessState.CREATED,
            generation=1,
            cwd=cwd,
            root=root,
            model=config.model,
            system_prompt_path=system_prompt_path,
            created_at=datetime.now(UTC),
            config=config,
        )

        self._processes[pid] = process
        logger.info(
            "Process spawned: pid=%s, owner=%s, zone=%s, model=%s",
            pid,
            owner_id,
            zone_id,
            config.model,
        )
        return process

    # ------------------------------------------------------------------
    # get_process
    # ------------------------------------------------------------------

    async def get_process(self, pid: str) -> AgentProcess | None:
        """Read process descriptor."""
        return self._processes.get(pid)

    # ------------------------------------------------------------------
    # terminate
    # ------------------------------------------------------------------

    async def terminate(
        self,
        pid: str,
        *,
        exit_code: int = 0,
    ) -> None:
        """Terminate an agent process.

        1. Transition to ZOMBIE.
        2. Unregister from AgentRegistry.
        3. Remove from process table.
        """
        process = self._processes.get(pid)
        if process is None:
            logger.warning("Cannot terminate unknown process: %s", pid)
            return

        # Cancel running loop task if any
        task = self._tasks.pop(pid, None)
        if task is not None and not task.done():
            task.cancel()

        # Transition to ZOMBIE
        process = replace(process, state=AgentProcessState.ZOMBIE)
        self._processes[pid] = process

        # Unregister from AgentRegistry
        if self._agent_registry is not None:
            try:
                await self._agent_registry.unregister(pid)
            except Exception as exc:
                logger.warning("AgentRegistry.unregister failed for %s: %s", pid, exc)

        # Remove from process table + caches
        self._processes.pop(pid, None)
        self._session_store.clear_cache(pid)

        logger.info(
            "Process terminated: pid=%s, exit_code=%d",
            pid,
            exit_code,
        )

    # ------------------------------------------------------------------
    # signal
    # ------------------------------------------------------------------

    async def signal(
        self,
        pid: str,
        sig: AgentSignal,
        *,
        payload: dict[str, Any] | None = None,  # noqa: ARG002
    ) -> None:
        """Send a signal to an agent process."""
        process = self._processes.get(pid)
        if process is None:
            logger.warning("Cannot signal unknown process: %s", pid)
            return

        match sig:
            case AgentSignal.SIGSTOP:
                task = self._tasks.pop(pid, None)
                if task is not None and not task.done():
                    task.cancel()
                process = replace(process, state=AgentProcessState.STOPPED)
                self._processes[pid] = process
                logger.info("Process stopped: pid=%s", pid)

            case AgentSignal.SIGCONT:
                process = replace(process, state=AgentProcessState.SLEEPING)
                self._processes[pid] = process
                logger.info("Process continued: pid=%s", pid)

            case AgentSignal.SIGTERM:
                await self.terminate(pid)

            case AgentSignal.SIGKILL:
                # Immediate removal, no cleanup
                task = self._tasks.pop(pid, None)
                if task is not None and not task.done():
                    task.cancel()
                self._processes.pop(pid, None)
                logger.info("Process killed: pid=%s", pid)

            case AgentSignal.SIGUSR1:
                logger.debug("SIGUSR1 received for pid=%s (steering injection)", pid)

    # ------------------------------------------------------------------
    # list_processes
    # ------------------------------------------------------------------

    async def list_processes(
        self,
        *,
        zone_id: str | None = None,
        owner_id: str | None = None,
    ) -> list[AgentProcess]:
        """List processes, optionally filtered by zone or owner."""
        results = list(self._processes.values())

        if zone_id is not None:
            results = [p for p in results if p.zone_id == zone_id]
        if owner_id is not None:
            results = [p for p in results if p.owner_id == owner_id]

        return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_system_context(owner_id: str, zone_id: str) -> OperationContext:
    """Create a system-level OperationContext for VFS operations."""
    from nexus.contracts.types import OperationContext

    return OperationContext(
        user_id=owner_id,
        groups=[],
        zone_id=zone_id,
        is_system=True,
    )
