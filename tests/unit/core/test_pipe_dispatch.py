"""Integration tests: Rust kernel IPC pipe operations (#1201, #1496).

Verifies that Rust kernel IPC pipe operations (create_pipe,
pipe_write_nowait, pipe_read_nowait, close_pipe) work correctly.

PathRouter was deleted in §12 Phase F3. Pipe detection (DT_PIPE inode
lookup) is now handled by the kernel + DLC directly in NexusFS callers.

See: Rust kernel IPC registry
"""

from __future__ import annotations

import pytest

# ======================================================================
# PyKernel IPC pipe read/write (replaces PipeManager tests)
# ======================================================================


class TestKernelPipeReadWrite:
    def _make_kernel(self):
        """Create a Rust PyKernel instance for IPC testing."""
        from nexus_runtime import PyKernel

        return PyKernel()

    def test_pipe_write_then_read(self) -> None:
        kernel = self._make_kernel()
        kernel.create_pipe("/pipes/roundtrip", 1024)

        kernel.pipe_write_nowait("/pipes/roundtrip", b"hello")
        data = kernel.pipe_read_nowait("/pipes/roundtrip")
        assert bytes(data) == b"hello"

        kernel.close_all_pipes()

    def test_pipe_write_full_raises(self) -> None:
        kernel = self._make_kernel()
        kernel.create_pipe("/pipes/tiny", 10)
        kernel.pipe_write_nowait("/pipes/tiny", b"x" * 10)

        with pytest.raises(RuntimeError, match="PipeFull"):
            kernel.pipe_write_nowait("/pipes/tiny", b"overflow")

        kernel.close_all_pipes()

    def test_pipe_close(self) -> None:
        """close_pipe() signals the closed flag (pipe still registered);
        destroy_pipe() removes it from the registry.
        """
        kernel = self._make_kernel()
        kernel.create_pipe("/pipes/delme", 1024)

        # close_pipe: sets closed flag but does NOT remove from registry
        kernel.close_pipe("/pipes/delme")
        assert "/pipes/delme" in kernel.list_pipes()

        # destroy_pipe: removes from registry
        kernel.destroy_pipe("/pipes/delme")
        assert "/pipes/delme" not in kernel.list_pipes()

        kernel.close_all_pipes()
