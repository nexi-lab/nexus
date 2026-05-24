"""Async DB engine teardown for the FastAPI lifespan (Issue #3775).

asyncpg connections are bound to the asyncio loop that created the pool.
Disposing them from a worker thread (where ``NexusFS.close()`` runs) raises
``RuntimeError: Future attached to a different loop`` and leaks connections.

The FastAPI lifespan must await this on its own loop *before* dispatching
``NexusFS.aclose`` to a worker thread. This module is the helper that does
the loop-bound async dispose; kernel code stays sync per
``docs/architecture/KERNEL-ARCHITECTURE.md``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.core.nexus_fs import NexusFS

logger = logging.getLogger(__name__)


async def adispose_async_engines(nx: "NexusFS") -> None:
    """Dispose the record store's async engines on the current loop.

    Disposes **only** the async engines. The record store stays attached
    and its sync engine usable so that ``NexusFS.close()`` callbacks
    (e.g. write observer ``flush_sync``) can still write through the sync
    ``session_factory``. Sync engine teardown happens later in
    ``record_store.close()`` *after* those callbacks have run.

    Failure is logged at warning level — the subsequent sync ``close()``
    will still run callbacks and attempt best-effort sync dispose of the
    (still live) async engine, which is better than silently orphaning
    the store.
    """
    record_store = getattr(nx, "_record_store", None)
    if record_store is None:
        return
    aclose_fn = getattr(record_store, "aclose", None)
    if aclose_fn is None:
        return  # legacy store without aclose — close() will handle sync dispose
    try:
        await aclose_fn()
    except Exception:
        logger.warning(
            "record_store.aclose failed; sync close() will attempt fallback",
            exc_info=True,
        )
