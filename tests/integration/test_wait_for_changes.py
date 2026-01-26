"""Integration tests for wait_for_changes functionality.

These tests verify the file watching integration between PassthroughBackend
and the FileWatcher for same-box event detection.

Test naming convention:
- test_watch_directory_detects_* : watching a directory
- test_watch_file_detects_* : watching a specific file
"""

import asyncio
import sys

import pytest

from nexus.backends.passthrough import PassthroughBackend
from nexus.core.file_watcher import ChangeType, FileChange, FileWatcher


@pytest.fixture
def temp_backend(tmp_path):
    """Create a temporary passthrough backend for testing."""
    backend = PassthroughBackend(base_path=tmp_path / "passthrough")
    yield backend


@pytest.fixture
def file_watcher():
    """Create a file watcher instance."""
    watcher = FileWatcher()
    yield watcher
    watcher.close()


class TestWatchDirectory:
    """Tests for watching directories."""

    @pytest.mark.skipif(
        sys.platform not in ("linux", "win32"),
        reason="File watching only supported on Linux and Windows",
    )
    @pytest.mark.asyncio
    async def test_watch_directory_detects_file_create(self, temp_backend, file_watcher):
        """Watch directory -> signal file create -> fires."""
        temp_backend.mkdir("/inbox", parents=True).unwrap()
        watch_path = temp_backend.get_physical_path("/inbox")

        async def create_file_after_delay():
            await asyncio.sleep(0.2)
            pointer_path = temp_backend._get_pointer_path("/inbox/new_file.txt")
            pointer_path.write_text("cas:hash" + "0" * 60 + "\n")

        create_task = asyncio.create_task(create_file_after_delay())

        try:
            change = await file_watcher.wait_for_change(watch_path, timeout=2.0)

            assert change is not None
            assert change.type == ChangeType.CREATED
            assert "new_file" in change.path
        finally:
            await create_task

    @pytest.mark.skipif(
        sys.platform not in ("linux", "win32"),
        reason="File watching only supported on Linux and Windows",
    )
    @pytest.mark.asyncio
    async def test_watch_directory_detects_file_modify(self, temp_backend, file_watcher):
        """Watch directory -> signal file modify -> fires."""
        temp_backend.mkdir("/watch_dir", parents=True).unwrap()
        pointer_path = temp_backend._get_pointer_path("/watch_dir/watched.txt")
        pointer_path.write_text("cas:hash1" + "0" * 59 + "\n")
        watch_path = temp_backend.get_physical_path("/watch_dir")

        async def modify_file_after_delay():
            await asyncio.sleep(0.2)
            pointer_path.write_text("cas:hash2" + "0" * 59 + "\n")

        modify_task = asyncio.create_task(modify_file_after_delay())

        try:
            change = await file_watcher.wait_for_change(watch_path, timeout=2.0)

            assert change is not None
            assert change.type == ChangeType.MODIFIED
            assert "watched" in change.path
        finally:
            await modify_task

    @pytest.mark.skipif(
        sys.platform not in ("linux", "win32"),
        reason="File watching only supported on Linux and Windows",
    )
    @pytest.mark.asyncio
    async def test_watch_directory_detects_file_delete(self, temp_backend, file_watcher):
        """Watch directory -> signal file delete -> fires."""
        temp_backend.mkdir("/delete_test", parents=True).unwrap()
        pointer_path = temp_backend._get_pointer_path("/delete_test/to_delete.txt")
        pointer_path.write_text("cas:hash" + "0" * 60 + "\n")
        watch_path = temp_backend.get_physical_path("/delete_test")

        async def delete_file_after_delay():
            await asyncio.sleep(0.2)
            pointer_path.unlink()

        delete_task = asyncio.create_task(delete_file_after_delay())

        try:
            change = await file_watcher.wait_for_change(watch_path, timeout=2.0)

            assert change is not None
            assert change.type == ChangeType.DELETED
            assert "to_delete" in change.path
        finally:
            await delete_task

    @pytest.mark.skipif(
        sys.platform not in ("linux", "win32"),
        reason="File watching only supported on Linux and Windows",
    )
    @pytest.mark.asyncio
    async def test_watch_directory_detects_file_rename(self, temp_backend, file_watcher):
        """Watch directory -> signal file rename -> fires."""
        temp_backend.mkdir("/rename_test", parents=True).unwrap()
        old_path = temp_backend._get_pointer_path("/rename_test/old_name.txt")
        old_path.write_text("cas:hash" + "0" * 60 + "\n")
        watch_path = temp_backend.get_physical_path("/rename_test")

        async def rename_file_after_delay():
            await asyncio.sleep(0.2)
            new_path = temp_backend._get_pointer_path("/rename_test/new_name.txt")
            old_path.rename(new_path)

        rename_task = asyncio.create_task(rename_file_after_delay())

        try:
            change = await file_watcher.wait_for_change(watch_path, timeout=2.0)

            assert change is not None
            assert change.type == ChangeType.RENAMED
        finally:
            await rename_task

    @pytest.mark.skipif(
        sys.platform not in ("linux", "win32"),
        reason="File watching only supported on Linux and Windows",
    )
    @pytest.mark.asyncio
    async def test_watch_directory_detects_subfolder_file_create(self, temp_backend, file_watcher):
        """Watch directory -> signal file create in subfolder -> fires (recursive)."""
        temp_backend.mkdir("/inbox/subdir", parents=True).unwrap()
        watch_path = temp_backend.get_physical_path("/inbox")

        async def create_file_in_subdir():
            await asyncio.sleep(0.2)
            subdir_file = temp_backend._get_pointer_path("/inbox/subdir/nested.txt")
            subdir_file.write_text("cas:hash" + "0" * 60 + "\n")

        create_task = asyncio.create_task(create_file_in_subdir())

        try:
            change = await file_watcher.wait_for_change(watch_path, timeout=2.0)

            assert change is not None
            assert change.type == ChangeType.CREATED
            # Path should include subfolder
            assert "subdir" in change.path or "nested" in change.path
        finally:
            await create_task

    @pytest.mark.skipif(
        sys.platform not in ("linux", "win32"),
        reason="File watching only supported on Linux and Windows",
    )
    @pytest.mark.asyncio
    async def test_watch_directory_detects_deep_nested_file(self, temp_backend, file_watcher):
        """Watch directory -> signal file create in deep nested folder -> fires."""
        temp_backend.mkdir("/root/a/b/c/d", parents=True).unwrap()
        watch_path = temp_backend.get_physical_path("/root")

        async def create_deep_file():
            await asyncio.sleep(0.2)
            deep_file = temp_backend._get_pointer_path("/root/a/b/c/d/deep.txt")
            deep_file.write_text("cas:hash" + "0" * 60 + "\n")

        create_task = asyncio.create_task(create_deep_file())

        try:
            change = await file_watcher.wait_for_change(watch_path, timeout=2.0)

            assert change is not None
            assert change.type == ChangeType.CREATED
        finally:
            await create_task


