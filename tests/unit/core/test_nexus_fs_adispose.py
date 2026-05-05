"""Tests for NexusFS.adispose_async_engines (Issue #3775).

Covers ownership semantics: on success the record store is cleared; on
failure the store remains attached so a sync close fallback can run and the
caller can recover, rather than orphaning a live async pool.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.core.nexus_fs import NexusFS


def _make_stub_nexus_fs(record_store):
    """Build a minimal NexusFS shell wired only with what adispose touches."""
    nx = NexusFS.__new__(NexusFS)
    nx._record_store = record_store
    return nx


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
    nx = _make_stub_nexus_fs(rs)

    await nx.adispose_async_engines()

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
    nx = _make_stub_nexus_fs(rs)

    await nx.adispose_async_engines()  # must not raise

    assert nx._record_store is rs
    rs.aclose.assert_awaited_once()
    rs.close.assert_not_called()


@pytest.mark.asyncio
async def test_adispose_noop_for_legacy_store_without_aclose():
    """Legacy stores without aclose() are left to close() entirely."""
    rs = MagicMock(spec=["close"])
    rs.close = MagicMock()
    nx = _make_stub_nexus_fs(rs)

    await nx.adispose_async_engines()  # must not raise

    rs.close.assert_not_called()  # close() runs later in NexusFS.close
    assert nx._record_store is rs


@pytest.mark.asyncio
async def test_adispose_noop_when_record_store_absent():
    nx = _make_stub_nexus_fs(None)
    await nx.adispose_async_engines()  # must not raise
    assert nx._record_store is None
