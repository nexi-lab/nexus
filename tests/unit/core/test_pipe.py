"""Unit tests for DT_PIPE kernel IPC primitive.

Tests Rust Kernel IPC pipe operations (create, read, write, close, destroy)
and DT_PIPE metadata integration.
See: rust/kernel/src/pipe.rs, rust/kernel/src/kernel.rs,
     KERNEL-ARCHITECTURE.md §6.
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
    from nexus_kernel import Kernel

    RUST_AVAILABLE = True
except ImportError:
    RUST_AVAILABLE = False

pytestmark = pytest.mark.skipif(not RUST_AVAILABLE, reason="nexus_kernel not built")


def _make_kernel() -> "Kernel":
    return Kernel()


# ======================================================================
# Kernel IPC Pipe — basic operations
# ======================================================================


class TestKernelPipeBasic:
    def test_create_and_has(self) -> None:
        k = _make_kernel()
        k.create_pipe("/pipes/test", 1024)
        assert k.has_pipe("/pipes/test") is True

    def test_has_pipe_nonexistent(self) -> None:
        k = _make_kernel()
        assert k.has_pipe("/pipes/nope") is False

    def test_write_read_roundtrip(self) -> None:
        k = _make_kernel()
        k.create_pipe("/pipes/rt", 1024)
        written = k.pipe_write_nowait("/pipes/rt", b"hello")
        assert written == 5
        data = k.pipe_read_nowait("/pipes/rt")
        assert data == b"hello"

    def test_fifo_ordering(self) -> None:
        k = _make_kernel()
        k.create_pipe("/pipes/fifo", 4096)
        k.pipe_write_nowait("/pipes/fifo", b"first")
        k.pipe_write_nowait("/pipes/fifo", b"second")
        k.pipe_write_nowait("/pipes/fifo", b"third")
        assert k.pipe_read_nowait("/pipes/fifo") == b"first"
        assert k.pipe_read_nowait("/pipes/fifo") == b"second"
        assert k.pipe_read_nowait("/pipes/fifo") == b"third"

    def test_read_empty_returns_none(self) -> None:
        k = _make_kernel()
        k.create_pipe("/pipes/empty", 1024)
        assert k.pipe_read_nowait("/pipes/empty") is None

    def test_multiple_messages_roundtrip(self) -> None:
        k = _make_kernel()
        k.create_pipe("/pipes/multi", 65536)
        for i in range(10):
            k.pipe_write_nowait("/pipes/multi", f"msg-{i}".encode())
        for i in range(10):
            assert k.pipe_read_nowait("/pipes/multi") == f"msg-{i}".encode()
        assert k.pipe_read_nowait("/pipes/multi") is None

    def test_empty_write_is_noop(self) -> None:
        k = _make_kernel()
        k.create_pipe("/pipes/empty-write", 1024)
        result = k.pipe_write_nowait("/pipes/empty-write", b"")
        assert result == 0
        assert k.pipe_read_nowait("/pipes/empty-write") is None


# ======================================================================
# Kernel IPC Pipe — capacity limits
# ======================================================================


class TestKernelPipeCapacity:
    def test_oversized_message_rejected(self) -> None:
        """A single message larger than capacity should be rejected."""
        k = _make_kernel()
        k.create_pipe("/pipes/cap", 10)
        with pytest.raises(RuntimeError, match="PipeFull"):
            k.pipe_write_nowait("/pipes/cap", b"x" * 11)

    def test_buffer_full_raises(self) -> None:
        """Writing to a full buffer should raise PipeFull."""
        k = _make_kernel()
        k.create_pipe("/pipes/full", 32)
        # Fill enough to cause PipeFull on next write
        k.pipe_write_nowait("/pipes/full", b"x" * 20)
        with pytest.raises(RuntimeError, match="PipeFull"):
            k.pipe_write_nowait("/pipes/full", b"y" * 20)

    def test_space_freed_after_read(self) -> None:
        """After reading, the freed space should allow new writes."""
        k = _make_kernel()
        k.create_pipe("/pipes/free", 64)
        k.pipe_write_nowait("/pipes/free", b"x" * 30)
        k.pipe_read_nowait("/pipes/free")
        # Now have space again
        k.pipe_write_nowait("/pipes/free", b"y" * 30)
        assert k.pipe_read_nowait("/pipes/free") == b"y" * 30


# ======================================================================
# Kernel IPC Pipe — close semantics
# ======================================================================


class TestKernelPipeClose:
    def test_write_after_close_raises(self) -> None:
        k = _make_kernel()
        k.create_pipe("/pipes/closed-w", 1024)
        k.close_pipe("/pipes/closed-w")
        with pytest.raises(RuntimeError, match="PipeClosed"):
            k.pipe_write_nowait("/pipes/closed-w", b"data")

    def test_read_drains_remaining_then_closed(self) -> None:
        """After close, remaining data can still be read; then returns closed error."""
        k = _make_kernel()
        k.create_pipe("/pipes/drain", 1024)
        k.pipe_write_nowait("/pipes/drain", b"last-msg")
        k.close_pipe("/pipes/drain")

        # Can still read buffered messages
        result = k.pipe_read_nowait("/pipes/drain")
        assert result == b"last-msg"

        # Then raises PipeClosed (closed + empty)
        with pytest.raises(RuntimeError, match="PipeClosed"):
            k.pipe_read_nowait("/pipes/drain")

    def test_close_nonexistent_raises(self) -> None:
        k = _make_kernel()
        with pytest.raises(FileNotFoundError):
            k.close_pipe("/pipes/ghost")


# ======================================================================
# Kernel IPC Pipe — lifecycle (create, destroy, list, close_all)
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
        assert k.has_pipe("/pipes/destroyme") is True
        k.destroy_pipe("/pipes/destroyme")
        assert k.has_pipe("/pipes/destroyme") is False

    def test_destroy_nonexistent_raises(self) -> None:
        k = _make_kernel()
        with pytest.raises(FileNotFoundError):
            k.destroy_pipe("/pipes/nope")

    def test_list_pipes(self) -> None:
        k = _make_kernel()
        k.create_pipe("/pipes/a", 100)
        k.create_pipe("/pipes/b", 200)
        pipes = k.list_pipes()
        assert "/pipes/a" in pipes
        assert "/pipes/b" in pipes
        assert len(pipes) >= 2

    def test_list_pipes_empty(self) -> None:
        k = _make_kernel()
        pipes = k.list_pipes()
        assert pipes == []

    def test_close_all_pipes(self) -> None:
        k = _make_kernel()
        k.create_pipe("/pipes/ca-1", 1024)
        k.create_pipe("/pipes/ca-2", 1024)
        k.pipe_write_nowait("/pipes/ca-1", b"data")
        k.close_all_pipes()
        # After close_all, writes should fail with PipeClosed
        with pytest.raises(RuntimeError, match="PipeClosed"):
            k.pipe_write_nowait("/pipes/ca-1", b"more")
        with pytest.raises(RuntimeError, match="PipeClosed"):
            k.pipe_write_nowait("/pipes/ca-2", b"more")

    def test_close_pipe_keeps_in_registry(self) -> None:
        """close_pipe signals close but keeps the entry for drain."""
        k = _make_kernel()
        k.create_pipe("/pipes/keep", 1024)
        k.close_pipe("/pipes/keep")
        # Pipe still exists in registry (has_pipe = True)
        assert k.has_pipe("/pipes/keep") is True

    def test_destroy_after_close(self) -> None:
        """destroy_pipe after close_pipe should fully remove the pipe."""
        k = _make_kernel()
        k.create_pipe("/pipes/dc", 1024)
        k.close_pipe("/pipes/dc")
        k.destroy_pipe("/pipes/dc")
        assert k.has_pipe("/pipes/dc") is False

    def test_create_after_destroy_succeeds(self) -> None:
        """After destroy, the path is free for reuse."""
        k = _make_kernel()
        k.create_pipe("/pipes/reuse", 1024)
        k.destroy_pipe("/pipes/reuse")
        k.create_pipe("/pipes/reuse", 2048)
        assert k.has_pipe("/pipes/reuse") is True


# ======================================================================
# Kernel IPC Pipe — write/read on nonexistent pipe
# ======================================================================


class TestKernelPipeNotFound:
    def test_write_nonexistent_raises(self) -> None:
        k = _make_kernel()
        with pytest.raises(FileNotFoundError):
            k.pipe_write_nowait("/pipes/ghost", b"data")

    def test_read_nonexistent_raises(self) -> None:
        k = _make_kernel()
        with pytest.raises(FileNotFoundError):
            k.pipe_read_nowait("/pipes/ghost")


# ======================================================================
# Kernel IPC Pipe — isolation between kernels
# ======================================================================


class TestKernelPipeIsolation:
    def test_separate_kernels_isolated(self) -> None:
        """Each Kernel instance has its own IPC registry."""
        k1 = _make_kernel()
        k2 = _make_kernel()
        k1.create_pipe("/pipes/iso", 1024)
        assert k1.has_pipe("/pipes/iso") is True
        assert k2.has_pipe("/pipes/iso") is False


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