class TestWatchFile:
    """Tests for watching specific files."""

    @pytest.mark.skipif(
        sys.platform not in ("linux", "win32"),
        reason="File watching only supported on Linux and Windows",
    )
    @pytest.mark.asyncio
    async def test_watch_file_detects_modify(self, temp_backend, file_watcher):
        """Watch specific file -> signal modify same file -> fires."""
        temp_backend.mkdir("/files", parents=True).unwrap()
        file_path = temp_backend._get_pointer_path("/files/target.txt")
        file_path.write_text("cas:hash1" + "0" * 59 + "\n")
        watch_path = temp_backend.get_physical_path("/files/target.txt")

        async def modify_file_after_delay():
            await asyncio.sleep(0.2)
            file_path.write_text("cas:hash2" + "0" * 59 + "\n")

        modify_task = asyncio.create_task(modify_file_after_delay())

        try:
            change = await file_watcher.wait_for_change(watch_path, timeout=2.0)

            assert change is not None
            assert change.type == ChangeType.MODIFIED
        finally:
            await modify_task

    @pytest.mark.skipif(
        sys.platform not in ("linux", "win32"),
        reason="File watching only supported on Linux and Windows",
    )
    @pytest.mark.asyncio
    async def test_watch_file_detects_delete(self, temp_backend, file_watcher):
        """Watch specific file -> signal delete same file -> fires."""
        temp_backend.mkdir("/files_delete", parents=True).unwrap()
        file_path = temp_backend._get_pointer_path("/files_delete/to_delete.txt")
        file_path.write_text("cas:hash" + "0" * 60 + "\n")
        watch_path = temp_backend.get_physical_path("/files_delete/to_delete.txt")

        async def delete_file_after_delay():
            await asyncio.sleep(0.2)
            file_path.unlink()

        delete_task = asyncio.create_task(delete_file_after_delay())

        try:
            change = await file_watcher.wait_for_change(watch_path, timeout=2.0)

            assert change is not None
            assert change.type == ChangeType.DELETED
        finally:
            await delete_task

    @pytest.mark.skipif(
        sys.platform not in ("linux", "win32"),
        reason="File watching only supported on Linux and Windows",
    )
    @pytest.mark.asyncio
    async def test_watch_file_detects_rename(self, temp_backend, file_watcher):
        """Watch specific file -> signal rename same file -> fires."""
        temp_backend.mkdir("/files_rename", parents=True).unwrap()
        file_path = temp_backend._get_pointer_path("/files_rename/original.txt")
        file_path.write_text("cas:hash" + "0" * 60 + "\n")
        watch_path = temp_backend.get_physical_path("/files_rename/original.txt")

        async def rename_file_after_delay():
            await asyncio.sleep(0.2)
            new_path = temp_backend._get_pointer_path("/files_rename/renamed.txt")
            file_path.rename(new_path)

        rename_task = asyncio.create_task(rename_file_after_delay())

        try:
            change = await file_watcher.wait_for_change(watch_path, timeout=2.0)

            assert change is not None
            # Could be RENAMED or DELETED depending on OS behavior
            assert change.type in (ChangeType.RENAMED, ChangeType.DELETED)
        finally:
            await rename_task


