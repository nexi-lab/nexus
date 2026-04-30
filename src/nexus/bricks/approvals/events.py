"""Event dispatcher: futures + Postgres LISTEN/NOTIFY bridge."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Callable, Coroutine
from typing import Any

import asyncpg

from nexus.bricks.approvals.models import Decision

logger = logging.getLogger(__name__)


class Dispatcher:
    """In-process map of request_id → futures awaiting a Decision.

    Each waiter optionally carries the caller's ``session_id`` so the
    service layer can fan out per-waiter session_allow rows when an
    approval is decided with SESSION scope (Issue #3790 follow-up: the
    decided row only stores ONE session_id — the winning insert's — so
    coalesced waiters with different session_ids would otherwise lose
    their session caching).
    """

    def __init__(self) -> None:
        # Each entry is (future, session_id-or-None). A list (not a set)
        # because multiple waiters may share the same session_id and we
        # need to drop only one entry on cancel.
        self._waiters: dict[str, list[tuple[asyncio.Future[Decision], str | None]]] = defaultdict(
            list
        )

    def register(
        self, request_id: str, *, session_id: str | None = None
    ) -> asyncio.Future[Decision]:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Decision] = loop.create_future()
        self._waiters[request_id].append((fut, session_id))
        return fut

    def cancel(self, fut: asyncio.Future[Decision]) -> None:
        """Remove one future from any list it appears in."""
        for rid, lst in list(self._waiters.items()):
            for entry in lst:
                if entry[0] is fut:
                    lst.remove(entry)
                    if not lst:
                        del self._waiters[rid]
                    return

    def resolve(self, request_id: str, decision: Decision) -> None:
        waiters = self._waiters.pop(request_id, ())
        for fut, _sid in waiters:
            if not fut.done():
                fut.set_result(decision)

    def in_flight_request_ids(self) -> list[str]:
        return list(self._waiters.keys())

    def waiter_count(self, request_id: str) -> int:
        """Number of futures currently parked on this request_id.

        Public read-only accessor for diagnostics and benchmarks. Returns 0
        when ``request_id`` is unknown. Callers should treat this strictly
        as a snapshot — concurrent ``register``/``resolve``/``cancel`` will
        change the value at any moment.
        """
        return len(self._waiters.get(request_id, []))

    def session_ids_for(self, request_id: str) -> list[str]:
        """Return distinct, non-empty session_ids registered for this id.

        Used by ApprovalService.decide(scope=SESSION) to fan out
        ``session_allow`` rows to every coalesced waiter — not only the
        winning insert's session_id (Issue #3790 follow-up).

        Returned in registration order with duplicates removed; never
        contains ``None`` or empty strings.
        """
        seen: set[str] = set()
        out: list[str] = []
        for _fut, sid in self._waiters.get(request_id, ()):
            if not sid or sid in seen:
                continue
            seen.add(sid)
            out.append(sid)
        return out


NotifyHandler = Callable[[str], Coroutine[Any, Any, None]]


class NotifyBridge:
    """Bridge to Postgres LISTEN/NOTIFY using a dedicated asyncpg connection.

    Holds one connection borrowed from the pool for the lifetime of the bridge.
    Multiple LISTEN channels are supported; notify() acquires a fresh connection
    each call so listening continues uninterrupted.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self._listen_conn: asyncpg.Connection | None = None
        self._handlers: dict[str, NotifyHandler] = {}

    async def start(self, handlers: dict[str, NotifyHandler]) -> None:
        self._handlers = dict(handlers)
        self._listen_conn = await self._pool.acquire()
        for channel in self._handlers:
            await self._listen_conn.add_listener(channel, self._on_notify)

    async def stop(self) -> None:
        if self._listen_conn is None:
            return
        for channel in list(self._handlers):
            try:
                await self._listen_conn.remove_listener(channel, self._on_notify)
            except Exception:
                logger.debug("remove_listener failed for %s", channel, exc_info=True)
        await self._pool.release(self._listen_conn)
        self._listen_conn = None
        self._handlers = {}

    async def notify(self, channel: str, payload: str) -> None:
        async with self._pool.acquire() as conn:
            # Use parameterised payload via SELECT pg_notify; NOTIFY does not accept params.
            await conn.execute("SELECT pg_notify($1, $2)", channel, payload)

    def _on_notify(
        self,
        _connection: asyncpg.Connection,
        _pid: int,
        channel: str,
        payload: str,
    ) -> None:
        handler = self._handlers.get(channel)
        if handler is None:
            return
        # asyncpg invokes this synchronously; schedule async handler.
        asyncio.create_task(handler(payload))
