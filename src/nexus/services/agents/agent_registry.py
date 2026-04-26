"""AgentRegistry — kernel agent lifecycle manager.

Pure in-memory agent registry, analogous to Linux task_struct array.
No metastore persistence — agent state is ephemeral (tied to OS
process lifespan).  On nexusd restart, all agents are gone.

VFS visibility is provided by AgentStatusResolver (procfs model): reading
``/{zone}/proc/{pid}/status`` generates content from memory at
read time, like Linux ``/proc/{pid}/status``.

    core/agent_registry.py  = kernel/fork.c + kernel/exit.c + kernel/signal.c
    core/agent_status_resolver.py           = fs/proc/ (procfs virtual filesystem)

Concurrency model:
  - spawn/kill/signal/get/list are synchronous (fast PID allocation
    + in-memory dict write). Safe under asyncio event loop (no await).
  - wait() is async (blocks on asyncio.Event until target state).
  - State transitions are validated against VALID_AGENT_TRANSITIONS.

See: contracts/process_types.py for AgentDescriptor, AgentState.
"""

import asyncio
import contextlib
import logging
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from nexus.contracts.process_types import (
    VALID_AGENT_TRANSITIONS,
    AgentDescriptor,
    AgentError,
    AgentKind,
    AgentNotFoundError,
    AgentSignal,
    AgentState,
    ExternalProcessInfo,
    InvalidTransitionError,
)

logger = logging.getLogger(__name__)