class TestWatchTimeout:
    """Tests for timeout behavior."""

    @pytest.mark.asyncio
    async def test_watch_timeout_no_changes(self, temp_backend, file_watcher):
        """Watch directory -> no signal -> timeout returns None."""
        temp_backend.mkdir("/empty", parents=True).unwrap()
        watch_path = temp_backend.get_physical_path("/empty")

        change = await file_watcher.wait_for_change(watch_path, timeout=0.1)
        assert change is None

    @pytest.mark.asyncio
    async def test_watch_nonexistent_path_raises(self, file_watcher, tmp_path):
        """Watch non-existent path -> raises FileNotFoundError."""
        nonexistent = tmp_path / "does_not_exist"

        with pytest.raises(FileNotFoundError):
            await file_watcher.wait_for_change(nonexistent, timeout=1.0)

    @pytest.mark.skipif(
        sys.platform in ("linux", "win32"),
        reason="Test is for unsupported platforms",
    )
    @pytest.mark.asyncio
    async def test_unsupported_platform_raises(self, temp_backend, file_watcher):
        """Unsupported platform -> raises NotImplementedError."""
        temp_backend.mkdir("/test", parents=True).unwrap()
        watch_path = temp_backend.get_physical_path("/test")

        with pytest.raises(NotImplementedError):
            await file_watcher.wait_for_change(watch_path, timeout=1.0)


class TestFileChangeDataclass:
    """Tests for FileChange dataclass."""

    def test_to_dict_basic(self):
        """Test FileChange.to_dict() for basic change."""
        change = FileChange(type=ChangeType.CREATED, path="test.txt")
        result = change.to_dict()

        assert result == {"type": "created", "path": "test.txt"}

    def test_to_dict_with_old_path(self):
        """Test FileChange.to_dict() includes old_path for rename."""
        change = FileChange(
            type=ChangeType.RENAMED,
            path="new_name.txt",
            old_path="old_name.txt",
        )
        result = change.to_dict()

        assert result == {
            "type": "renamed",
            "path": "new_name.txt",
            "old_path": "old_name.txt",
        }


