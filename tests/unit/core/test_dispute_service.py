"""Unit tests for DisputeService (Issue #1356).

Exhaustive state machine matrix (4 states × 4 transitions = 16 combinations):
- Valid: filed→auto_mediating, filed→dismissed,
         auto_mediating→resolved, auto_mediating→dismissed
- Invalid: all other 12 combinations → InvalidTransitionError

Plus:
- File dispute: creates record, validates self-dispute
- Resolve: updates status, sets resolution + timestamps
- Dismiss: updates status, sets reason
- Get + list queries
- Duplicate dispute detection
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from nexus.services.reputation.dispute_service import (
    DisputeNotFoundError,
    DisputeService,
    DuplicateDisputeError,
    InvalidTransitionError,
)
from nexus.storage.models import Base

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    """In-memory SQLite database."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def session_factory(engine):
    """Session factory for tests."""
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def dispute_service(session_factory):
    """DisputeService instance for testing."""
    return DisputeService(session_factory=session_factory)


def _file_dispute(dispute_service, exchange_id="exchange-1"):
    """Helper: file a dispute and return the record."""
    return dispute_service.file_dispute(
        exchange_id=exchange_id,
        complainant_agent_id="agent-a",
        respondent_agent_id="agent-b",
        zone_id="default",
        reason="Test dispute",
    )


# ---------------------------------------------------------------------------
# 1. File dispute
# ---------------------------------------------------------------------------


class TestFileDispute:
    """Test dispute filing."""

    def test_file_dispute_success(self, dispute_service):
        """Filing a dispute creates a record with 'filed' status."""
        record = _file_dispute(dispute_service)

        assert record.exchange_id == "exchange-1"
        assert record.complainant_agent_id == "agent-a"
        assert record.respondent_agent_id == "agent-b"
        assert record.status == "filed"
        assert record.tier == 1
        assert record.reason == "Test dispute"
        assert record.resolution is None
        assert record.resolved_at is None

    def test_file_duplicate_dispute_rejected(self, dispute_service):
        """Duplicate dispute for same exchange raises DuplicateDisputeError."""
        _file_dispute(dispute_service, exchange_id="exchange-dup")

        with pytest.raises(DuplicateDisputeError):
            _file_dispute(dispute_service, exchange_id="exchange-dup")

    def test_file_self_dispute_rejected(self, dispute_service):
        """Self-dispute raises ValueError."""
        with pytest.raises(ValueError, match="Cannot file dispute against yourself"):
            dispute_service.file_dispute(
                exchange_id="exchange-self",
                complainant_agent_id="agent-a",
                respondent_agent_id="agent-a",
                zone_id="default",
                reason="Self dispute",
            )


# ---------------------------------------------------------------------------
# 2. State machine — valid transitions
# ---------------------------------------------------------------------------


class TestValidTransitions:
    """Test all 4 valid state transitions."""

    def test_filed_to_auto_mediating(self, dispute_service):
        """filed → auto_mediating is valid."""
        dispute = _file_dispute(dispute_service, "exchange-t1")
        result = dispute_service.auto_mediate(dispute.id)
        assert result.status == "auto_mediating"

    def test_filed_to_dismissed(self, dispute_service):
        """filed → dismissed is valid."""
        dispute = _file_dispute(dispute_service, "exchange-t2")
        result = dispute_service.dismiss(dispute.id, "No merit")
        assert result.status == "dismissed"
        assert result.resolution == "No merit"
        assert result.resolved_at is not None

    def test_auto_mediating_to_resolved(self, dispute_service):
        """auto_mediating → resolved is valid."""
        dispute = _file_dispute(dispute_service, "exchange-t3")
        dispute_service.auto_mediate(dispute.id)
        result = dispute_service.resolve(
            dispute.id, "Complainant was right", evidence_hash="abc123"
        )
        assert result.status == "resolved"
        assert result.resolution == "Complainant was right"
        assert result.resolution_evidence_hash == "abc123"
        assert result.resolved_at is not None
        assert result.appeal_deadline is not None

    def test_auto_mediating_to_dismissed(self, dispute_service):
        """auto_mediating → dismissed is valid."""
        dispute = _file_dispute(dispute_service, "exchange-t4")
        dispute_service.auto_mediate(dispute.id)
        result = dispute_service.dismiss(dispute.id, "Insufficient evidence")
        assert result.status == "dismissed"


