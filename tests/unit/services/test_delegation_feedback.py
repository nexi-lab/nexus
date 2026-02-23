"""Unit tests for delegation feedback loop (#1619).

Tests that complete_delegation() correctly:
1. Submits positive feedback on COMPLETED outcome
2. Submits negative reliability feedback on FAILED outcome
3. Submits negative timeliness feedback on TIMEOUT outcome
4. Skips feedback when reputation_service is None
5. Raises on delegation not found
6. Raises on already-completed delegation
"""

from unittest.mock import MagicMock, patch

import pytest

from nexus.bricks.delegation.errors import DelegationError, DelegationNotFoundError
from nexus.bricks.delegation.models import (
    DelegationMode,
    DelegationOutcome,
    DelegationRecord,
    DelegationStatus,
)
from nexus.bricks.delegation.service import DelegationService
from nexus.contracts.constants import ROOT_ZONE_ID


def _make_record(
    delegation_id: str = "del-1",
    agent_id: str = "worker-1",
    parent_agent_id: str = "coordinator-1",
    status: DelegationStatus = DelegationStatus.ACTIVE,
    zone_id: str | None = "zone-1",
) -> DelegationRecord:
    """Create a minimal DelegationRecord for testing."""
    return DelegationRecord(
        delegation_id=delegation_id,
        agent_id=agent_id,
        parent_agent_id=parent_agent_id,
        delegation_mode=DelegationMode.COPY,
        status=status,
        zone_id=zone_id,
    )


@pytest.fixture()
def mock_reputation_service():
    service = MagicMock()
    service.submit_feedback.return_value = MagicMock()
    return service


@pytest.fixture()
def delegation_service(mock_reputation_service):
    """DelegationService with mocked internals."""
    service = DelegationService(
        record_store=MagicMock(),
        rebac_manager=MagicMock(),
        reputation_service=mock_reputation_service,
    )
    return service


