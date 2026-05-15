"""AgentRegistryProtocol — kernel-level agent lifecycle contract (Issue #1509, #1800).

Kernel contract for agent management. No LLM, no tools, no agent logic.
Those belong in the service-layer AgentService (Phase 4).

    contracts/protocols/agent_registry.py = kernel syscall interface
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.contracts.process_types import (
        AgentDescriptor,
        AgentKind,
        AgentSignal,
        AgentState,
        ExternalProcessInfo,
    )


@runtime_checkable
class AgentRegistryProtocol(Protocol):
    """Kernel agent registry — PID allocation, state machine, signals, wait()."""

    def spawn(
        self,
        name: str,
        owner_id: str,
        zone_id: str,
        *,
        kind: AgentKind = ...,
        pid: str | None = None,
        parent_pid: str | None = None,
        cwd: str = "/",
        external_info: ExternalProcessInfo | None = None,
        labels: dict[str, str] | None = None,
    ) -> AgentDescriptor: ...

    def kill(
        self,
        pid: str,
        *,
        exit_code: int = 0,
    ) -> AgentDescriptor: ...

    def signal(
        self,
        pid: str,
        sig: AgentSignal,
        *,
        payload: dict[str, Any] | None = None,
    ) -> AgentDescriptor: ...

    async def wait(
        self,
        pid: str,
        *,
        target_states: frozenset[AgentState] | None = None,
        timeout: float | None = None,
    ) -> AgentDescriptor | None: ...

    def get(self, pid: str) -> AgentDescriptor | None: ...

    def list_processes(
        self,
        *,
        zone_id: str | None = None,
        owner_id: str | None = None,
        kind: AgentKind | None = None,
        state: AgentState | None = None,
    ) -> list[AgentDescriptor]: ...

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
    ) -> AgentDescriptor: ...

    def heartbeat(self, pid: str) -> AgentDescriptor: ...

    def unregister_external(self, pid: str) -> None: ...


# Backward-compat alias (Issue #1800)
AgentRegistryProtocol = AgentRegistryProtocol
"""Deprecated alias — use ``AgentRegistryProtocol``."""