# ---------------------------------------------------------------------------
# 3. State machine — invalid transitions (exhaustive)
# ---------------------------------------------------------------------------


class TestInvalidTransitions:
    """Test all 12 invalid state transitions raise InvalidTransitionError."""

    def test_filed_to_resolved_invalid(self, dispute_service):
        """filed → resolved is invalid."""
        dispute = _file_dispute(dispute_service, "exchange-inv-1")
        with pytest.raises(InvalidTransitionError):
            dispute_service.resolve(dispute.id, "Should fail")

    def test_filed_to_filed_invalid(self, dispute_service):
        """filed → filed is invalid (no self-transition)."""
        dispute = _file_dispute(dispute_service, "exchange-inv-2")
        with pytest.raises(InvalidTransitionError):
            dispute_service._transition(dispute_service._session_factory(), dispute.id, "filed")

    def test_auto_mediating_to_filed_invalid(self, dispute_service):
        """auto_mediating → filed is invalid."""
        dispute = _file_dispute(dispute_service, "exchange-inv-3")
        dispute_service.auto_mediate(dispute.id)
        with pytest.raises(InvalidTransitionError):
            dispute_service._transition(dispute_service._session_factory(), dispute.id, "filed")

    def test_auto_mediating_to_auto_mediating_invalid(self, dispute_service):
        """auto_mediating → auto_mediating is invalid."""
        dispute = _file_dispute(dispute_service, "exchange-inv-4")
        dispute_service.auto_mediate(dispute.id)
        with pytest.raises(InvalidTransitionError):
            dispute_service.auto_mediate(dispute.id)

    def test_resolved_to_filed_invalid(self, dispute_service):
        """resolved → filed is invalid (terminal state)."""
        dispute = _file_dispute(dispute_service, "exchange-inv-5")
        dispute_service.auto_mediate(dispute.id)
        dispute_service.resolve(dispute.id, "Done")
        with pytest.raises(InvalidTransitionError):
            dispute_service._transition(dispute_service._session_factory(), dispute.id, "filed")

    def test_resolved_to_auto_mediating_invalid(self, dispute_service):
        """resolved → auto_mediating is invalid (terminal state)."""
        dispute = _file_dispute(dispute_service, "exchange-inv-6")
        dispute_service.auto_mediate(dispute.id)
        dispute_service.resolve(dispute.id, "Done")
        with pytest.raises(InvalidTransitionError):
            dispute_service.auto_mediate(dispute.id)

    def test_resolved_to_resolved_invalid(self, dispute_service):
        """resolved → resolved is invalid (terminal state)."""
        dispute = _file_dispute(dispute_service, "exchange-inv-7")
        dispute_service.auto_mediate(dispute.id)
        dispute_service.resolve(dispute.id, "Done")
        with pytest.raises(InvalidTransitionError):
            dispute_service.resolve(dispute.id, "Again")

    def test_resolved_to_dismissed_invalid(self, dispute_service):
        """resolved → dismissed is invalid (terminal state)."""
        dispute = _file_dispute(dispute_service, "exchange-inv-8")
        dispute_service.auto_mediate(dispute.id)
        dispute_service.resolve(dispute.id, "Done")
        with pytest.raises(InvalidTransitionError):
            dispute_service.dismiss(dispute.id, "Should fail")

    def test_dismissed_to_filed_invalid(self, dispute_service):
        """dismissed → filed is invalid (terminal state)."""
        dispute = _file_dispute(dispute_service, "exchange-inv-9")
        dispute_service.dismiss(dispute.id, "No merit")
        with pytest.raises(InvalidTransitionError):
            dispute_service._transition(dispute_service._session_factory(), dispute.id, "filed")

    def test_dismissed_to_auto_mediating_invalid(self, dispute_service):
        """dismissed → auto_mediating is invalid (terminal state)."""
        dispute = _file_dispute(dispute_service, "exchange-inv-10")
        dispute_service.dismiss(dispute.id, "No merit")
        with pytest.raises(InvalidTransitionError):
            dispute_service.auto_mediate(dispute.id)

    def test_dismissed_to_resolved_invalid(self, dispute_service):
        """dismissed → resolved is invalid (terminal state)."""
        dispute = _file_dispute(dispute_service, "exchange-inv-11")
        dispute_service.dismiss(dispute.id, "No merit")
        with pytest.raises(InvalidTransitionError):
            dispute_service.resolve(dispute.id, "Should fail")

    def test_dismissed_to_dismissed_invalid(self, dispute_service):
        """dismissed → dismissed is invalid (terminal state)."""
        dispute = _file_dispute(dispute_service, "exchange-inv-12")
        dispute_service.dismiss(dispute.id, "No merit")
        with pytest.raises(InvalidTransitionError):
            dispute_service.dismiss(dispute.id, "Again")


