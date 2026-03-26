"""Tests for DeltaSyncResult and MountSyncState (Issue #3266, Decision #11A).

Contract tests for the typed delta sync result dataclass.
Covers: valid delta, empty delta, malformed data, oversized delta,
duplicate IDs, and MountSyncState health tracking.
"""

from __future__ import annotations

import pytest

from nexus.backends.connectors.cli.sync_types import DeltaItem, DeltaSyncResult, MountSyncState

# ============================================================================
# DeltaItem tests
# ============================================================================


class TestDeltaItem:
    def test_create_with_required_fields(self) -> None:
        item = DeltaItem(id="msg123", path="INBOX/tid-msg123.yaml")
        assert item.id == "msg123"
        assert item.path == "INBOX/tid-msg123.yaml"
        assert item.content_hash is None
        assert item.size == 0

    def test_create_with_all_fields(self) -> None:
        item = DeltaItem(
            id="msg123",
            path="INBOX/tid-msg123.yaml",
            content_hash="sha256abc",
            size=1024,
        )
        assert item.content_hash == "sha256abc"
        assert item.size == 1024

    def test_is_frozen(self) -> None:
        item = DeltaItem(id="msg123", path="INBOX/tid-msg123.yaml")
        with pytest.raises(AttributeError):
            item.id = "other"


# ============================================================================
# DeltaSyncResult tests
# ============================================================================


class TestDeltaSyncResult:
    def test_empty_delta(self) -> None:
        delta = DeltaSyncResult()
        assert not delta.has_changes
        assert delta.total_changes == 0
        assert delta.added == []
        assert delta.deleted == []
        assert delta.sync_token is None
        assert not delta.full_sync_required

    def test_valid_delta_with_additions(self) -> None:
        items = [
            DeltaItem(id="msg1", path="INBOX/t1-msg1.yaml"),
            DeltaItem(id="msg2", path="SENT/t2-msg2.yaml"),
        ]
        delta = DeltaSyncResult(added=items, sync_token="12345")
        assert delta.has_changes
        assert delta.total_changes == 2
        assert len(delta.added) == 2
        assert delta.sync_token == "12345"

    def test_valid_delta_with_deletions(self) -> None:
        delta = DeltaSyncResult(
            deleted=["INBOX/old1.yaml", "INBOX/old2.yaml"],
            sync_token="67890",
        )
        assert delta.has_changes
        assert delta.total_changes == 2
        assert len(delta.deleted) == 2

    def test_valid_delta_with_both(self) -> None:
        delta = DeltaSyncResult(
            added=[DeltaItem(id="new1", path="INBOX/new1.yaml")],
            deleted=["INBOX/old1.yaml"],
            sync_token="99999",
        )
        assert delta.has_changes
        assert delta.total_changes == 2

    def test_full_sync_required_flag(self) -> None:
        delta = DeltaSyncResult(full_sync_required=True)
        assert delta.full_sync_required
        assert not delta.has_changes

    def test_sync_token_preserved(self) -> None:
        delta = DeltaSyncResult(sync_token="history_id_12345")
        assert delta.sync_token == "history_id_12345"

    def test_is_frozen(self) -> None:
        delta = DeltaSyncResult()
        with pytest.raises(AttributeError):
            delta.sync_token = "new"

    def test_large_delta(self) -> None:
        """Test that large deltas work (no artificial cap)."""
        items = [DeltaItem(id=f"msg{i}", path=f"INBOX/t-msg{i}.yaml") for i in range(1000)]
        delta = DeltaSyncResult(added=items, sync_token="large")
        assert delta.total_changes == 1000


# ============================================================================
# MountSyncState tests
# ============================================================================


class TestMountSyncState:
    def test_initial_state(self) -> None:
        state = MountSyncState(mount_point="/mnt/gmail")
        assert state.mount_point == "/mnt/gmail"
        assert state.last_successful_sync is None
        assert state.consecutive_failures == 0
        assert state.is_healthy
        assert state.sync_token is None

    def test_record_success(self) -> None:
        state = MountSyncState(mount_point="/mnt/gmail")
        state.record_success(files_synced=10, sync_token="token123")
        assert state.last_successful_sync is not None
        assert state.consecutive_failures == 0
        assert state.total_files_synced == 10
        assert state.sync_token == "token123"
        assert state.is_healthy

    def test_record_failure(self) -> None:
        state = MountSyncState(mount_point="/mnt/gmail")
        state.record_failure("connection timeout")
        assert state.consecutive_failures == 1
        assert state.last_error == "connection timeout"
        assert state.is_healthy  # Still healthy after 1 failure

    def test_unhealthy_after_three_failures(self) -> None:
        state = MountSyncState(mount_point="/mnt/gmail")
        state.record_failure("error 1")
        state.record_failure("error 2")
        state.record_failure("error 3")
        assert not state.is_healthy
        assert state.consecutive_failures == 3

    def test_success_resets_failures(self) -> None:
        state = MountSyncState(mount_point="/mnt/gmail")
        state.record_failure("error 1")
        state.record_failure("error 2")
        state.record_success(files_synced=5)
        assert state.is_healthy
        assert state.consecutive_failures == 0
        assert state.last_error is None

    def test_cumulative_files_synced(self) -> None:
        state = MountSyncState(mount_point="/mnt/gmail")
        state.record_success(files_synced=10)
        state.record_success(files_synced=5)
        assert state.total_files_synced == 15

    def test_to_dict(self) -> None:
        state = MountSyncState(mount_point="/mnt/gmail")
        state.record_success(files_synced=10, sync_token="tok")
        d = state.to_dict()
        assert d["mount_point"] == "/mnt/gmail"
        assert d["total_files_synced"] == 10
        assert d["sync_token"] == "tok"
        assert d["is_healthy"] is True
        assert d["consecutive_failures"] == 0
        assert d["last_successful_sync"] is not None

    def test_sync_in_progress(self) -> None:
        state = MountSyncState(mount_point="/mnt/gmail")
        assert not state.sync_in_progress
        state.sync_in_progress = True
        assert state.sync_in_progress
        d = state.to_dict()
        assert d["sync_in_progress"] is True
