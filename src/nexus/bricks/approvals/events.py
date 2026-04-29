"""Event dispatcher: futures + Postgres LISTEN/NOTIFY bridge."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from nexus.bricks.approvals.models import Decision

logger = logging.getLogger(__name__)


class Dispatcher:
    """In-process map of request_id → futures awaiting a Decision."""

    def __init__(self) -> None:
        self._waiters: dict[str, list[asyncio.Future[Decision]]] = defaultdict(list)

    def register(self, request_id: str) -> asyncio.Future[Decision]:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Decision] = loop.create_future()
        self._waiters[request_id].append(fut)
        return fut

    def cancel(self, fut: asyncio.Future[Decision]) -> None:
        """Remove one future from any list it appears in."""
        for rid, lst in list(self._waiters.items()):
            if fut in lst:
                lst.remove(fut)
                if not lst:
                    del self._waiters[rid]
                return

    def resolve(self, request_id: str, decision: Decision) -> None:
        waiters = self._waiters.pop(request_id, ())
        for fut in waiters:
            if not fut.done():
                fut.set_result(decision)

    def in_flight_request_ids(self) -> list[str]:
        return list(self._waiters.keys())
