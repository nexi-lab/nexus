"""Unit tests for audit_strict_mode behavior across all write paths.

Documents and tests the unified error policy:
- ALL write paths (write, delete, rename, write_batch) respect audit_strict_mode
- When audit_strict_mode=True: AuditLogError raised on observer failure
- When audit_strict_mode=False: errors logged but suppressed

Phase 1.2 of #1246/#1330 consolidation plan.
Updated in Phase 2.1 to reflect unified _notify_observer() policy.
"""

from __future__ import annotations

import tempfile
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nexus import LocalBackend, NexusFS
from nexus.core.exceptions import AuditLogError
from tests.helpers.in_memory_metadata_store import InMemoryFileMetadataStore


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


def _make_nx(
    temp_dir: Path,
    *,
    audit_strict_mode: bool = True,
    write_observer: object | None = None,
) -> NexusFS:
    """Create a minimal NexusFS with controllable audit_strict_mode."""
    return NexusFS(
        backend=LocalBackend(str(temp_dir / "data")),
        metadata_store=InMemoryFileMetadataStore(),
        enforce_permissions=False,
        auto_parse=False,
        audit_strict_mode=audit_strict_mode,
        write_observer=write_observer,
    )


def _failing_observer() -> MagicMock:
    """Create a write observer that raises on every method."""
    observer = MagicMock()
    error = RuntimeError("SQL connection lost")
    observer.on_write.side_effect = error
    observer.on_delete.side_effect = error
    observer.on_rename.side_effect = error
    observer.on_write_batch.side_effect = error
    return observer


# =========================================================================
# write() — respects audit_strict_mode
# =========================================================================


class TestWriteStrictModeTrue:
    """write() with audit_strict_mode=True should raise on observer failure."""

    def test_write_raises_audit_log_error(self, temp_dir: Path) -> None:
        observer = _failing_observer()
        nx = _make_nx(temp_dir, audit_strict_mode=True, write_observer=observer)

        try:
            with pytest.raises(AuditLogError):
                nx.write("/test.txt", b"content")
        finally:
            nx.close()

    def test_write_file_still_exists_in_metastore_after_error(
        self, temp_dir: Path
    ) -> None:
        """Even when AuditLogError is raised, the sled write has already committed."""
        observer = _failing_observer()
        nx = _make_nx(temp_dir, audit_strict_mode=True, write_observer=observer)

        try:
            with pytest.raises(AuditLogError):
                nx.write("/test.txt", b"content")

            # The file IS in metastore (sled write happened before observer)
            meta = nx.metadata.get("/test.txt")
            assert meta is not None
            assert meta.size == len(b"content")
        finally:
            nx.close()


class TestWriteStrictModeFalse:
    """write() with audit_strict_mode=False should succeed despite observer failure."""

    def test_write_succeeds_despite_observer_failure(self, temp_dir: Path) -> None:
        observer = _failing_observer()
        nx = _make_nx(temp_dir, audit_strict_mode=False, write_observer=observer)

        try:
            result = nx.write("/test.txt", b"content")
            assert "etag" in result
        finally:
            nx.close()

    def test_write_observer_was_called(self, temp_dir: Path) -> None:
        observer = _failing_observer()
        nx = _make_nx(temp_dir, audit_strict_mode=False, write_observer=observer)

        try:
            nx.write("/test.txt", b"content")
            observer.on_write.assert_called_once()
        finally:
            nx.close()


# =========================================================================
# delete() — respects audit_strict_mode (unified via _notify_observer)
# =========================================================================


class TestDeleteRespectsStrictMode:
    """delete() raises AuditLogError when audit_strict_mode=True."""

    def test_delete_raises_audit_log_error_with_strict_mode_true(
        self, temp_dir: Path
    ) -> None:
        """delete() raises AuditLogError when observer fails and strict mode is on."""
        observer = _failing_observer()
        nx = _make_nx(temp_dir, audit_strict_mode=True, write_observer=observer)

        try:
            # First write a file (with no observer to avoid the write failure)
            nx._write_observer = None
            nx.write("/test.txt", b"content")
            nx._write_observer = observer

            with pytest.raises(AuditLogError):
                nx.delete("/test.txt")
        finally:
            nx.close()

    def test_delete_succeeds_with_strict_mode_false(self, temp_dir: Path) -> None:
        """delete() succeeds despite observer failure when strict mode is off."""
        observer = _failing_observer()
        nx = _make_nx(temp_dir, audit_strict_mode=False, write_observer=observer)

        try:
            nx._write_observer = None
            nx.write("/test.txt", b"content")
            nx._write_observer = observer

            result = nx.delete("/test.txt")
            assert result is not None
            assert nx.metadata.get("/test.txt") is None
        finally:
            nx.close()

    def test_delete_observer_is_called_before_metastore_delete(
        self, temp_dir: Path
    ) -> None:
        """Verify observer is called on delete."""
        observer = MagicMock()  # non-failing observer
        nx = _make_nx(temp_dir, audit_strict_mode=True, write_observer=observer)

        try:
            nx._write_observer = None
            nx.write("/test.txt", b"content")
            nx._write_observer = observer

            nx.delete("/test.txt")
            observer.on_delete.assert_called_once()
        finally:
            nx.close()


