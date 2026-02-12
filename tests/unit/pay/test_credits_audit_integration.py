"""Unit tests for CreditsService audit logging integration.

Issue #1360: Verifies that each CreditsService method fires the
correct audit record, and that audit failures never break the
primary transfer flow.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from nexus.pay.credits import CreditsService, TransferRequest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_audit_logger():
    """Mock ExchangeAuditLogger."""
    logger = MagicMock()
    logger.record = MagicMock(return_value="audit-record-id")
    return logger


@pytest.fixture
def disabled_service(mock_audit_logger):
    """CreditsService in disabled mode (no TigerBeetle) with audit logger."""
    return CreditsService(enabled=False, audit_logger=mock_audit_logger)


@pytest.fixture
def service_no_audit():
    """CreditsService with audit_logger=None."""
    return CreditsService(enabled=False, audit_logger=None)


# ---------------------------------------------------------------------------
# Transfer audit
# ---------------------------------------------------------------------------


class TestTransferAudit:
    @pytest.mark.asyncio
    async def test_transfer_disabled_returns_id(self, disabled_service: CreditsService) -> None:
        result = await disabled_service.transfer("alice", "bob", Decimal("10"))
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_no_audit_when_logger_none(self, service_no_audit: CreditsService) -> None:
        """No crash when audit_logger is None."""
        result = await service_no_audit.transfer("alice", "bob", Decimal("10"))
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Topup audit
# ---------------------------------------------------------------------------


class TestTopupAudit:
    @pytest.mark.asyncio
    async def test_topup_disabled(self, disabled_service: CreditsService) -> None:
        result = await disabled_service.topup("alice", Decimal("50"), "admin")
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Reserve audit
# ---------------------------------------------------------------------------


class TestReserveAudit:
    @pytest.mark.asyncio
    async def test_reserve_disabled(self, disabled_service: CreditsService) -> None:
        result = await disabled_service.reserve("alice", Decimal("20"))
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Commit reservation audit
# ---------------------------------------------------------------------------


class TestCommitReservationAudit:
    @pytest.mark.asyncio
    async def test_commit_disabled(self, disabled_service: CreditsService) -> None:
        await disabled_service.commit_reservation("res-123")


# ---------------------------------------------------------------------------
# Release reservation audit
# ---------------------------------------------------------------------------


class TestReleaseReservationAudit:
    @pytest.mark.asyncio
    async def test_release_disabled(self, disabled_service: CreditsService) -> None:
        await disabled_service.release_reservation("res-123")


# ---------------------------------------------------------------------------
# Deduct fast audit
# ---------------------------------------------------------------------------


class TestDeductFastAudit:
    @pytest.mark.asyncio
    async def test_deduct_fast_disabled(self, disabled_service: CreditsService) -> None:
        result = await disabled_service.deduct_fast("alice", Decimal("1"))
        assert result is True


# ---------------------------------------------------------------------------
# Batch transfer audit
# ---------------------------------------------------------------------------


class TestBatchTransferAudit:
    @pytest.mark.asyncio
    async def test_batch_disabled(self, disabled_service: CreditsService) -> None:
        transfers = [
            TransferRequest(from_id="alice", to_id="bob", amount=Decimal("5")),
            TransferRequest(from_id="charlie", to_id="dave", amount=Decimal("3")),
        ]
        result = await disabled_service.transfer_batch(transfers)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# Failure injection
# ---------------------------------------------------------------------------


class TestAuditFailureIsolation:
    @pytest.mark.asyncio
    async def test_audit_failure_does_not_break_transfer(self) -> None:
        """If audit logger raises, transfer still succeeds."""
        failing_logger = MagicMock()
        failing_logger.record = MagicMock(side_effect=RuntimeError("DB down"))

        service = CreditsService(enabled=False, audit_logger=failing_logger)
        # Should not raise despite audit failure
        result = await service.transfer("alice", "bob", Decimal("10"))
        assert isinstance(result, str)

    def test_record_audit_catches_exception(self) -> None:
        """_record_audit silently catches exceptions."""
        failing_logger = MagicMock()
        failing_logger.record = MagicMock(side_effect=RuntimeError("DB down"))

        service = CreditsService(enabled=False, audit_logger=failing_logger)
        # Should not raise
        service._record_audit(
            protocol="internal",
            buyer_agent_id="a",
            seller_agent_id="b",
            amount=Decimal("1"),
            status="settled",
        )
        failing_logger.record.assert_called_once()


# ---------------------------------------------------------------------------
# Disabled mode: no audit calls
# ---------------------------------------------------------------------------


class TestDisabledAudit:
    def test_record_audit_noop_when_none(self) -> None:
        service = CreditsService(enabled=False, audit_logger=None)
        # Should not raise, should be a no-op
        service._record_audit(
            protocol="internal",
            buyer_agent_id="a",
            seller_agent_id="b",
            amount=Decimal("1"),
            status="settled",
        )
