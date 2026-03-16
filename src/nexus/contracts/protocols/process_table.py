"""ProcessTableProtocol — kernel-level process lifecycle contract (Issue #1509).

Kernel contract for process management. No LLM, no tools, no agent logic.
Those belong in the service-layer AgentService (Phase 4).

    contracts/protocols/process_table.py = kernel syscall interface
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.contracts.process_types import (
        ExternalProcessInfo,
        ProcessDescriptor,
        ProcessKind,
        ProcessSignal,
        ProcessState,
    )


@runtime_checkable
class ProcessTableProtocol(Protocol):
    """Kernel process table — PID allocation, state machine, signals, wait()."""

    def spawn(
        self,
        name: str,
        owner_id: str,
        zone_id: str,
        *,
        kind: ProcessKind = ...,
        parent_pid: str | None = None,
        cwd: str = "/",
        external_info: ExternalProcessInfo | None = None,
        labels: dict[str, str] | None = None,
    ) -> ProcessDescriptor: ...

    def kill(
        self,
        pid: str,
        *,
        exit_code: int = 0,
    ) -> ProcessDescriptor: ...

    def signal(
        self,
        pid: str,
        sig: ProcessSignal,
        *,
        payload: dict[str, Any] | None = None,
    ) -> ProcessDescriptor: ...

    async def wait(
        self,
        pid: str,
        *,
        target_states: frozenset[ProcessState] | None = None,
        timeout: float | None = None,
    ) -> ProcessDescriptor | None: ...

    def get(self, pid: str) -> ProcessDescriptor | None: ...

    def list_processes(
        self,
        *,
        zone_id: str | None = None,
        owner_id: str | None = None,
        kind: ProcessKind | None = None,
        state: ProcessState | None = None,
    ) -> list[ProcessDescriptor]: ...

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
    ) -> ProcessDescriptor: ...

    def heartbeat(self, pid: str) -> ProcessDescriptor: ...

    def unregister_external(self, pid: str) -> None: ...
