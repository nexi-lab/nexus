"""Tests for FUSE event firing (Issue #1115)."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.core.event_bus import FileEvent, FileEventType
from nexus.fuse.operations import HAS_EVENT_BUS, NexusFUSEOperations


class MockMountMode:
    """Mock mount mode for testing."""

    def __init__(self, value: str = "binary"):
        self.value = value


class TestFUSEEventFiring:
    """Test FUSE operations fire events correctly."""

    @pytest.fixture
    def mock_nexus_fs(self) -> MagicMock:
        """Create mock NexusFilesystem."""
        fs = MagicMock()
        fs.exists.return_value = True
        fs.is_directory.return_value = False
        fs.read.return_value = b"test content"
        fs.list.return_value = []
        return fs

    @pytest.fixture
    def fuse_ops(self, mock_nexus_fs: MagicMock) -> NexusFUSEOperations:
        """Create FUSE operations instance for testing."""
        ops = NexusFUSEOperations(
            nexus_fs=mock_nexus_fs,
            mode=MockMountMode("binary"),  # type: ignore[arg-type]
            cache_config={"events_enabled": True},
        )
        return ops

    @pytest.fixture
    def event_loop(self) -> asyncio.AbstractEventLoop:
        """Create and set event loop."""
        loop = asyncio.new_event_loop()
        yield loop
        loop.close()

    def test_event_bus_available(self) -> None:
        """Verify event bus is available."""
        assert HAS_EVENT_BUS is True
        assert FileEventType is not None
        assert FileEvent is not None

    def test_fire_event_method_exists(self, fuse_ops: NexusFUSEOperations) -> None:
        """Verify _fire_event method exists."""
        assert hasattr(fuse_ops, "_fire_event")
        assert callable(fuse_ops._fire_event)

    def test_set_event_loop_method_exists(self, fuse_ops: NexusFUSEOperations) -> None:
        """Verify set_event_loop method exists."""
        assert hasattr(fuse_ops, "set_event_loop")
        assert callable(fuse_ops.set_event_loop)

    def test_event_loop_can_be_set(
        self, fuse_ops: NexusFUSEOperations, event_loop: asyncio.AbstractEventLoop
    ) -> None:
        """Test event loop can be set."""
        fuse_ops.set_event_loop(event_loop)
        assert fuse_ops._event_loop is event_loop

    def test_fire_event_no_loop_does_not_crash(
        self, fuse_ops: NexusFUSEOperations
    ) -> None:
        """Test _fire_event doesn't crash without event loop."""
        # Should not raise even without event loop
        fuse_ops._fire_event(FileEventType.FILE_WRITE, "/test/file.txt", size=100)

    def test_write_fires_event(
        self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock
    ) -> None:
        """Test write() fires FILE_WRITE event."""
        # Setup
        mock_nexus_fs.exists.return_value = True
        mock_nexus_fs.read.return_value = b"old"

        # Open file first
        fd = fuse_ops.create("/test.txt", 0o644)

        with patch.object(fuse_ops, "_fire_event") as mock_fire:
            fuse_ops.write("/test.txt", b"new data", 0, fd)
            mock_fire.assert_called_once()
            call_args = mock_fire.call_args
            assert call_args[0][0] == FileEventType.FILE_WRITE
            assert "/test.txt" in call_args[0][1]

    def test_create_fires_event(
        self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock
    ) -> None:
        """Test create() fires FILE_WRITE event."""
        mock_nexus_fs.exists.return_value = False

        with patch.object(fuse_ops, "_fire_event") as mock_fire:
            fuse_ops.create("/new_file.txt", 0o644)
            mock_fire.assert_called_once()
            call_args = mock_fire.call_args
            assert call_args[0][0] == FileEventType.FILE_WRITE
            assert call_args[0][1] == "/new_file.txt"
            assert call_args[1]["size"] == 0

    def test_unlink_fires_event(
        self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock
    ) -> None:
        """Test unlink() fires FILE_DELETE event."""
        with patch.object(fuse_ops, "_fire_event") as mock_fire:
            fuse_ops.unlink("/test.txt")
            mock_fire.assert_called_once()
            call_args = mock_fire.call_args
            assert call_args[0][0] == FileEventType.FILE_DELETE
            assert call_args[0][1] == "/test.txt"

    def test_rename_fires_event(
        self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock
    ) -> None:
        """Test rename() fires FILE_RENAME event."""
        mock_nexus_fs.exists.side_effect = lambda p: p == "/old.txt"
        mock_nexus_fs.is_directory.return_value = False

        with patch.object(fuse_ops, "_fire_event") as mock_fire:
            fuse_ops.rename("/old.txt", "/new.txt")
            mock_fire.assert_called_once()
            call_args = mock_fire.call_args
            assert call_args[0][0] == FileEventType.FILE_RENAME
            assert call_args[0][1] == "/new.txt"
            assert call_args[1]["old_path"] == "/old.txt"

    def test_mkdir_fires_event(
        self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock
    ) -> None:
        """Test mkdir() fires DIR_CREATE event."""
        with patch.object(fuse_ops, "_fire_event") as mock_fire:
            fuse_ops.mkdir("/new_dir", 0o755)
            mock_fire.assert_called_once()
            call_args = mock_fire.call_args
            assert call_args[0][0] == FileEventType.DIR_CREATE
            assert call_args[0][1] == "/new_dir"

    def test_rmdir_fires_event(
        self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock
    ) -> None:
        """Test rmdir() fires DIR_DELETE event."""
        with patch.object(fuse_ops, "_fire_event") as mock_fire:
            fuse_ops.rmdir("/old_dir")
            mock_fire.assert_called_once()
            call_args = mock_fire.call_args
            assert call_args[0][0] == FileEventType.DIR_DELETE
            assert call_args[0][1] == "/old_dir"

    def test_chmod_fires_event(
        self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock
    ) -> None:
        """Test chmod() fires METADATA_CHANGE event."""
        with patch.object(fuse_ops, "_fire_event") as mock_fire:
            fuse_ops.chmod("/test.txt", 0o755)
            mock_fire.assert_called_once()
            call_args = mock_fire.call_args
            assert call_args[0][0] == FileEventType.METADATA_CHANGE
            assert call_args[0][1] == "/test.txt"

    def test_truncate_fires_event(
        self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock
    ) -> None:
        """Test truncate() fires FILE_WRITE event."""
        mock_nexus_fs.read.return_value = b"old content"

        with patch.object(fuse_ops, "_fire_event") as mock_fire:
            fuse_ops.truncate("/test.txt", 5)
            mock_fire.assert_called_once()
            call_args = mock_fire.call_args
            assert call_args[0][0] == FileEventType.FILE_WRITE
            assert call_args[0][1] == "/test.txt"
            assert call_args[1]["size"] == 5

    def test_events_disabled_does_not_fire(
        self, mock_nexus_fs: MagicMock
    ) -> None:
        """Test events not fired when disabled."""
        ops = NexusFUSEOperations(
            nexus_fs=mock_nexus_fs,
            mode=MockMountMode("binary"),  # type: ignore[arg-type]
            cache_config={"events_enabled": False},
        )

        with patch.object(ops, "_dispatch_event") as mock_dispatch:
            ops.mkdir("/test_dir", 0o755)
            mock_dispatch.assert_not_called()
