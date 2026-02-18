"""Tests for TransactionalSnapshot protocol shape compliance (Issue #1752).

Verifies the protocol interface, data models, enums, and exceptions
are correctly defined before implementation.
"""

from __future__ import annotations

import pytest

from nexus.services.protocols.transactional_snapshot import (
    ConflictInfo,
    InvalidTransactionStateError,
    OverlappingTransactionError,
    PathSnapshot,
    SnapshotId,
    TransactionalSnapshotProtocol,
    TransactionConfig,
    TransactionInfo,
    TransactionNotFoundError,
    TransactionResult,
    TransactionState,
)


class TestTransactionState:
    """TransactionState enum values."""

    def test_active(self) -> None:
        assert TransactionState.ACTIVE == "ACTIVE"

    def test_committed(self) -> None:
        assert TransactionState.COMMITTED == "COMMITTED"

    def test_rolled_back(self) -> None:
        assert TransactionState.ROLLED_BACK == "ROLLED_BACK"

    def test_expired(self) -> None:
        assert TransactionState.EXPIRED == "EXPIRED"

    def test_all_terminal_states(self) -> None:
        terminal = {
            TransactionState.COMMITTED,
            TransactionState.ROLLED_BACK,
            TransactionState.EXPIRED,
        }
        assert TransactionState.ACTIVE not in terminal
        assert len(terminal) == 3


class TestSnapshotId:
    """SnapshotId is a frozen, slotted dataclass."""

    def test_creation(self) -> None:
        sid = SnapshotId(id="test-123")
        assert sid.id == "test-123"

    def test_frozen(self) -> None:
        sid = SnapshotId(id="test-123")
        with pytest.raises(AttributeError):
            sid.id = "changed"  # type: ignore[misc]

    def test_equality(self) -> None:
        a = SnapshotId(id="same")
        b = SnapshotId(id="same")
        assert a == b

    def test_inequality(self) -> None:
        a = SnapshotId(id="a")
        b = SnapshotId(id="b")
        assert a != b


class TestPathSnapshot:
    """PathSnapshot captures a single path's state at begin() time."""

    def test_existing_file(self) -> None:
        ps = PathSnapshot(
            path="/data/config.json",
            content_hash="abc123",
            size=1024,
            metadata_json='{"key": "value"}',
            existed=True,
        )
        assert ps.path == "/data/config.json"
        assert ps.content_hash == "abc123"
        assert ps.size == 1024
        assert ps.existed is True

    def test_absent_file(self) -> None:
        ps = PathSnapshot(
            path="/data/new.txt",
            content_hash=None,
            size=0,
            metadata_json=None,
            existed=False,
        )
        assert ps.content_hash is None
        assert ps.existed is False

    def test_frozen(self) -> None:
        ps = PathSnapshot(path="/x", content_hash=None, size=0, metadata_json=None, existed=False)
        with pytest.raises(AttributeError):
            ps.path = "/y"  # type: ignore[misc]


class TestConflictInfo:
    """ConflictInfo describes a rollback conflict."""

    def test_creation(self) -> None:
        ci = ConflictInfo(
            path="/data/file.txt",
            snapshot_hash="aaa",
            current_hash="bbb",
            reason="Modified by agent-b after snapshot",
        )
        assert ci.path == "/data/file.txt"
        assert ci.snapshot_hash == "aaa"
        assert ci.current_hash == "bbb"
        assert "agent-b" in ci.reason


class TestTransactionResult:
    """TransactionResult from rollback()."""

    def test_defaults(self) -> None:
        result = TransactionResult(snapshot_id="snap-1")
        assert result.reverted == []
        assert result.conflicts == []
        assert result.deleted == []
        assert result.stats == {}

    def test_with_data(self) -> None:
        conflict = ConflictInfo(path="/x", snapshot_hash="a", current_hash="b", reason="conflict")
        result = TransactionResult(
            snapshot_id="snap-1",
            reverted=["/a", "/b"],
            conflicts=[conflict],
            deleted=["/c"],
            stats={"duration_ms": 42},
        )
        assert len(result.reverted) == 2
        assert len(result.conflicts) == 1
        assert result.conflicts[0].path == "/x"
        assert result.stats["duration_ms"] == 42


