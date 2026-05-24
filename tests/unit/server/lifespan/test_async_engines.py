"""Tests for nexus.server.lifespan._async_engines.adispose_async_engines (Issue #3775).

Covers ownership semantics: on success the record store stays attached so
NexusFS.close() callbacks can still use the sync session_factory; on
failure the store remains attached so the sync close() can run a fallback
sync dispose rather than orphaning a live async pool.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.server.lifespan._async_engines import adispose_async_engines


def _stub_nx(record_store):
    """Minimal NexusFS-shaped stub: only _record_store is touched."""
    return SimpleNamespace(_record_store=record_store)


@pytest.mark.asyncio
async def test_adispose_keeps_record_store_attached_on_success():
    """Issue #3775: adispose disposes only async engines; the store stays attached.

    ``NexusFS.close()`` runs close-callbacks (e.g. write-observer
    ``flush_sync``) that use the sync ``session_factory``. If the record
    store were detached or its sync engine disposed here, those callbacks
    would either fail or reopen a fresh pool that nothing later disposes.
    """
    rs = MagicMock()
    rs.aclose = AsyncMock()
    nx = _stub_nx(rs)

    await adispose_async_engines(nx)

    rs.aclose.assert_awaited_once()
    # Store must remain attached so close() runs sync dispose AFTER callbacks.
    assert nx._record_store is rs
    # Sync close() must NOT have run here.
    rs.close.assert_not_called()


@pytest.mark.asyncio
async def test_adispose_keeps_record_store_attached_on_aclose_failure():
    """aclose failure must not orphan the store; close() will attempt fallback."""
    rs = MagicMock()
    rs.aclose = AsyncMock(side_effect=RuntimeError("simulated dispose failure"))
    nx = _stub_nx(rs)

    await adispose_async_engines(nx)  # must not raise

    assert nx._record_store is rs
    rs.aclose.assert_awaited_once()
    rs.close.assert_not_called()


@pytest.mark.asyncio
async def test_adispose_noop_for_legacy_store_without_aclose():
    """Legacy stores without aclose() are left to close() entirely."""
    rs = MagicMock(spec=["close"])
    rs.close = MagicMock()
    nx = _stub_nx(rs)

    await adispose_async_engines(nx)  # must not raise

    rs.close.assert_not_called()  # close() runs later in NexusFS.close
    assert nx._record_store is rs


@pytest.mark.asyncio
async def test_adispose_noop_when_record_store_absent():
    nx = _stub_nx(None)
    await adispose_async_engines(nx)  # must not raise
    assert nx._record_store is None
