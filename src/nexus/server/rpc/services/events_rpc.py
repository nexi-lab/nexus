"""Events RPC Service — event replay.

Issue #1520.
"""

import logging
from datetime import datetime
from typing import Any

from nexus.contracts.rpc import rpc_expose

logger = logging.getLogger(__name__)


class EventsRPCService:
    """RPC surface for event replay."""

    def __init__(self, replay_service: Any) -> None:
        self._replay_service = replay_service

    @rpc_expose(description="Replay historical events", admin_only=True)
    async def events_replay(
        self,
        since: str | None = None,
        event_type: str | None = None,
        path: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        since_timestamp = datetime.fromisoformat(since) if since else None
        result = self._replay_service.replay(
            since_timestamp=since_timestamp,
            event_types=[event_type] if event_type else None,
            path_pattern=path,
            limit=limit,
        )
        if isinstance(result, dict):
            return result
        raw_events = getattr(result, "events", result)
        events = [e.to_dict() if hasattr(e, "to_dict") else e for e in raw_events]
        return {
            "events": events,
            "next_cursor": getattr(result, "next_cursor", None),
            "has_more": bool(getattr(result, "has_more", len(events) >= limit)),
        }
