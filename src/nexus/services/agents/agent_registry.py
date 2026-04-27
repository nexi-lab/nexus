"""AgentRegistry — service-tier agent lifecycle manager.

Thin shim over the Rust ``services::agent_table::AgentTable`` SSOT. State
(pid → AgentState + condvar wakeup) lives in Rust; this class adds the
Python-side OS behavior layer:

  * PID allocation
  * Parent/child tree maintenance
  * VALID_AGENT_TRANSITIONS validation
  * Signal semantics (SIGTERM → kill, SIGCONT → READY + bump generation,
    SIGUSR1 → label merge, etc.)
  * IPC provisioning hook
  * Richer PCB fields not stored in Rust (cwd, root, labels, generation,
    children, external_info) — kept on the Python AgentDescriptor

Every state-mutating call propagates to Rust via ``kernel.agent_*`` so
the kernel-side ``AgentStatusResolver`` (procfs view) and any blocking
``kernel.agent_wait`` waiter see the same lifecycle. Reads currently
serve from the local Python descriptor cache because PCB fields aren't
mirrored in Rust; the cache is invalidated by every Python mutation
that goes through this class, so it stays consistent with the Rust
state SSOT it dual-writes.

  services/agents/agent_registry.py = kernel/fork.c + exit.c + signal.c
  rust/services/src/agent_table.rs   = task_struct array
  rust/kernel/src/agent_status_resolver.rs = fs/proc/

Concurrency model:
  * spawn / kill / signal / get / list_processes are synchronous
  * wait()       — async wrapper around the Rust condvar (GIL released)
  * wait_state() — sync entry into the same Rust condvar

See: contracts/process_types.py for AgentDescriptor / AgentState.
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
    """Service-tier agent lifecycle manager backed by the Rust AgentTable SSOT."""

    def __init__(self, kernel: Any | None = None) -> None:
        self._processes: dict[str, AgentDescriptor] = {}
        self._kernel: Any = kernel  # nexus_kernel.PyKernel — None disables Rust dual-write
        self._provisioner: Any = None  # Optional IPC provisioner (AgentProvisioner)
        # asyncio.Event fallback used when no kernel is attached. Production
        # path (factory-wired with the kernel) goes through the Rust condvar
        # in agent_wait; this fallback exists so unit tests of the Python
        # behavior layer don't require a Rust kernel.
        self._wait_events: dict[str, list[asyncio.Event]] = {}

    # ------------------------------------------------------------------
    # Kernel binding
    # ------------------------------------------------------------------

    def attach_kernel(self, kernel: Any) -> None:
        """Late-bind the kernel after construction (factory wiring path)."""
        self._kernel = kernel

    # ------------------------------------------------------------------
    # IPC provisioner hook
    # ------------------------------------------------------------------

    def set_provisioner(self, provisioner: Any) -> None:
        """Inject IPC provisioner for automatic directory creation on register."""
        self._provisioner = provisioner
        logger.debug("AgentRegistry: IPC provisioner set")

    async def provision(
        self,
        agent_id: str,
        name: str | None = None,
        skills: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Provision IPC directories for an agent. Non-fatal."""
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
    # Rust SSOT dual-write helpers
    # ------------------------------------------------------------------

    def _kernel_register(self, desc: AgentDescriptor) -> None:
        """Mirror a Python descriptor into the Rust AgentTable.

        Rust accepts both upper- and lowercase enum strings; we forward the
        StrEnum value as-is (lowercase).
        """
        if self._kernel is None:
            return
        try:
            created_ms = int(desc.created_at.timestamp() * 1000)
            self._kernel.agent_register(
                desc.pid,
                desc.name,
                desc.kind.value,
                desc.owner_id,
                desc.zone_id,
                created_ms,
                desc.ppid,
                desc.external_info.connection_id if desc.external_info else None,
            )
        except Exception as exc:  # SSOT lag is recoverable; never block Python writes
            logger.warning("agent_table dual-write (register) failed for %s: %s", desc.pid, exc)

    def _kernel_update_state(self, pid: str, state: AgentState) -> None:
        if self._kernel is None:
            return
        try:
            self._kernel.agent_update_state(pid, state.value)
        except Exception as exc:
            logger.warning("agent_table dual-write (update_state) failed for %s: %s", pid, exc)

    def _kernel_unregister(self, pid: str) -> None:
        if self._kernel is None:
            return
        try:
            self._kernel.agent_unregister(pid)
        except Exception as exc:
            logger.warning("agent_table dual-write (unregister) failed for %s: %s", pid, exc)

    def _kernel_heartbeat(self, pid: str, when: datetime) -> None:
        if self._kernel is None:
            return
        try:
            self._kernel.agent_heartbeat(pid, int(when.timestamp() * 1000))
        except Exception as exc:
            logger.warning("agent_table dual-write (heartbeat) failed for %s: %s", pid, exc)

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
        self._kernel_update_state(desc.pid, new_state)
        self._notify_waiters(desc.pid)
        return updated

    def _notify_waiters(self, pid: str) -> None:
        """Wake asyncio waiters parked on this PID (kernel-less fallback path)."""
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
        self._kernel_register(desc)

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
            return desc

        updated = self._transition(desc, AgentState.TERMINATED, exit_code=exit_code)

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
                if desc.state != AgentState.TERMINATED:
                    desc = self._transition(desc, AgentState.TERMINATED, exit_code=-9)
                self._reap(desc)
                return desc
            case AgentSignal.SIGUSR1:
                # Label-only update: no state change, no Rust write needed
                # (AgentTable does not store labels — Python-side PCB only).
                # Wake fallback asyncio waiters so callers observing label
                # changes via the kernel-less wait() path see the update.
                if payload:
                    merged = {**desc.labels, **{k: str(v) for k, v in payload.items()}}
                    desc = replace(desc, labels=merged, updated_at=datetime.now(UTC))
                    self._processes[pid] = desc
                self._notify_waiters(pid)
                return desc
            case _:
                raise AgentError(f"unknown signal: {sig}")

    def wait_state(
        self,
        pid: str,
        target_state: AgentState | str,
        timeout_ms: int = 5000,
    ) -> AgentDescriptor | None:
        """Block until the agent reaches ``target_state`` or timeout.

        Returns the (Python) descriptor when the target is reached, ``None``
        on timeout. Delegates to ``kernel.agent_wait`` which parks on a Rust
        condvar with the GIL released.
        """
        if self._kernel is None:
            raise RuntimeError("AgentRegistry.wait_state requires a kernel attachment")
        target_str = target_state.value if isinstance(target_state, AgentState) else target_state
        try:
            self._kernel.agent_wait(pid, target_str, int(timeout_ms))
        except RuntimeError as exc:
            msg = str(exc)
            if "timeout" in msg or "not_found" in msg:
                return None
            raise
        desc = self._processes.get(pid)
        if desc is not None and desc.state == AgentState.TERMINATED:
            self._reap(desc)
        return desc

    async def wait(
        self,
        pid: str,
        *,
        target_states: frozenset[AgentState] | None = None,
        timeout: float | None = None,
    ) -> AgentDescriptor | None:
        """Wait for a target state.

        Production path (kernel attached): pumps the Rust condvar through
        ``asyncio.to_thread`` — the Rust call releases the GIL so other
        coroutines keep running.

        Fallback path (no kernel — unit tests of the Python behavior
        layer): parks on a per-pid asyncio.Event. Behaviorally identical
        to the prior pure-Python implementation.

        ``target_states`` accepts a set for source-compat. Multi-state
        callers waiting on the Rust path receive the strictest single
        target (TERMINATED preferred, else first member); the condvar
        wakes on any transition so callers needing the full set should
        re-check ``get(pid).state`` after each wake.
        """
        if target_states is None:
            target_states = frozenset({AgentState.TERMINATED})
        if not target_states:
            raise ValueError("target_states must be non-empty")

        desc = self._processes.get(pid)
        if desc is None:
            raise AgentNotFoundError(f"process not found: {pid}")
        if desc.state in target_states:
            if desc.state == AgentState.TERMINATED:
                self._reap(desc)
            return desc

        if self._kernel is not None:
            target = (
                AgentState.TERMINATED
                if AgentState.TERMINATED in target_states
                else next(iter(target_states))
            )
            timeout_ms = int(timeout * 1000) if timeout is not None else 60_000
            return await asyncio.to_thread(self.wait_state, pid, target, timeout_ms)

        return await self._wait_via_event(pid, target_states, timeout)

    async def _wait_via_event(
        self,
        pid: str,
        target_states: frozenset[AgentState],
        timeout: float | None,
    ) -> AgentDescriptor | None:
        """Kernel-less fallback for unit tests."""
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
                    return None
                if desc.state in target_states:
                    if desc.state == AgentState.TERMINATED:
                        self._reap(desc)
                    return desc
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
    # Convenience queries
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
        self._kernel_heartbeat(pid, now)
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
        self._kernel_unregister(pid)

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
        # Best-effort kernel unregister for anything still in the local cache
        # (kill() cascades through _reap which already calls _kernel_unregister,
        # but TERMINATED-on-construction or partial-init paths can land here).
        for pid in list(self._processes):
            self._kernel_unregister(pid)
        self._processes.clear()
        self._wait_events.clear()
        logger.debug("AgentRegistry closed — all agents cleared")