class AgentRegistry:
    """Manages agent lifecycle — PID allocation, state machine, signals, wait().

    Pure in-memory — analogous to Linux's task_struct table.
    VFS visibility via AgentStatusResolver (procfs), not metastore persistence.
    """

    def __init__(self) -> None:
        self._processes: dict[str, AgentDescriptor] = {}
        self._wait_events: dict[str, list[asyncio.Event]] = {}
        self._provisioner: Any = None  # Optional IPC provisioner (AgentProvisioner)

    # ------------------------------------------------------------------
    # IPC provisioner hook
    # ------------------------------------------------------------------

    def set_provisioner(self, provisioner: Any) -> None:
        """Inject IPC provisioner for automatic directory creation on register.

        Called by factory after both AgentRegistry and AgentProvisioner exist.
        When set, ``provision()`` delegates to ``provisioner.provision()``.
        """
        self._provisioner = provisioner
        logger.debug("AgentRegistry: IPC provisioner set")

    async def provision(
        self,
        agent_id: str,
        name: str | None = None,
        skills: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Provision IPC directories for an agent. Non-fatal.

        Returns True if provisioning succeeded, False otherwise.
        No-op (returns False) when no provisioner is configured.
        """
        if self._provisioner is None:
            return False
        try:
            await self._provisioner.provision(agent_id, name=name, skills=skills, metadata=metadata)
            return True
        except Exception as exc:
            logger.warning(
                "IPC provisioning failed for agent %s (non-fatal): %s",
                agent_id,
                exc,
            )
            return False

    # ------------------------------------------------------------------
    # PID allocation
    # ------------------------------------------------------------------

    def _alloc_pid(self) -> str:
        """Allocate a unique PID (UUID4 hex prefix)."""
        return uuid.uuid4().hex[:12]

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------

    def _transition(
        self,
        desc: AgentDescriptor,
        new_state: AgentState,
        **kwargs: Any,
    ) -> AgentDescriptor:
        """Validate and apply state transition. Returns new descriptor."""
        allowed = VALID_AGENT_TRANSITIONS.get(desc.state, frozenset())
        if new_state not in allowed:
            raise InvalidTransitionError(
                f"cannot transition {desc.pid} from {desc.state} to {new_state}"
            )
        now = datetime.now(UTC)
        updated = replace(desc, state=new_state, updated_at=now, **kwargs)
        self._processes[desc.pid] = updated
        self._notify_waiters(desc.pid)
        return updated

    def _notify_waiters(self, pid: str) -> None:
        """Wake all waiters for a PID."""
        events = self._wait_events.get(pid)
        if events:
            for ev in events:
                ev.set()

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def spawn(
        self,
        name: str,
        owner_id: str,
        zone_id: str,
        *,
        kind: AgentKind = AgentKind.MANAGED,
        pid: str | None = None,
        parent_pid: str | None = None,
        cwd: str = "/",
        external_info: ExternalProcessInfo | None = None,
        labels: dict[str, str] | None = None,
    ) -> AgentDescriptor:
        """Create a new process in REGISTERED state."""
        # Validate parent
        if parent_pid is not None:
            parent = self._processes.get(parent_pid)
            if parent is None:
                raise AgentNotFoundError(f"parent not found: {parent_pid}")

        pid = pid or self._alloc_pid()
        now = datetime.now(UTC)

        desc = AgentDescriptor(
            pid=pid,
            ppid=parent_pid,
            name=name,
            owner_id=owner_id,
            zone_id=zone_id,
            kind=kind,
            state=AgentState.REGISTERED,
            generation=1,
            cwd=cwd,
            external_info=external_info,
            labels=labels or {},
            created_at=now,
            updated_at=now,
        )

        self._processes[pid] = desc

        # Update parent.children
        if parent_pid is not None:
            parent = self._processes[parent_pid]
            updated_parent = replace(
                parent,
                children=parent.children + (pid,),
                updated_at=now,
            )
            self._processes[parent_pid] = updated_parent

        logger.debug("process spawned: pid=%s name=%s kind=%s", pid, name, kind)
        return desc

    def kill(self, pid: str, *, exit_code: int = 0) -> AgentDescriptor:
        """Kill a process — transition to TERMINATED, auto-reap if orphan."""
        desc = self._processes.get(pid)
        if desc is None:
            raise AgentNotFoundError(f"process not found: {pid}")

        if desc.state == AgentState.TERMINATED:
            return desc  # already dead

        updated = self._transition(desc, AgentState.TERMINATED, exit_code=exit_code)

        # Auto-reap if orphan (no parent to wait())
        if updated.ppid is None:
            self._reap(updated)

        return updated

    def signal(
        self,
        pid: str,
        sig: AgentSignal,
        *,
        payload: dict[str, Any] | None = None,
    ) -> AgentDescriptor:
        """Send a signal to a process."""
        desc = self._processes.get(pid)
        if desc is None:
            raise AgentNotFoundError(f"process not found: {pid}")

        match sig:
            case AgentSignal.SIGSTOP:
                return self._transition(desc, AgentState.SUSPENDED)
            case AgentSignal.SIGCONT:
                new_gen = desc.generation + 1
                return self._transition(desc, AgentState.READY, generation=new_gen)
            case AgentSignal.SIGTERM:
                return self.kill(pid)
            case AgentSignal.SIGKILL:
                # Force kill + immediate reap regardless of parent
                if desc.state != AgentState.TERMINATED:
                    desc = self._transition(desc, AgentState.TERMINATED, exit_code=-9)
                self._reap(desc)
                return desc
            case AgentSignal.SIGUSR1:
                # User-defined signal — merge payload into labels, notify waiters
                if payload:
                    merged = {**desc.labels, **{k: str(v) for k, v in payload.items()}}
                    desc = replace(desc, labels=merged, updated_at=datetime.now(UTC))
                    self._processes[pid] = desc
                self._notify_waiters(pid)
                return desc
            case _:
                raise AgentError(f"unknown signal: {sig}")

    async def wait(
        self,
        pid: str,
        *,
        target_states: frozenset[AgentState] | None = None,
        timeout: float | None = None,
    ) -> AgentDescriptor | None:
        """Wait for process to reach target state. Reaps ZOMBIE on return."""
        if target_states is None:
            target_states = frozenset({AgentState.TERMINATED})

        desc = self._processes.get(pid)
        if desc is None:
            raise AgentNotFoundError(f"process not found: {pid}")

        # Already in target state?
        if desc.state in target_states:
            if desc.state == AgentState.TERMINATED:
                self._reap(desc)
            return desc

        # Create event and wait
        event = asyncio.Event()
        waiters = self._wait_events.setdefault(pid, [])
        waiters.append(event)
        try:
            while True:
                try:
                    await asyncio.wait_for(event.wait(), timeout=timeout)
                except TimeoutError:
                    return None

                desc = self._processes.get(pid)
                if desc is None:
                    return None  # reaped by someone else

                if desc.state in target_states:
                    if desc.state == AgentState.TERMINATED:
                        self._reap(desc)
                    return desc

                # Not in target state yet — reset and wait again
                event.clear()
        finally:
            waiters = self._wait_events.get(pid, [])
            if event in waiters:
                waiters.remove(event)
            if not waiters:
                self._wait_events.pop(pid, None)

    def get(self, pid: str) -> AgentDescriptor | None:
        """Look up process by PID."""
        return self._processes.get(pid)

    def list_processes(
        self,
        *,
        zone_id: str | None = None,
        owner_id: str | None = None,
        kind: AgentKind | None = None,
        state: AgentState | None = None,
    ) -> list[AgentDescriptor]:
        """List processes with optional filters."""
        result = list(self._processes.values())
        if zone_id is not None:
            result = [p for p in result if p.zone_id == zone_id]
        if owner_id is not None:
            result = [p for p in result if p.owner_id == owner_id]
        if kind is not None:
            result = [p for p in result if p.kind == kind]
        if state is not None:
            result = [p for p in result if p.state == state]
        return result

    # ------------------------------------------------------------------
    # Convenience queries (Issue #1692)
    # ------------------------------------------------------------------

    def count_by_state(self, state: AgentState, *, zone_id: str | None = None) -> int:
        """Count processes in a given state."""
        return len(self.list_processes(state=state, zone_id=zone_id))

    def list_by_priority(
        self,
        *,
        zone_id: str | None = None,
        batch_size: int = 10,
    ) -> list[AgentDescriptor]:
        """List BUSY processes sorted by eviction priority (lowest first), then LRU."""
        procs = self.list_processes(state=AgentState.BUSY, zone_id=zone_id)
        procs.sort(
            key=lambda p: (
                int(p.labels.get("eviction_priority", "50")),
                p.updated_at,
            )
        )
        return procs[:batch_size]

    # ------------------------------------------------------------------
    # External process management
    # ------------------------------------------------------------------

    def register_external(
        self,
        name: str,
        owner_id: str,
        zone_id: str,
        *,
        connection_id: str,
        host_pid: int | None = None,
        remote_addr: str | None = None,
        protocol: str = "grpc",
        parent_pid: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> AgentDescriptor:
        """Register an external process (gRPC agent connects)."""
        now = datetime.now(UTC)
        ext_info = ExternalProcessInfo(
            connection_id=connection_id,
            host_pid=host_pid,
            remote_addr=remote_addr,
            protocol=protocol,
            last_heartbeat=now,
        )
        return self.spawn(
            name,
            owner_id,
            zone_id,
            kind=AgentKind.UNMANAGED,
            pid=connection_id,
            parent_pid=parent_pid,
            external_info=ext_info,
            labels=labels,
        )

    def heartbeat(self, pid: str) -> AgentDescriptor:
        """Update heartbeat timestamp for an external process."""
        desc = self._processes.get(pid)
        if desc is None:
            raise AgentNotFoundError(f"process not found: {pid}")
        if desc.kind != AgentKind.UNMANAGED:
            raise AgentError(f"heartbeat only for unmanaged processes: {pid}")
        if desc.external_info is None:
            raise AgentError(f"missing external_info: {pid}")

        now = datetime.now(UTC)
        new_ext = replace(desc.external_info, last_heartbeat=now)
        updated = replace(desc, external_info=new_ext, updated_at=now)
        self._processes[pid] = updated
        return updated

    def unregister_external(self, pid: str) -> None:
        """Unregister an external process — TERMINATED + reap."""
        desc = self._processes.get(pid)
        if desc is None:
            raise AgentNotFoundError(f"process not found: {pid}")
        if desc.kind != AgentKind.UNMANAGED:
            raise AgentError(f"unregister_external only for unmanaged processes: {pid}")

        if desc.state != AgentState.TERMINATED:
            desc = self._transition(desc, AgentState.TERMINATED)
        self._reap(desc)

    # ------------------------------------------------------------------
    # Reap — remove process from table
    # ------------------------------------------------------------------

    def _reap(self, desc: AgentDescriptor) -> None:
        """Remove process from table and clean up parent.children."""
        pid = desc.pid
        self._processes.pop(pid, None)
        self._wait_events.pop(pid, None)

        # Remove from parent's children list
        if desc.ppid is not None:
            parent = self._processes.get(desc.ppid)
            if parent is not None:
                new_children = tuple(c for c in parent.children if c != pid)
                updated_parent = replace(
                    parent,
                    children=new_children,
                    updated_at=datetime.now(UTC),
                )
                self._processes[desc.ppid] = updated_parent

        logger.debug("process reaped: pid=%s name=%s", pid, desc.name)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def close_all(self) -> None:
        """Shutdown: kill all processes, clear state."""
        for pid in list(self._processes):
            desc = self._processes.get(pid)
            if desc is not None and desc.state != AgentState.TERMINATED:
                with contextlib.suppress(AgentError, InvalidTransitionError):
                    self.kill(pid)
        self._processes.clear()
        self._wait_events.clear()
        logger.debug("AgentRegistry closed — all agents cleared")
