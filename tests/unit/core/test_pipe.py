"""Unit tests for DT_PIPE kernel IPC primitive.

Tests Rust PyKernel IPC pipe operations (create, read, write, close, destroy)
and DT_PIPE metadata integration.
See: rust/kernel/src/pipe.rs, rust/kernel/src/kernel.rs,
     KERNEL-ARCHITECTURE.md §6.

After the PyKernel boundary cleanup, ``has_pipe``, ``pipe_write_nowait``,
``pipe_read_nowait``, and ``destroy_pipe`` were removed from the PyO3
surface.  Tests now use the syscall equivalents:

    has_pipe(path)           -> sys_stat(path, "root")["entry_type"] == DT_PIPE
    pipe_write_nowait(path)  -> sys_write(path, ctx, data)
    pipe_read_nowait(path)   -> sys_read(path, ctx, timeout_ms=0).data
    destroy_pipe(path)       -> sys_unlink(path, ctx)
"""

from dataclasses import replace

import pytest

from nexus.contracts.metadata import DT_PIPE, DT_REG, FileMetadata
from nexus.core.pipe import (
    PipeClosedError,
    PipeEmptyError,
    PipeError,
    PipeExistsError,
    PipeFullError,
    PipeNotFoundError,
)

try:
    from nexus_runtime import PyKernel, PyOperationContext

    RUST_AVAILABLE = True
except ImportError:
    RUST_AVAILABLE = False

pytestmark = pytest.mark.skipif(not RUST_AVAILABLE, reason="nexus_runtime not built")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_kernel() -> "PyKernel":
    return PyKernel()


def _sys_ctx() -> "PyOperationContext":
    """System OperationContext for test syscalls."""
    return PyOperationContext(is_system=True)


def _pipe_exists(k: "PyKernel", path: str) -> bool:
    """Check if a DT_PIPE exists via sys_stat (replaces has_pipe)."""
    stat = k.sys_stat(path, "root")
    return stat is not None and stat["entry_type"] == DT_PIPE


def _pipe_write(k: "PyKernel", path: str, data: bytes) -> int:
    """Write to a DT_PIPE via sys_write (replaces pipe_write_nowait)."""
    result = k.sys_write(path, _sys_ctx(), data)
    return result.size


def _pipe_read(k: "PyKernel", path: str) -> bytes | None:
    """Non-blocking read from a DT_PIPE via sys_read (replaces pipe_read_nowait)."""
    result = k.sys_read(path, _sys_ctx(), timeout_ms=0)
    return bytes(result.data) if result.data is not None else None


def _pipe_destroy(k: "PyKernel", path: str) -> None:
    """Destroy a DT_PIPE via sys_unlink (replaces destroy_pipe)."""
    k.sys_unlink(path, _sys_ctx())


# ======================================================================
# PyKernel IPC Pipe -- basic operations
# ======================================================================


class TestKernelPipeBasic:
    def test_create_and_stat(self) -> None:
        k = _make_kernel()
        k.create_pipe("/pipes/test", 1024)
        assert _pipe_exists(k, "/pipes/test") is True

    def test_stat_nonexistent(self) -> None:
        k = _make_kernel()
        assert _pipe_exists(k, "/pipes/nope") is False

    def test_write_read_roundtrip(self) -> None:
        k = _make_kernel()
        k.create_pipe("/pipes/rt", 1024)
        _pipe_write(k, "/pipes/rt", b"hello")
        data = _pipe_read(k, "/pipes/rt")
        assert data == b"hello"

    def test_fifo_ordering(self) -> None:
        k = _make_kernel()
        k.create_pipe("/pipes/fifo", 4096)
        _pipe_write(k, "/pipes/fifo", b"first")
        _pipe_write(k, "/pipes/fifo", b"second")
        _pipe_write(k, "/pipes/fifo", b"third")
        assert _pipe_read(k, "/pipes/fifo") == b"first"
        assert _pipe_read(k, "/pipes/fifo") == b"second"
        assert _pipe_read(k, "/pipes/fifo") == b"third"

    def test_read_empty_returns_none(self) -> None:
        k = _make_kernel()
        k.create_pipe("/pipes/empty", 1024)
        assert _pipe_read(k, "/pipes/empty") is None

    def test_multiple_messages_roundtrip(self) -> None:
        k = _make_kernel()
        k.create_pipe("/pipes/multi", 65536)
        for i in range(10):
            _pipe_write(k, "/pipes/multi", f"msg-{i}".encode())
        for i in range(10):
            assert _pipe_read(k, "/pipes/multi") == f"msg-{i}".encode()
        assert _pipe_read(k, "/pipes/multi") is None

    def test_empty_write_is_noop(self) -> None:
        k = _make_kernel()
        k.create_pipe("/pipes/empty-write", 1024)
        _pipe_write(k, "/pipes/empty-write", b"")
        assert _pipe_read(k, "/pipes/empty-write") is None


