"""ProcessTable — kernel process lifecycle manager (Issue #1509).

Pure in-memory process table, analogous to Linux task_struct array.
No metastore persistence — process state is ephemeral (tied to OS
process lifespan).  On nexusd restart, all processes are gone.

VFS visibility is provided by ProcResolver (procfs model): reading
``/{zone}/proc/{pid}/status`` generates content from memory at
read time, like Linux ``/proc/{pid}/status``.

    core/process_table.py  = kernel/fork.c + kernel/exit.c + kernel/signal.c
    system_services/proc/proc_resolver.py  = fs/proc/ (procfs virtual filesystem)

Concurrency model:
  - spawn/kill/signal/get/list are synchronous (fast PID allocation
    + in-memory dict write). Safe under asyncio event loop (no await).
  - wait() is async (blocks on asyncio.Event until target state).
  - State transitions are validated against VALID_PROCESS_TRANSITIONS.

See: contracts/process_types.py for ProcessDescriptor, ProcessState.
"""

import asyncio
import contextlib
import logging
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.process_types import (
    VALID_PROCESS_TRANSITIONS,
    ExternalProcessInfo,
    InvalidTransitionError,
    ProcessDescriptor,
    ProcessError,
    ProcessKind,
    ProcessNotFoundError,
    ProcessSignal,
    ProcessState,
)

logger = logging.getLogger(__name__)


