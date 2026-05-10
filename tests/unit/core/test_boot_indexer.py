"""Tests for BootIndexer — sandbox workspace walk (Issue #3786).

Tests cover:
1. Successful walk: all files fed to search daemon, health transitions to "ready"
2. Walk failure (missing dir): logs error, health transitions to "ready" anyway
3. start_async() returns immediately (non-blocking)
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nexus.core.boot_indexer import BootIndexer


class TestBootIndexerSuccessfulWalk:
    """BootIndexer walks workspace and feeds files to search daemon."""

    def test_all_files_fed_to_search_daemon(self, tmp_path: Path) -> None:
        """All files in the workspace directory are passed to search_daemon.index_file."""
        # Create a small directory tree
        (tmp_path / "a.txt").write_text("hello")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.py").write_text("world")
        (tmp_path / "sub" / "c.md").write_text("# doc")

        search_daemon = MagicMock()
        health_state: dict[str, str] = {"status": "indexing"}

        indexer = BootIndexer(tmp_path, search_daemon, health_state)
        indexer.start_async()

        # Wait for the background thread to finish (generous timeout for CI)
        deadline = time.monotonic() + 5.0
        while health_state["status"] != "ready" and time.monotonic() < deadline:
            time.sleep(0.01)

        assert health_state["status"] == "ready"

        # All three files should have been indexed
        indexed_paths = {c.args[0] for c in search_daemon.index_file.call_args_list}
        assert tmp_path / "a.txt" in indexed_paths
        assert tmp_path / "sub" / "b.py" in indexed_paths
        assert tmp_path / "sub" / "c.md" in indexed_paths

    def test_directories_not_fed_to_search_daemon(self, tmp_path: Path) -> None:
        """Directory entries are skipped — only files are passed to the daemon."""
        (tmp_path / "file.txt").write_text("content")
        (tmp_path / "subdir").mkdir()

        search_daemon = MagicMock()
        health_state: dict[str, str] = {"status": "indexing"}

        indexer = BootIndexer(tmp_path, search_daemon, health_state)
        indexer.start_async()

        deadline = time.monotonic() + 5.0
        while health_state["status"] != "ready" and time.monotonic() < deadline:
            time.sleep(0.01)

        # Only the file, not the directory
        indexed_paths = {c.args[0] for c in search_daemon.index_file.call_args_list}
        assert tmp_path / "subdir" not in indexed_paths
        assert tmp_path / "file.txt" in indexed_paths

    def test_health_transitions_to_ready_after_walk(self, tmp_path: Path) -> None:
        """health_state is set to 'ready' after successful walk completion."""
        (tmp_path / "x.txt").write_text("x")

        search_daemon = MagicMock()
        health_state: dict[str, str] = {"status": "indexing"}

        indexer = BootIndexer(tmp_path, search_daemon, health_state)
        indexer.start_async()

        deadline = time.monotonic() + 5.0
        while health_state["status"] != "ready" and time.monotonic() < deadline:
            time.sleep(0.01)

        assert health_state["status"] == "ready"

    def test_empty_workspace_transitions_to_ready(self, tmp_path: Path) -> None:
        """An empty workspace (no files) still transitions to 'ready'."""
        search_daemon = MagicMock()
        health_state: dict[str, str] = {"status": "indexing"}

        indexer = BootIndexer(tmp_path, search_daemon, health_state)
        indexer.start_async()

        deadline = time.monotonic() + 5.0
        while health_state["status"] != "ready" and time.monotonic() < deadline:
            time.sleep(0.01)

        assert health_state["status"] == "ready"
        search_daemon.index_file.assert_not_called()


class TestBootIndexerWalkFailure:
    """BootIndexer handles walk errors gracefully — partial index is acceptable."""

    def test_missing_directory_logs_error_and_transitions_to_ready(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When the workspace dir doesn't exist, error is logged and state → 'ready'."""
        missing = tmp_path / "nonexistent_workspace"

        search_daemon = MagicMock()
        health_state: dict[str, str] = {"status": "indexing"}

        import logging

        with caplog.at_level(logging.ERROR, logger="nexus.core.boot_indexer"):
            indexer = BootIndexer(missing, search_daemon, health_state)
            indexer.start_async()

            deadline = time.monotonic() + 5.0
            while health_state["status"] != "ready" and time.monotonic() < deadline:
                time.sleep(0.01)

        assert health_state["status"] == "ready"
        # An error message must have been emitted
        assert any(
            "error" in r.message.lower() or str(missing) in r.message for r in caplog.records
        )

    def test_walk_error_never_leaves_state_indexing(self, tmp_path: Path) -> None:
        """Even when an exception is raised mid-walk, state must not stay 'indexing'."""
        search_daemon = MagicMock()
        search_daemon.index_file.side_effect = RuntimeError("daemon exploded")
        health_state: dict[str, str] = {"status": "indexing"}

        (tmp_path / "file.txt").write_text("data")

        indexer = BootIndexer(tmp_path, search_daemon, health_state)
        indexer.start_async()

        deadline = time.monotonic() + 5.0
        while health_state["status"] != "ready" and time.monotonic() < deadline:
            time.sleep(0.01)

        assert health_state["status"] == "ready"


class TestBootIndexerNonBlocking:
    """start_async() must return immediately without blocking the caller."""

    def test_start_async_returns_immediately(self, tmp_path: Path) -> None:
        """start_async() returns in well under 1 second even for a large tree."""
        # Create enough files that a synchronous walk would be measurable
        (tmp_path / "sub").mkdir()
        for i in range(20):
            (tmp_path / "sub" / f"file_{i}.txt").write_text(f"content {i}")

        search_daemon = MagicMock()
        health_state: dict[str, str] = {"status": "indexing"}

        # Slow down each index_file call to make synchronous behaviour obvious
        slow_barrier = threading.Event()

        def slow_index(path: Path) -> None:
            slow_barrier.wait(timeout=10)

        search_daemon.index_file.side_effect = slow_index

        indexer = BootIndexer(tmp_path, search_daemon, health_state)

        t0 = time.monotonic()
        indexer.start_async()
        elapsed = time.monotonic() - t0

        # start_async() must return before any indexing completes
        assert elapsed < 0.5, f"start_async() took {elapsed:.3f}s — appears synchronous"

        # Unblock the daemon so the thread can finish cleanly
        slow_barrier.set()

    def test_start_async_spawns_background_thread(self, tmp_path: Path) -> None:
        """start_async() spawns a daemon thread (does not block the event loop)."""
        (tmp_path / "f.txt").write_text("hi")

        search_daemon = MagicMock()
        index_started = threading.Event()
        release_index = threading.Event()
        health_state: dict[str, str] = {"status": "indexing"}

        def blocking_index(path: Path) -> None:
            index_started.set()
            release_index.wait(timeout=5)

        search_daemon.index_file.side_effect = blocking_index

        threads_before = set(threading.enumerate())
        indexer = BootIndexer(tmp_path, search_daemon, health_state)
        indexer.start_async()
        assert index_started.wait(timeout=5), "background indexing did not start"
        threads_after = set(threading.enumerate())

        # At least one new thread was created
        new_threads = threads_after - threads_before
        assert new_threads, "start_async() did not spawn any new threads"

        release_index.set()

        # Wait for completion to avoid dangling threads in the test suite
        deadline = time.monotonic() + 5.0
        while health_state["status"] != "ready" and time.monotonic() < deadline:
            time.sleep(0.01)
