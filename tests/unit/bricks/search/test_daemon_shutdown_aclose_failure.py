"""Regression: SearchDaemon.shutdown completes despite record_store.aclose failure (Issue #3775).

A previous patch awaited ``record_store.aclose()`` directly inside
``shutdown``. If that raised, the rest of teardown — sync ``close()``,
daemon-state clearing, path-context engine cleanup — was skipped, and
``_shutting_down`` stayed set so retries early-exited. Persistent leak.

The shutdown code now wraps aclose in try/except/finally so sync close and
state clearing always run.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from nexus.bricks.search.daemon import DaemonConfig, SearchDaemon


@pytest.mark.asyncio
async def test_shutdown_completes_when_record_store_aclose_raises():
    daemon = SearchDaemon(
        DaemonConfig(
            database_url=None,
            txtai_model=None,
            vector_warmup_enabled=False,
            refresh_enabled=False,
            scope_refresh_seconds=0,
        )
    )

    # Mark daemon as initialized so shutdown's early-exit guard does not skip.
    daemon._initialized = True
    daemon._owns_engine = True

    # Stub record store: aclose raises, but close() must still run.
    aclose_called = False
    close_called = False

    class FailingStore:
        async def aclose(self) -> None:
            nonlocal aclose_called
            aclose_called = True
            raise RuntimeError("simulated cross-loop dispose failure")

        def close(self) -> None:
            nonlocal close_called
            close_called = True

    daemon._record_store = FailingStore()
    daemon._async_engine = MagicMock()
    daemon._async_session: Any = MagicMock()

    # Must not raise.
    await daemon.shutdown()

    # All teardown steps must have run despite aclose failure.
    assert aclose_called
    assert close_called
    assert daemon._record_store is None
    assert daemon._async_engine is None
    assert daemon._async_session is None
    assert daemon._initialized is False