# =========================================================================
# rename() — respects audit_strict_mode (unified via _notify_observer)
# =========================================================================


class TestRenameRespectsStrictMode:
    """rename() raises AuditLogError when audit_strict_mode=True."""

    def test_rename_raises_audit_log_error_with_strict_mode_true(
        self, temp_dir: Path
    ) -> None:
        observer = _failing_observer()
        nx = _make_nx(temp_dir, audit_strict_mode=True, write_observer=observer)

        try:
            nx._write_observer = None
            nx.write("/old.txt", b"content")
            nx._write_observer = observer

            with pytest.raises(AuditLogError):
                nx.rename("/old.txt", "/new.txt")
        finally:
            nx.close()

    def test_rename_succeeds_with_strict_mode_false(self, temp_dir: Path) -> None:
        observer = _failing_observer()
        nx = _make_nx(temp_dir, audit_strict_mode=False, write_observer=observer)

        try:
            nx._write_observer = None
            nx.write("/old.txt", b"content")
            nx._write_observer = observer

            result = nx.rename("/old.txt", "/new.txt")
            assert result is not None
        finally:
            nx.close()


# =========================================================================
# write_batch() — respects audit_strict_mode (unified via _notify_observer)
# =========================================================================


class TestWriteBatchRespectsStrictMode:
    """write_batch() raises AuditLogError when audit_strict_mode=True."""

    def test_write_batch_raises_audit_log_error_with_strict_mode_true(
        self, temp_dir: Path
    ) -> None:
        observer = _failing_observer()
        nx = _make_nx(temp_dir, audit_strict_mode=True, write_observer=observer)

        try:
            files = [("/a.txt", b"aaa"), ("/b.txt", b"bbb")]
            with pytest.raises(AuditLogError):
                nx.write_batch(files)
        finally:
            nx.close()

    def test_write_batch_succeeds_with_strict_mode_false(
        self, temp_dir: Path
    ) -> None:
        observer = _failing_observer()
        nx = _make_nx(temp_dir, audit_strict_mode=False, write_observer=observer)

        try:
            files = [("/a.txt", b"aaa"), ("/b.txt", b"bbb")]
            results = nx.write_batch(files)
            assert len(results) == 2
        finally:
            nx.close()


# =========================================================================
# No observer — all operations should work without write_observer
# =========================================================================


class TestNoObserver:
    """Operations succeed normally when write_observer is None."""

    def test_write_without_observer(self, temp_dir: Path) -> None:
        nx = _make_nx(temp_dir, write_observer=None)
        try:
            result = nx.write("/test.txt", b"content")
            assert "etag" in result
        finally:
            nx.close()

    def test_delete_without_observer(self, temp_dir: Path) -> None:
        nx = _make_nx(temp_dir, write_observer=None)
        try:
            nx.write("/test.txt", b"content")
            result = nx.delete("/test.txt")
            assert result is not None
            assert nx.metadata.get("/test.txt") is None
        finally:
            nx.close()

    def test_rename_without_observer(self, temp_dir: Path) -> None:
        nx = _make_nx(temp_dir, write_observer=None)
        try:
            nx.write("/old.txt", b"content")
            result = nx.rename("/old.txt", "/new.txt")
            assert result is not None
        finally:
            nx.close()

    def test_write_batch_without_observer(self, temp_dir: Path) -> None:
        nx = _make_nx(temp_dir, write_observer=None)
        try:
            results = nx.write_batch([("/a.txt", b"a"), ("/b.txt", b"b")])
            assert len(results) == 2
        finally:
            nx.close()
