"""Cross-process integration tests for SharedMemoryPipeBackend / SharedMemoryStreamBackend (#1680).

Uses multiprocessing.Process to verify true cross-process shared memory IPC.
All child targets are module-level functions (required for macOS spawn start method).
Results returned via multiprocessing.Queue (fd-safe across spawn).
"""

from __future__ import annotations

import multiprocessing
import os
import time

# ---------------------------------------------------------------------------
# Child process helpers (must be top-level for pickling on macOS spawn)
# ---------------------------------------------------------------------------


def _child_pipe_read_one(shm_path: str, result_q: multiprocessing.Queue):
    """Child: attach to shared ring buffer, poll-read one message."""
    from nexus_runtime import SharedMemoryPipeBackend

    reader = SharedMemoryPipeBackend.attach(shm_path, -1, -1)
    for _ in range(200):
        try:
            msg = reader.pop()
            result_q.put(msg)
            return
        except RuntimeError:
            time.sleep(0.01)
    result_q.put(None)


def _child_pipe_read_three(shm_path: str, result_q: multiprocessing.Queue):
    """Child: read 3 messages from shared ring buffer."""
    from nexus_runtime import SharedMemoryPipeBackend

    reader = SharedMemoryPipeBackend.attach(shm_path, -1, -1)
    results = []
    for _ in range(3):
        for _ in range(200):
            try:
                msg = reader.pop()
                results.append(msg)
                break
            except RuntimeError:
                time.sleep(0.01)
    result_q.put(b"|".join(results))


def _child_crash(shm_path: str):
    """Child: attach then crash immediately."""
    from nexus_runtime import SharedMemoryPipeBackend

    _reader = SharedMemoryPipeBackend.attach(shm_path, -1, -1)
    os._exit(1)


def _child_stream_read_one(shm_path: str, result_q: multiprocessing.Queue):
    """Child: poll-read one message at offset 0 from shared stream buffer."""
    from nexus_runtime import SharedMemoryStreamBackend

    reader = SharedMemoryStreamBackend.attach(shm_path, -1)
    for _ in range(200):
        try:
            data, _next = reader.read_at(0)
            result_q.put(data)
            return
        except RuntimeError:
            time.sleep(0.01)
    result_q.put(None)


def _child_stream_read_at(shm_path: str, offset: int, result_q: multiprocessing.Queue):
    """Child: read one message at given offset from shared stream buffer."""
    from nexus_runtime import SharedMemoryStreamBackend

    reader = SharedMemoryStreamBackend.attach(shm_path, -1)
    for _ in range(200):
        try:
            data, _ = reader.read_at(offset)
            result_q.put(data)
            return
        except RuntimeError:
            time.sleep(0.01)
    result_q.put(None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCrossProcessPipe:
    """True cross-process ring buffer tests."""

    def test_cross_process_pipe_roundtrip(self):
        """Parent writes, child reads via separate processes."""
        from nexus_runtime import SharedMemoryPipeBackend

        core, shm_path, data_rd_fd, space_rd_fd = SharedMemoryPipeBackend.create(4096)
        os.close(data_rd_fd)
        os.close(space_rd_fd)

        q = multiprocessing.Queue()
        proc = multiprocessing.Process(target=_child_pipe_read_one, args=(shm_path, q))
        proc.start()

        # Parent writes
        core.push(b"cross-process-hello")

        proc.join(timeout=10)
        assert proc.exitcode == 0

        result = q.get(timeout=5)
        assert result == b"cross-process-hello"
        core.cleanup()

    def test_cross_process_multiple_messages(self):
        """Multiple messages through cross-process ring buffer."""
        from nexus_runtime import SharedMemoryPipeBackend

        core, shm_path, data_rd_fd, space_rd_fd = SharedMemoryPipeBackend.create(4096)
        os.close(data_rd_fd)
        os.close(space_rd_fd)

        q = multiprocessing.Queue()
        proc = multiprocessing.Process(target=_child_pipe_read_three, args=(shm_path, q))
        proc.start()

        time.sleep(0.05)
        core.push(b"msg1")
        core.push(b"msg2")
        core.push(b"msg3")

        proc.join(timeout=10)
        assert proc.exitcode == 0

        result = q.get(timeout=5)
        assert result == b"msg1|msg2|msg3"
        core.cleanup()

    def test_child_crash_cleanup(self):
        """Shared memory file persists after child crash — creator cleans up."""
        from nexus_runtime import SharedMemoryPipeBackend

        core, shm_path, data_rd_fd, space_rd_fd = SharedMemoryPipeBackend.create(64)
        os.close(data_rd_fd)
        os.close(space_rd_fd)

        proc = multiprocessing.Process(target=_child_crash, args=(shm_path,))
        proc.start()
        proc.join(timeout=5)

        # File should still exist after child crash
        assert os.path.exists(shm_path)

        # Creator cleans up
        core.cleanup()
        assert not os.path.exists(shm_path)


class TestCrossProcessStream:
    """True cross-process stream buffer tests."""

    def test_cross_process_stream_roundtrip(self):
        """Parent writes, child reads via separate processes."""
        from nexus_runtime import SharedMemoryStreamBackend

        core, shm_path, data_rd_fd = SharedMemoryStreamBackend.create(4096)
        os.close(data_rd_fd)

        q = multiprocessing.Queue()
        proc = multiprocessing.Process(target=_child_stream_read_one, args=(shm_path, q))
        proc.start()

        core.push(b"stream-cross-process")

        proc.join(timeout=10)
        assert proc.exitcode == 0

        result = q.get(timeout=5)
        assert result == b"stream-cross-process"
        core.cleanup()

    def test_cross_process_stream_multi_reader(self):
        """Multiple child readers with independent cursors."""
        from nexus_runtime import SharedMemoryStreamBackend

        core, shm_path, data_rd_fd = SharedMemoryStreamBackend.create(4096)
        os.close(data_rd_fd)

        # Write two messages before spawning readers
        core.push(b"first")
        core.push(b"second")

        q1 = multiprocessing.Queue()
        q2 = multiprocessing.Queue()

        # Reader A reads from offset 0
        p1 = multiprocessing.Process(target=_child_stream_read_at, args=(shm_path, 0, q1))
        p1.start()

        # Reader B reads from offset of second message (4 + 5 = 9)
        p2 = multiprocessing.Process(target=_child_stream_read_at, args=(shm_path, 9, q2))
        p2.start()

        p1.join(timeout=10)
        p2.join(timeout=10)
        assert p1.exitcode == 0
        assert p2.exitcode == 0

        result1 = q1.get(timeout=5)
        result2 = q2.get(timeout=5)
        assert result1 == b"first"
        assert result2 == b"second"
        core.cleanup()