# ======================================================================
# PyKernel IPC Pipe -- capacity limits
# ======================================================================


class TestKernelPipeCapacity:
    def test_oversized_message_rejected(self) -> None:
        """A single message larger than capacity should be rejected."""
        k = _make_kernel()
        k.create_pipe("/pipes/cap", 10)
        with pytest.raises(RuntimeError, match="PipeFull"):
            _pipe_write(k, "/pipes/cap", b"x" * 11)

    def test_buffer_full_raises(self) -> None:
        """Writing to a full buffer should raise PipeFull."""
        k = _make_kernel()
        k.create_pipe("/pipes/full", 32)
        # Fill enough to cause PipeFull on next write
        _pipe_write(k, "/pipes/full", b"x" * 20)
        with pytest.raises(RuntimeError, match="PipeFull"):
            _pipe_write(k, "/pipes/full", b"y" * 20)

    def test_space_freed_after_read(self) -> None:
        """After reading, the freed space should allow new writes."""
        k = _make_kernel()
        k.create_pipe("/pipes/free", 64)
        _pipe_write(k, "/pipes/free", b"x" * 30)
        _pipe_read(k, "/pipes/free")
        # Now have space again
        _pipe_write(k, "/pipes/free", b"y" * 30)
        assert _pipe_read(k, "/pipes/free") == b"y" * 30


# ======================================================================
# PyKernel IPC Pipe -- close semantics
# ======================================================================


class TestKernelPipeClose:
    def test_write_after_close_raises(self) -> None:
        k = _make_kernel()
        k.create_pipe("/pipes/closed-w", 1024)
        k.close_pipe("/pipes/closed-w")
        with pytest.raises(RuntimeError, match="PipeClosed"):
            _pipe_write(k, "/pipes/closed-w", b"data")

    def test_read_drains_remaining_then_closed(self) -> None:
        """After close, remaining data can still be read; then returns closed error."""
        k = _make_kernel()
        k.create_pipe("/pipes/drain", 1024)
        _pipe_write(k, "/pipes/drain", b"last-msg")
        k.close_pipe("/pipes/drain")

        # Can still read buffered messages
        result = _pipe_read(k, "/pipes/drain")
        assert result == b"last-msg"

        # Then raises PipeClosed (closed + empty)
        with pytest.raises(RuntimeError, match="PipeClosed"):
            _pipe_read(k, "/pipes/drain")

    def test_close_nonexistent_raises(self) -> None:
        k = _make_kernel()
        with pytest.raises(FileNotFoundError):
            k.close_pipe("/pipes/ghost")


# ======================================================================
# PyKernel IPC Pipe -- lifecycle (create, destroy, list, close_all)
# ======================================================================


class TestKernelPipeLifecycle:
    def test_create_duplicate_raises(self) -> None:
        k = _make_kernel()
        k.create_pipe("/pipes/dup", 1024)
        with pytest.raises(RuntimeError, match="PipeExists"):
            k.create_pipe("/pipes/dup", 1024)

    def test_destroy_pipe(self) -> None:
        k = _make_kernel()
        k.create_pipe("/pipes/destroyme", 1024)
        assert _pipe_exists(k, "/pipes/destroyme") is True
        _pipe_destroy(k, "/pipes/destroyme")
        assert _pipe_exists(k, "/pipes/destroyme") is False

    def test_destroy_nonexistent_raises(self) -> None:
        k = _make_kernel()
        with pytest.raises(FileNotFoundError):
            _pipe_destroy(k, "/pipes/nope")

    def test_close_all_pipes(self) -> None:
        k = _make_kernel()
        k.create_pipe("/pipes/ca-1", 1024)
        k.create_pipe("/pipes/ca-2", 1024)
        _pipe_write(k, "/pipes/ca-1", b"data")
        k.close_all_pipes()
        # After close_all, writes should fail with PipeClosed
        with pytest.raises(RuntimeError, match="PipeClosed"):
            _pipe_write(k, "/pipes/ca-1", b"more")
        with pytest.raises(RuntimeError, match="PipeClosed"):
            _pipe_write(k, "/pipes/ca-2", b"more")

    def test_close_pipe_keeps_in_registry(self) -> None:
        """close_pipe signals close but keeps the entry for drain."""
        k = _make_kernel()
        k.create_pipe("/pipes/keep", 1024)
        k.close_pipe("/pipes/keep")
        # Pipe still exists in metastore (sys_stat returns DT_PIPE)
        assert _pipe_exists(k, "/pipes/keep") is True

    def test_destroy_after_close(self) -> None:
        """destroy_pipe after close_pipe should fully remove the pipe."""
        k = _make_kernel()
        k.create_pipe("/pipes/dc", 1024)
        k.close_pipe("/pipes/dc")
        _pipe_destroy(k, "/pipes/dc")
        assert _pipe_exists(k, "/pipes/dc") is False

    def test_create_after_destroy_succeeds(self) -> None:
        """After destroy, the path is free for reuse."""
        k = _make_kernel()
        k.create_pipe("/pipes/reuse", 1024)
        _pipe_destroy(k, "/pipes/reuse")
        k.create_pipe("/pipes/reuse", 2048)
        assert _pipe_exists(k, "/pipes/reuse") is True


