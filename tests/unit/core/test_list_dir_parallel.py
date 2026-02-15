"""Unit tests for _list_dir_parallel (Issue #901).

Tests parallel directory traversal using ThreadPoolExecutor with mock backends.
Verifies correctness, error handling, and edge cases for BFS-based parallel listing.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from nexus.services.search_service import (
    LIST_PARALLEL_MAX_DEPTH,
    LIST_PARALLEL_WORKERS,
    SearchService,
)


def _make_mixin() -> SearchService:
    return SearchService.__new__(SearchService)


def _make_backend(tree: dict[str, list[str]]) -> MagicMock:
    """Create a mock backend with a directory tree.

    Args:
        tree: Mapping of backend_path -> list of entries.
              Directories should have trailing '/'.
              Example: {"": ["file.txt", "subdir/"], "subdir": ["nested.txt"]}
    """
    backend = MagicMock()

    def list_dir(path: str, context: object = None) -> list[str]:
        if path in tree:
            return tree[path]
        raise FileNotFoundError(f"Directory not found: {path}")

    backend.list_dir = MagicMock(side_effect=list_dir)
    return backend


class TestListDirParallelFlat:
    """Test flat directory (no subdirectories)."""

    def test_flat_directory_returns_all_files(self) -> None:
        """Flat directory with only files should return all entries."""
        mixin = _make_mixin()
        backend = _make_backend({"": ["a.txt", "b.txt", "c.txt"]})
        ctx = MagicMock()

        result = mixin._list_dir_parallel(
            backend=backend, root_path="/mount", backend_path="", context=ctx
        )

        assert sorted(result) == ["/mount/a.txt", "/mount/b.txt", "/mount/c.txt"]
        # Only one list_dir call for root
        assert backend.list_dir.call_count == 1

    def test_flat_directory_with_dirs_non_recursive(self) -> None:
        """Non-recursive listing should include dirs but not recurse."""
        mixin = _make_mixin()
        backend = _make_backend({"": ["file.txt", "subdir/"]})
        ctx = MagicMock()

        result = mixin._list_dir_parallel(
            backend=backend,
            root_path="/mount",
            backend_path="",
            context=ctx,
            recursive=False,
        )

        assert sorted(result) == ["/mount/file.txt", "/mount/subdir"]
        # Only one list_dir call — did NOT recurse into subdir
        assert backend.list_dir.call_count == 1


class TestListDirParallelDeepTree:
    """Test deep nested directory trees (3+ levels)."""

    def test_three_level_tree(self) -> None:
        """Three-level tree should return all entries at all levels."""
        mixin = _make_mixin()
        backend = _make_backend(
            {
                "": ["root.txt", "level1/"],
                "level1": ["l1.txt", "level2/"],
                "level1/level2": ["l2.txt", "level3/"],
                "level1/level2/level3": ["deep.txt"],
            }
        )
        ctx = MagicMock()

        result = mixin._list_dir_parallel(
            backend=backend, root_path="/mount", backend_path="", context=ctx
        )

        expected = sorted(
            [
                "/mount/root.txt",
                "/mount/level1",
                "/mount/level1/l1.txt",
                "/mount/level1/level2",
                "/mount/level1/level2/l2.txt",
                "/mount/level1/level2/level3",
                "/mount/level1/level2/level3/deep.txt",
            ]
        )
        assert sorted(result) == expected
        # 4 list_dir calls total (one per directory level)
        assert backend.list_dir.call_count == 4

    def test_wide_tree_multiple_subdirs(self) -> None:
        """Wide tree with multiple subdirectories at same level."""
        mixin = _make_mixin()
        backend = _make_backend(
            {
                "": ["dir_a/", "dir_b/", "dir_c/"],
                "dir_a": ["a1.txt", "a2.txt"],
                "dir_b": ["b1.txt"],
                "dir_c": ["c1.txt", "nested/"],
                "dir_c/nested": ["deep.txt"],
            }
        )
        ctx = MagicMock()

        result = mixin._list_dir_parallel(
            backend=backend, root_path="/root", backend_path="", context=ctx
        )

        expected = sorted(
            [
                "/root/dir_a",
                "/root/dir_a/a1.txt",
                "/root/dir_a/a2.txt",
                "/root/dir_b",
                "/root/dir_b/b1.txt",
                "/root/dir_c",
                "/root/dir_c/c1.txt",
                "/root/dir_c/nested",
                "/root/dir_c/nested/deep.txt",
            ]
        )
        assert sorted(result) == expected


class TestListDirParallelErrorHandling:
    """Test error handling when subdirectory listing fails."""

    def test_single_subdirectory_failure(self) -> None:
        """When one subdirectory fails, other results are still returned."""
        mixin = _make_mixin()

        call_count = {"n": 0}

        def list_dir(path: str, context: object = None) -> list[str]:
            call_count["n"] += 1
            if path == "":
                return ["ok_dir/", "bad_dir/", "file.txt"]
            if path == "ok_dir":
                return ["good.txt"]
            if path == "bad_dir":
                raise OSError("Simulated network error")
            raise FileNotFoundError(path)

        backend = MagicMock()
        backend.list_dir = MagicMock(side_effect=list_dir)
        ctx = MagicMock()

        result = mixin._list_dir_parallel(
            backend=backend, root_path="/mount", backend_path="", context=ctx
        )

        # bad_dir itself is still in results (added before recursion attempt)
        # but its contents are missing due to the error
        assert "/mount/file.txt" in result
        assert "/mount/ok_dir" in result
        assert "/mount/ok_dir/good.txt" in result
        assert "/mount/bad_dir" in result
        # No entries from bad_dir's contents
        assert all("bad_dir/" not in p for p in result)


class TestListDirParallelEdgeCases:
    """Test edge cases."""

    def test_empty_directory(self) -> None:
        """Empty directory should return empty list."""
        mixin = _make_mixin()
        backend = _make_backend({"": []})
        ctx = MagicMock()

        result = mixin._list_dir_parallel(
            backend=backend, root_path="/mount", backend_path="", context=ctx
        )

        assert result == []

    def test_empty_subdirectory(self) -> None:
        """Subdirectory with no contents should still appear in results."""
        mixin = _make_mixin()
        backend = _make_backend({"": ["empty_dir/"], "empty_dir": []})
        ctx = MagicMock()

        result = mixin._list_dir_parallel(
            backend=backend, root_path="/mount", backend_path="", context=ctx
        )

        assert result == ["/mount/empty_dir"]

    def test_backend_path_non_empty(self) -> None:
        """Backend path should be correctly concatenated for subdirs."""
        mixin = _make_mixin()
        backend = _make_backend(
            {
                "start": ["sub/"],
                "start/sub": ["file.txt"],
            }
        )
        ctx = MagicMock()

        result = mixin._list_dir_parallel(
            backend=backend,
            root_path="/mount",
            backend_path="start",
            context=ctx,
        )

        assert sorted(result) == ["/mount/sub", "/mount/sub/file.txt"]

    def test_parallel_workers_constant_is_set(self) -> None:
        """LIST_PARALLEL_WORKERS should be configured."""
        assert LIST_PARALLEL_WORKERS == 10

    def test_max_depth_guard_prevents_infinite_traversal(self) -> None:
        """BFS should stop at LIST_PARALLEL_MAX_DEPTH to prevent infinite loops."""
        mixin = _make_mixin()

        # Create a backend that always returns a subdirectory (simulating a loop)
        def infinite_list_dir(path: str, context: object = None) -> list[str]:
            return ["file.txt", "deeper/"]

        backend = MagicMock()
        backend.list_dir = MagicMock(side_effect=infinite_list_dir)
        ctx = MagicMock()

        result = mixin._list_dir_parallel(
            backend=backend, root_path="/mount", backend_path="", context=ctx
        )

        # Should have terminated after LIST_PARALLEL_MAX_DEPTH levels
        # +1 for the root level call which happens outside the BFS loop
        assert backend.list_dir.call_count <= LIST_PARALLEL_MAX_DEPTH + 1
        # Should still return some results (not crash or hang)
        assert len(result) > 0