class ProcessTable:
    """Manages process lifecycle — PID allocation, state machine, signals, wait().

    Pure in-memory — analogous to Linux's task_struct table.
    VFS visibility via ProcResolver (procfs), not metastore persistence.
    """

    def __init__(self, zone_id: str = ROOT_ZONE_ID) -> None:
        self._zone_id = zone_id
        self._processes: dict[str, ProcessDescriptor] = {}
        self._wait_events: dict[str, list[asyncio.Event]] = {}

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
        desc: ProcessDescriptor,
        new_state: ProcessState,
        **kwargs: Any,
    ) -> ProcessDescriptor:
        """Validate and apply state transition. Returns new descriptor."""
        allowed = VALID_PROCESS_TRANSITIONS.get(desc.state, frozenset())
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
        kind: ProcessKind = ProcessKind.MANAGED,
        pid: str | None = None,
        parent_pid: str | None = None,
        cwd: str = "/",
        external_info: ExternalProcessInfo | None = None,
        labels: dict[str, str] | None = None,
    ) -> ProcessDescriptor:
        """Create a new process in RUNNING state."""
        # Validate parent
        if parent_pid is not None:
            parent = self._processes.get(parent_pid)
            if parent is None:
                raise ProcessNotFoundError(f"parent not found: {parent_pid}")

        pid = pid or self._alloc_pid()
        now = datetime.now(UTC)

        desc = ProcessDescriptor(
            pid=pid,
            ppid=parent_pid,
            name=name,
            owner_id=owner_id,
            zone_id=zone_id,
            kind=kind,
            state=ProcessState.RUNNING,
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

    def kill(self, pid: str, *, exit_code: int = 0) -> ProcessDescriptor:
        """Kill a process — transition to ZOMBIE, auto-reap if orphan."""
        desc = self._processes.get(pid)
        if desc is None:
            raise ProcessNotFoundError(f"process not found: {pid}")

        if desc.state == ProcessState.ZOMBIE:
            return desc  # already dead

        updated = self._transition(desc, ProcessState.ZOMBIE, exit_code=exit_code)

        # Auto-reap if orphan (no parent to wait())
        if updated.ppid is None:
            self._reap(updated)

        return updated

    def signal(
        self,
        pid: str,
        sig: ProcessSignal,
        *,
        payload: dict[str, Any] | None = None,
    ) -> ProcessDescriptor:
        """Send a signal to a process."""
        desc = self._processes.get(pid)
        if desc is None:
            raise ProcessNotFoundError(f"process not found: {pid}")

        match sig:
            case ProcessSignal.SIGSTOP:
                return self._transition(desc, ProcessState.STOPPED)
            case ProcessSignal.SIGCONT:
                new_gen = desc.generation + 1
                return self._transition(desc, ProcessState.SLEEPING, generation=new_gen)
            case ProcessSignal.SIGTERM:
                return self.kill(pid)
            case ProcessSignal.SIGKILL:
                # Force kill + immediate reap regardless of parent
                if desc.state != ProcessState.ZOMBIE:
                    desc = self._transition(desc, ProcessState.ZOMBIE, exit_code=-9)
                self._reap(desc)
                return desc
            case ProcessSignal.SIGUSR1:
                # User-defined signal — merge payload into labels, notify waiters
                if payload:
                    merged = {**desc.labels, **{k: str(v) for k, v in payload.items()}}
                    desc = replace(desc, labels=merged, updated_at=datetime.now(UTC))
                    self._processes[pid] = desc
                self._notify_waiters(pid)
                return desc
            case _:
                raise ProcessError(f"unknown signal: {sig}")

    async def wait(
        self,
        pid: str,
        *,
        target_states: frozenset[ProcessState] | None = None,
        timeout: float | None = None,
    ) -> ProcessDescriptor | None:
        """Wait for process to reach target state. Reaps ZOMBIE on return."""
        if target_states is None:
            target_states = frozenset({ProcessState.ZOMBIE})

        desc = self._processes.get(pid)
        if desc is None:
            raise ProcessNotFoundError(f"process not found: {pid}")

        # Already in target state?
        if desc.state in target_states:
            if desc.state == ProcessState.ZOMBIE:
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
                    if desc.state == ProcessState.ZOMBIE:
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

    def get(self, pid: str) -> ProcessDescriptor | None:
        """Look up process by PID."""
        return self._processes.get(pid)

    def list_processes(
        self,
        *,
        zone_id: str | None = None,
        owner_id: str | None = None,
        kind: ProcessKind | None = None,
        state: ProcessState | None = None,
    ) -> list[ProcessDescriptor]:
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

    def count_by_state(self, state: ProcessState, *, zone_id: str | None = None) -> int:
        """Count processes in a given state."""
        return len(self.list_processes(state=state, zone_id=zone_id))

    def list_by_priority(
        self,
        *,
        zone_id: str | None = None,
        batch_size: int = 10,
    ) -> list[ProcessDescriptor]:
        """List RUNNING processes sorted by eviction priority (lowest first), then LRU."""
        procs = self.list_processes(state=ProcessState.RUNNING, zone_id=zone_id)
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
    ) -> ProcessDescriptor:
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
            kind=ProcessKind.UNMANAGED,
            parent_pid=parent_pid,
            external_info=ext_info,
            labels=labels,
        )

    def heartbeat(self, pid: str) -> ProcessDescriptor:
        """Update heartbeat timestamp for an external process."""
        desc = self._processes.get(pid)
        if desc is None:
            raise ProcessNotFoundError(f"process not found: {pid}")
        if desc.kind != ProcessKind.UNMANAGED:
            raise ProcessError(f"heartbeat only for unmanaged processes: {pid}")
        if desc.external_info is None:
            raise ProcessError(f"missing external_info: {pid}")

        now = datetime.now(UTC)
        new_ext = replace(desc.external_info, last_heartbeat=now)
        updated = replace(desc, external_info=new_ext, updated_at=now)
        self._processes[pid] = updated
        return updated

    def unregister_external(self, pid: str) -> None:
        """Unregister an external process — ZOMBIE + reap."""
        desc = self._processes.get(pid)
        if desc is None:
            raise ProcessNotFoundError(f"process not found: {pid}")
        if desc.kind != ProcessKind.UNMANAGED:
            raise ProcessError(f"unregister_external only for unmanaged processes: {pid}")

        if desc.state != ProcessState.ZOMBIE:
            desc = self._transition(desc, ProcessState.ZOMBIE)
        self._reap(desc)

    # ------------------------------------------------------------------
    # Reap — remove process from table
    # ------------------------------------------------------------------

    def _reap(self, desc: ProcessDescriptor) -> None:
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
            if desc is not None and desc.state != ProcessState.ZOMBIE:
                with contextlib.suppress(ProcessError, InvalidTransitionError):
                    self.kill(pid)
        self._processes.clear()
        self._wait_events.clear()
        logger.debug("ProcessTable closed — all processes cleared")
