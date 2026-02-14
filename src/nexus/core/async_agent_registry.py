"""Async wrapper for AgentRegistry (Issue #1440).

Thin adapter that wraps the sync ``AgentRegistry`` to satisfy
``AgentRegistryProtocol`` (all-async signatures).  Uses
``asyncio.to_thread`` for I/O-bound methods (DB access).

Follows the established ``AsyncFileMetadataWrapper`` pattern.

References:
    - Issue #1440: Async wrappers for 4 sync kernel protocols
    - Issue #1383: Define 6 kernel protocol interfaces
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from nexus.core.agent_record import AgentState
from nexus.services.protocols.agent_registry import AgentInfo

if TYPE_CHECKING:
    from nexus.core.agent_record import AgentRecord
    from nexus.core.agent_registry import AgentRegistry


def _to_agent_info(record: AgentRecord) -> AgentInfo:
    """Convert an ``AgentRecord`` to the protocol-level ``AgentInfo``.

    Maps the persistence-layer dataclass to the lightweight snapshot
    that downstream consumers expect from the protocol.
    """
    return AgentInfo(
        agent_id=record.agent_id,
        owner_id=record.owner_id,
        zone_id=record.zone_id,
        name=record.name,
        state=record.state.value,
        generation=record.generation,
    )


class AsyncAgentRegistry:
    """Async adapter for ``AgentRegistry`` conforming to ``AgentRegistryProtocol``.

    All methods with DB I/O delegate via ``asyncio.to_thread``.
    The ``AgentRecord`` → ``AgentInfo`` conversion happens at the boundary.

    Thread Safety: The underlying ``AgentRegistry`` is thread-safe —
    database operations use session-per-operation (no held sessions),
    and the heartbeat buffer is protected by ``threading.Lock``.
    Each ``to_thread`` call may run on a different pool thread, which
    is safe due to the session-per-operation pattern.
    """

    def __init__(self, inner: AgentRegistry) -> None:
        self._inner = inner

    async def register(
        self,
        agent_id: str,
        owner_id: str,
        *,
        zone_id: str | None = None,
        name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentInfo:
        record = await asyncio.to_thread(
            self._inner.register,
            agent_id,
            owner_id,
            zone_id=zone_id,
            name=name,
            metadata=metadata,
        )
        return _to_agent_info(record)

    async def get(self, agent_id: str) -> AgentInfo | None:
        record = await asyncio.to_thread(self._inner.get, agent_id)
        if record is None:
            return None
        return _to_agent_info(record)

    async def transition(
        self,
        agent_id: str,
        target_state: str,
        *,
        expected_generation: int | None = None,
    ) -> AgentInfo:
        try:
            state_enum = AgentState(target_state)
        except ValueError:
            valid = [s.value for s in AgentState]
            raise ValueError(
                f"Invalid target state {target_state!r}. Valid: {', '.join(valid)}"
            ) from None

        record = await asyncio.to_thread(
            self._inner.transition,
            agent_id,
            state_enum,
            expected_generation=expected_generation,
        )
        return _to_agent_info(record)

    async def heartbeat(self, agent_id: str) -> None:
        await asyncio.to_thread(self._inner.heartbeat, agent_id)

    async def list_by_zone(self, zone_id: str) -> list[AgentInfo]:
        records = await asyncio.to_thread(self._inner.list_by_zone, zone_id)
        return [_to_agent_info(r) for r in records]

    async def unregister(self, agent_id: str) -> bool:
        return await asyncio.to_thread(self._inner.unregister, agent_id)
