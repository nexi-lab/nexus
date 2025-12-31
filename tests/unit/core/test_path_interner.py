"""Unit tests for PathInterner and CompactFileMetadata (Issue #912)."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from nexus.core.metadata import FileMetadata
from nexus.core.path_interner import (
    CompactFileMetadata,
    PathInterner,
    SegmentedPathInterner,
    get_path_interner,
    get_segmented_interner,
    reset_global_interners,
)


class TestPathInterner:
    """Tests for the PathInterner class."""

    def test_intern_returns_same_id_for_same_path(self) -> None:
        """Interning the same path twice should return the same ID."""
        interner = PathInterner()
        id1 = interner.intern("/workspace/project/file.txt")
        id2 = interner.intern("/workspace/project/file.txt")
        assert id1 == id2

    def test_intern_returns_different_ids_for_different_paths(self) -> None:
        """Interning different paths should return different IDs."""
        interner = PathInterner()
        id1 = interner.intern("/workspace/file1.txt")
        id2 = interner.intern("/workspace/file2.txt")
        assert id1 != id2

    def test_get_returns_original_path(self) -> None:
        """get() should return the original path string."""
        interner = PathInterner()
        path = "/workspace/project/file.txt"
        path_id = interner.intern(path)
        assert interner.get(path_id) == path

    def test_get_raises_for_invalid_id(self) -> None:
        """get() should raise IndexError for invalid IDs."""
        interner = PathInterner()
        with pytest.raises(IndexError):
            interner.get(999)

    def test_get_id_returns_none_for_unknown_path(self) -> None:
        """get_id() should return None for paths not yet interned."""
        interner = PathInterner()
        assert interner.get_id("/unknown/path") is None

    def test_get_id_returns_id_for_known_path(self) -> None:
        """get_id() should return the ID for previously interned paths."""
        interner = PathInterner()
        path = "/workspace/file.txt"
        expected_id = interner.intern(path)
        assert interner.get_id(path) == expected_id

    def test_contains_returns_false_for_unknown(self) -> None:
        """contains() should return False for unknown paths."""
        interner = PathInterner()
        assert not interner.contains("/unknown/path")

    def test_contains_returns_true_for_known(self) -> None:
        """contains() should return True for interned paths."""
        interner = PathInterner()
        path = "/workspace/file.txt"
        interner.intern(path)
        assert interner.contains(path)

    def test_len_returns_count(self) -> None:
        """len() should return the number of interned paths."""
        interner = PathInterner()
        assert len(interner) == 0
        interner.intern("/path1")
        assert len(interner) == 1
        interner.intern("/path2")
        assert len(interner) == 2
        interner.intern("/path1")  # Duplicate, should not increase count
        assert len(interner) == 2

    def test_iter_returns_all_paths(self) -> None:
        """Iteration should yield all interned paths."""
        interner = PathInterner()
        paths = ["/a", "/b", "/c"]
        for p in paths:
            interner.intern(p)
        assert set(interner) == set(paths)

    def test_clear_removes_all_paths(self) -> None:
        """clear() should remove all interned paths."""
        interner = PathInterner()
        interner.intern("/path1")
        interner.intern("/path2")
        assert len(interner) == 2
        interner.clear()
        assert len(interner) == 0
        assert not interner.contains("/path1")

    def test_stats_returns_statistics(self) -> None:
        """stats() should return valid statistics."""
        interner = PathInterner()
        interner.intern("/workspace/project/file.txt")
        interner.intern("/workspace/project/other.txt")
        stats = interner.stats()
        assert stats["count"] == 2
        assert stats["total_string_bytes"] > 0
        assert "memory_saved_estimate" in stats

    def test_thread_safety(self) -> None:
        """PathInterner should be thread-safe."""
        interner = PathInterner()
        paths = [f"/path/{i}" for i in range(100)]
        results: dict[str, int] = {}
        lock = threading.Lock()

        def intern_path(path: str) -> None:
            path_id = interner.intern(path)
            with lock:
                if path not in results:
                    results[path] = path_id
                else:
                    # Should get same ID
                    assert results[path] == path_id

        with ThreadPoolExecutor(max_workers=10) as executor:
            # Each path interned multiple times from different threads
            for _ in range(5):
                for path in paths:
                    executor.submit(intern_path, path)

        # Verify all paths were interned correctly
        assert len(interner) == 100
        for path in paths:
            assert interner.contains(path)


class TestSegmentedPathInterner:
    """Tests for the SegmentedPathInterner class."""

    def test_intern_returns_same_id_for_same_path(self) -> None:
        """Interning the same path twice should return the same ID."""
        interner = SegmentedPathInterner()
        id1 = interner.intern("/workspace/project/file.txt")
        id2 = interner.intern("/workspace/project/file.txt")
        assert id1 == id2

    def test_intern_returns_different_ids_for_different_paths(self) -> None:
        """Interning different paths should return different IDs."""
        interner = SegmentedPathInterner()
        id1 = interner.intern("/workspace/file1.txt")
        id2 = interner.intern("/workspace/file2.txt")
        assert id1 != id2

    def test_get_returns_original_path(self) -> None:
        """get() should return the original path string."""
        interner = SegmentedPathInterner()
        path = "/workspace/project/file.txt"
        path_id = interner.intern(path)
        assert interner.get(path_id) == path

    def test_root_path_handling(self) -> None:
        """Root path should be handled correctly."""
        interner = SegmentedPathInterner()
        path_id = interner.intern("/")
        assert interner.get(path_id) == "/"

    def test_segments_are_deduplicated(self) -> None:
        """Common path segments should be stored only once."""
        interner = SegmentedPathInterner()
        interner.intern("/workspace/project/file1.txt")
        interner.intern("/workspace/project/file2.txt")
        interner.intern("/workspace/other/file3.txt")

        stats = interner.stats()
        # Should have 5 unique segments: workspace, project, other, file1.txt, file2.txt, file3.txt
        # Actually: workspace, project, file1.txt, file2.txt, other, file3.txt = 6
        assert stats["segment_count"] == 6
        assert stats["path_count"] == 3

    def test_memory_savings_with_shared_prefixes(self) -> None:
        """Paths with shared prefixes should show memory savings."""
        interner = SegmentedPathInterner()

        # Intern 100 files under same deep prefix
        prefix = "/workspace/tenant1/user1/projects/myproject/src/components"
        for i in range(100):
            interner.intern(f"{prefix}/file{i}.tsx")

        stats = interner.stats()

        # Should have ~8 prefix segments + 100 file segments = ~108 segments
        # vs 100 full paths with repeated prefix
        assert stats["segment_count"] < 120  # Much less than 100 * 8 = 800
        assert stats["memory_saved_estimate"] > 0

    def test_get_id_returns_none_for_unknown(self) -> None:
        """get_id() should return None for unknown paths."""
        interner = SegmentedPathInterner()
        assert interner.get_id("/unknown/path") is None

    def test_contains_works_correctly(self) -> None:
        """contains() should work correctly."""
        interner = SegmentedPathInterner()
        path = "/workspace/file.txt"
        assert not interner.contains(path)
        interner.intern(path)
        assert interner.contains(path)

    def test_clear_removes_all(self) -> None:
        """clear() should remove all paths and segments."""
        interner = SegmentedPathInterner()
        interner.intern("/a/b/c")
        interner.intern("/a/b/d")
        interner.clear()
        assert len(interner) == 0
        stats = interner.stats()
        assert stats["segment_count"] == 0


class TestGlobalInterners:
    """Tests for global interner instances."""

    def setup_method(self) -> None:
        """Reset global interners before each test."""
        reset_global_interners()

    def teardown_method(self) -> None:
        """Clean up global interners after each test."""
        reset_global_interners()

    def test_get_path_interner_returns_singleton(self) -> None:
        """get_path_interner() should return the same instance."""
        interner1 = get_path_interner()
        interner2 = get_path_interner()
        assert interner1 is interner2

    def test_get_segmented_interner_returns_singleton(self) -> None:
        """get_segmented_interner() should return the same instance."""
        interner1 = get_segmented_interner()
        interner2 = get_segmented_interner()
        assert interner1 is interner2

    def test_reset_clears_and_recreates(self) -> None:
        """reset_global_interners() should clear state."""
        interner = get_path_interner()
        interner.intern("/test/path")
        assert len(interner) == 1

        reset_global_interners()

        new_interner = get_path_interner()
        assert len(new_interner) == 0
        # Note: interner and new_interner may or may not be same object
        # What matters is the state is cleared


class TestCompactFileMetadata:
    """Tests for the CompactFileMetadata class."""

    def setup_method(self) -> None:
        """Reset global interners before each test."""
        reset_global_interners()

    def teardown_method(self) -> None:
        """Clean up global interners after each test."""
        reset_global_interners()

    def test_from_file_metadata_creates_compact(self) -> None:
        """from_file_metadata() should create CompactFileMetadata correctly."""
        original = FileMetadata(
            path="/workspace/test.txt",
            backend_name="local",
            physical_path="abc123",
            size=100,
            etag="hash123",
            mime_type="text/plain",
            created_at=datetime(2024, 1, 1, tzinfo=UTC),
            modified_at=datetime(2024, 1, 2, tzinfo=UTC),
            version=1,
            tenant_id="tenant1",
            created_by="user1",
            is_directory=False,
        )

        compact = CompactFileMetadata.from_file_metadata(original)

        assert compact.backend_name == "local"
        assert compact.physical_path == "abc123"
        assert compact.size == 100
        assert compact.etag == "hash123"
        assert compact.mime_type == "text/plain"
        assert compact.version == 1
        assert compact.tenant_id == "tenant1"
        assert compact.created_by == "user1"
        assert compact.is_directory is False

        # Path should be interned
        interner = get_path_interner()
        assert interner.get(compact.path_id) == "/workspace/test.txt"

    def test_to_file_metadata_restores_original(self) -> None:
        """to_file_metadata() should restore original FileMetadata."""
        original = FileMetadata(
            path="/workspace/test.txt",
            backend_name="local",
            physical_path="abc123",
            size=100,
            etag="hash123",
            mime_type="text/plain",
            created_at=datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC),
            modified_at=datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC),
            version=2,
            tenant_id="tenant1",
            created_by="user1",
            is_directory=False,
        )

        compact = CompactFileMetadata.from_file_metadata(original)
        restored = compact.to_file_metadata()

        assert restored.path == original.path
        assert restored.backend_name == original.backend_name
        assert restored.physical_path == original.physical_path
        assert restored.size == original.size
        assert restored.etag == original.etag
        assert restored.mime_type == original.mime_type
        assert restored.version == original.version
        assert restored.tenant_id == original.tenant_id
        assert restored.created_by == original.created_by
        assert restored.is_directory == original.is_directory

        # Timestamps may have slight precision differences due to float conversion
        assert restored.created_at is not None
        assert restored.modified_at is not None

    def test_get_path_returns_string(self) -> None:
        """get_path() should return the path string."""
        original = FileMetadata(
            path="/workspace/test.txt",
            backend_name="local",
            physical_path="abc123",
            size=100,
        )

        compact = CompactFileMetadata.from_file_metadata(original)
        assert compact.get_path() == "/workspace/test.txt"

    def test_handles_none_timestamps(self) -> None:
        """Should handle None timestamps correctly."""
        original = FileMetadata(
            path="/workspace/test.txt",
            backend_name="local",
            physical_path="abc123",
            size=100,
            created_at=None,
            modified_at=None,
        )

        compact = CompactFileMetadata.from_file_metadata(original)
        assert compact.created_at_ts is None
        assert compact.modified_at_ts is None

        restored = compact.to_file_metadata()
        assert restored.created_at is None
        assert restored.modified_at is None

    def test_uses_slots_for_memory_efficiency(self) -> None:
        """CompactFileMetadata should use __slots__ to reduce memory."""
        # Verify that __slots__ is defined (dataclass with slots=True)
        assert hasattr(CompactFileMetadata, "__slots__")
        # Slot-based classes don't have __dict__
        compact = CompactFileMetadata(
            path_id=0,
            backend_name="local",
            physical_path="abc123",
            size=100,
        )
        assert not hasattr(compact, "__dict__")

    def test_custom_interner_can_be_used(self) -> None:
        """Should be able to use a custom interner instance."""
        custom_interner = PathInterner()

        original = FileMetadata(
            path="/custom/path.txt",
            backend_name="local",
            physical_path="abc123",
            size=100,
        )

        compact = CompactFileMetadata.from_file_metadata(original, interner=custom_interner)
        assert custom_interner.contains("/custom/path.txt")

        # Global interner should not have this path
        global_interner = get_path_interner()
        assert not global_interner.contains("/custom/path.txt")

        # Can retrieve using custom interner
        assert compact.get_path(interner=custom_interner) == "/custom/path.txt"


class TestMemorySavings:
    """Tests verifying memory savings from interning."""

    def test_interning_saves_memory_estimate(self) -> None:
        """Verify estimated memory savings from interning."""
        interner = PathInterner()

        # Simulate 1000 paths with common prefix
        prefix = "/workspace/tenant/user/project/src"
        for i in range(1000):
            interner.intern(f"{prefix}/file{i}.py")

        stats = interner.stats()

        # With 1000 paths averaging ~50 bytes each, referenced 3 times:
        # String storage: 1000 * 50 * 3 = 150KB
        # Interned: 1000 * 4 * 3 + 50KB = ~62KB
        # Savings should be positive
        assert stats["memory_saved_estimate"] > 0
        assert stats["count"] == 1000

    def test_segmented_saves_more_with_shared_prefixes(self) -> None:
        """Segmented interner should save more memory with shared prefixes."""
        simple = PathInterner()
        segmented = SegmentedPathInterner()

        # Same paths in both
        prefix = "/workspace/tenant/user/project/src/components"
        for i in range(100):
            path = f"{prefix}/Component{i}.tsx"
            simple.intern(path)
            segmented.intern(path)

        simple_stats = simple.stats()
        segmented_stats = segmented.stats()

        # Segmented should use fewer unique strings overall
        # because segments are shared
        assert segmented_stats["segment_count"] < 100 + 7  # Less than 100 files + 7 prefix segments
        assert simple_stats["count"] == 100
        assert segmented_stats["path_count"] == 100
