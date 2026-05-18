"""Integration tests: Rust kernel IPC pipe operations (#1201, #1496).

Verifies that Rust kernel IPC pipe operations (create_pipe,
sys_write, sys_read, close_pipe) work correctly through the syscall
interface.

After the PyKernel boundary cleanup, pipe_write_nowait, pipe_read_nowait,
has_pipe, and destroy_pipe were removed from the PyO3 surface.  Tests now
use the syscall equivalents (sys_write, sys_read, sys_stat, sys_unlink).

See: Rust kernel IPC registry
"""

from __future__ import annotations

import pytest

try:
    from nexus_runtime import PyKernel, PyOperationContext

    RUST_AVAILABLE = True
except ImportError:
    RUST_AVAILABLE = False

pytestmark = pytest.mark.skipif(not RUST_AVAILABLE, reason="nexus_runtime not built")

DT_PIPE = 3


def _sys_ctx() -> "PyOperationContext":
    return PyOperationContext(is_system=True)


def _pipe_exists(kernel: "PyKernel", path: str) -> bool:
    stat = kernel.sys_stat(path, "root")
    return stat is not None and stat["entry_type"] == DT_PIPE


# ======================================================================
# PyKernel IPC pipe read/write (replaces PipeManager tests)
# ======================================================================


class TestKernelPipeReadWrite:
    def _make_kernel(self):
        """Create a Rust PyKernel instance for IPC testing."""
        return PyKernel()

    def test_pipe_write_then_read(self) -> None:
        kernel = self._make_kernel()
        kernel.create_pipe("/pipes/roundtrip", 1024)

        kernel.sys_write("/pipes/roundtrip", _sys_ctx(), b"hello")
        result = kernel.sys_read("/pipes/roundtrip", _sys_ctx(), timeout_ms=0)
        assert bytes(result.data) == b"hello"

        kernel.close_all_pipes()

    def test_pipe_write_full_raises(self) -> None:
        kernel = self._make_kernel()
        kernel.create_pipe("/pipes/tiny", 10)
        kernel.sys_write("/pipes/tiny", _sys_ctx(), b"x" * 10)

        with pytest.raises(RuntimeError, match="PipeFull"):
            kernel.sys_write("/pipes/tiny", _sys_ctx(), b"overflow")

        kernel.close_all_pipes()

    def test_pipe_close(self) -> None:
        """close_pipe() signals the closed flag (pipe still registered);
        sys_unlink() removes it from the registry.
        """
        kernel = self._make_kernel()
        kernel.create_pipe("/pipes/delme", 1024)

        # close_pipe: sets closed flag but does NOT remove from registry
        kernel.close_pipe("/pipes/delme")
        assert _pipe_exists(kernel, "/pipes/delme")

        # sys_unlink: removes from registry
        kernel.sys_unlink("/pipes/delme", _sys_ctx())
        assert not _pipe_exists(kernel, "/pipes/delme")

        kernel.close_all_pipes()
