"""Awaitable compatibility for NexusFS handles."""

from __future__ import annotations

import pytest

from tests.testkit import make_test_nexus


@pytest.mark.asyncio
async def test_nexus_fs_await_returns_self(tmp_path) -> None:
    """Existing integration paths use ``await nexus.connect(...)``.

    ``nexus.connect`` is synchronous today, but the returned filesystem
    handle must remain await-compatible so async callers keep working.
    """
    nx = make_test_nexus(tmp_path)
    try:
        assert await nx is nx
    finally:
        nx.close()
