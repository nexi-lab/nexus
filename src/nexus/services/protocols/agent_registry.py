"""Agent registry service protocol (Issue #1383).

Defines the contract for agent identity and lifecycle management.
Existing implementation: ``nexus.core.agent_registry.AgentRegistry`` (sync).

Storage Affinity: **RecordStore** — relational agent identity and lifecycle state.

References:
    - docs/design/KERNEL-ARCHITECTURE.md §3
    - docs/architecture/data-storage-matrix.md (Four Pillars)
    - Issue #1383: Define 6 kernel protocol interfaces
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class AgentInfo:
    """Lightweight snapshot of agent identity and state.

    A protocol-level subset of ``AgentRecord`` — carries only the fields
    that downstream consumers need, without exposing persistence details.

    Attributes:
        agent_id: Unique agent identifier.
        owner_id: User ID who owns this agent.
        zone_id: Zone/organization ID for multi-zone isolation.
        name: Human-readable display name.
        state: Current lifecycle state (string, e.g. "CONNECTED").
        generation: Session generation counter.
    """

    agent_id: str
    owner_id: str
    zone_id: str | None
    name: str | None
    state: str
    generation: int


@runtime_checkable
class AgentRegistryProtocol(Protocol):
    """Service contract for agent identity and lifecycle management.

    All methods are async.  The existing ``AgentRegistry`` (sync) conforms
    once wrapped with an async adapter.
    """

    async def register(
        self,
        agent_id: str,
        owner_id: str,
        *,
        zone_id: str | None = None,
        name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentInfo: ...

    async def get(self, agent_id: str) -> AgentInfo | None: ...

    async def transition(
        self,
        agent_id: str,
        target_state: str,
        *,
        expected_generation: int | None = None,
    ) -> AgentInfo: ...

    async def heartbeat(self, agent_id: str) -> None: ...

    async def list_by_zone(self, zone_id: str) -> list[AgentInfo]: ...

    async def unregister(self, agent_id: str) -> bool: ...