# ---------------------------------------------------------------------------
# 4. Not found
# ---------------------------------------------------------------------------


class TestDisputeNotFound:
    """Test DisputeNotFoundError for missing disputes."""

    def test_auto_mediate_not_found(self, dispute_service):
        with pytest.raises(DisputeNotFoundError):
            dispute_service.auto_mediate("nonexistent-id")

    def test_resolve_not_found(self, dispute_service):
        with pytest.raises(DisputeNotFoundError):
            dispute_service.resolve("nonexistent-id", "Should fail")

    def test_dismiss_not_found(self, dispute_service):
        with pytest.raises(DisputeNotFoundError):
            dispute_service.dismiss("nonexistent-id", "Should fail")


# ---------------------------------------------------------------------------
# 5. Get + list queries
# ---------------------------------------------------------------------------


class TestDisputeQueries:
    """Test dispute retrieval and listing."""

    def test_get_dispute_by_id(self, dispute_service):
        """Get dispute by ID."""
        dispute = _file_dispute(dispute_service, "exchange-get")
        result = dispute_service.get_dispute(dispute.id)
        assert result is not None
        assert result.id == dispute.id
        assert result.status == "filed"

    def test_get_dispute_not_found(self, dispute_service):
        """Get non-existent dispute returns None."""
        assert dispute_service.get_dispute("nonexistent") is None

    def test_list_disputes_by_exchange(self, dispute_service):
        """List disputes filtered by exchange ID."""
        _file_dispute(dispute_service, "exchange-list-1")
        _file_dispute(dispute_service, "exchange-list-2")

        result = dispute_service.list_disputes(exchange_id="exchange-list-1")
        assert len(result) == 1
        assert result[0].exchange_id == "exchange-list-1"

    def test_list_disputes_by_agent(self, dispute_service):
        """List disputes filtered by agent (as complainant or respondent)."""
        _file_dispute(dispute_service, "exchange-list-agent-1")

        # agent-a is complainant
        result = dispute_service.list_disputes(agent_id="agent-a")
        assert len(result) >= 1

        # agent-b is respondent
        result = dispute_service.list_disputes(agent_id="agent-b")
        assert len(result) >= 1

    def test_list_disputes_by_status(self, dispute_service):
        """List disputes filtered by status."""
        dispute = _file_dispute(dispute_service, "exchange-list-status")
        dispute_service.auto_mediate(dispute.id)

        filed = dispute_service.list_disputes(status="filed")
        mediating = dispute_service.list_disputes(status="auto_mediating")

        # This dispute should now be in auto_mediating
        mediating_ids = {d.id for d in mediating}
        filed_ids = {d.id for d in filed}
        assert dispute.id in mediating_ids
        assert dispute.id not in filed_ids

    def test_list_disputes_by_zone(self, dispute_service):
        """List disputes filtered by zone."""
        dispute_service.file_dispute(
            exchange_id="exchange-zone-1",
            complainant_agent_id="agent-a",
            respondent_agent_id="agent-b",
            zone_id="zone-x",
            reason="Zone test",
        )

        result = dispute_service.list_disputes(zone_id="zone-x")
        assert len(result) == 1
        assert result[0].zone_id == "zone-x"

        result_empty = dispute_service.list_disputes(zone_id="zone-y")
        assert len(result_empty) == 0

    def test_list_disputes_empty(self, dispute_service):
        """List with no matching disputes returns empty list."""
        assert dispute_service.list_disputes(status="resolved") == []
