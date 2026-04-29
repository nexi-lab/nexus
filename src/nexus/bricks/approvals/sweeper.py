"""Background sweeper that expires past-due pending requests."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from nexus.bricks.approvals.repository import ApprovalRepository

logger = logging.getLogger(__name__)


class Sweeper:
    def __init__(
        self,
        repository: ApprovalRepository,
        interval_seconds: float,
        on_expired: Callable[[list[str]], None],
    ) -> None:
        self._repo = repository
        self._interval = interval_seconds
        self._on_expired = on_expired
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop_event.set()
        await self._task
        self._task = None

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                ids = await self._repo.sweep_expired(now=datetime.now(UTC))
                if ids:
                    self._on_expired(ids)
            except Exception:
                logger.exception("sweeper iteration failed")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval)
            except TimeoutError:
                continue