class TestCompleteDelegation:
    def test_complete_delegation_success_submits_positive_feedback(
        self, delegation_service, mock_reputation_service
    ):
        """COMPLETED -> positive reliability + quality feedback."""
        active_record = _make_record(status=DelegationStatus.ACTIVE)
        completed_record = _make_record(status=DelegationStatus.COMPLETED)

        with (
            patch.object(
                delegation_service,
                "_load_delegation_record",
                side_effect=[active_record, completed_record],
            ),
            patch.object(delegation_service, "_update_delegation_status"),
        ):
            result = delegation_service.complete_delegation(
                delegation_id="del-1",
                outcome=DelegationOutcome.COMPLETED,
                quality_score=0.95,
            )

        assert result.status == DelegationStatus.COMPLETED
        mock_reputation_service.submit_feedback.assert_called_once_with(
            rater_agent_id="coordinator-1",
            rated_agent_id="worker-1",
            exchange_id="del-1",
            zone_id="zone-1",
            outcome="positive",
            reliability_score=1.0,
            quality_score=0.95,
        )

    def test_complete_delegation_success_default_quality(
        self, delegation_service, mock_reputation_service
    ):
        """COMPLETED with no quality_score uses 0.8 default."""
        active_record = _make_record()
        completed_record = _make_record(status=DelegationStatus.COMPLETED)

        with (
            patch.object(
                delegation_service,
                "_load_delegation_record",
                side_effect=[active_record, completed_record],
            ),
            patch.object(delegation_service, "_update_delegation_status"),
        ):
            delegation_service.complete_delegation(
                delegation_id="del-1",
                outcome=DelegationOutcome.COMPLETED,
            )

        mock_reputation_service.submit_feedback.assert_called_once()
        call_kwargs = mock_reputation_service.submit_feedback.call_args.kwargs
        assert call_kwargs["quality_score"] == 0.8

    def test_complete_delegation_failure_submits_negative_feedback(
        self, delegation_service, mock_reputation_service
    ):
        """FAILED -> negative reliability feedback."""
        active_record = _make_record()
        completed_record = _make_record(status=DelegationStatus.COMPLETED)

        with (
            patch.object(
                delegation_service,
                "_load_delegation_record",
                side_effect=[active_record, completed_record],
            ),
            patch.object(delegation_service, "_update_delegation_status"),
        ):
            delegation_service.complete_delegation(
                delegation_id="del-1",
                outcome=DelegationOutcome.FAILED,
            )

        mock_reputation_service.submit_feedback.assert_called_once_with(
            rater_agent_id="coordinator-1",
            rated_agent_id="worker-1",
            exchange_id="del-1",
            zone_id="zone-1",
            outcome="negative",
            reliability_score=0.0,
        )

    def test_complete_delegation_timeout_submits_timeliness_feedback(
        self, delegation_service, mock_reputation_service
    ):
        """TIMEOUT -> negative timeliness feedback."""
        active_record = _make_record()
        completed_record = _make_record(status=DelegationStatus.COMPLETED)

        with (
            patch.object(
                delegation_service,
                "_load_delegation_record",
                side_effect=[active_record, completed_record],
            ),
            patch.object(delegation_service, "_update_delegation_status"),
        ):
            delegation_service.complete_delegation(
                delegation_id="del-1",
                outcome=DelegationOutcome.TIMEOUT,
            )

        mock_reputation_service.submit_feedback.assert_called_once_with(
            rater_agent_id="coordinator-1",
            rated_agent_id="worker-1",
            exchange_id="del-1",
            zone_id="zone-1",
            outcome="negative",
            timeliness_score=0.0,
        )

    def test_complete_delegation_no_reputation_service_skips_feedback(self):
        """No reputation_service -> feedback skipped, completion still succeeds."""
        service = DelegationService(
            record_store=MagicMock(),
            rebac_manager=MagicMock(),
            reputation_service=None,
        )
        active_record = _make_record()
        completed_record = _make_record(status=DelegationStatus.COMPLETED)

        with (
            patch.object(
                service,
                "_load_delegation_record",
                side_effect=[active_record, completed_record],
            ),
            patch.object(service, "_update_delegation_status"),
        ):
            result = service.complete_delegation(
                delegation_id="del-1",
                outcome=DelegationOutcome.COMPLETED,
            )

        assert result.status == DelegationStatus.COMPLETED

    def test_complete_delegation_not_found_raises(self, delegation_service):
        """Non-existent delegation -> DelegationNotFoundError."""
        with (
            patch.object(delegation_service, "_load_delegation_record", return_value=None),
            pytest.raises(DelegationNotFoundError, match="not found"),
        ):
            delegation_service.complete_delegation(
                delegation_id="nonexistent",
                outcome=DelegationOutcome.COMPLETED,
            )

    def test_complete_delegation_already_completed_raises(self, delegation_service):
        """Already-completed delegation -> DelegationError."""
        completed_record = _make_record(status=DelegationStatus.COMPLETED)

        with (
            patch.object(
                delegation_service, "_load_delegation_record", return_value=completed_record
            ),
            pytest.raises(DelegationError, match="not active"),
        ):
            delegation_service.complete_delegation(
                delegation_id="del-1",
                outcome=DelegationOutcome.COMPLETED,
            )

    def test_complete_delegation_null_zone_defaults_to_default(
        self, delegation_service, mock_reputation_service
    ):
        """zone_id=None uses ROOT_ZONE_ID ('root') for feedback."""
        active_record = _make_record(zone_id=None)
        completed_record = _make_record(zone_id=None, status=DelegationStatus.COMPLETED)

        with (
            patch.object(
                delegation_service,
                "_load_delegation_record",
                side_effect=[active_record, completed_record],
            ),
            patch.object(delegation_service, "_update_delegation_status"),
        ):
            delegation_service.complete_delegation(
                delegation_id="del-1",
                outcome=DelegationOutcome.COMPLETED,
            )

        call_kwargs = mock_reputation_service.submit_feedback.call_args.kwargs
        assert call_kwargs["zone_id"] == ROOT_ZONE_ID
