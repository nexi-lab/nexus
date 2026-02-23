"""Unit tests for trust gate in DelegationService (#1619).

Tests that the trust gate in delegate() correctly:
1. Passes when score is above threshold
2. Rejects when score is below threshold
3. Rejects when no score exists and threshold > 0
4. Is disabled by default (min_trust_score=0.0)
5. Is skipped when reputation_service is None
"""

from dataclasses import dataclass
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from nexus.bricks.delegation.errors import InsufficientTrustError
from nexus.bricks.delegation.models import DelegationMode
from nexus.bricks.delegation.service import DelegationService
from nexus.contracts.constants import ROOT_ZONE_ID


@dataclass(frozen=True)
class FakeReputationScore:
    """Minimal ReputationScore for testing."""

    agent_id: str = "coordinator-1"
    context: str = "general"
    window: str = "all_time"
    composite_score: float = 0.8
    composite_confidence: float = 0.7
    reliability_alpha: float = 5.0
    reliability_beta: float = 1.0
    quality_alpha: float = 4.0
    quality_beta: float = 1.0
    timeliness_alpha: float = 3.0
    timeliness_beta: float = 1.0
    fairness_alpha: float = 3.0
    fairness_beta: float = 1.0
    total_interactions: int = 10
    positive_interactions: int = 8
    negative_interactions: int = 2
    disputed_interactions: int = 0
    global_trust_score: float | None = None
    updated_at: datetime = datetime(2025, 1, 1)
    zone_id: str = ROOT_ZONE_ID


@pytest.fixture()
def mock_reputation_service():
    """Mock ReputationService with controllable get_reputation."""
    service = MagicMock()
    service.get_reputation.return_value = FakeReputationScore(composite_score=0.8)
    return service


@pytest.fixture()
def mock_deps():
    """Mock dependencies for DelegationService (record_store, rebac_manager)."""
    record_store = MagicMock()
    rebac_manager = MagicMock()
    entity_registry = MagicMock()
    agent_registry = MagicMock()
    return record_store, rebac_manager, entity_registry, agent_registry


def _make_service(mock_deps, reputation_service=None):
    """Create DelegationService with mock dependencies."""
    rs, rebac, entity_reg, agent_reg = mock_deps
    return DelegationService(
        record_store=rs,
        rebac_manager=rebac,
        entity_registry=entity_reg,
        agent_registry=agent_reg,
        reputation_service=reputation_service,
    )


class TestTrustGate:
    """Tests for the trust gate in delegate()."""

    def test_delegate_trust_gate_passes(self, mock_deps, mock_reputation_service):
        """Score above threshold -> delegation proceeds (trust gate passes)."""
        mock_reputation_service.get_reputation.return_value = FakeReputationScore(
            composite_score=0.9
        )
        service = _make_service(mock_deps, reputation_service=mock_reputation_service)

        # Mock internal methods to isolate trust gate testing
        with (
            patch.object(service, "_validate_coordinator", return_value=None),
            patch.object(service, "_compute_lease_expiry", return_value=None),
            patch.object(service, "_enumerate_parent_grants", return_value=[]),
            patch.object(service, "_create_grant_tuples"),
            patch.object(service, "_persist_delegation_record"),
            patch.object(service, "_create_worker_api_key", return_value="nxk_test"),
            patch.object(service, "_get_worker_mount_table", return_value=[]),
        ):
            result = service.delegate(
                coordinator_agent_id="coordinator-1",
                coordinator_owner_id="owner-1",
                worker_id="worker-1",
                worker_name="Worker 1",
                delegation_mode=DelegationMode.COPY,
                min_trust_score=0.5,
            )

        assert result.worker_agent_id == "worker-1"
        mock_reputation_service.get_reputation.assert_called_once_with("coordinator-1")

    def test_delegate_trust_gate_rejects_low_score(self, mock_deps, mock_reputation_service):
        """Score below threshold -> InsufficientTrustError."""
        mock_reputation_service.get_reputation.return_value = FakeReputationScore(
            composite_score=0.3
        )
        service = _make_service(mock_deps, reputation_service=mock_reputation_service)

        with (
            patch.object(service, "_validate_coordinator", return_value=None),
            pytest.raises(InsufficientTrustError) as exc_info,
        ):
            service.delegate(
                coordinator_agent_id="coordinator-1",
                coordinator_owner_id="owner-1",
                worker_id="worker-1",
                worker_name="Worker 1",
                delegation_mode=DelegationMode.COPY,
                min_trust_score=0.5,
            )

        assert exc_info.value.agent_id == "coordinator-1"
        assert exc_info.value.score == 0.3
        assert exc_info.value.threshold == 0.5

    def test_delegate_trust_gate_rejects_no_score(self, mock_deps, mock_reputation_service):
        """No score + threshold > 0 -> InsufficientTrustError."""
        mock_reputation_service.get_reputation.return_value = None
        service = _make_service(mock_deps, reputation_service=mock_reputation_service)

        with (
            patch.object(service, "_validate_coordinator", return_value=None),
            pytest.raises(InsufficientTrustError) as exc_info,
        ):
            service.delegate(
                coordinator_agent_id="coordinator-1",
                coordinator_owner_id="owner-1",
                worker_id="worker-1",
                worker_name="Worker 1",
                delegation_mode=DelegationMode.COPY,
                min_trust_score=0.5,
            )

        assert exc_info.value.score is None
        assert exc_info.value.threshold == 0.5

    def test_delegate_trust_gate_disabled_by_default(self, mock_deps, mock_reputation_service):
        """min_trust_score=0.0 (default) -> trust gate skipped."""
        service = _make_service(mock_deps, reputation_service=mock_reputation_service)

        with (
            patch.object(service, "_validate_coordinator", return_value=None),
            patch.object(service, "_compute_lease_expiry", return_value=None),
            patch.object(service, "_enumerate_parent_grants", return_value=[]),
            patch.object(service, "_create_grant_tuples"),
            patch.object(service, "_persist_delegation_record"),
            patch.object(service, "_create_worker_api_key", return_value="nxk_test"),
            patch.object(service, "_get_worker_mount_table", return_value=[]),
        ):
            result = service.delegate(
                coordinator_agent_id="coordinator-1",
                coordinator_owner_id="owner-1",
                worker_id="worker-1",
                worker_name="Worker 1",
                delegation_mode=DelegationMode.COPY,
                min_trust_score=0.0,  # default — disabled
            )

        assert result.worker_agent_id == "worker-1"
        # get_reputation should NOT have been called
        mock_reputation_service.get_reputation.assert_not_called()

    def test_delegate_no_reputation_service(self, mock_deps):
        """reputation_service=None -> trust gate skipped entirely."""
        service = _make_service(mock_deps, reputation_service=None)

        with (
            patch.object(service, "_validate_coordinator", return_value=None),
            patch.object(service, "_compute_lease_expiry", return_value=None),
            patch.object(service, "_enumerate_parent_grants", return_value=[]),
            patch.object(service, "_create_grant_tuples"),
            patch.object(service, "_persist_delegation_record"),
            patch.object(service, "_create_worker_api_key", return_value="nxk_test"),
            patch.object(service, "_get_worker_mount_table", return_value=[]),
        ):
            # Even with high threshold, should succeed since no reputation_service
            result = service.delegate(
                coordinator_agent_id="coordinator-1",
                coordinator_owner_id="owner-1",
                worker_id="worker-1",
                worker_name="Worker 1",
                delegation_mode=DelegationMode.COPY,
                min_trust_score=0.99,
            )

        assert result.worker_agent_id == "worker-1"
