"""Tests for thread safety of FUSE operations."""

from __future__ import annotations

import os
import threading
from typing import Any
from unittest.mock import MagicMock


class TestThreadSafety:
    """Concurrent access: fd allocation and open_files integrity."""

    def test_concurrent_open_no_fd_collisions(
        self, fuse_ops: Any, mock_nexus_fs: MagicMock
    ) -> None:
        """Multiple threads opening files should never get the same fd."""
        mock_nexus_fs.exists.return_value = True
        fds: list[int] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def open_file(idx: int) -> None:
            try:
                fd = fuse_ops.open(f"/file_{idx}.txt", os.O_RDONLY)
                with lock:
                    fds.append(fd)
            except Exception as e:
                with lock:
                    errors.append(e)

        threads = [threading.Thread(target=open_file, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Errors during concurrent open: {errors}"
        assert len(fds) == 50
        assert len(set(fds)) == 50, "Duplicate file descriptors detected!"

    def test_concurrent_open_release(self, fuse_ops: Any, mock_nexus_fs: MagicMock) -> None:
        """Open + release in multiple threads should not corrupt open_files."""
        mock_nexus_fs.exists.return_value = True
        errors: list[Exception] = []

        def open_and_release(idx: int) -> None:
            try:
                fd = fuse_ops.open(f"/file_{idx}.txt", os.O_RDONLY)
                fuse_ops.release(f"/file_{idx}.txt", fd)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=open_and_release, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Errors: {errors}"
        # All files should be released
        assert len(fuse_ops.open_files) == 0

    def test_concurrent_fd_counter_monotonic(self, fuse_ops: Any, mock_nexus_fs: MagicMock) -> None:
        """fd_counter should always increase monotonically under contention."""
        mock_nexus_fs.exists.return_value = True
        fds: list[int] = []
        lock = threading.Lock()

        def open_file(idx: int) -> None:
            fd = fuse_ops.open(f"/file_{idx}.txt", os.O_RDONLY)
            with lock:
                fds.append(fd)

        threads = [threading.Thread(target=open_file, args=(i,)) for i in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        sorted_fds = sorted(fds)
        # Verify strictly monotonic
        for i in range(1, len(sorted_fds)):
            assert sorted_fds[i] > sorted_fds[i - 1]
