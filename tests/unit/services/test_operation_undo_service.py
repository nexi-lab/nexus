"""Tests for OperationUndoService (S24: Operations Undo)."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from nexus.bricks.versioning.operation_undo_service import OperationUndoService, UndoResult

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_dlc(content: bytes) -> MagicMock:
    """Return a mock_dlc where resolve_path succeeds and _kernel reads content."""
    dlc = MagicMock()
    dlc.resolve_path.return_value = ("backend_name", "/", "/root")
    dlc._kernel = MagicMock()
    dlc._kernel.sys_read_raw.return_value = content

    return dlc


def _make_service(
    content: bytes = b"old-content",
    fallback_backend: MagicMock | None = None,
) -> tuple[OperationUndoService, MagicMock, MagicMock, MagicMock, MagicMock]:
    """Create a service with mocked kernel primitives.

    Returns (service, write_fn, delete_fn, rename_fn, exists_fn).
    """
    dlc = _make_dlc(content)
    write_fn = MagicMock()
    delete_fn = MagicMock()
    rename_fn = MagicMock()
    exists_fn = MagicMock(return_value=True)
    svc = OperationUndoService(
        dlc=dlc,
        write_fn=write_fn,
        delete_fn=delete_fn,
        rename_fn=rename_fn,
        exists_fn=exists_fn,
        fallback_backend=fallback_backend,
    )
    return svc, write_fn, delete_fn, rename_fn, exists_fn


def _op(op_type: str, **kwargs: object) -> SimpleNamespace:
    """Build a fake OperationLogModel-like object."""
    defaults = {
        "operation_type": op_type,
        "path": "/workspace/test.txt",
        "new_path": None,
        "snapshot_hash": None,
        "metadata_snapshot": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# Write undo
# ---------------------------------------------------------------------------


class TestUndoWrite:
    def test_restore_previous_version(self) -> None:
        svc, write_fn, delete_fn, *_ = _make_service(content=b"previous")
        op = _op("write", snapshot_hash="abc123")

        result = svc.undo_operation(op)

        assert result == UndoResult(
            success=True,
            message="Restored previous version of /workspace/test.txt",
            operation_type="write",
            path="/workspace/test.txt",
        )
        write_fn.assert_called_once_with("/workspace/test.txt", b"previous")
        delete_fn.assert_not_called()

    def test_delete_newly_created_file(self) -> None:
        svc, write_fn, delete_fn, *_ = _make_service()
        op = _op("write")  # no snapshot_hash → file was new

        result = svc.undo_operation(op)

        assert result.success is True
        assert "newly created" in result.message
        delete_fn.assert_called_once_with("/workspace/test.txt")
        write_fn.assert_not_called()


# ---------------------------------------------------------------------------
# Delete undo
# ---------------------------------------------------------------------------


class TestUndoDelete:
    def test_restore_deleted_file(self) -> None:
        svc, write_fn, *_ = _make_service(content=b"restored")
        op = _op("delete", snapshot_hash="hash456")

        result = svc.undo_operation(op)

        assert result.success is True
        assert "Restored deleted file" in result.message
        write_fn.assert_called_once_with("/workspace/test.txt", b"restored")

    def test_no_snapshot_returns_failure(self) -> None:
        svc, write_fn, *_ = _make_service()
        op = _op("delete")  # no snapshot_hash

        result = svc.undo_operation(op)

        assert result.success is False
        assert "no snapshot" in result.message
        write_fn.assert_not_called()


# ---------------------------------------------------------------------------
# Rename undo
# ---------------------------------------------------------------------------


class TestUndoRename:
    def test_rename_back(self) -> None:
        svc, _, _, rename_fn, exists_fn = _make_service()
        exists_fn.return_value = True
        op = _op("rename", new_path="/workspace/moved.txt")

        result = svc.undo_operation(op)

        assert result.success is True
        rename_fn.assert_called_once_with("/workspace/moved.txt", "/workspace/test.txt")

    def test_new_path_missing(self) -> None:
        svc, _, _, rename_fn, _ = _make_service()
        op = _op("rename")  # no new_path

        result = svc.undo_operation(op)

        assert result.success is False
        assert "missing new_path" in result.message
        rename_fn.assert_not_called()

    def test_new_path_no_longer_exists(self) -> None:
        svc, _, _, rename_fn, exists_fn = _make_service()
        exists_fn.return_value = False
        op = _op("rename", new_path="/workspace/moved.txt")

        result = svc.undo_operation(op)

        assert result.success is False
        assert "no longer exists" in result.message
        rename_fn.assert_not_called()


# ---------------------------------------------------------------------------
# Unknown operation type
# ---------------------------------------------------------------------------


class TestUndoUnknown:
    def test_unknown_op_type(self) -> None:
        svc, *_ = _make_service()
        op = _op("chmod")

        result = svc.undo_operation(op)

        assert result.success is False
        assert "not implemented" in result.message


# ---------------------------------------------------------------------------
# Fallback backend
# ---------------------------------------------------------------------------


class TestFallbackBackend:
    def test_uses_fallback_when_dlc_resolve_fails(self) -> None:
        fallback = MagicMock()
        fallback.read_content.return_value = b"fallback-data"

        dlc = MagicMock()
        dlc.resolve_path.side_effect = RuntimeError("resolve failed")

        svc = OperationUndoService(
            dlc=dlc,
            write_fn=MagicMock(),
            delete_fn=MagicMock(),
            rename_fn=MagicMock(),
            exists_fn=MagicMock(),
            fallback_backend=fallback,
        )
        op = _op("write", snapshot_hash="hash789")

        result = svc.undo_operation(op)

        assert result.success is True
        fallback.read_content.assert_called_once_with("hash789")
        svc._write.assert_called_once_with("/workspace/test.txt", b"fallback-data")

    def test_raises_when_no_fallback_and_dlc_resolve_fails(self) -> None:
        dlc = MagicMock()
        dlc.resolve_path.side_effect = RuntimeError("resolve failed")

        svc = OperationUndoService(
            dlc=dlc,
            write_fn=MagicMock(),
            delete_fn=MagicMock(),
            rename_fn=MagicMock(),
            exists_fn=MagicMock(),
        )
        op = _op("write", snapshot_hash="hash789")

        with pytest.raises(RuntimeError, match="resolve failed"):
            svc.undo_operation(op)
