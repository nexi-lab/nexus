"""Unit tests for AppendOnlyQueryMixin.

Issue #1360: Tests generic filter application, cursor-based pagination,
and count queries using a concrete model (ExchangeAuditLogModel).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.storage.models._base import Base
from nexus.storage.models.exchange_audit_log import ExchangeAuditLogModel
from nexus.storage.query_mixin import AppendOnlyQueryMixin

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
def mixin():
    return AppendOnlyQueryMixin(
        model_class=ExchangeAuditLogModel,
        id_column_name="id",
        created_column_name="created_at",
    )


def _insert_record(session, **overrides: Any) -> str:
    """Insert a record directly for testing the mixin in isolation."""
    from datetime import UTC, datetime

    from nexus.storage.exchange_audit_logger import compute_record_hash

    now = datetime.now(UTC)
    defaults: dict[str, Any] = {
        "protocol": "internal",
        "buyer_agent_id": "buyer-1",
        "seller_agent_id": "seller-1",
        "amount": Decimal("10"),
        "currency": "credits",
        "status": "settled",
        "application": "gateway",
        "zone_id": "default",
        "trace_id": None,
        "transfer_id": None,
        "created_at": now,
    }
    defaults.update(overrides)

    record_hash = compute_record_hash(
        protocol=defaults["protocol"],
        buyer_agent_id=defaults["buyer_agent_id"],
        seller_agent_id=defaults["seller_agent_id"],
        amount=defaults["amount"],
        currency=defaults["currency"],
        status=defaults["status"],
        application=defaults["application"],
        zone_id=defaults["zone_id"],
        trace_id=defaults["trace_id"],
        transfer_id=defaults["transfer_id"],
        created_at=defaults["created_at"],
    )

    row = ExchangeAuditLogModel(
        record_hash=record_hash,
        **defaults,
    )
    session.add(row)
    session.flush()
    record_id: str = row.id
    return record_id


# ---------------------------------------------------------------------------
# Filter application
# ---------------------------------------------------------------------------


class TestApplyFilters:
    def test_exact_match_filter(self, mixin: AppendOnlyQueryMixin, session) -> None:
        _insert_record(session, protocol="x402", transfer_id="f-1")
        _insert_record(session, protocol="internal", transfer_id="f-2")
        session.commit()

        rows, _ = mixin.list_cursor(session, filters={"protocol": "x402"}, limit=10)
        assert len(rows) == 1
        assert rows[0].protocol == "x402"

    def test_multiple_filters(self, mixin: AppendOnlyQueryMixin, session) -> None:
        _insert_record(session, protocol="x402", status="settled", transfer_id="m-1")
        _insert_record(session, protocol="x402", status="failed", transfer_id="m-2")
        _insert_record(session, protocol="internal", status="settled", transfer_id="m-3")
        session.commit()

        rows, _ = mixin.list_cursor(
            session, filters={"protocol": "x402", "status": "settled"}, limit=10
        )
        assert len(rows) == 1

    def test_none_values_skipped(self, mixin: AppendOnlyQueryMixin, session) -> None:
        _insert_record(session, transfer_id="n-1")
        session.commit()

        rows, _ = mixin.list_cursor(session, filters={"protocol": None, "status": None}, limit=10)
        assert len(rows) == 1  # No filtering applied

    def test_unknown_column_ignored(self, mixin: AppendOnlyQueryMixin, session) -> None:
        _insert_record(session, transfer_id="u-1")
        session.commit()

        rows, _ = mixin.list_cursor(session, filters={"nonexistent_col": "value"}, limit=10)
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# Cursor pagination
# ---------------------------------------------------------------------------


class TestCursorPagination:
    def test_paginate_forward(self, mixin: AppendOnlyQueryMixin, session) -> None:
        for i in range(5):
            _insert_record(session, transfer_id=f"p-{i}")
        session.commit()

        rows1, cursor = mixin.list_cursor(session, limit=3)
        assert len(rows1) == 3
        assert cursor is not None

        rows2, cursor2 = mixin.list_cursor(session, limit=3, cursor=cursor)
        assert len(rows2) == 2
        assert cursor2 is None

    def test_empty_table(self, mixin: AppendOnlyQueryMixin, session) -> None:
        rows, cursor = mixin.list_cursor(session, limit=10)
        assert rows == []
        assert cursor is None

    def test_exact_page_size(self, mixin: AppendOnlyQueryMixin, session) -> None:
        for i in range(3):
            _insert_record(session, transfer_id=f"e-{i}")
        session.commit()

        rows, cursor = mixin.list_cursor(session, limit=3)
        assert len(rows) == 3
        assert cursor is None  # Exactly fits, no more pages


# ---------------------------------------------------------------------------
# Count
# ---------------------------------------------------------------------------


class TestCount:
    def test_count_all(self, mixin: AppendOnlyQueryMixin, session) -> None:
        for i in range(4):
            _insert_record(session, transfer_id=f"c-{i}")
        session.commit()

        assert mixin.count(session) == 4

    def test_count_with_filters(self, mixin: AppendOnlyQueryMixin, session) -> None:
        for i in range(4):
            _insert_record(
                session,
                status="settled" if i < 2 else "failed",
                transfer_id=f"cf-{i}",
            )
        session.commit()

        assert mixin.count(session, filters={"status": "settled"}) == 2
        assert mixin.count(session, filters={"status": "failed"}) == 2

    def test_count_empty(self, mixin: AppendOnlyQueryMixin, session) -> None:
        assert mixin.count(session) == 0
