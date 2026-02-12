"""Unit tests for ExchangeAuditLogger — immutable transaction audit trail.

Issue #1360: Tests record creation, hash computation, Merkle trees,
tamper detection, cursor pagination, filtering, aggregations, and
ORM immutability guards.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.orm import Session, sessionmaker

from nexus.storage.exchange_audit_logger import (
    ExchangeAuditLogger,
    _build_merkle_root,
    compute_metadata_hash,
    compute_record_hash,
)
from nexus.storage.models._base import Base
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
def session(session_factory):
    s = session_factory()
    yield s
    s.close()


@pytest.fixture
def audit_logger(session_factory):
    return ExchangeAuditLogger(session_factory=session_factory)


def _make_record_kwargs(**overrides: Any) -> dict[str, Any]:
    """Build a default set of kwargs for audit_logger.record()."""
    defaults: dict[str, Any] = {
        "protocol": "internal",
        "buyer_agent_id": "buyer-1",
        "seller_agent_id": "seller-1",
        "amount": Decimal("10.500000"),
        "currency": "credits",
        "status": "settled",
        "application": "gateway",
        "zone_id": "default",
        "trace_id": None,
        "metadata": None,
        "transfer_id": "tx-001",
    }
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# Record creation
# ---------------------------------------------------------------------------


class TestRecordCreation:
    def test_creates_record_with_all_fields(
        self, audit_logger: ExchangeAuditLogger, session: Session
    ) -> None:
        record_id = audit_logger.record(**_make_record_kwargs())

        from sqlalchemy import select

        row = session.execute(
            select(ExchangeAuditLogModel).where(ExchangeAuditLogModel.id == record_id)
        ).scalar_one()

        assert row.protocol == "internal"
        assert row.buyer_agent_id == "buyer-1"
        assert row.seller_agent_id == "seller-1"
        assert Decimal(str(row.amount)) == Decimal("10.500000")
        assert row.currency == "credits"
        assert row.status == "settled"
        assert row.application == "gateway"
        assert row.zone_id == "default"
        assert row.transfer_id == "tx-001"

    def test_record_returns_uuid_string(self, audit_logger: ExchangeAuditLogger) -> None:
        record_id = audit_logger.record(**_make_record_kwargs())
        assert isinstance(record_id, str)
        assert len(record_id) == 36  # UUID format

    def test_record_hash_is_sha256_hex(
        self, audit_logger: ExchangeAuditLogger, session: Session
    ) -> None:
        record_id = audit_logger.record(**_make_record_kwargs())
        from sqlalchemy import select

        row = session.execute(
            select(ExchangeAuditLogModel).where(ExchangeAuditLogModel.id == record_id)
        ).scalar_one()
        assert len(row.record_hash) == 64  # SHA-256 hex digest

    def test_metadata_hash_computed(
        self, audit_logger: ExchangeAuditLogger, session: Session
    ) -> None:
        record_id = audit_logger.record(
            **_make_record_kwargs(metadata={"key": "value", "nested": {"a": 1}})
        )
        from sqlalchemy import select

        row = session.execute(
            select(ExchangeAuditLogModel).where(ExchangeAuditLogModel.id == record_id)
        ).scalar_one()
        assert row.metadata_hash is not None
        assert len(row.metadata_hash) == 64

    def test_metadata_hash_none_when_no_metadata(
        self, audit_logger: ExchangeAuditLogger, session: Session
    ) -> None:
        record_id = audit_logger.record(**_make_record_kwargs(metadata=None))
        from sqlalchemy import select

        row = session.execute(
            select(ExchangeAuditLogModel).where(ExchangeAuditLogModel.id == record_id)
        ).scalar_one()
        assert row.metadata_hash is None

    def test_multiple_records_have_unique_ids(self, audit_logger: ExchangeAuditLogger) -> None:
        ids = {audit_logger.record(**_make_record_kwargs(transfer_id=f"tx-{i}")) for i in range(5)}
        assert len(ids) == 5


# ---------------------------------------------------------------------------
# Hash computation (golden values)
# ---------------------------------------------------------------------------


class TestHashComputation:
    def test_record_hash_golden_value(self) -> None:
        """Verify hash is stable and reproducible."""
        ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)
        h = compute_record_hash(
            protocol="internal",
            buyer_agent_id="buyer-1",
            seller_agent_id="seller-1",
            amount=Decimal("10.500000"),
            currency="credits",
            status="settled",
            application="gateway",
            zone_id="default",
            trace_id=None,
            transfer_id="tx-001",
            created_at=ts,
        )
        # Amount is normalized to 6dp: "10.500000"
        canonical = (
            "internal|buyer-1|seller-1|10.500000|credits|settled|gateway|default||tx-001|"
            + ts.isoformat()
        )
        expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        assert h == expected

    def test_record_hash_normalizes_amount(self) -> None:
        """Decimal('10') and Decimal('10.000000') produce the same hash."""
        ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)
        kwargs: dict[str, Any] = {
            "protocol": "internal",
            "buyer_agent_id": "buyer-1",
            "seller_agent_id": "seller-1",
            "currency": "credits",
            "status": "settled",
            "application": "gateway",
            "zone_id": "default",
            "trace_id": None,
            "transfer_id": "tx-001",
            "created_at": ts,
        }
        h1 = compute_record_hash(amount=Decimal("10"), **kwargs)
        h2 = compute_record_hash(amount=Decimal("10.000000"), **kwargs)
        assert h1 == h2

    def test_record_hash_changes_with_any_field(self) -> None:
        """Hash must change if any single field changes."""
        ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)
        base_kwargs: dict[str, Any] = {
            "protocol": "internal",
            "buyer_agent_id": "buyer-1",
            "seller_agent_id": "seller-1",
            "amount": Decimal("10.500000"),
            "currency": "credits",
            "status": "settled",
            "application": "gateway",
            "zone_id": "default",
            "trace_id": None,
            "transfer_id": "tx-001",
            "created_at": ts,
        }
        base_hash = compute_record_hash(**base_kwargs)

        variations = [
            {"protocol": "x402"},
            {"buyer_agent_id": "buyer-2"},
            {"seller_agent_id": "seller-2"},
            {"amount": Decimal("99")},
            {"currency": "USD"},
            {"status": "failed"},
            {"application": "ads"},
            {"zone_id": "other-zone"},
            {"trace_id": "some-trace"},
            {"transfer_id": "tx-999"},
        ]
        for var in variations:
            h = compute_record_hash(**{**base_kwargs, **var})
            assert h != base_hash, f"Hash unchanged for {var}"

    def test_metadata_hash_golden_value(self) -> None:
        meta = {"b": 2, "a": 1}  # Deliberately unordered
        h = compute_metadata_hash(meta)
        expected = hashlib.sha256(b'{"a": 1, "b": 2}').hexdigest()
        assert h == expected

    def test_metadata_hash_none_for_empty(self) -> None:
        assert compute_metadata_hash(None) is None
        assert compute_metadata_hash({}) is None


# ---------------------------------------------------------------------------
# Merkle tree (golden values)
# ---------------------------------------------------------------------------


class TestMerkleTree:
    def test_empty_list(self) -> None:
        assert _build_merkle_root([]) == hashlib.sha256(b"").hexdigest()

    def test_single_leaf(self) -> None:
        h = "abc123"
        assert _build_merkle_root([h]) == h

    def test_two_leaves_golden(self) -> None:
        a, b = "aaa", "bbb"
        expected = hashlib.sha256((a + b).encode("utf-8")).hexdigest()
        assert _build_merkle_root([a, b]) == expected

    def test_odd_leaves_duplicates_last(self) -> None:
        a, b, c = "aaa", "bbb", "ccc"
        ab = hashlib.sha256((a + b).encode("utf-8")).hexdigest()
        cc = hashlib.sha256((c + c).encode("utf-8")).hexdigest()
        root = hashlib.sha256((ab + cc).encode("utf-8")).hexdigest()
        assert _build_merkle_root([a, b, c]) == root

    def test_merkle_root_via_logger(self, audit_logger: ExchangeAuditLogger) -> None:
        ids = []
        for i in range(4):
            rid = audit_logger.record(**_make_record_kwargs(transfer_id=f"merkle-tx-{i}"))
            ids.append(rid)

        root = audit_logger.compute_merkle_root(ids[0], ids[-1])
        assert len(root) == 64  # SHA-256 hex


# ---------------------------------------------------------------------------
# Tamper detection
# ---------------------------------------------------------------------------


class TestTamperDetection:
    def test_verify_integrity_valid(self, audit_logger: ExchangeAuditLogger) -> None:
        record_id = audit_logger.record(**_make_record_kwargs())
        assert audit_logger.verify_integrity(record_id) is True

    def test_verify_integrity_after_tamper(
        self, audit_logger: ExchangeAuditLogger, session_factory
    ) -> None:
        record_id = audit_logger.record(**_make_record_kwargs())

        # Tamper directly via SQL (bypass ORM guard)
        session = session_factory()
        session.execute(
            update(ExchangeAuditLogModel)
            .where(ExchangeAuditLogModel.id == record_id)
            .values(amount=Decimal("999"))
        )
        session.commit()
        session.close()

        assert audit_logger.verify_integrity(record_id) is False

    def test_verify_integrity_missing_record(self, audit_logger: ExchangeAuditLogger) -> None:
        assert audit_logger.verify_integrity("nonexistent-id") is False


# ---------------------------------------------------------------------------
# Cursor-based pagination
# ---------------------------------------------------------------------------


class TestCursorPagination:
    def test_first_page(self, audit_logger: ExchangeAuditLogger) -> None:
        for i in range(5):
            audit_logger.record(**_make_record_kwargs(transfer_id=f"page-tx-{i}"))

        rows, cursor = audit_logger.list_transactions_cursor(limit=3)
        assert len(rows) == 3
        assert cursor is not None

    def test_next_page(self, audit_logger: ExchangeAuditLogger) -> None:
        for i in range(5):
            audit_logger.record(**_make_record_kwargs(transfer_id=f"next-tx-{i}"))

        rows1, cursor1 = audit_logger.list_transactions_cursor(limit=3)
        rows2, cursor2 = audit_logger.list_transactions_cursor(limit=3, cursor=cursor1)

        assert len(rows2) == 2
        assert cursor2 is None  # No more pages
        all_ids = [r.id for r in rows1] + [r.id for r in rows2]
        assert len(set(all_ids)) == 5  # No duplicates

    def test_empty_result(self, audit_logger: ExchangeAuditLogger) -> None:
        rows, cursor = audit_logger.list_transactions_cursor(limit=10)
        assert rows == []
        assert cursor is None

    def test_last_page_no_cursor(self, audit_logger: ExchangeAuditLogger) -> None:
        for i in range(3):
            audit_logger.record(**_make_record_kwargs(transfer_id=f"last-tx-{i}"))

        rows, cursor = audit_logger.list_transactions_cursor(limit=10)
        assert len(rows) == 3
        assert cursor is None


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


class TestFiltering:
    def test_filter_by_protocol(self, audit_logger: ExchangeAuditLogger) -> None:
        audit_logger.record(**_make_record_kwargs(protocol="x402", transfer_id="f-1"))
        audit_logger.record(**_make_record_kwargs(protocol="internal", transfer_id="f-2"))

        rows, _ = audit_logger.list_transactions_cursor(filters={"protocol": "x402"}, limit=10)
        assert len(rows) == 1
        assert rows[0].protocol == "x402"

    def test_filter_by_status(self, audit_logger: ExchangeAuditLogger) -> None:
        audit_logger.record(**_make_record_kwargs(status="settled", transfer_id="s-1"))
        audit_logger.record(**_make_record_kwargs(status="failed", transfer_id="s-2"))
        audit_logger.record(**_make_record_kwargs(status="settled", transfer_id="s-3"))

        rows, _ = audit_logger.list_transactions_cursor(filters={"status": "settled"}, limit=10)
        assert len(rows) == 2
        assert all(r.status == "settled" for r in rows)

    def test_filter_by_buyer(self, audit_logger: ExchangeAuditLogger) -> None:
        audit_logger.record(**_make_record_kwargs(buyer_agent_id="alice", transfer_id="b-1"))
        audit_logger.record(**_make_record_kwargs(buyer_agent_id="bob", transfer_id="b-2"))

        rows, _ = audit_logger.list_transactions_cursor(
            filters={"buyer_agent_id": "alice"}, limit=10
        )
        assert len(rows) == 1
        assert rows[0].buyer_agent_id == "alice"

    def test_filter_by_zone(self, audit_logger: ExchangeAuditLogger) -> None:
        audit_logger.record(**_make_record_kwargs(zone_id="zone-a", transfer_id="z-1"))
        audit_logger.record(**_make_record_kwargs(zone_id="zone-b", transfer_id="z-2"))

        rows, _ = audit_logger.list_transactions_cursor(filters={"zone_id": "zone-a"}, limit=10)
        assert len(rows) == 1

    def test_combined_filters(self, audit_logger: ExchangeAuditLogger) -> None:
        audit_logger.record(
            **_make_record_kwargs(protocol="x402", status="settled", transfer_id="c-1")
        )
        audit_logger.record(
            **_make_record_kwargs(protocol="x402", status="failed", transfer_id="c-2")
        )
        audit_logger.record(
            **_make_record_kwargs(protocol="internal", status="settled", transfer_id="c-3")
        )

        rows, _ = audit_logger.list_transactions_cursor(
            filters={"protocol": "x402", "status": "settled"}, limit=10
        )
        assert len(rows) == 1

    def test_count_with_filters(self, audit_logger: ExchangeAuditLogger) -> None:
        for i in range(5):
            audit_logger.record(
                **_make_record_kwargs(
                    status="settled" if i % 2 == 0 else "failed",
                    transfer_id=f"cnt-{i}",
                )
            )
        assert audit_logger.count_transactions(status="settled") == 3
        assert audit_logger.count_transactions(status="failed") == 2


# ---------------------------------------------------------------------------
# Aggregations
# ---------------------------------------------------------------------------


class TestAggregations:
    def test_basic_aggregation(self, audit_logger: ExchangeAuditLogger) -> None:
        for i in range(3):
            audit_logger.record(
                **_make_record_kwargs(
                    amount=Decimal("10"),
                    transfer_id=f"agg-{i}",
                )
            )

        agg = audit_logger.get_aggregations(zone_id="default")
        assert int(float(agg["total_volume"])) == 30
        assert agg["tx_count"] == 3

    def test_top_counterparties(self, audit_logger: ExchangeAuditLogger) -> None:
        audit_logger.record(
            **_make_record_kwargs(
                buyer_agent_id="alice", amount=Decimal("100"), transfer_id="top-1"
            )
        )
        audit_logger.record(
            **_make_record_kwargs(buyer_agent_id="alice", amount=Decimal("50"), transfer_id="top-2")
        )
        audit_logger.record(
            **_make_record_kwargs(buyer_agent_id="bob", amount=Decimal("10"), transfer_id="top-3")
        )

        agg = audit_logger.get_aggregations(zone_id="default")
        assert len(agg["top_buyers"]) >= 2
        # Alice should be first (highest volume)
        assert agg["top_buyers"][0]["agent_id"] == "alice"

    def test_aggregation_empty(self, audit_logger: ExchangeAuditLogger) -> None:
        agg = audit_logger.get_aggregations(zone_id="nonexistent")
        assert agg["tx_count"] == 0
        assert float(agg["total_volume"]) == 0


# ---------------------------------------------------------------------------
# ORM immutability guard
# ---------------------------------------------------------------------------


class TestImmutabilityGuard:
    def test_update_rejected(self, audit_logger: ExchangeAuditLogger, session_factory) -> None:
        record_id = audit_logger.record(**_make_record_kwargs())

        session = session_factory()
        from sqlalchemy import select

        row = session.execute(
            select(ExchangeAuditLogModel).where(ExchangeAuditLogModel.id == record_id)
        ).scalar_one()

        # Attempt to modify via ORM
        row.status = "tampered"
        with pytest.raises(RuntimeError, match="immutable.*UPDATE"):
            session.flush()
        session.close()

    def test_delete_rejected(self, audit_logger: ExchangeAuditLogger, session_factory) -> None:
        record_id = audit_logger.record(**_make_record_kwargs())

        session = session_factory()
        from sqlalchemy import select

        row = session.execute(
            select(ExchangeAuditLogModel).where(ExchangeAuditLogModel.id == record_id)
        ).scalar_one()

        session.delete(row)
        with pytest.raises(RuntimeError, match="immutable.*DELETE"):
            session.flush()
        session.close()


# ---------------------------------------------------------------------------
# Get transaction
# ---------------------------------------------------------------------------


class TestGetTransaction:
    def test_get_existing(self, audit_logger: ExchangeAuditLogger) -> None:
        record_id = audit_logger.record(**_make_record_kwargs())
        row = audit_logger.get_transaction(record_id)
        assert row is not None
        assert row.id == record_id

    def test_get_nonexistent(self, audit_logger: ExchangeAuditLogger) -> None:
        assert audit_logger.get_transaction("no-such-id") is None