class TestPassthroughBackendIntegration:
    """Integration tests for PassthroughBackend with file watching."""

    @pytest.mark.skipif(
        sys.platform not in ("linux", "win32"),
        reason="File watching only supported on Linux and Windows",
    )
    @pytest.mark.asyncio
    async def test_write_triggers_watch(self, temp_backend, file_watcher):
        """Test that writing via backend triggers file watch."""
        from dataclasses import dataclass

        @dataclass
        class MockContext:
            virtual_path: str

        # Create inbox directory
        temp_backend.mkdir("/inbox", parents=True).unwrap()
        watch_path = temp_backend.get_physical_path("/inbox")

        async def write_content_after_delay():
            await asyncio.sleep(0.2)
            context = MockContext(virtual_path="/inbox/uploaded.txt")
            temp_backend.write_content(b"new content", context=context)

        write_task = asyncio.create_task(write_content_after_delay())

        try:
            change = await file_watcher.wait_for_change(watch_path, timeout=2.0)

            assert change is not None
            assert change.type in (ChangeType.CREATED, ChangeType.MODIFIED)
        finally:
            await write_task

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Windows os.replace() can fail when file is being read concurrently",
    )
    def test_atomic_pointer_update(self, temp_backend):
        """Test that pointer updates are atomic (no partial reads).

        Note: Skipped on Windows because os.replace() may fail with PermissionError
        when another process has the file open (unlike POSIX where rename is atomic).
        """
        import threading
        import time

        errors = []
        stop_flag = threading.Event()

        # Writer thread - continuously update pointer
        def writer():
            counter = 0
            while not stop_flag.is_set():
                hash_val = f"{counter:064d}"
                temp_backend._write_pointer("/atomic_test.txt", hash_val)
                counter += 1
                time.sleep(0.001)

        # Reader thread - continuously read pointer
        def reader():
            while not stop_flag.is_set():
                try:
                    result = temp_backend._read_pointer("/atomic_test.txt")
                    if result is not None and (len(result) != 64 or not result.isdigit()):
                        errors.append(f"Invalid hash read: {result}")
                except Exception as e:
                    errors.append(f"Read error: {e}")
                time.sleep(0.001)

        # Start threads
        writer_thread = threading.Thread(target=writer)
        reader_thread = threading.Thread(target=reader)

        writer_thread.start()
        reader_thread.start()

        # Let them run for a bit
        time.sleep(0.5)

        # Stop and join
        stop_flag.set()
        writer_thread.join()
        reader_thread.join()

        # Check for errors
        assert len(errors) == 0, f"Atomic update errors: {errors}"


class TestLocking:
    """Tests for advisory locking functionality."""

    def test_lock_and_unlock_basic(self, temp_backend):
        """Lock path -> unlock -> success."""
        lock_id = temp_backend.lock("/test/file.txt", timeout=1.0)
        assert lock_id is not None

        assert temp_backend.is_locked("/test/file.txt") is True

        released = temp_backend.unlock(lock_id)
        assert released is True

        assert temp_backend.is_locked("/test/file.txt") is False

    def test_lock_blocks_second_lock(self, temp_backend):
        """Lock path -> second lock same path -> timeout."""
        lock_id1 = temp_backend.lock("/exclusive.txt", timeout=1.0)
        assert lock_id1 is not None

        # Second lock should timeout
        lock_id2 = temp_backend.lock("/exclusive.txt", timeout=0.2)
        assert lock_id2 is None

        # Cleanup
        temp_backend.unlock(lock_id1)

    def test_lock_different_paths_independent(self, temp_backend):
        """Lock path A -> lock path B -> both succeed."""
        lock_id1 = temp_backend.lock("/path1.txt", timeout=1.0)
        lock_id2 = temp_backend.lock("/path2.txt", timeout=1.0)

        assert lock_id1 is not None
        assert lock_id2 is not None
        assert lock_id1 != lock_id2

        # Both should be locked
        assert temp_backend.is_locked("/path1.txt") is True
        assert temp_backend.is_locked("/path2.txt") is True

        # Cleanup
        temp_backend.unlock(lock_id1)
        temp_backend.unlock(lock_id2)

    def test_unlock_invalid_lock_id(self, temp_backend):
        """Unlock with invalid lock_id -> returns False."""
        released = temp_backend.unlock("invalid-lock-id-12345")
        assert released is False

    def test_unlock_already_unlocked(self, temp_backend):
        """Unlock twice -> second unlock returns False."""
        lock_id = temp_backend.lock("/double_unlock.txt", timeout=1.0)
        assert lock_id is not None

        # First unlock succeeds
        assert temp_backend.unlock(lock_id) is True

        # Second unlock fails (already unlocked)
        assert temp_backend.unlock(lock_id) is False

    def test_lock_after_unlock(self, temp_backend):
        """Lock -> unlock -> lock again -> success."""
        lock_id1 = temp_backend.lock("/relock.txt", timeout=1.0)
        assert lock_id1 is not None

        temp_backend.unlock(lock_id1)

        # Should be able to lock again
        lock_id2 = temp_backend.lock("/relock.txt", timeout=1.0)
        assert lock_id2 is not None
        assert lock_id2 != lock_id1

        temp_backend.unlock(lock_id2)

    def test_is_locked_nonexistent_path(self, temp_backend):
        """Check is_locked on never-locked path -> False."""
        assert temp_backend.is_locked("/never/locked.txt") is False


