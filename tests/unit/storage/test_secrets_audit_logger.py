"""Tests for SecretsAuditLogger (Issue #997).

Mirrors the ExchangeAuditLogger test suite pattern.
Covers: CRUD, hash integrity, immutability, pagination, filters, verify integrity.
"""

from __future__ import annotations

import gc
import tempfile
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.storage.models._base import Base
from nexus.storage.secrets_audit_logger import (
    SecretsAuditLogger,
    compute_metadata_hash,
    compute_record_hash,
)


@pytest.fixture
def session_factory():
    """In-memory SQLite session factory for testing."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    yield factory
    engine.dispose()
    gc.collect()


@pytest.fixture
def audit_logger(session_factory):
    return SecretsAuditLogger(session_factory=session_factory)


class TestSecretsAuditLoggerWrite:
    """Test audit event creation."""

    def test_log_event_creates_record(self, audit_logger):
        record_id = audit_logger.log_event(
            event_type="credential_created",
            actor_id="alice@example.com",
            provider="google",
            credential_id="cred-123",
            zone_id="default",
        )
        assert record_id is not None

        row = audit_logger.get_event(record_id)
        assert row is not None
        assert row.event_type == "credential_created"
        assert row.actor_id == "alice@example.com"
        assert row.provider == "google"

    def test_log_event_with_details(self, audit_logger):
        record_id = audit_logger.log_event(
            event_type="token_rotated",
            actor_id="alice@example.com",
            provider="google",
            details={"rotation_counter": 5},
        )
        row = audit_logger.get_event(record_id)
        assert row.details is not None
        assert '"rotation_counter": 5' in row.details
        assert row.metadata_hash is not None

    def test_log_event_with_all_fields(self, audit_logger):
        record_id = audit_logger.log_event(
            event_type="family_invalidated",
            actor_id="system",
            provider="google",
            credential_id="cred-456",
            token_family_id="family-789",
            zone_id="org_acme",
            ip_address="192.168.1.1",
            details={"reason": "reuse_detected"},
        )
        row = audit_logger.get_event(record_id)
        assert row.event_type == "family_invalidated"
        assert row.token_family_id == "family-789"
        assert row.ip_address == "192.168.1.1"
        assert row.zone_id == "org_acme"


class TestSecretsAuditLoggerIntegrity:
    """Test hash integrity and tamper detection."""

    def test_record_hash_is_valid(self, audit_logger):
        record_id = audit_logger.log_event(
            event_type="credential_created",
            actor_id="alice@example.com",
            provider="google",
            zone_id="default",
        )
        assert audit_logger.verify_integrity(record_id) is True

    def test_verify_nonexistent_record(self, audit_logger):
        assert audit_logger.verify_integrity("nonexistent-id") is False

    def test_compute_record_hash_deterministic(self):
        now = datetime.now(UTC)
        h1 = compute_record_hash(
            event_type="test", actor_id="alice", provider="google",
            credential_id=None, token_family_id=None,
            zone_id="default", ip_address=None, created_at=now,
        )
        h2 = compute_record_hash(
            event_type="test", actor_id="alice", provider="google",
            credential_id=None, token_family_id=None,
            zone_id="default", ip_address=None, created_at=now,
        )
        assert h1 == h2

    def test_compute_metadata_hash_none(self):
        assert compute_metadata_hash(None) is None
        assert compute_metadata_hash({}) is None

    def test_compute_metadata_hash_deterministic(self):
        h1 = compute_metadata_hash({"a": 1, "b": 2})
        h2 = compute_metadata_hash({"b": 2, "a": 1})  # Different order
        assert h1 == h2


class TestSecretsAuditLoggerImmutability:
    """Test that audit records cannot be modified or deleted."""

    def test_update_raises(self, audit_logger, session_factory):
        record_id = audit_logger.log_event(
            event_type="credential_created",
            actor_id="alice@example.com",
            zone_id="default",
        )

        session = session_factory()
        try:
            from nexus.storage.models.secrets_audit_log import SecretsAuditLogModel
            from sqlalchemy import select

            row = session.execute(
                select(SecretsAuditLogModel).where(SecretsAuditLogModel.id == record_id)
            ).scalar_one()

            row.actor_id = "tampered"
            with pytest.raises(RuntimeError, match="immutable"):
                session.flush()
        finally:
            session.rollback()
            session.close()

    def test_delete_raises(self, audit_logger, session_factory):
        record_id = audit_logger.log_event(
            event_type="credential_created",
            actor_id="alice@example.com",
            zone_id="default",
        )

        session = session_factory()
        try:
            from nexus.storage.models.secrets_audit_log import SecretsAuditLogModel
            from sqlalchemy import select

            row = session.execute(
                select(SecretsAuditLogModel).where(SecretsAuditLogModel.id == record_id)
            ).scalar_one()

            session.delete(row)
            with pytest.raises(RuntimeError, match="immutable"):
                session.flush()
        finally:
            session.rollback()
            session.close()


class TestSecretsAuditLoggerQuery:
    """Test query and pagination."""

    def test_list_events_returns_all(self, audit_logger):
        for i in range(5):
            audit_logger.log_event(
                event_type="credential_created",
                actor_id=f"user{i}@example.com",
                zone_id="default",
            )

        rows, cursor = audit_logger.list_events_cursor(limit=10)
        assert len(rows) == 5
        assert cursor is None  # No more pages

    def test_list_events_pagination(self, audit_logger):
        for i in range(5):
            audit_logger.log_event(
                event_type="credential_created",
                actor_id=f"user{i}@example.com",
                zone_id="default",
            )

        rows1, cursor1 = audit_logger.list_events_cursor(limit=3)
        assert len(rows1) == 3
        assert cursor1 is not None

        rows2, cursor2 = audit_logger.list_events_cursor(limit=3, cursor=cursor1)
        assert len(rows2) == 2
        assert cursor2 is None

    def test_list_events_with_filters(self, audit_logger):
        audit_logger.log_event(
            event_type="credential_created", actor_id="alice", zone_id="zone_a"
        )
        audit_logger.log_event(
            event_type="token_rotated", actor_id="bob", zone_id="zone_b"
        )

        rows, _ = audit_logger.list_events_cursor(
            filters={"zone_id": "zone_a"}
        )
        assert len(rows) == 1
        assert rows[0].actor_id == "alice"

    def test_count_events(self, audit_logger):
        for _ in range(3):
            audit_logger.log_event(
                event_type="credential_created",
                actor_id="alice",
                zone_id="default",
            )
        assert audit_logger.count_events(zone_id="default") == 3

    def test_iter_events(self, audit_logger):
        for i in range(3):
            audit_logger.log_event(
                event_type="credential_created",
                actor_id=f"user{i}",
                zone_id="default",
            )
        rows = audit_logger.iter_events(filters={"zone_id": "default"})
        assert len(rows) == 3
