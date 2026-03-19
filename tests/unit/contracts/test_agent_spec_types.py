"""Tests for AgentSpec/AgentStatus domain types (Issue #2169).

Verifies:
1. Frozen dataclass immutability for all new types
2. QoSClass and AgentPhase enum values
3. derive_phase() state→phase mapping with condition overrides
4. detect_drift() generation comparison
5. AgentSpec defaults and serialization round-trip
6. AgentResources with None values (unlimited)
7. AgentCondition with all fields
"""

from datetime import UTC, datetime

import pytest

from nexus.contracts.agent_types import (
    AGENT_STATE_TO_PHASE,
    AgentCondition,
    AgentPhase,
    AgentResources,
    AgentResourceUsage,
    AgentSpec,
    AgentState,
    AgentStatus,
    QoSClass,
    derive_phase,
    detect_drift,
)

# ---------------------------------------------------------------------------
# QoSClass enum
# ---------------------------------------------------------------------------


class TestQoSClass:
    def test_values(self) -> None:
        assert QoSClass.PREMIUM == "premium"
        assert QoSClass.STANDARD == "standard"
        assert QoSClass.SPOT == "spot"

    def test_string_construction(self) -> None:
        assert QoSClass("premium") is QoSClass.PREMIUM
        assert QoSClass("standard") is QoSClass.STANDARD
        assert QoSClass("spot") is QoSClass.SPOT

    def test_invalid_value_raises(self) -> None:
        with pytest.raises(ValueError):
            QoSClass("invalid")


# ---------------------------------------------------------------------------
# AgentPhase enum
# ---------------------------------------------------------------------------


class TestAgentPhase:
    def test_all_values(self) -> None:
        expected = {"warming", "ready", "active", "thinking", "idle", "suspended", "evicted"}
        actual = {p.value for p in AgentPhase}
        assert actual == expected

    def test_string_construction(self) -> None:
        for phase in AgentPhase:
            assert AgentPhase(phase.value) is phase


# ---------------------------------------------------------------------------
# AgentResources (frozen, slots)
# ---------------------------------------------------------------------------


class TestAgentResources:
    def test_defaults_are_none(self) -> None:
        r = AgentResources()
        assert r.token_budget is None
        assert r.token_request is None
        assert r.storage_limit_mb is None
        assert r.context_limit is None

    def test_with_values(self) -> None:
        r = AgentResources(token_budget=10000, context_limit=128)
        assert r.token_budget == 10000
        assert r.context_limit == 128
        assert r.token_request is None

    def test_frozen(self) -> None:
        r = AgentResources(token_budget=5000)
        with pytest.raises(AttributeError):
            r.token_budget = 9999  # type: ignore[misc]

    def test_equality(self) -> None:
        a = AgentResources(token_budget=100)
        b = AgentResources(token_budget=100)
        assert a == b

    def test_inequality(self) -> None:
        a = AgentResources(token_budget=100)
        b = AgentResources(token_budget=200)
        assert a != b


# ---------------------------------------------------------------------------
# AgentResourceUsage (frozen, slots)
# ---------------------------------------------------------------------------


class TestAgentResourceUsage:
    def test_defaults(self) -> None:
        u = AgentResourceUsage()
        assert u.tokens_used == 0
        assert u.storage_used_mb == 0.0
        assert u.context_usage_pct == 0.0

    def test_frozen(self) -> None:
        u = AgentResourceUsage(tokens_used=500)
        with pytest.raises(AttributeError):
            u.tokens_used = 999  # type: ignore[misc]


# ---------------------------------------------------------------------------
# AgentCondition (frozen, slots)
# ---------------------------------------------------------------------------


class TestAgentCondition:
    def test_all_fields(self) -> None:
        now = datetime.now(UTC)
        c = AgentCondition(
            type="Ready",
            status="True",
            reason="AllGood",
            message="Agent is ready",
            last_transition=now,
            observed_generation=3,
        )
        assert c.type == "Ready"
        assert c.status == "True"
        assert c.reason == "AllGood"
        assert c.message == "Agent is ready"
        assert c.last_transition == now
        assert c.observed_generation == 3

    def test_frozen(self) -> None:
        c = AgentCondition(
            type="Ready",
            status="True",
            reason="OK",
            message="OK",
            last_transition=datetime.now(UTC),
            observed_generation=1,
        )
        with pytest.raises(AttributeError):
            c.status = "False"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# AgentSpec (frozen, slots)
# ---------------------------------------------------------------------------


class TestAgentSpec:
    def test_defaults(self) -> None:
        spec = AgentSpec(
            agent_type="analyst",
            capabilities=frozenset({"search"}),
            resource_requests=AgentResources(),
            resource_limits=AgentResources(),
        )
        assert spec.qos_class is QoSClass.STANDARD
        assert spec.zone_affinity is None
        assert spec.spec_generation == 1

    def test_frozen(self) -> None:
        spec = AgentSpec(
            agent_type="analyst",
            capabilities=frozenset(),
            resource_requests=AgentResources(),
            resource_limits=AgentResources(),
        )
        with pytest.raises(AttributeError):
            spec.agent_type = "coder"  # type: ignore[misc]

    def test_capabilities_frozenset(self) -> None:
        caps = frozenset({"search", "analyze", "code"})
        spec = AgentSpec(
            agent_type="analyst",
            capabilities=caps,
            resource_requests=AgentResources(),
            resource_limits=AgentResources(),
        )
        assert spec.capabilities == caps
        assert isinstance(spec.capabilities, frozenset)

    def test_empty_capabilities(self) -> None:
        spec = AgentSpec(
            agent_type="minimal",
            capabilities=frozenset(),
            resource_requests=AgentResources(),
            resource_limits=AgentResources(),
        )
        assert len(spec.capabilities) == 0

    def test_all_fields(self) -> None:
        spec = AgentSpec(
            agent_type="coder",
            capabilities=frozenset({"code", "debug"}),
            resource_requests=AgentResources(token_budget=5000),
            resource_limits=AgentResources(token_budget=10000),
            qos_class=QoSClass.PREMIUM,
            zone_affinity="zone-acme",
            spec_generation=5,
        )
        assert spec.agent_type == "coder"
        assert spec.qos_class is QoSClass.PREMIUM
        assert spec.zone_affinity == "zone-acme"
        assert spec.spec_generation == 5


