"""Unit tests for Merkle checkpoint background task.

Issue #1360: Tests checkpoint creation, Merkle root correctness,
threshold-based triggering, and idempotency.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from nexus.storage.exchange_audit_logger import ExchangeAuditLogger, _build_merkle_root
from nexus.storage.merkle_checkpoint import MerkleCheckpointTask
from nexus.storage.models._base import Base
from nexus.storage.models.audit_checkpoint import AuditCheckpointModel
from nexus.storage.models.exchange_audit_log import ExchangeAuditLogModel

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine)


@pytest.fixture
def audit_logger(session_factory):
    return ExchangeAuditLogger(session_factory=session_factory)


@pytest.fixture
def checkpoint_task(session_factory, audit_logger):
    return MerkleCheckpointTask(
        session_factory=session_factory,
        audit_logger=audit_logger,
        interval_seconds=60,
        threshold=3,  # Low threshold for testing
    )


def _seed_records(audit_logger: ExchangeAuditLogger, count: int) -> list[str]:
    ids = []
    for i in range(count):
        rid = audit_logger.record(
            protocol="internal",
            buyer_agent_id="buyer",
            seller_agent_id="seller",
            amount=Decimal("10"),
            status="settled",
            application="gateway",
            zone_id="default",
            transfer_id=f"merkle-seed-{i}",
        )
        ids.append(rid)
    return ids


# ---------------------------------------------------------------------------
# Checkpoint creation
# ---------------------------------------------------------------------------


class TestCheckpointCreation:
    def test_creates_checkpoint_when_threshold_met(
        self,
        checkpoint_task: MerkleCheckpointTask,
        audit_logger: ExchangeAuditLogger,
        session_factory,
    ) -> None:
        _seed_records(audit_logger, 5)  # Above threshold of 3

        checkpoint_id = checkpoint_task._maybe_checkpoint()
        assert checkpoint_id is not None

        session = session_factory()
        cp = session.execute(
            select(AuditCheckpointModel).where(AuditCheckpointModel.id == checkpoint_id)
        ).scalar_one()
        # Checkpoint covers exactly threshold records (capped per batch)
        assert cp.record_count == 3
        assert len(cp.merkle_root) == 64
        session.close()

    def test_no_checkpoint_below_threshold(
        self, checkpoint_task: MerkleCheckpointTask, audit_logger: ExchangeAuditLogger
    ) -> None:
        _seed_records(audit_logger, 2)  # Below threshold of 3
        assert checkpoint_task._maybe_checkpoint() is None

    def test_no_checkpoint_when_empty(self, checkpoint_task: MerkleCheckpointTask) -> None:
        assert checkpoint_task._maybe_checkpoint() is None


# ---------------------------------------------------------------------------
# Merkle root correctness
# ---------------------------------------------------------------------------


class TestMerkleRootCorrectness:
    def test_checkpoint_root_matches_manual_computation(
        self,
        checkpoint_task: MerkleCheckpointTask,
        audit_logger: ExchangeAuditLogger,
        session_factory,
    ) -> None:
        _seed_records(audit_logger, 4)

        # Compute expected root manually (only first 3 = threshold)
        session = session_factory()
        rows = list(
            session.execute(
                select(ExchangeAuditLogModel.record_hash)
                .order_by(ExchangeAuditLogModel.created_at, ExchangeAuditLogModel.id)
                .limit(3)  # Matches threshold cap
            )
        )
        hashes = [r[0] for r in rows]
        expected_root = _build_merkle_root(hashes)
        session.close()

        checkpoint_id = checkpoint_task._maybe_checkpoint()
        assert checkpoint_id is not None

        session = session_factory()
        cp = session.execute(
            select(AuditCheckpointModel).where(AuditCheckpointModel.id == checkpoint_id)
        ).scalar_one()
        assert cp.merkle_root == expected_root
        session.close()


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_no_duplicate_checkpoints(
        self,
        checkpoint_task: MerkleCheckpointTask,
        audit_logger: ExchangeAuditLogger,
        session_factory,
    ) -> None:
        _seed_records(audit_logger, 5)

        cp1 = checkpoint_task._maybe_checkpoint()
        assert cp1 is not None

        # Second call should not create another (no new records)
        cp2 = checkpoint_task._maybe_checkpoint()
        assert cp2 is None

        session = session_factory()
        count = session.execute(select(func.count()).select_from(AuditCheckpointModel)).scalar_one()
        assert count == 1
        session.close()

    def test_new_checkpoint_after_more_records(
        self,
        checkpoint_task: MerkleCheckpointTask,
        audit_logger: ExchangeAuditLogger,
        session_factory,
    ) -> None:
        _seed_records(audit_logger, 3)
        cp1 = checkpoint_task._maybe_checkpoint()
        assert cp1 is not None

        # Add more records
        _seed_records(audit_logger, 4)
        cp2 = checkpoint_task._maybe_checkpoint()
        assert cp2 is not None
        assert cp2 != cp1

        session = session_factory()
        count = session.execute(select(func.count()).select_from(AuditCheckpointModel)).scalar_one()
        assert count == 2
        session.close()