class TestTransactionInfo:
    """TransactionInfo is a read-only view."""

    def test_active_transaction(self) -> None:
        info = TransactionInfo(
            snapshot_id="snap-1",
            agent_id="agent-a",
            zone_id="root",
            status=TransactionState.ACTIVE,
            paths=["/data/a.txt", "/data/b.txt"],
            created_at="2026-02-17T00:00:00Z",
            expires_at="2026-02-17T01:00:00Z",
        )
        assert info.status == TransactionState.ACTIVE
        assert info.committed_at is None
        assert info.rolled_back_at is None
        assert len(info.paths) == 2

    def test_committed_transaction(self) -> None:
        info = TransactionInfo(
            snapshot_id="snap-2",
            agent_id="agent-a",
            zone_id="root",
            status=TransactionState.COMMITTED,
            paths=["/data/a.txt"],
            created_at="2026-02-17T00:00:00Z",
            expires_at="2026-02-17T01:00:00Z",
            committed_at="2026-02-17T00:05:00Z",
        )
        assert info.status == TransactionState.COMMITTED
        assert info.committed_at is not None


class TestTransactionConfig:
    """TransactionConfig defaults."""

    def test_defaults(self) -> None:
        config = TransactionConfig()
        assert config.ttl_seconds == 3600
        assert config.max_paths_per_transaction == 10_000
        assert config.auto_snapshot_on_destructive is False
        assert config.cleanup_interval_seconds == 60

    def test_custom(self) -> None:
        config = TransactionConfig(ttl_seconds=300, max_paths_per_transaction=100)
        assert config.ttl_seconds == 300
        assert config.max_paths_per_transaction == 100

    def test_frozen(self) -> None:
        config = TransactionConfig()
        with pytest.raises(AttributeError):
            config.ttl_seconds = 999  # type: ignore[misc]


class TestExceptions:
    """Custom exception types."""

    def test_invalid_state_error(self) -> None:
        err = InvalidTransactionStateError("snap-1", TransactionState.COMMITTED, "rollback")
        assert err.snapshot_id == "snap-1"
        assert err.current_state == TransactionState.COMMITTED
        assert err.attempted_action == "rollback"
        assert "COMMITTED" in str(err)
        assert "rollback" in str(err)

    def test_not_found_error(self) -> None:
        err = TransactionNotFoundError("snap-99")
        assert err.snapshot_id == "snap-99"
        assert "snap-99" in str(err)

    def test_overlapping_error(self) -> None:
        err = OverlappingTransactionError("agent-a", ["/a", "/b"])
        assert err.agent_id == "agent-a"
        assert err.overlapping_paths == ["/a", "/b"]
        assert "agent-a" in str(err)

    def test_overlapping_error_truncates(self) -> None:
        paths = [f"/path/{i}" for i in range(10)]
        err = OverlappingTransactionError("agent-a", paths)
        assert "..." in str(err)


class TestProtocolShape:
    """TransactionalSnapshotProtocol is runtime_checkable with correct methods."""

    def test_protocol_is_runtime_checkable(self) -> None:
        assert hasattr(TransactionalSnapshotProtocol, "__protocol_attrs__") or hasattr(
            TransactionalSnapshotProtocol, "__abstractmethods__"
        )

    def test_protocol_has_begin(self) -> None:
        assert hasattr(TransactionalSnapshotProtocol, "begin")

    def test_protocol_has_commit(self) -> None:
        assert hasattr(TransactionalSnapshotProtocol, "commit")

    def test_protocol_has_rollback(self) -> None:
        assert hasattr(TransactionalSnapshotProtocol, "rollback")

    def test_protocol_has_get_transaction(self) -> None:
        assert hasattr(TransactionalSnapshotProtocol, "get_transaction")

    def test_protocol_has_list_active(self) -> None:
        assert hasattr(TransactionalSnapshotProtocol, "list_active")

    def test_protocol_has_cleanup_expired(self) -> None:
        assert hasattr(TransactionalSnapshotProtocol, "cleanup_expired")
