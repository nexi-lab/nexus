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
async def test_adispose_clears_record_store_on_success():
    """Successful aclose clears self._record_store so close() skips it."""
    rs = MagicMock()
    rs.aclose = AsyncMock()
    nx = _make_stub_nexus_fs(rs)

    await nx.adispose_async_engines()

    rs.aclose.assert_awaited_once()
    assert nx._record_store is None


@pytest.mark.asyncio
async def test_adispose_preserves_record_store_on_aclose_failure():
    """Issue #3775: aclose failure must NOT orphan the record store.

    Previously a failure was caught at debug level and ``self._record_store``
    was set to ``None`` regardless. That hid disposal failures and made the
    sync ``close()`` skip the store entirely, leaking both async and sync
    resources with no recovery handle.
    """
    rs = MagicMock()
    rs.aclose = AsyncMock(side_effect=RuntimeError("simulated dispose failure"))
    nx = _make_stub_nexus_fs(rs)

    await nx.adispose_async_engines()  # must not raise

    # Store must remain attached — caller (NexusFS.close) needs the handle
    # to attempt sync teardown and surface the failure.
    assert nx._record_store is rs
    rs.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_adispose_falls_back_to_sync_close_when_aclose_missing():
    """Legacy stores without aclose() get sync close on success."""
    rs = MagicMock(spec=["close"])
    rs.close = MagicMock()
    nx = _make_stub_nexus_fs(rs)

    await nx.adispose_async_engines()

    rs.close.assert_called_once()
    assert nx._record_store is None


@pytest.mark.asyncio
async def test_adispose_preserves_record_store_when_legacy_close_fails():
    """Legacy fallback failure also preserves the store for recovery."""
    rs = MagicMock(spec=["close"])
    rs.close = MagicMock(side_effect=RuntimeError("simulated close failure"))
    nx = _make_stub_nexus_fs(rs)

    await nx.adispose_async_engines()  # must not raise

    assert nx._record_store is rs


@pytest.mark.asyncio
async def test_adispose_noop_when_record_store_absent():
    nx = _make_stub_nexus_fs(None)
    await nx.adispose_async_engines()  # must not raise
    assert nx._record_store is None
