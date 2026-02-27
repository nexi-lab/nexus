"""End-to-end test for trust-based routing API (#1619).

Full integration test with real services (SQLite in-memory):
1. Submit feedback to build an agent's reputation
2. Query trust score via ReputationService
3. Create delegation with min_trust_score — verify trust gate
4. Complete delegation — verify feedback submitted
5. Query reputation again — verify score updated
"""

import pytest

from nexus.bricks.delegation.errors import InsufficientTrustError
from nexus.bricks.delegation.models import (
    DelegationMode,
    DelegationOutcome,
    DelegationStatus,
)
from nexus.bricks.delegation.service import DelegationService
from nexus.bricks.rebac.entity_registry import EntityRegistry
from nexus.bricks.rebac.manager import EnhancedReBACManager
from nexus.bricks.reputation.reputation_service import ReputationService
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.services.agents.agent_registry import AgentRegistry
from tests.helpers.in_memory_record_store import InMemoryRecordStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def record_store():
    """Shared in-memory RecordStore for all components."""
    store = InMemoryRecordStore()
    yield store
    store.close()


@pytest.fixture()
def engine(record_store):
    return record_store.engine


@pytest.fixture()
def session_factory(record_store):
    return record_store.session_factory


@pytest.fixture()
def rebac_manager(engine):
    manager = EnhancedReBACManager(
        engine=engine,
        cache_ttl_seconds=0,
        max_depth=10,
    )
    yield manager
    manager.close()


@pytest.fixture()
def entity_registry(record_store):
    return EntityRegistry(record_store)


@pytest.fixture()
def agent_registry(record_store, entity_registry):
    return AgentRegistry(
        record_store=record_store,
        entity_registry=entity_registry,
    )


@pytest.fixture()
def reputation_service(record_store):
    return ReputationService(
        record_store=record_store,
    )


@pytest.fixture()
def delegation_service(
    record_store, rebac_manager, entity_registry, agent_registry, reputation_service
):
    return DelegationService(
        record_store=record_store,
        rebac_manager=rebac_manager,
        entity_registry=entity_registry,
        agent_registry=agent_registry,
        reputation_service=reputation_service,
    )


