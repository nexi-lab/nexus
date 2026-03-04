"""ProcessManager — agent process lifecycle orchestrator.

Manages agent processes: spawn, resume, terminate, signal.
In-memory process table for Phase 1 (single-node).

Design doc: docs/design/AGENT-PROCESS-ARCHITECTURE.md §5.2, §10.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from nexus.contracts.agent_process import (
    AgentContext,
    AgentEvent,
    AgentProcess,
    AgentProcessConfig,
    AgentProcessState,
    AgentSignal,
    Error,
)
from nexus.contracts.llm_types import Message
from nexus.system_services.agent_runtime.loop import agent_loop
from nexus.system_services.agent_runtime.session_store import SessionStore
from nexus.system_services.agent_runtime.tool_dispatcher import ToolDispatcher

if TYPE_CHECKING:
    from nexus.contracts.protocols.llm_provider import LLMProviderProtocol
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
    """Manages agent process lifecycle: spawn, resume, terminate.

    In-memory process table (single-node Phase 1).
    """

    def __init__(
        self,
        vfs: NexusFS,
        llm_provider: LLMProviderProtocol,
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
        self._dispatchers: dict[str, ToolDispatcher] = {}  # cached per process

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
        self._vfs.sys_write(
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
        self._vfs.sys_write(
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
    # resume
    # ------------------------------------------------------------------

    async def resume(
        self,
        pid: str,
        message: Message,
    ) -> AsyncIterator[AgentEvent]:
        """Send a message to an agent process and run the agent loop.

        1. Load AgentProcess from table.
        2. Build OperationContext.
        3. Read SYSTEM.md + MEMORY.md from VFS.
        4. Load checkpoint (conversation history) from CAS.
        5. Build AgentContext.
        6. Transition SLEEPING->RUNNING.
        7. Run agent_loop() with event callback.
        8. Save checkpoint to CAS.
        9. Transition RUNNING->SLEEPING.
        10. Yield AgentEvents.
        """
        process = self._processes.get(pid)
        if process is None:
            yield Error(error=f"Process not found: {pid}")
            return

        config = process.config
        if config is None:
            yield Error(error=f"Process {pid} has no config")
            return

        ctx = _make_system_context(process.owner_id, process.zone_id)

        # Transition to RUNNING
        process = replace(
            process,
            state=AgentProcessState.RUNNING,
            last_scheduled=datetime.now(UTC),
        )
        self._processes[pid] = process

        # Load system prompt from VFS
        system_prompt = _DEFAULT_SYSTEM_PROMPT
        if process.system_prompt_path:
            try:
                raw = self._vfs.sys_read(process.system_prompt_path, context=ctx)
                if isinstance(raw, dict):
                    raw = raw.get("content", b"")
                if isinstance(raw, bytes):
                    system_prompt = raw.decode("utf-8", errors="replace")
            except Exception as exc:
                logger.warning("Failed to read SYSTEM.md for %s: %s", pid, exc)

        # Load MEMORY.md if it exists
        memory_path = f"{process.cwd}/MEMORY.md"
        memory_content = ""
        try:
            if self._vfs.sys_access(memory_path, context=ctx):
                raw = self._vfs.sys_read(memory_path, context=ctx)
                if isinstance(raw, dict):
                    raw = raw.get("content", b"")
                if isinstance(raw, bytes):
                    memory_content = raw.decode("utf-8", errors="replace")
        except Exception:
            pass  # MEMORY.md is optional

        if memory_content:
            system_prompt += f"\n\n## Working Memory\n\n{memory_content}"

        # Load checkpoint (previous conversation)
        prev_messages = await self._session_store.load(pid, ctx, cwd=process.cwd)

        # Build message list: previous + new user message
        messages = list(prev_messages)
        messages.append(message)

        # Get or create cached ToolDispatcher for this process
        dispatcher = self._dispatchers.get(pid)
        if dispatcher is None:
            dispatcher = ToolDispatcher(
                self._vfs,
                self._sandbox,
                default_cwd=process.cwd,
            )
            self._dispatchers[pid] = dispatcher
        tools = dispatcher.get_tool_definitions(config.tools)

        # Build context
        agent_context = AgentContext(
            system_prompt=system_prompt,
            messages=tuple(messages),
            tools=tuple(tools),
        )

        # Queue-based streaming: events arrive as they happen
        queue: asyncio.Queue[AgentEvent | None] = asyncio.Queue(maxsize=256)
        agent_cwd = process.cwd  # capture before closure

        async def _on_event(event: AgentEvent) -> None:
            await queue.put(event)

        async def _on_checkpoint(messages: list[Message]) -> None:
            await self._session_store.save(pid, messages, ctx, cwd=agent_cwd)

        async def _run_loop() -> None:
            try:
                result_messages = await agent_loop(
                    llm=self._llm,
                    dispatcher=dispatcher,
                    context=agent_context,
                    config=config,
                    ctx=ctx,
                    on_event=_on_event,
                    on_checkpoint=_on_checkpoint,
                    cwd=agent_cwd,
                )
                # Final save (captures the last assistant message / Completed)
                await self._session_store.save(pid, result_messages, ctx, cwd=agent_cwd)
            except Exception as exc:
                error_msg = f"Agent loop failed for {pid}: {exc}"
                logger.error(error_msg)
                await queue.put(Error(error=error_msg))
            finally:
                await queue.put(None)  # sentinel

        task = asyncio.create_task(_run_loop())
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield event
        finally:
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            # Transition to SLEEPING
            process = replace(process, state=AgentProcessState.SLEEPING)
            self._processes[pid] = process

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
        self._dispatchers.pop(pid, None)
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
