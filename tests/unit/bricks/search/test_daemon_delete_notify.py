"""SearchDaemon delete notification regressions."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_notify_file_change_delete_prunes_indexes_immediately() -> None:
    """Delete hooks should hide removed files without waiting for debounce."""
    from nexus.bricks.search.daemon import SearchDaemon

    daemon = SearchDaemon.__new__(SearchDaemon)
    daemon.config = SimpleNamespace(refresh_enabled=True)
    daemon._mutation_resolver = None
    daemon._pending_refresh_paths = {"/workspace/demo/delete-test.md"}
    daemon._pending_delete_paths = set()
    daemon._mutation_wakeup = asyncio.Event()
    daemon._delete_indexes_for_paths = AsyncMock()

    await SearchDaemon.notify_file_change(
        daemon,
        "/workspace/demo/delete-test.md",
        "delete",
    )

    daemon._delete_indexes_for_paths.assert_awaited_once_with(["/workspace/demo/delete-test.md"])
    assert "/workspace/demo/delete-test.md" in daemon._pending_delete_paths
    assert "/workspace/demo/delete-test.md" not in daemon._pending_refresh_paths