# ======================================================================
# PyKernel IPC Pipe -- write/read on nonexistent pipe
# ======================================================================


class TestKernelPipeNotFound:
    def test_write_nonexistent_raises(self) -> None:
        k = _make_kernel()
        with pytest.raises(FileNotFoundError):
            _pipe_write(k, "/pipes/ghost", b"data")

    def test_read_nonexistent_raises(self) -> None:
        k = _make_kernel()
        with pytest.raises(FileNotFoundError):
            _pipe_read(k, "/pipes/ghost")


# ======================================================================
# PyKernel IPC Pipe -- isolation between kernels
# ======================================================================


class TestKernelPipeIsolation:
    def test_separate_kernels_isolated(self) -> None:
        """Each PyKernel instance has its own IPC registry."""
        k1 = _make_kernel()
        k2 = _make_kernel()
        k1.create_pipe("/pipes/iso", 1024)
        assert _pipe_exists(k1, "/pipes/iso") is True
        assert _pipe_exists(k2, "/pipes/iso") is False


# ======================================================================
# DT_PIPE metadata integration
# ======================================================================


class TestDTPipeMetadata:
    def test_dt_pipe_constant(self) -> None:
        assert DT_PIPE == 3

    def test_is_pipe_property(self) -> None:
        meta = FileMetadata(
            path="/nexus/pipes/test",
            size=0,
            entry_type=DT_PIPE,
        )
        assert meta.is_pipe is True
        assert meta.is_reg is False
        assert meta.is_dir is False
        assert meta.is_mount is False

    def test_validate_still_checks_path_for_pipe(self) -> None:
        """DT_PIPE still needs a valid path."""
        meta = FileMetadata(
            path="",
            size=0,
            entry_type=DT_PIPE,
        )
        with pytest.raises(Exception, match="path is required"):
            meta.validate()


# ======================================================================
# sys_setattr upsert semantics
# ======================================================================


class TestSysSetAttrUpsert:
    """Test sys_setattr upsert: create-on-write for metadata."""

    def test_setattr_update_mutable_fields(self) -> None:
        """sys_setattr on existing inode only updates mutable fields."""
        meta = FileMetadata(
            path="/existing/file",
            size=100,
            entry_type=DT_REG,
            mime_type="text/plain",
        )
        # Update mime_type (mutable)
        updated = replace(meta, mime_type="application/json")
        assert updated.mime_type == "application/json"
        assert updated.path == "/existing/file"
        assert updated.entry_type == DT_REG

    def test_setattr_entry_type_immutable_after_creation(self) -> None:
        """entry_type should not change after creation."""
        meta = FileMetadata(
            path="/existing/file",
            size=100,
            entry_type=DT_REG,
        )
        assert meta.entry_type == DT_REG


# ======================================================================
# Exception hierarchy
# ======================================================================


class TestPipeExceptionHierarchy:
    def test_pipe_exists_is_subclass_of_pipe_error(self) -> None:
        assert issubclass(PipeExistsError, PipeError)

    def test_pipe_full_is_subclass_of_pipe_error(self) -> None:
        assert issubclass(PipeFullError, PipeError)

    def test_pipe_empty_is_subclass_of_pipe_error(self) -> None:
        assert issubclass(PipeEmptyError, PipeError)

    def test_pipe_closed_is_subclass_of_pipe_error(self) -> None:
        assert issubclass(PipeClosedError, PipeError)

    def test_pipe_not_found_is_subclass_of_pipe_error(self) -> None:
        assert issubclass(PipeNotFoundError, PipeError)
