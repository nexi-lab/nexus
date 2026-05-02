"""Unit tests for SnapshotWriteHook (Issue #1770)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from nexus.bricks.snapshot.snapshot_hook import SnapshotWriteHook
from nexus.contracts.vfs_hooks import DeleteHookContext, WriteHookContext


@pytest.fixture()
def mock_svc() -> MagicMock:
    return MagicMock()


@pytest.fixture()
def hook(mock_svc: MagicMock) -> SnapshotWriteHook:
    return SnapshotWriteHook(mock_svc)


def _make_meta(**overrides: object) -> MagicMock:
    meta = MagicMock()
    meta.content_id = overrides.get("content_id", "old-etag")
    meta.size = overrides.get("size", 1024)
    meta.version = overrides.get("version", 3)
    meta.modified_at = overrides.get("modified_at", datetime(2026, 1, 1, tzinfo=UTC))
    meta.last_writer_address = overrides.get("last_writer_address")
    return meta


# ── HookSpec protocol ────────────────────────────────────────────────


class TestHookSpec:
    def test_hook_spec_declares_write_and_delete(self, hook: SnapshotWriteHook) -> None:
        spec = hook.hook_spec()
        assert hook in spec.write_hooks
        assert hook in spec.delete_hooks

    def test_name(self, hook: SnapshotWriteHook) -> None:
        assert hook.name == "snapshot_write_tracker"


# ── on_post_write ─────────────────────────────────────────────────────


class TestOnPostWrite:
    def test_tracks_write_when_transaction_active(
        self, hook: SnapshotWriteHook, mock_svc: MagicMock
    ) -> None:
        mock_svc.is_tracked.return_value = "txn-1"
        old = _make_meta()
        ctx = WriteHookContext(
            path="/file.txt",
            content=b"data",
            context=None,
            old_metadata=old,
            content_id="new-hash",
        )
        hook.on_post_write(ctx)

        mock_svc.track_write.assert_called_once_with(
            "txn-1",
            "/file.txt",
            "old-etag",
            {
                "size": 1024,
                "version": 3,
                "modified_at": "2026-01-01T00:00:00+00:00",
                "last_writer_address": None,
            },
            "new-hash",
        )

    def test_captures_pre_write_writer_address(
        self, hook: SnapshotWriteHook, mock_svc: MagicMock
    ) -> None:
        """Capture the original writer's address with ``original_metadata``
        so historical reads of ``original_hash`` after a cross-node
        overwrite can scatter-gather chunks from the original writer
        rather than just the current one (Issue #3989, codex r8)."""
        mock_svc.is_tracked.return_value = "txn-x"
        old = _make_meta(last_writer_address="original-writer:2126")
        ctx = WriteHookContext(
            path="/file.txt",
            content=b"data",
            context=None,
            old_metadata=old,
            content_id="new-hash",
        )
        hook.on_post_write(ctx)
        captured_meta = mock_svc.track_write.call_args.args[3]
        assert captured_meta["last_writer_address"] == "original-writer:2126"

    def test_skips_when_no_transaction(self, hook: SnapshotWriteHook, mock_svc: MagicMock) -> None:
        mock_svc.is_tracked.return_value = None
        ctx = WriteHookContext(
            path="/file.txt",
            content=b"data",
            context=None,
            old_metadata=_make_meta(),
            content_id="new-hash",
        )
        hook.on_post_write(ctx)
        mock_svc.track_write.assert_not_called()

    def test_handles_new_file_no_old_metadata(
        self, hook: SnapshotWriteHook, mock_svc: MagicMock
    ) -> None:
        mock_svc.is_tracked.return_value = "txn-2"
        ctx = WriteHookContext(
            path="/new.txt",
            content=b"data",
            context=None,
            old_metadata=None,
            content_id="hash-1",
            is_new_file=True,
        )
        hook.on_post_write(ctx)

        mock_svc.track_write.assert_called_once_with(
            "txn-2",
            "/new.txt",
            None,
            None,
            "hash-1",
        )


# ── on_post_delete ────────────────────────────────────────────────────


class TestOnPostDelete:
    def test_tracks_delete_when_transaction_active(
        self, hook: SnapshotWriteHook, mock_svc: MagicMock
    ) -> None:
        mock_svc.is_tracked.return_value = "txn-1"
        meta = _make_meta()
        ctx = DeleteHookContext(path="/file.txt", context=None, metadata=meta)
        hook.on_post_delete(ctx)

        mock_svc.track_delete.assert_called_once_with(
            "txn-1",
            "/file.txt",
            "old-etag",
            {
                "size": 1024,
                "version": 3,
                "modified_at": "2026-01-01T00:00:00+00:00",
                "last_writer_address": None,
            },
        )

    def test_captures_pre_delete_writer_address(
        self, hook: SnapshotWriteHook, mock_svc: MagicMock
    ) -> None:
        """Pre-delete ``last_writer_address`` must be captured so a
        post-delete historical read of the snapshot hash can scatter-gather
        chunks from the writer's node when local chunks are missing
        (Issue #3989, codex r8)."""
        mock_svc.is_tracked.return_value = "txn-y"
        meta = _make_meta(last_writer_address="writer-7:2126")
        ctx = DeleteHookContext(path="/file.txt", context=None, metadata=meta)
        hook.on_post_delete(ctx)
        captured_meta = mock_svc.track_delete.call_args.args[3]
        assert captured_meta["last_writer_address"] == "writer-7:2126"

    def test_skips_when_no_transaction(self, hook: SnapshotWriteHook, mock_svc: MagicMock) -> None:
        mock_svc.is_tracked.return_value = None
        ctx = DeleteHookContext(path="/file.txt", context=None, metadata=_make_meta())
        hook.on_post_delete(ctx)
        mock_svc.track_delete.assert_not_called()

    def test_skips_when_no_metadata(self, hook: SnapshotWriteHook, mock_svc: MagicMock) -> None:
        mock_svc.is_tracked.return_value = "txn-3"
        ctx = DeleteHookContext(path="/file.txt", context=None, metadata=None)
        hook.on_post_delete(ctx)
        mock_svc.track_delete.assert_not_called()
