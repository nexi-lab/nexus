"""Brick compliance tests for the search brick (Issue #2036).

Validates that search brick components satisfy their declared protocols
via ``isinstance()`` checks (structural subtyping with ``@runtime_checkable``).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.bricks.search.lifecycle_adapter import SearchBrickLifecycleAdapter
from nexus.contracts.protocols.brick_lifecycle import BrickLifecycleProtocol
from nexus.contracts.protocols.file_reader import FileReaderProtocol
from nexus.contracts.protocols.search import SearchBrickProtocol


class TestSearchDaemonSatisfiesBrickProtocol:
    """Decision #10: SearchDaemon must satisfy SearchBrickProtocol."""

    def test_search_daemon_has_required_interface(self) -> None:
        from nexus.bricks.search.daemon import SearchDaemon

        daemon = SearchDaemon.__new__(SearchDaemon)
        assert isinstance(daemon, SearchBrickProtocol)


class TestLifecycleAdapterSatisfiesLifecycleProtocol:
    """Decision #10: Lifecycle adapter must satisfy BrickLifecycleProtocol."""

    def test_adapter_satisfies_protocol(self) -> None:
        mock_daemon = MagicMock()
        adapter = SearchBrickLifecycleAdapter(mock_daemon)
        assert isinstance(adapter, BrickLifecycleProtocol)

    @pytest.mark.asyncio
    async def test_adapter_delegates_start(self) -> None:
        mock_daemon = MagicMock()
        mock_daemon.startup = AsyncMock()
        adapter = SearchBrickLifecycleAdapter(mock_daemon)
        await adapter.start()
        mock_daemon.startup.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_adapter_delegates_stop(self) -> None:
        mock_daemon = MagicMock()
        mock_daemon.shutdown = AsyncMock()
        adapter = SearchBrickLifecycleAdapter(mock_daemon)
        await adapter.stop()
        mock_daemon.shutdown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_adapter_health_check_true(self) -> None:
        mock_daemon = MagicMock()
        mock_daemon.get_health.return_value = {"initialized": True}
        adapter = SearchBrickLifecycleAdapter(mock_daemon)
        assert await adapter.health_check() is True

    @pytest.mark.asyncio
    async def test_adapter_health_check_false(self) -> None:
        mock_daemon = MagicMock()
        mock_daemon.get_health.return_value = {"initialized": False}
        adapter = SearchBrickLifecycleAdapter(mock_daemon)
        assert await adapter.health_check() is False


class TestNexusFSFileReaderSatisfiesProtocol:
    """Decision #11: _NexusFSFileReader adapter must satisfy FileReaderProtocol."""

    def test_adapter_satisfies_protocol(self) -> None:
        from nexus.factory import _NexusFSFileReader

        mock_nx = MagicMock()
        reader = _NexusFSFileReader(mock_nx)
        assert isinstance(reader, FileReaderProtocol)
