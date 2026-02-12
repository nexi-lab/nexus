"""Integration tests for audit log immutability (PostgreSQL only).

Issue #1360: Verifies that the PostgreSQL trigger prevents UPDATE
and DELETE on the exchange_audit_log table. These tests require
a PostgreSQL connection and are skipped if not available.

Run with:
    NEXUS_DATABASE_URL=postgresql://... uv run pytest tests/integration/test_audit_immutability.py -o "addopts=" -v
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from nexus.storage.exchange_audit_logger import ExchangeAuditLogger
from nexus.storage.models._base import Base

# Skip entire module if no PostgreSQL URL
PG_URL = os.environ.get("NEXUS_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not PG_URL.startswith(("postgres", "postgresql")),
    reason="PostgreSQL required for trigger tests",
)

TRIGGER_SQL = """
CREATE OR REPLACE FUNCTION prevent_audit_modification()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'Exchange audit log records are immutable: % not allowed',
        TG_OP;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'exchange_audit_log_no_update'
    ) THEN
        CREATE TRIGGER exchange_audit_log_no_update
            BEFORE UPDATE ON exchange_audit_log
            FOR EACH ROW EXECUTE FUNCTION prevent_audit_modification();
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'exchange_audit_log_no_delete'
    ) THEN
        CREATE TRIGGER exchange_audit_log_no_delete
            BEFORE DELETE ON exchange_audit_log
            FOR EACH ROW EXECUTE FUNCTION prevent_audit_modification();
    END IF;
END
$$;
"""


@pytest.fixture(scope="module")
def pg_engine():
    """Create PostgreSQL engine and set up schema + triggers."""
    engine = create_engine(PG_URL)
    Base.metadata.create_all(engine)
    with engine.connect() as conn:
        conn.execute(text(TRIGGER_SQL))
        conn.commit()
    yield engine
    # Clean up table after tests
    with engine.connect() as conn:
        conn.execute(text("DROP TABLE IF EXISTS exchange_audit_log CASCADE"))
        conn.execute(text("DROP TABLE IF EXISTS audit_checkpoint CASCADE"))
        conn.commit()
    engine.dispose()


@pytest.fixture
def pg_session_factory(pg_engine):
    return sessionmaker(bind=pg_engine)


@pytest.fixture
def pg_audit_logger(pg_session_factory):
    return ExchangeAuditLogger(session_factory=pg_session_factory)


def _create_record(logger: ExchangeAuditLogger) -> str:
    return logger.record(
        protocol="internal",
        buyer_agent_id="buyer-1",
        seller_agent_id="seller-1",
        amount=Decimal("10"),
        status="settled",
        application="gateway",
        zone_id="default",
        transfer_id=None,
    )


class TestPostgreSQLImmutability:
    def test_insert_succeeds(self, pg_audit_logger: ExchangeAuditLogger) -> None:
        record_id = _create_record(pg_audit_logger)
        assert record_id is not None

    def test_raw_update_rejected(
        self, pg_audit_logger: ExchangeAuditLogger, pg_session_factory
    ) -> None:
        record_id = _create_record(pg_audit_logger)

        session = pg_session_factory()
        with pytest.raises(Exception, match="immutable"):
            session.execute(
                text("UPDATE exchange_audit_log SET status = 'tampered' WHERE id = :id"),
                {"id": record_id},
            )
            session.commit()
        session.rollback()
        session.close()

    def test_raw_delete_rejected(
        self, pg_audit_logger: ExchangeAuditLogger, pg_session_factory
    ) -> None:
        record_id = _create_record(pg_audit_logger)

        session = pg_session_factory()
        with pytest.raises(Exception, match="immutable"):
            session.execute(
                text("DELETE FROM exchange_audit_log WHERE id = :id"),
                {"id": record_id},
            )
            session.commit()
        session.rollback()
        session.close()

    def test_trigger_error_message_correct(
        self, pg_audit_logger: ExchangeAuditLogger, pg_session_factory
    ) -> None:
        record_id = _create_record(pg_audit_logger)

        session = pg_session_factory()
        try:
            session.execute(
                text("UPDATE exchange_audit_log SET amount = 999 WHERE id = :id"),
                {"id": record_id},
            )
            session.commit()
            pytest.fail("Expected exception from trigger")
        except Exception as e:
            assert "immutable" in str(e).lower()
            assert "UPDATE" in str(e)
        finally:
            session.rollback()
            session.close()

    def test_verify_integrity_after_insert(self, pg_audit_logger: ExchangeAuditLogger) -> None:
        record_id = _create_record(pg_audit_logger)
        assert pg_audit_logger.verify_integrity(record_id) is True
