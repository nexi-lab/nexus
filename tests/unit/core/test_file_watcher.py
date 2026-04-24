"""Unit tests for FileWatcher kernel primitive."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from nexus.core.file_events import FileEvent, FileEventType
from nexus.core.file_watcher import FileWatcher, RemoteWatchProtocol


def _make_event(path: str = "/test/file.txt") -> FileEvent:
    return FileEvent(
        type=FileEventType.FILE_WRITE,
        path=path,
    )


class TestFileWatcherLocal:
    """Test local OBSERVE-path watch (kernel-owned)."""

    @pytest.mark.asyncio
    async def test_on_mutation_resolves_waiter(self) -> None:
        fw = FileWatcher()
        event = _make_event()

        result = None

        async def waiter() -> None:
            nonlocal result
            result = await fw.wait_local("/test/file.txt", timeout=5.0)

        async def mutator() -> None:
            await asyncio.sleep(0.01)
            fw.on_mutation(event)

        await asyncio.gather(waiter(), mutator())
        assert result is not None
        assert result.path == "/test/file.txt"

    @pytest.mark.asyncio
    async def test_wait_local_timeout_returns_none(self) -> None:
        fw = FileWatcher()
        result = await fw.wait_local("/test/file.txt", timeout=0.05)
        assert result is None

    @pytest.mark.asyncio
    async def test_on_mutation_only_resolves_matching_path(self) -> None:
        fw = FileWatcher()

        result_a = None
        result_b = None

        async def waiter_a() -> None:
            nonlocal result_a
            result_a = await fw.wait_local("/test/a.txt", timeout=0.1)

        async def waiter_b() -> None:
            nonlocal result_b
            result_b = await fw.wait_local("/test/b.txt", timeout=5.0)

        async def mutator() -> None:
            await asyncio.sleep(0.01)
            fw.on_mutation(_make_event("/test/b.txt"))

        await asyncio.gather(waiter_a(), waiter_b(), mutator())
        assert result_a is None  # didn't match
        assert result_b is not None
        assert result_b.path == "/test/b.txt"

    def test_hook_spec_returns_observer(self) -> None:
        fw = FileWatcher()
        spec = fw.hook_spec()
        assert fw in spec.observers


class TestFileWatcherRemote:
    """Test remote watcher (kernel-knows)."""

    def test_no_remote_watcher_by_default(self) -> None:
        fw = FileWatcher()
        assert fw.has_remote_watcher is False

    def test_set_remote_watcher(self) -> None:
        fw = FileWatcher()
        mock_watcher = AsyncMock(spec=RemoteWatchProtocol)
        fw.set_remote_watcher(mock_watcher)
        assert fw.has_remote_watcher is True

    @pytest.mark.asyncio
    async def test_wait_remote_returns_none_when_no_watcher(self) -> None:
        fw = FileWatcher()
        result = await fw._wait_remote("zone1", "/test/file.txt", timeout=0.05)
        assert result is None

    @pytest.mark.asyncio
    async def test_wait_remote_delegates_to_watcher(self) -> None:
        fw = FileWatcher()
        event = _make_event()
        mock_watcher = AsyncMock(spec=RemoteWatchProtocol)
        mock_watcher.wait_for_event.return_value = event
        fw.set_remote_watcher(mock_watcher)

        result = await fw._wait_remote("zone1", "/test/file.txt", timeout=5.0)
        assert result is event
        mock_watcher.wait_for_event.assert_called_once_with(
            zone_id="zone1",
            path_pattern="/test/file.txt",
            timeout=5.0,
        )


class TestFileWatcherWait:
    """Test unified wait() that races local + remote."""

    @pytest.mark.asyncio
    async def test_wait_local_only_when_no_remote(self) -> None:
        fw = FileWatcher()
        event = _make_event()

        async def mutator() -> None:
            await asyncio.sleep(0.01)
            fw.on_mutation(event)

        task = asyncio.create_task(fw.wait("/test/file.txt", timeout=5.0))
        await mutator()
        result = await task
        assert result is not None
        assert result.path == "/test/file.txt"

    @pytest.mark.asyncio
    async def test_wait_races_local_and_remote(self) -> None:
        """When remote resolves first, wait() returns remote result."""
        fw = FileWatcher()
        remote_event = _make_event("/remote/file.txt")
        mock_watcher = AsyncMock(spec=RemoteWatchProtocol)
        mock_watcher.wait_for_event.return_value = remote_event
        fw.set_remote_watcher(mock_watcher)

        result = await fw.wait("/remote/file.txt", timeout=5.0, zone_id="zone2")
        assert result is not None
        assert result.path == "/remote/file.txt"

    @pytest.mark.asyncio
    async def test_wait_local_wins_race(self) -> None:
        """When local resolves first, wait() returns local result."""
        fw = FileWatcher()
        local_event = _make_event("/local/file.txt")

        async def slow_remote(*args, **kwargs):
            await asyncio.sleep(10)
            return None

        mock_watcher = AsyncMock(spec=RemoteWatchProtocol)
        mock_watcher.wait_for_event.side_effect = slow_remote
        fw.set_remote_watcher(mock_watcher)

        async def mutator() -> None:
            await asyncio.sleep(0.01)
            fw.on_mutation(local_event)

        task = asyncio.create_task(fw.wait("/local/file.txt", timeout=5.0))
        await mutator()
        result = await task
        assert result is not None
        assert result.path == "/local/file.txt"

    @pytest.mark.asyncio
    async def test_wait_timeout_returns_none(self) -> None:
        fw = FileWatcher()
        result = await fw.wait("/test/file.txt", timeout=0.05)
        assert result is None