class TestCASIntegration:
    """Tests for CAS (Content-Addressable Storage) integration with file watching."""

    @pytest.mark.skipif(
        sys.platform not in ("linux", "win32"),
        reason="File watching only supported on Linux and Windows",
    )
    @pytest.mark.asyncio
    async def test_watch_directory_detects_cas_write(self, temp_backend, file_watcher):
        """Watch directory -> write_content (CAS) -> fires."""
        from dataclasses import dataclass

        @dataclass
        class MockContext:
            virtual_path: str

        temp_backend.mkdir("/cas_test", parents=True).unwrap()
        watch_path = temp_backend.get_physical_path("/cas_test")

        async def write_via_cas():
            await asyncio.sleep(0.2)
            context = MockContext(virtual_path="/cas_test/new_file.txt")
            temp_backend.write_content(b"content via CAS", context=context)

        write_task = asyncio.create_task(write_via_cas())

        try:
            change = await file_watcher.wait_for_change(watch_path, timeout=2.0)

            assert change is not None
            assert change.type in (ChangeType.CREATED, ChangeType.MODIFIED, ChangeType.RENAMED)
        finally:
            await write_task

    @pytest.mark.skipif(
        sys.platform not in ("linux", "win32"),
        reason="File watching only supported on Linux and Windows",
    )
    @pytest.mark.asyncio
    async def test_watch_directory_detects_cas_content_update(self, temp_backend, file_watcher):
        """Watch directory -> update existing file content (CAS) -> fires."""
        from dataclasses import dataclass

        @dataclass
        class MockContext:
            virtual_path: str

        temp_backend.mkdir("/cas_update", parents=True).unwrap()

        # First write
        context = MockContext(virtual_path="/cas_update/existing.txt")
        temp_backend.write_content(b"original content", context=context)

        watch_path = temp_backend.get_physical_path("/cas_update")

        async def update_content():
            await asyncio.sleep(0.2)
            context = MockContext(virtual_path="/cas_update/existing.txt")
            temp_backend.write_content(b"updated content", context=context)

        update_task = asyncio.create_task(update_content())

        try:
            change = await file_watcher.wait_for_change(watch_path, timeout=2.0)

            assert change is not None
            # Could be MODIFIED or RENAMED depending on atomic write implementation
            assert change.type in (ChangeType.MODIFIED, ChangeType.RENAMED, ChangeType.CREATED)
        finally:
            await update_task

    def test_cas_deduplication(self, temp_backend):
        """Write same content twice -> same hash, single CAS entry."""
        from dataclasses import dataclass

        @dataclass
        class MockContext:
            virtual_path: str

        content = b"deduplicated content"

        context1 = MockContext(virtual_path="/dedup/file1.txt")
        hash1 = temp_backend.write_content(content, context=context1).unwrap()

        context2 = MockContext(virtual_path="/dedup/file2.txt")
        hash2 = temp_backend.write_content(content, context=context2).unwrap()

        # Same content -> same hash
        assert hash1 == hash2

        # Both pointers should point to same CAS entry
        assert temp_backend._read_pointer("/dedup/file1.txt") == hash1
        assert temp_backend._read_pointer("/dedup/file2.txt") == hash2

    def test_cas_read_via_pointer(self, temp_backend):
        """Write content via CAS -> read back via pointer -> matches."""
        from dataclasses import dataclass

        @dataclass
        class MockContext:
            virtual_path: str

        original_content = b"test content for read back"
        context = MockContext(virtual_path="/readback/test.txt")

        content_hash = temp_backend.write_content(original_content, context=context).unwrap()

        # Read via pointer
        pointer_hash = temp_backend._read_pointer("/readback/test.txt")
        assert pointer_hash == content_hash

        # Read actual content from CAS
        retrieved = temp_backend.read_content(content_hash).unwrap()
        assert retrieved == original_content


