"""Events RPC Service — event replay and listing.

Issue #1520.
"""

import logging
from typing import Any

from nexus.contracts.rpc import rpc_expose

logger = logging.getLogger(__name__)


class EventsRPCService:
    """RPC surface for event replay."""

    def __init__(self, replay_service: Any) -> None:
        self._replay_service = replay_service

    @rpc_expose(description="Replay historical events")
    async def events_replay(
        self,
        since: str | None = None,
        event_type: str | None = None,
        path: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        result = await self._replay_service.replay(
            since_timestamp=since,
            event_types=[event_type] if event_type else None,
            path_pattern=path,
            limit=limit,
        )
        if isinstance(result, dict):
            return result
        events = [e.to_dict() if hasattr(e, "to_dict") else e for e in result]
        return {"events": events, "has_more": len(events) >= limit}

    @rpc_expose(description="List events with filters (v1-compat)")
    def events_list(
        self,
        zone_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        path_prefix: str | None = None,
        agent_id: str | None = None,
        operation_type: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        from datetime import datetime

        since_dt = datetime.fromisoformat(since) if since else None
        until_dt = datetime.fromisoformat(until) if until else None

        result = self._replay_service.list_v1(
            zone_id=zone_id,
            agent_id=agent_id,
            operation_type=operation_type,
            path_prefix=path_prefix,
            since=since_dt,
            until=until_dt,
            limit=limit,
            cursor=cursor,
        )
        events = [
            {
                "event_id": ev.event_id,
                "type": ev.type,
                "path": ev.path,
                "new_path": ev.new_path,
                "zone_id": ev.zone_id,
                "agent_id": ev.agent_id,
                "status": ev.status,
                "delivered": ev.delivered,
                "timestamp": ev.timestamp or None,
            }
            for ev in result.events
        ]
        return {
            "events": events,
            "next_cursor": result.next_cursor,
            "has_more": result.has_more,
        }