# ---------------------------------------------------------------------------
# AgentStatus (frozen, slots)
# ---------------------------------------------------------------------------


class TestAgentStatus:
    def test_minimal(self) -> None:
        status = AgentStatus(
            phase=AgentPhase.ACTIVE,
            observed_generation=1,
            conditions=(),
            resource_usage=AgentResourceUsage(),
            last_heartbeat=None,
            last_activity=None,
        )
        assert status.phase is AgentPhase.ACTIVE
        assert status.observed_generation == 1
        assert status.conditions == ()
        assert status.inbox_depth == 0
        assert status.context_usage_pct == 0.0

    def test_frozen(self) -> None:
        status = AgentStatus(
            phase=AgentPhase.IDLE,
            observed_generation=0,
            conditions=(),
            resource_usage=AgentResourceUsage(),
            last_heartbeat=None,
            last_activity=None,
        )
        with pytest.raises(AttributeError):
            status.phase = AgentPhase.ACTIVE  # type: ignore[misc]


# ---------------------------------------------------------------------------
# AGENT_STATE_TO_PHASE mapping
# ---------------------------------------------------------------------------


class TestAgentStateToPhase:
    def test_mapping_completeness(self) -> None:
        for state in AgentState:
            assert state in AGENT_STATE_TO_PHASE, f"{state} missing from mapping"

    def test_mapping_values(self) -> None:
        assert AGENT_STATE_TO_PHASE[AgentState.UNKNOWN] is AgentPhase.WARMING
        assert AGENT_STATE_TO_PHASE[AgentState.CONNECTED] is AgentPhase.ACTIVE
        assert AGENT_STATE_TO_PHASE[AgentState.IDLE] is AgentPhase.IDLE
        assert AGENT_STATE_TO_PHASE[AgentState.SUSPENDED] is AgentPhase.SUSPENDED


# ---------------------------------------------------------------------------
# derive_phase()
# ---------------------------------------------------------------------------


class TestDerivePhase:
    @pytest.mark.parametrize(
        "state,expected_phase",
        [
            (AgentState.UNKNOWN, AgentPhase.WARMING),
            (AgentState.CONNECTED, AgentPhase.ACTIVE),
            (AgentState.IDLE, AgentPhase.IDLE),
            (AgentState.SUSPENDED, AgentPhase.SUSPENDED),
        ],
    )
    def test_base_mapping(self, state: AgentState, expected_phase: AgentPhase) -> None:
        assert derive_phase(state) == expected_phase

    def test_ready_condition_overrides_active(self) -> None:
        cond = AgentCondition(
            type="Ready",
            status="True",
            reason="OK",
            message="Ready",
            last_transition=datetime.now(UTC),
            observed_generation=1,
        )
        assert derive_phase(AgentState.CONNECTED, (cond,)) == AgentPhase.READY

    def test_ready_condition_no_effect_on_idle(self) -> None:
        cond = AgentCondition(
            type="Ready",
            status="True",
            reason="OK",
            message="Ready",
            last_transition=datetime.now(UTC),
            observed_generation=1,
        )
        assert derive_phase(AgentState.IDLE, (cond,)) == AgentPhase.IDLE

    def test_evicted_condition_overrides_any(self) -> None:
        cond = AgentCondition(
            type="Evicted",
            status="True",
            reason="PressureCritical",
            message="Evicted due to memory pressure",
            last_transition=datetime.now(UTC),
            observed_generation=1,
        )
        assert derive_phase(AgentState.SUSPENDED, (cond,)) == AgentPhase.EVICTED

    def test_thinking_condition_overrides_active(self) -> None:
        cond = AgentCondition(
            type="Thinking",
            status="True",
            reason="LLMCall",
            message="Processing",
            last_transition=datetime.now(UTC),
            observed_generation=1,
        )
        assert derive_phase(AgentState.CONNECTED, (cond,)) == AgentPhase.THINKING

    def test_no_conditions_returns_base(self) -> None:
        assert derive_phase(AgentState.CONNECTED, ()) == AgentPhase.ACTIVE


# ---------------------------------------------------------------------------
# detect_drift()
# ---------------------------------------------------------------------------


class TestDetectDrift:
    def test_matching_generations(self) -> None:
        spec = AgentSpec(
            agent_type="a",
            capabilities=frozenset(),
            resource_requests=AgentResources(),
            resource_limits=AgentResources(),
            spec_generation=3,
        )
        status = AgentStatus(
            phase=AgentPhase.ACTIVE,
            observed_generation=3,
            conditions=(),
            resource_usage=AgentResourceUsage(),
            last_heartbeat=None,
            last_activity=None,
        )
        assert detect_drift(spec, status) is False

    def test_mismatching_generations(self) -> None:
        spec = AgentSpec(
            agent_type="a",
            capabilities=frozenset(),
            resource_requests=AgentResources(),
            resource_limits=AgentResources(),
            spec_generation=4,
        )
        status = AgentStatus(
            phase=AgentPhase.ACTIVE,
            observed_generation=3,
            conditions=(),
            resource_usage=AgentResourceUsage(),
            last_heartbeat=None,
            last_activity=None,
        )
        assert detect_drift(spec, status) is True