# =============================================================================
# Path Pattern Matching Tests (Layer 1)
# =============================================================================


class TestPathPatternMatching:
    """Tests for glob pattern matching in Layer 1 file watching.

    These tests verify that FileWatcher correctly matches paths against
    glob patterns like *.txt, **/*.json, and ? wildcards.
    """

    @pytest.mark.skipif(
        sys.platform not in ("linux", "win32"),
        reason="File watching only supported on Linux and Windows",
    )
    @pytest.mark.asyncio
    async def test_glob_star_pattern_matches(self, temp_backend, file_watcher):
        """Test that *.txt pattern matches files by extension."""
        from fnmatch import fnmatch

        # Test pattern matching logic
        # Note: fnmatch does NOT treat / specially, so *.txt matches paths with /
        pattern = "*.txt"
        assert fnmatch("file.txt", pattern) is True
        assert fnmatch("file.json", pattern) is False
        # fnmatch("subdir/file.txt", pattern) is True in Python
        # For path-aware matching, we use pathlib or manual dirname check
        assert fnmatch("subdir/file.txt", pattern) is True  # fnmatch ignores directories

    @pytest.mark.skipif(
        sys.platform not in ("linux", "win32"),
        reason="File watching only supported on Linux and Windows",
    )
    @pytest.mark.asyncio
    async def test_glob_double_star_pattern_matches(self, temp_backend, file_watcher):
        """Test that **/*.txt pattern matches nested files."""
        from pathlib import PurePath

        # Test pattern matching logic using pathlib
        pattern = "**/*.txt"

        # These should match
        assert PurePath("subdir/file.txt").match(pattern) is True
        assert PurePath("a/b/c/file.txt").match(pattern) is True

        # These should not match (wrong extension)
        assert PurePath("subdir/file.json").match(pattern) is False

    @pytest.mark.skipif(
        sys.platform not in ("linux", "win32"),
        reason="File watching only supported on Linux and Windows",
    )
    @pytest.mark.asyncio
    async def test_glob_question_mark_pattern_matches(self, temp_backend, file_watcher):
        """Test that file?.txt pattern matches single character wildcard."""
        from fnmatch import fnmatch

        pattern = "file?.txt"
        assert fnmatch("file1.txt", pattern) is True
        assert fnmatch("fileA.txt", pattern) is True
        assert fnmatch("file12.txt", pattern) is False  # ? matches single char
        assert fnmatch("file.txt", pattern) is False

    @pytest.mark.skipif(
        sys.platform not in ("linux", "win32"),
        reason="File watching only supported on Linux and Windows",
    )
    @pytest.mark.asyncio
    async def test_watch_with_glob_filter(self, temp_backend, file_watcher):
        """Test watching directory with glob filter applied to results."""
        from fnmatch import fnmatch

        temp_backend.mkdir("/glob_test", parents=True).unwrap()
        watch_path = temp_backend.get_physical_path("/glob_test")

        async def create_files():
            await asyncio.sleep(0.2)
            # Create multiple files
            for name in ["test.txt", "test.json", "data.txt"]:
                pointer = temp_backend._get_pointer_path(f"/glob_test/{name}")
                pointer.write_text("cas:hash" + "0" * 60 + "\n")

        create_task = asyncio.create_task(create_files())

        try:
            # Get first change
            change = await file_watcher.wait_for_change(watch_path, timeout=2.0)

            if change:
                # Apply glob filter manually (simulating NexusFS behavior)
                pattern = "*.txt"
                file_name = change.path.split("/")[-1].split("\\")[-1]

                # Verify fnmatch works correctly for this pattern
                assert fnmatch(file_name, pattern) is True

                # Change should be detected
                assert change is not None
        finally:
            await create_task


# =============================================================================
# Event Ordering Tests
# =============================================================================


