"""Tests for TransactionSnapshotModel (Issue #1752).

Validates model fields, indexes, and validation logic.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from nexus.storage.models._base import Base
from nexus.storage.models.transactional_snapshot import TransactionSnapshotModel


@pytest.fixture()
def engine():
    """In-memory SQLite engine with all tables created."""
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture()
def session(engine):
    """SQLAlchemy session for tests."""
    maker = sessionmaker(bind=engine)
    with maker() as sess:
        yield sess


def _make_snapshot(
    *,
    agent_id: str = "agent-a",
    zone_id: str = "root",
    status: str = "ACTIVE",
    paths: list[str] | None = None,
    snapshot_data: dict | None = None,
) -> TransactionSnapshotModel:
    """Create a TransactionSnapshotModel with sensible defaults."""
    if paths is None:
        paths = ["/data/file.txt"]
    if snapshot_data is None:
        snapshot_data = {
            p: {"content_hash": f"hash_{i}", "size": 100, "metadata": None, "existed": True}
            for i, p in enumerate(paths)
        }
    now = datetime.now(UTC)
    return TransactionSnapshotModel(
        agent_id=agent_id,
        zone_id=zone_id,
        status=status,
        paths_json=json.dumps(paths),
        snapshot_data_json=json.dumps(snapshot_data),
        path_count=len(paths),
        created_at=now,
        expires_at=now + timedelta(hours=1),
    )


class TestModelCreation:
    """TransactionSnapshotModel can be created and persisted."""

    def test_create_active_snapshot(self, session: Session) -> None:
        snap = _make_snapshot()
        session.add(snap)
        session.commit()

        result = session.get(TransactionSnapshotModel, snap.snapshot_id)
        assert result is not None
        assert result.agent_id == "agent-a"
        assert result.zone_id == "root"
        assert result.status == "ACTIVE"
        assert result.path_count == 1

    def test_auto_generated_uuid(self, session: Session) -> None:
        snap = _make_snapshot()
        session.add(snap)
        session.commit()
        assert snap.snapshot_id is not None
        assert len(snap.snapshot_id) == 36  # UUID format

    def test_paths_json_roundtrip(self, session: Session) -> None:
        paths = ["/data/a.txt", "/data/b.txt", "/data/c.txt"]
        snap = _make_snapshot(paths=paths)
        session.add(snap)
        session.commit()
        session.refresh(snap)

        loaded_paths = json.loads(snap.paths_json)
        assert loaded_paths == paths

    def test_snapshot_data_json_roundtrip(self, session: Session) -> None:
        data = {
            "/data/file.txt": {
                "content_hash": "abc123",
                "size": 1024,
                "metadata": '{"key": "value"}',
                "existed": True,
            }
        }
        snap = _make_snapshot(snapshot_data=data)
        session.add(snap)
        session.commit()
        session.refresh(snap)

        loaded = json.loads(snap.snapshot_data_json)
        assert loaded["/data/file.txt"]["content_hash"] == "abc123"

    def test_timestamps(self, session: Session) -> None:
        snap = _make_snapshot()
        session.add(snap)
        session.commit()
        session.refresh(snap)

        assert snap.created_at is not None
        assert snap.expires_at is not None
        assert snap.expires_at > snap.created_at
        assert snap.committed_at is None
        assert snap.rolled_back_at is None

    def test_committed_snapshot(self, session: Session) -> None:
        snap = _make_snapshot(status="COMMITTED")
        snap.committed_at = datetime.now(UTC)
        session.add(snap)
        session.commit()
        session.refresh(snap)

        assert snap.status == "COMMITTED"
        assert snap.committed_at is not None

    def test_rolled_back_snapshot(self, session: Session) -> None:
        snap = _make_snapshot(status="ROLLED_BACK")
        snap.rolled_back_at = datetime.now(UTC)
        session.add(snap)
        session.commit()
        session.refresh(snap)

        assert snap.status == "ROLLED_BACK"
        assert snap.rolled_back_at is not None


class TestModelValidation:
    """TransactionSnapshotModel.validate() catches invalid data."""

    def test_valid_snapshot(self) -> None:
        snap = _make_snapshot()
        snap.validate()  # Should not raise

    def test_invalid_status(self) -> None:
        snap = _make_snapshot(status="INVALID")
        with pytest.raises(Exception, match="status must be one of"):
            snap.validate()

    def test_empty_agent_id(self) -> None:
        snap = _make_snapshot(agent_id="")
        with pytest.raises(Exception, match="agent_id is required"):
            snap.validate()

    def test_empty_zone_id(self) -> None:
        snap = _make_snapshot(zone_id="")
        with pytest.raises(Exception, match="zone_id is required"):
            snap.validate()

    def test_negative_path_count(self) -> None:
        snap = _make_snapshot()
        snap.path_count = -1
        with pytest.raises(Exception, match="path_count cannot be negative"):
            snap.validate()


class TestModelRepr:
    """__repr__ is readable."""

    def test_repr(self) -> None:
        snap = _make_snapshot()
        r = repr(snap)
        assert "TransactionSnapshotModel" in r
        assert "agent_id=agent-a" in r
        assert "status=ACTIVE" in r


class TestTableStructure:
    """Table and index presence in the database schema."""

    def test_table_exists(self, engine) -> None:
        inspector = inspect(engine)
        tables = inspector.get_table_names()
        assert "transaction_snapshots" in tables

    def test_columns_present(self, engine) -> None:
        inspector = inspect(engine)
        columns = {c["name"] for c in inspector.get_columns("transaction_snapshots")}
        expected = {
            "snapshot_id",
            "agent_id",
            "zone_id",
            "status",
            "paths_json",
            "snapshot_data_json",
            "path_count",
            "created_at",
            "expires_at",
            "committed_at",
            "rolled_back_at",
        }
        assert expected.issubset(columns)

    def test_indexes_present(self, engine) -> None:
        inspector = inspect(engine)
        indexes = inspector.get_indexes("transaction_snapshots")
        index_names = {idx["name"] for idx in indexes}

        assert "idx_txn_snapshot_agent_status" in index_names
        assert "idx_txn_snapshot_zone_agent" in index_names
        # Note: partial index (idx_txn_snapshot_active_expiry) is PostgreSQL-only
        # SQLite creates it as a regular index


class TestQueryPatterns:
    """Verify the query patterns TransactionalSnapshotService will use."""

    def test_find_active_by_agent(self, session: Session) -> None:
        """Most common query: find ACTIVE transactions for an agent."""
        session.add(_make_snapshot(agent_id="agent-a", status="ACTIVE"))
        session.add(_make_snapshot(agent_id="agent-a", status="COMMITTED"))
        session.add(_make_snapshot(agent_id="agent-b", status="ACTIVE"))
        session.commit()

        results = (
            session.query(TransactionSnapshotModel)
            .filter_by(agent_id="agent-a", status="ACTIVE")
            .all()
        )
        assert len(results) == 1

    def test_find_expired(self, session: Session) -> None:
        """TTL cleanup query: find ACTIVE transactions past expiry."""
        past = datetime.now(UTC) - timedelta(hours=2)
        snap = _make_snapshot()
        snap.expires_at = past
        session.add(snap)
        session.commit()

        now = datetime.now(UTC)
        results = (
            session.query(TransactionSnapshotModel)
            .filter(
                TransactionSnapshotModel.status == "ACTIVE",
                TransactionSnapshotModel.expires_at < now,
            )
            .all()
        )
        assert len(results) == 1

    def test_find_by_zone_and_agent(self, session: Session) -> None:
        """Zone-scoped query."""
        session.add(_make_snapshot(agent_id="agent-a", zone_id="acme"))
        session.add(_make_snapshot(agent_id="agent-a", zone_id="other"))
        session.commit()

        results = (
            session.query(TransactionSnapshotModel)
            .filter_by(agent_id="agent-a", zone_id="acme")
            .all()
        )
        assert len(results) == 1
