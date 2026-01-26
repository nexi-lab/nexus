"""Unit tests for FileWatcher callback mode.

Tests cover:
- Callback registration and invocation
- start/stop lifecycle
- add_watch/remove_watch operations
- Cross-platform behavior (Linux inotify / Windows ReadDirectoryChangesW)

Related: Issue #1106 Block 2
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import MagicMock

import pytest

from nexus.core.file_watcher import ChangeType, FileChange, FileWatcher

# =============================================================================
# FileWatcher Lifecycle Tests
# =============================================================================


class TestFileWatcherLifecycle:
    """Tests for FileWatcher start/stop lifecycle."""

    def test_initial_state(self):
        """Test FileWatcher initial state."""
        watcher = FileWatcher()
        assert watcher._started is False
        assert watcher._watches == {}
        watcher.close()

    @pytest.mark.skipif(
        sys.platform not in ("linux", "win32"),
        reason="File watching only supported on Linux and Windows",
    )
    def test_start_sets_started_flag(self):
        """Test that start() sets _started flag."""
        watcher = FileWatcher()
        try:
            watcher.start()
            assert watcher._started is True
        finally:
            watcher.stop()
            watcher.close()

    @pytest.mark.skipif(
        sys.platform not in ("linux", "win32"),
        reason="File watching only supported on Linux and Windows",
    )
    def test_stop_clears_started_flag(self):
        """Test that stop() clears _started flag."""
        watcher = FileWatcher()
        try:
            watcher.start()
            assert watcher._started is True
            watcher.stop()
            assert watcher._started is False
        finally:
            watcher.close()

    @pytest.mark.skipif(
        sys.platform not in ("linux", "win32"),
        reason="File watching only supported on Linux and Windows",
    )
    def test_double_start_is_safe(self):
        """Test that calling start() twice is safe."""
        watcher = FileWatcher()
        try:
            watcher.start()
            watcher.start()  # Should not raise
            assert watcher._started is True
        finally:
            watcher.stop()
            watcher.close()

    @pytest.mark.skipif(
        sys.platform not in ("linux", "win32"),
        reason="File watching only supported on Linux and Windows",
    )
    def test_double_stop_is_safe(self):
        """Test that calling stop() twice is safe."""
        watcher = FileWatcher()
        try:
            watcher.start()
            watcher.stop()
            watcher.stop()  # Should not raise
            assert watcher._started is False
        finally:
            watcher.close()


# =============================================================================
# Callback Registration Tests
# =============================================================================


class TestCallbackRegistration:
    """Tests for add_watch/remove_watch callback registration."""

    @pytest.mark.skipif(
        sys.platform not in ("linux", "win32"),
        reason="File watching only supported on Linux and Windows",
    )
    def test_add_watch_registers_callback(self, tmp_path):
        """Test that add_watch() registers a callback."""
        watcher = FileWatcher()
        callback = MagicMock()

        try:
            watcher.start()
            watcher.add_watch(tmp_path, callback)

            # Callback should be registered
            assert str(tmp_path) in watcher._watches or tmp_path in watcher._watches
        finally:
            watcher.stop()
            watcher.close()

    @pytest.mark.skipif(
        sys.platform not in ("linux", "win32"),
        reason="File watching only supported on Linux and Windows",
    )
    def test_add_watch_requires_started(self, tmp_path):
        """Test that add_watch() requires watcher to be started."""
        watcher = FileWatcher()
        callback = MagicMock()

        try:
            # Should raise or handle gracefully
            with pytest.raises((RuntimeError, ValueError)):
                watcher.add_watch(tmp_path, callback)
        finally:
            watcher.close()

    @pytest.mark.skipif(
        sys.platform not in ("linux", "win32"),
        reason="File watching only supported on Linux and Windows",
    )
    def test_remove_watch_unregisters_callback(self, tmp_path):
        """Test that remove_watch() unregisters a callback."""
        watcher = FileWatcher()
        callback = MagicMock()

        try:
            watcher.start()
            watcher.add_watch(tmp_path, callback)
            watcher.remove_watch(tmp_path)

            # Callback should be unregistered
            assert str(tmp_path) not in watcher._watches
        finally:
            watcher.stop()
            watcher.close()

    @pytest.mark.skipif(
        sys.platform not in ("linux", "win32"),
        reason="File watching only supported on Linux and Windows",
    )
    def test_remove_watch_nonexistent_is_safe(self, tmp_path):
        """Test that remove_watch() on non-watched path is safe."""
        watcher = FileWatcher()

        try:
            watcher.start()
            # Should not raise
            watcher.remove_watch(tmp_path / "nonexistent")
        finally:
            watcher.stop()
            watcher.close()


# =============================================================================
# Callback Invocation Tests
# =============================================================================


class TestCallbackInvocation:
    """Tests for callback invocation on file changes."""

    @pytest.mark.skipif(
        sys.platform not in ("linux", "win32"),
        reason="File watching only supported on Linux and Windows",
    )
    @pytest.mark.asyncio
    async def test_callback_invoked_on_file_create(self, tmp_path):
        """Test that callback is invoked when file is created."""
        watcher = FileWatcher()
        received_changes = []

        def callback(change: FileChange):
            received_changes.append(change)

        try:
            loop = asyncio.get_event_loop()
            watcher.start(loop)
            watcher.add_watch(tmp_path, callback, recursive=True)

            # Create a file
            test_file = tmp_path / "new_file.txt"
            await asyncio.sleep(0.1)  # Let watcher settle
            test_file.write_text("hello")

            # Wait for callback
            await asyncio.sleep(0.5)

            # Should have received at least one change
            assert len(received_changes) >= 1
            # At least one should be for our file
            paths = [c.path for c in received_changes]
            assert any("new_file" in p for p in paths)
        finally:
            watcher.stop()
            watcher.close()

    @pytest.mark.skipif(
        sys.platform not in ("linux", "win32"),
        reason="File watching only supported on Linux and Windows",
    )
    @pytest.mark.asyncio
    async def test_callback_invoked_on_file_modify(self, tmp_path):
        """Test that callback is invoked when file is modified."""
        watcher = FileWatcher()
        received_changes = []

        def callback(change: FileChange):
            received_changes.append(change)

        try:
            # Create file first
            test_file = tmp_path / "existing.txt"
            test_file.write_text("original")

            loop = asyncio.get_event_loop()
            watcher.start(loop)
            watcher.add_watch(tmp_path, callback, recursive=True)

            # Modify the file
            await asyncio.sleep(0.1)
            test_file.write_text("modified")

            # Wait for callback
            await asyncio.sleep(0.5)

            # Should have received modification
            assert len(received_changes) >= 1
        finally:
            watcher.stop()
            watcher.close()

    @pytest.mark.skipif(
        sys.platform not in ("linux", "win32"),
        reason="File watching only supported on Linux and Windows",
    )
    @pytest.mark.asyncio
    async def test_callback_invoked_on_file_delete(self, tmp_path):
        """Test that callback is invoked when file is deleted."""
        watcher = FileWatcher()
        received_changes = []

        def callback(change: FileChange):
            received_changes.append(change)

        try:
            # Create file first
            test_file = tmp_path / "to_delete.txt"
            test_file.write_text("delete me")

            loop = asyncio.get_event_loop()
            watcher.start(loop)
            watcher.add_watch(tmp_path, callback, recursive=True)

            # Delete the file
            await asyncio.sleep(0.1)
            test_file.unlink()

            # Wait for callback
            await asyncio.sleep(0.5)

            # Should have received deletion
            assert len(received_changes) >= 1
            # At least one should be DELETE type
            types = [c.type for c in received_changes]
            assert ChangeType.DELETED in types
        finally:
            watcher.stop()
            watcher.close()

    @pytest.mark.skipif(
        sys.platform not in ("linux", "win32"),
        reason="File watching only supported on Linux and Windows",
    )
    @pytest.mark.asyncio
    async def test_callback_receives_filechange_object(self, tmp_path):
        """Test that callback receives FileChange objects."""
        watcher = FileWatcher()
        received_changes = []

        def callback(change: FileChange):
            received_changes.append(change)

        try:
            loop = asyncio.get_event_loop()
            watcher.start(loop)
            watcher.add_watch(tmp_path, callback, recursive=True)

            # Create a file
            test_file = tmp_path / "test.txt"
            await asyncio.sleep(0.1)
            test_file.write_text("test")

            # Wait for callback
            await asyncio.sleep(0.5)

            # Verify FileChange structure
            assert len(received_changes) >= 1
            change = received_changes[0]
            assert isinstance(change, FileChange)
            assert hasattr(change, "type")
            assert hasattr(change, "path")
            assert isinstance(change.type, ChangeType)
        finally:
            watcher.stop()
            watcher.close()


# =============================================================================
# Recursive Watching Tests
# =============================================================================


class TestRecursiveWatching:
    """Tests for recursive directory watching."""

    @pytest.mark.skipif(
        sys.platform not in ("linux", "win32"),
        reason="File watching only supported on Linux and Windows",
    )
    @pytest.mark.asyncio
    async def test_recursive_watch_detects_nested_changes(self, tmp_path):
        """Test that recursive watch detects changes in subdirectories."""
        watcher = FileWatcher()
        received_changes = []

        def callback(change: FileChange):
            received_changes.append(change)

        try:
            # Create nested directory
            nested_dir = tmp_path / "level1" / "level2"
            nested_dir.mkdir(parents=True)

            loop = asyncio.get_event_loop()
            watcher.start(loop)
            watcher.add_watch(tmp_path, callback, recursive=True)

            # Create file in nested directory
            nested_file = nested_dir / "nested.txt"
            await asyncio.sleep(0.1)
            nested_file.write_text("nested content")

            # Wait for callback
            await asyncio.sleep(0.5)

            # Should detect nested file change
            assert len(received_changes) >= 1
            paths = [c.path for c in received_changes]
            assert any("nested" in p or "level" in p for p in paths)
        finally:
            watcher.stop()
            watcher.close()


# =============================================================================
# Backward Compatibility Tests
# =============================================================================


class TestBackwardCompatibility:
    """Tests for backward compatibility with wait_for_change()."""

    @pytest.mark.skipif(
        sys.platform not in ("linux", "win32"),
        reason="File watching only supported on Linux and Windows",
    )
    @pytest.mark.asyncio
    async def test_wait_for_change_still_works(self, tmp_path):
        """Test that wait_for_change() still works alongside callback mode."""
        watcher = FileWatcher()

        try:

            async def create_file():
                await asyncio.sleep(0.2)
                (tmp_path / "wait_test.txt").write_text("content")

            create_task = asyncio.create_task(create_file())

            change = await watcher.wait_for_change(tmp_path, timeout=2.0)

            assert change is not None
            assert change.type in (ChangeType.CREATED, ChangeType.MODIFIED)

            await create_task
        finally:
            watcher.close()

    @pytest.mark.skipif(
        sys.platform not in ("linux", "win32"),
        reason="File watching only supported on Linux and Windows",
    )
    @pytest.mark.asyncio
    async def test_wait_for_change_timeout(self, tmp_path):
        """Test that wait_for_change() returns None on timeout."""
        watcher = FileWatcher()

        try:
            change = await watcher.wait_for_change(tmp_path, timeout=0.1)
            assert change is None
        finally:
            watcher.close()


# =============================================================================
# FileChange Dataclass Tests
# =============================================================================


class TestFileChangeDataclass:
    """Tests for FileChange dataclass."""

    def test_create_basic_change(self):
        """Test creating a basic FileChange."""
        change = FileChange(type=ChangeType.CREATED, path="/test/file.txt")
        assert change.type == ChangeType.CREATED
        assert change.path == "/test/file.txt"
        assert change.old_path is None

    def test_create_rename_change(self):
        """Test creating a rename FileChange with old_path."""
        change = FileChange(
            type=ChangeType.RENAMED,
            path="/test/new_name.txt",
            old_path="/test/old_name.txt",
        )
        assert change.type == ChangeType.RENAMED
        assert change.path == "/test/new_name.txt"
        assert change.old_path == "/test/old_name.txt"

    def test_to_dict_basic(self):
        """Test FileChange.to_dict() for basic change."""
        change = FileChange(type=ChangeType.MODIFIED, path="/test/file.txt")
        result = change.to_dict()

        assert result["type"] == "modified"
        assert result["path"] == "/test/file.txt"
        assert "old_path" not in result

    def test_to_dict_with_old_path(self):
        """Test FileChange.to_dict() includes old_path for rename."""
        change = FileChange(
            type=ChangeType.RENAMED,
            path="/test/new.txt",
            old_path="/test/old.txt",
        )
        result = change.to_dict()

        assert result["type"] == "renamed"
        assert result["path"] == "/test/new.txt"
        assert result["old_path"] == "/test/old.txt"


# =============================================================================
# ChangeType Enum Tests
# =============================================================================


class TestChangeTypeEnum:
    """Tests for ChangeType enum."""

    def test_all_change_types_defined(self):
        """Test that all expected change types are defined."""
        assert ChangeType.CREATED.value == "created"
        assert ChangeType.MODIFIED.value == "modified"
        assert ChangeType.DELETED.value == "deleted"
        assert ChangeType.RENAMED.value == "renamed"

    def test_change_type_is_string(self):
        """Test that change type values are strings."""
        for change_type in ChangeType:
            assert isinstance(change_type.value, str)