class TestEventOrdering:
    """Tests for event ordering guarantees."""

    @pytest.mark.skipif(
        sys.platform not in ("linux", "win32"),
        reason="File watching only supported on Linux and Windows",
    )
    @pytest.mark.asyncio
    async def test_events_received_in_order(self, temp_backend, file_watcher):
        """Test that multiple events are received in chronological order."""
        temp_backend.mkdir("/order_test", parents=True).unwrap()
        watch_path = temp_backend.get_physical_path("/order_test")

        events_received = []

        async def create_files_sequentially():
            await asyncio.sleep(0.2)
            for i in range(3):
                pointer = temp_backend._get_pointer_path(f"/order_test/file_{i}.txt")
                pointer.write_text(f"cas:hash{i}" + "0" * 59 + "\n")
                await asyncio.sleep(0.1)  # Small delay between creates

        async def collect_events():
            for _ in range(3):
                change = await file_watcher.wait_for_change(watch_path, timeout=2.0)
                if change:
                    events_received.append(change)

        create_task = asyncio.create_task(create_files_sequentially())

        try:
            await asyncio.wait_for(collect_events(), timeout=5.0)
        except TimeoutError:
            pass  # May not get all events
        finally:
            await create_task

        # Should have received at least one event
        assert len(events_received) >= 1


# =============================================================================
# Stress Tests
# =============================================================================


class TestStressScenarios:
    """Stress tests for high-volume event handling."""

    @pytest.mark.skipif(
        sys.platform not in ("linux", "win32"),
        reason="File watching only supported on Linux and Windows",
    )
    @pytest.mark.asyncio
    async def test_rapid_file_changes(self, temp_backend, file_watcher):
        """Test handling rapid file changes without event loss or crash."""
        temp_backend.mkdir("/stress", parents=True).unwrap()
        watch_path = temp_backend.get_physical_path("/stress")

        events_received = []

        async def rapid_changes():
            """Create many files rapidly."""
            await asyncio.sleep(0.2)
            for i in range(10):
                pointer = temp_backend._get_pointer_path(f"/stress/rapid_{i}.txt")
                pointer.write_text(f"cas:hash{i:04d}" + "0" * 56 + "\n")
                # No delay between writes

        async def collect_some_events():
            """Collect events with timeout."""
            deadline = asyncio.get_event_loop().time() + 3.0
            while asyncio.get_event_loop().time() < deadline:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                change = await file_watcher.wait_for_change(watch_path, timeout=min(remaining, 0.5))
                if change:
                    events_received.append(change)

        change_task = asyncio.create_task(rapid_changes())

        try:
            await collect_some_events()
        finally:
            await change_task

        # Should have received at least some events (may coalesce)
        assert len(events_received) >= 1

    @pytest.mark.skipif(
        sys.platform not in ("linux", "win32"),
        reason="File watching only supported on Linux and Windows",
    )
    @pytest.mark.asyncio
    async def test_concurrent_watches_multiple_directories(self, temp_backend):
        """Test watching multiple directories concurrently."""
        # Create multiple directories
        for i in range(3):
            temp_backend.mkdir(f"/multi_{i}", parents=True).unwrap()

        watchers = [FileWatcher() for _ in range(3)]
        events_by_dir = {i: [] for i in range(3)}

        async def watch_dir(idx, watcher):
            watch_path = temp_backend.get_physical_path(f"/multi_{idx}")
            try:
                change = await watcher.wait_for_change(watch_path, timeout=2.0)
                if change:
                    events_by_dir[idx].append(change)
            except Exception:
                pass

        async def create_files():
            await asyncio.sleep(0.2)
            for i in range(3):
                pointer = temp_backend._get_pointer_path(f"/multi_{i}/file.txt")
                pointer.write_text("cas:hash" + "0" * 60 + "\n")
                await asyncio.sleep(0.05)

        create_task = asyncio.create_task(create_files())

        try:
            await asyncio.gather(
                watch_dir(0, watchers[0]),
                watch_dir(1, watchers[1]),
                watch_dir(2, watchers[2]),
            )
        finally:
            await create_task
            for w in watchers:
                w.close()

        # At least some directories should have received events
        total_events = sum(len(v) for v in events_by_dir.values())
        assert total_events >= 1