def _setup_coordinator(entity_registry, rebac_manager, agent_id="coordinator_agent"):
    """Register a coordinator agent with file grants."""
    entity_registry.register_entity(entity_type="user", entity_id="alice")
    entity_registry.register_entity(
        entity_type="agent",
        entity_id=agent_id,
        parent_type="user",
        parent_id="alice",
    )
    rebac_manager.rebac_write_batch(
        [
            {
                "subject": ("agent", agent_id),
                "relation": "direct_editor",
                "object": ("file", "/workspace/project/src/main.py"),
                "zone_id": ROOT_ZONE_ID,
            },
            {
                "subject": ("agent", agent_id),
                "relation": "direct_editor",
                "object": ("file", "/workspace/project/src/utils.py"),
                "zone_id": ROOT_ZONE_ID,
            },
        ]
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTrustRoutingE2E:
    """Full trust-based routing integration test."""

    def test_trust_gate_allows_high_reputation(
        self,
        delegation_service,
        reputation_service,
        entity_registry,
        rebac_manager,
    ):
        """Agent with good reputation passes the trust gate."""
        _setup_coordinator(entity_registry, rebac_manager)

        # Build up coordinator's reputation with positive feedback
        for i in range(5):
            reputation_service.submit_feedback(
                rater_agent_id=f"rater-{i}",
                rated_agent_id="coordinator_agent",
                exchange_id=f"exchange-{i}",
                zone_id=ROOT_ZONE_ID,
                outcome="positive",
                reliability_score=1.0,
                quality_score=0.9,
            )

        # Verify trust score is high
        score = reputation_service.get_reputation("coordinator_agent")
        assert score is not None
        assert score.composite_score > 0.6

        # Delegation with min_trust_score=0.5 should succeed
        result = delegation_service.delegate(
            coordinator_agent_id="coordinator_agent",
            coordinator_owner_id="alice",
            worker_id="worker-trust-ok",
            worker_name="Trusted Worker",
            delegation_mode=DelegationMode.COPY,
            zone_id=ROOT_ZONE_ID,
            min_trust_score=0.5,
        )

        assert result.worker_agent_id == "worker-trust-ok"
        assert result.delegation_id is not None

    def test_trust_gate_rejects_low_reputation(
        self,
        delegation_service,
        reputation_service,
        entity_registry,
        rebac_manager,
    ):
        """Agent with poor reputation is rejected by the trust gate."""
        _setup_coordinator(entity_registry, rebac_manager)

        # Build negative reputation
        for i in range(5):
            reputation_service.submit_feedback(
                rater_agent_id=f"rater-{i}",
                rated_agent_id="coordinator_agent",
                exchange_id=f"neg-exchange-{i}",
                zone_id=ROOT_ZONE_ID,
                outcome="negative",
                reliability_score=0.0,
            )

        # Delegation with min_trust_score=0.7 should fail
        with pytest.raises(InsufficientTrustError) as exc_info:
            delegation_service.delegate(
                coordinator_agent_id="coordinator_agent",
                coordinator_owner_id="alice",
                worker_id="worker-trust-fail",
                worker_name="Untrusted Worker",
                delegation_mode=DelegationMode.COPY,
                zone_id=ROOT_ZONE_ID,
                min_trust_score=0.7,
            )

        assert exc_info.value.agent_id == "coordinator_agent"
        assert exc_info.value.threshold == 0.7

    def test_trust_gate_rejects_unknown_agent(
        self,
        delegation_service,
        entity_registry,
        rebac_manager,
    ):
        """Agent with no reputation data is rejected when threshold > 0."""
        _setup_coordinator(entity_registry, rebac_manager)

        with pytest.raises(InsufficientTrustError) as exc_info:
            delegation_service.delegate(
                coordinator_agent_id="coordinator_agent",
                coordinator_owner_id="alice",
                worker_id="worker-no-rep",
                worker_name="Unknown Worker",
                delegation_mode=DelegationMode.COPY,
                zone_id=ROOT_ZONE_ID,
                min_trust_score=0.5,
            )

        assert exc_info.value.score is None

    def test_complete_delegation_updates_reputation(
        self,
        delegation_service,
        reputation_service,
        entity_registry,
        rebac_manager,
    ):
        """Completing a delegation submits feedback that updates reputation."""
        _setup_coordinator(entity_registry, rebac_manager)

        # Create delegation (no trust gate)
        result = delegation_service.delegate(
            coordinator_agent_id="coordinator_agent",
            coordinator_owner_id="alice",
            worker_id="worker-complete",
            worker_name="Completable Worker",
            delegation_mode=DelegationMode.COPY,
            zone_id=ROOT_ZONE_ID,
        )

        # Complete the delegation with positive outcome
        updated = delegation_service.complete_delegation(
            delegation_id=result.delegation_id,
            outcome=DelegationOutcome.COMPLETED,
            quality_score=0.95,
        )

        assert updated.status == DelegationStatus.COMPLETED

        # Verify reputation was updated for the worker
        worker_score = reputation_service.get_reputation("worker-complete")
        assert worker_score is not None
        assert worker_score.positive_interactions >= 1

    def test_complete_delegation_failed_outcome(
        self,
        delegation_service,
        reputation_service,
        entity_registry,
        rebac_manager,
    ):
        """Failed delegation submits negative reliability feedback."""
        _setup_coordinator(entity_registry, rebac_manager)

        result = delegation_service.delegate(
            coordinator_agent_id="coordinator_agent",
            coordinator_owner_id="alice",
            worker_id="worker-fail",
            worker_name="Failing Worker",
            delegation_mode=DelegationMode.COPY,
            zone_id=ROOT_ZONE_ID,
        )

        updated = delegation_service.complete_delegation(
            delegation_id=result.delegation_id,
            outcome=DelegationOutcome.FAILED,
        )

        assert updated.status == DelegationStatus.COMPLETED

        worker_score = reputation_service.get_reputation("worker-fail")
        assert worker_score is not None
        assert worker_score.negative_interactions >= 1

    def test_full_trust_routing_lifecycle(
        self,
        delegation_service,
        reputation_service,
        entity_registry,
        rebac_manager,
    ):
        """Full lifecycle: build reputation -> trust gate -> complete -> verify."""
        _setup_coordinator(entity_registry, rebac_manager)

        # Phase 1: Build coordinator's reputation
        for i in range(3):
            reputation_service.submit_feedback(
                rater_agent_id=f"external-{i}",
                rated_agent_id="coordinator_agent",
                exchange_id=f"lifecycle-{i}",
                zone_id=ROOT_ZONE_ID,
                outcome="positive",
                reliability_score=1.0,
                quality_score=0.9,
                timeliness_score=0.8,
            )

        # Phase 2: Trust-gated delegation
        score_before = reputation_service.get_reputation("coordinator_agent")
        assert score_before is not None
        assert score_before.composite_score > 0.5

        result = delegation_service.delegate(
            coordinator_agent_id="coordinator_agent",
            coordinator_owner_id="alice",
            worker_id="worker-lifecycle",
            worker_name="Lifecycle Worker",
            delegation_mode=DelegationMode.COPY,
            zone_id=ROOT_ZONE_ID,
            min_trust_score=0.5,
        )

        # Phase 3: Complete delegation
        updated = delegation_service.complete_delegation(
            delegation_id=result.delegation_id,
            outcome=DelegationOutcome.COMPLETED,
            quality_score=0.9,
        )
        assert updated.status == DelegationStatus.COMPLETED

        # Phase 4: Verify worker reputation was created
        worker_score = reputation_service.get_reputation("worker-lifecycle")
        assert worker_score is not None
        assert worker_score.composite_score > 0.5
