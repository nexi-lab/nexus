"""Integration-style tests for SandboxAuthService.

Uses a REAL AgentRegistry backed by in-memory SQLite (via session_factory)
and a MOCK SandboxManager to avoid real Docker/E2B provider calls.

Tests cover:
- authorize_agent_access (via create_sandbox) with valid agent + sandbox
- authorize_agent_access with unregistered agent (should fail)
- authorize_agent_access with non-existent sandbox (SandboxManager failure)
- authorize_agent_access with expired/suspended agent (should fail)
- Zone isolation: agent in zone A cannot access sandbox in zone B
- Stop and connect lifecycle flows
- Event recording with real AgentEventLog
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from nexus.core.agent_record import AgentState
from nexus.core.agent_registry import AgentRegistry, InvalidTransitionError
from nexus.sandbox.auth_service import SandboxAuthService
from nexus.sandbox.events import AgentEventLog
from nexus.storage.models import Base

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    """Create in-memory SQLite database with all tables."""
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session_factory(engine):
    """Create a session factory from the in-memory engine."""
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def agent_registry(session_factory):
    """Real AgentRegistry backed by in-memory SQLite."""
    return AgentRegistry(session_factory=session_factory)


@pytest.fixture
def event_log(session_factory):
    """Real AgentEventLog backed by in-memory SQLite."""
    return AgentEventLog(session_factory=session_factory)


@pytest.fixture
def mock_sandbox_manager():
    """Mock SandboxManager that simulates successful sandbox operations."""
    mgr = MagicMock()
    mgr.create_sandbox = AsyncMock(
        return_value={
            "sandbox_id": "sb-test-001",
            "name": "test-sandbox",
            "status": "active",
            "provider": "docker",
        }
    )
    mgr.stop_sandbox = AsyncMock(
        return_value={
            "sandbox_id": "sb-test-001",
            "status": "stopped",
        }
    )
    mgr.connect_sandbox = AsyncMock(
        return_value={
            "success": True,
            "sandbox_id": "sb-test-001",
            "mount_path": "/mnt/nexus",
        }
    )
    return mgr


@pytest.fixture
def auth_service(agent_registry, mock_sandbox_manager, event_log):
    """SandboxAuthService wired with real registry + real event log + mock sandbox mgr."""
    return SandboxAuthService(
        agent_registry=agent_registry,
        sandbox_manager=mock_sandbox_manager,
        namespace_manager=None,
        event_log=event_log,
        budget_enforcement=False,
    )


# ---------------------------------------------------------------------------
# Test: authorize agent access with valid agent + sandbox
# ---------------------------------------------------------------------------


class TestAuthorizeValidAgentAccess:
    """Test that a properly registered agent can create a sandbox."""

    async def test_registered_agent_creates_sandbox_successfully(
        self, auth_service, agent_registry
    ):
        """A registered agent with correct ownership should create a sandbox."""
        agent_registry.register(
            agent_id="agent-valid-1",
            owner_id="user-valid-1",
            zone_id="zone-alpha",
        )

        result = await auth_service.create_sandbox(
            agent_id="agent-valid-1",
            owner_id="user-valid-1",
            zone_id="zone-alpha",
            name="valid-sandbox",
        )

        assert result.sandbox["sandbox_id"] == "sb-test-001"
        assert result.sandbox["status"] == "active"
        assert result.agent_record.state == AgentState.CONNECTED
        assert result.agent_record.generation == 1

    async def test_agent_transitions_to_connected_state(
        self, auth_service, agent_registry
    ):
        """After sandbox creation, the agent should be in CONNECTED state in the registry."""
        agent_registry.register(
            agent_id="agent-transition-1",
            owner_id="user-1",
            zone_id="zone-1",
        )

        await auth_service.create_sandbox(
            agent_id="agent-transition-1",
            owner_id="user-1",
            zone_id="zone-1",
            name="transition-sandbox",
        )

        # Verify via direct registry lookup
        record = agent_registry.get("agent-transition-1")
        assert record is not None
        assert record.state == AgentState.CONNECTED
        assert record.generation == 1

    async def test_sandbox_manager_receives_correct_parameters(
        self, auth_service, agent_registry, mock_sandbox_manager
    ):
        """SandboxManager.create_sandbox should be called with the correct parameters."""
        agent_registry.register(
            agent_id="agent-params-1",
            owner_id="user-params-1",
            zone_id="zone-params",
        )

        await auth_service.create_sandbox(
            agent_id="agent-params-1",
            owner_id="user-params-1",
            zone_id="zone-params",
            name="params-sandbox",
            ttl_minutes=15,
            provider="docker",
            template_id="template-abc",
        )

        mock_sandbox_manager.create_sandbox.assert_awaited_once_with(
            name="params-sandbox",
            user_id="user-params-1",
            zone_id="zone-params",
            agent_id="agent-params-1",
            ttl_minutes=15,
            provider="docker",
            template_id="template-abc",
        )

    async def test_event_recorded_on_successful_creation(
        self, auth_service, agent_registry, event_log
    ):
        """A sandbox.created event should be recorded in the real event log."""
        agent_registry.register(
            agent_id="agent-event-1",
            owner_id="user-event-1",
            zone_id="zone-1",
        )

        await auth_service.create_sandbox(
            agent_id="agent-event-1",
            owner_id="user-event-1",
            zone_id="zone-1",
            name="event-sandbox",
        )

        events = event_log.list_events("agent-event-1")
        assert len(events) == 1
        assert events[0]["event_type"] == "sandbox.created"
        assert events[0]["payload"]["sandbox_id"] == "sb-test-001"


# ---------------------------------------------------------------------------
# Test: authorize agent access with unregistered agent (should fail)
# ---------------------------------------------------------------------------


class TestUnregisteredAgentAccess:
    """Test that unregistered agents are rejected before any sandbox operations."""

    async def test_unregistered_agent_raises_value_error(self, auth_service):
        """An agent not in the registry should raise ValueError."""
        with pytest.raises(ValueError, match="not found"):
            await auth_service.create_sandbox(
                agent_id="ghost-agent",
                owner_id="user-1",
                zone_id="zone-1",
                name="should-never-create",
            )

    async def test_unregistered_agent_never_calls_sandbox_manager(
        self, auth_service, mock_sandbox_manager
    ):
        """SandboxManager should never be invoked for unregistered agents."""
        with pytest.raises(ValueError):
            await auth_service.create_sandbox(
                agent_id="missing-agent",
                owner_id="user-1",
                zone_id="zone-1",
                name="should-never-create",
            )

        mock_sandbox_manager.create_sandbox.assert_not_awaited()

    async def test_no_events_recorded_for_unregistered_agent(
        self, auth_service, event_log
    ):
        """No events should be recorded when the agent is not found."""
        with pytest.raises(ValueError):
            await auth_service.create_sandbox(
                agent_id="phantom-agent",
                owner_id="user-1",
                zone_id="zone-1",
                name="should-never-create",
            )

        events = event_log.list_events("phantom-agent")
        assert len(events) == 0


# ---------------------------------------------------------------------------
# Test: authorize agent access with non-existent sandbox (should fail)
# ---------------------------------------------------------------------------


class TestNonExistentSandbox:
    """Test behavior when SandboxManager fails to create a sandbox."""

    async def test_sandbox_creation_failure_propagates_error(
        self, agent_registry, event_log
    ):
        """If SandboxManager.create_sandbox fails, the error should propagate."""
        failing_mgr = MagicMock()
        failing_mgr.create_sandbox = AsyncMock(
            side_effect=RuntimeError("Provider unavailable: no containers")
        )

        service = SandboxAuthService(
            agent_registry=agent_registry,
            sandbox_manager=failing_mgr,
            event_log=event_log,
        )

        agent_registry.register(
            agent_id="agent-sandbox-fail",
            owner_id="user-1",
            zone_id="zone-1",
        )

        with pytest.raises(RuntimeError, match="Provider unavailable"):
            await service.create_sandbox(
                agent_id="agent-sandbox-fail",
                owner_id="user-1",
                zone_id="zone-1",
                name="doomed-sandbox",
            )

    async def test_no_event_recorded_on_sandbox_failure(
        self, agent_registry, event_log
    ):
        """No events should be recorded when sandbox creation fails."""
        failing_mgr = MagicMock()
        failing_mgr.create_sandbox = AsyncMock(
            side_effect=RuntimeError("Docker daemon not running")
        )

        service = SandboxAuthService(
            agent_registry=agent_registry,
            sandbox_manager=failing_mgr,
            event_log=event_log,
        )

        agent_registry.register(
            agent_id="agent-no-event",
            owner_id="user-1",
            zone_id="zone-1",
        )

        with pytest.raises(RuntimeError):
            await service.create_sandbox(
                agent_id="agent-no-event",
                owner_id="user-1",
                zone_id="zone-1",
                name="doomed-sandbox",
            )

        events = event_log.list_events("agent-no-event")
        assert len(events) == 0

    async def test_agent_state_is_connected_even_after_sandbox_failure(
        self, agent_registry, event_log
    ):
        """Agent transitions to CONNECTED before sandbox creation.

        If sandbox creation fails, the agent remains CONNECTED because the
        auth service does not roll back the state transition. This is a known
        design trade-off: the caller should handle cleanup.
        """
        failing_mgr = MagicMock()
        failing_mgr.create_sandbox = AsyncMock(
            side_effect=RuntimeError("Container limit reached")
        )

        service = SandboxAuthService(
            agent_registry=agent_registry,
            sandbox_manager=failing_mgr,
            event_log=event_log,
        )

        agent_registry.register(
            agent_id="agent-orphan-state",
            owner_id="user-1",
            zone_id="zone-1",
        )

        with pytest.raises(RuntimeError):
            await service.create_sandbox(
                agent_id="agent-orphan-state",
                owner_id="user-1",
                zone_id="zone-1",
                name="doomed-sandbox",
            )

        # Agent was transitioned to CONNECTED before create_sandbox failed
        record = agent_registry.get("agent-orphan-state")
        assert record is not None
        assert record.state == AgentState.CONNECTED


# ---------------------------------------------------------------------------
# Test: authorize agent access with expired/suspended agent (should fail)
# ---------------------------------------------------------------------------


class TestExpiredSuspendedAgentAccess:
    """Test that suspended agents cannot create sandboxes.

    In the Agent OS state machine, SUSPENDED agents can only transition to
    CONNECTED. However, a CONNECTED agent cannot transition to CONNECTED again
    (no self-transitions). This means a SUSPENDED agent CAN create a sandbox
    because SUSPENDED -> CONNECTED is a valid transition.

    An agent that is ALREADY CONNECTED cannot create another sandbox because
    CONNECTED -> CONNECTED is NOT valid.
    """

    async def test_already_connected_agent_cannot_create_sandbox(
        self, auth_service, agent_registry
    ):
        """An agent already in CONNECTED state cannot create another sandbox.

        CONNECTED -> CONNECTED is not in the valid transitions table.
        """
        agent_registry.register(
            agent_id="agent-connected",
            owner_id="user-1",
            zone_id="zone-1",
        )
        # First transition to CONNECTED
        agent_registry.transition("agent-connected", AgentState.CONNECTED)

        with pytest.raises(InvalidTransitionError):
            await auth_service.create_sandbox(
                agent_id="agent-connected",
                owner_id="user-1",
                zone_id="zone-1",
                name="second-sandbox",
            )

    async def test_suspended_agent_can_create_sandbox(
        self, auth_service, agent_registry
    ):
        """A SUSPENDED agent CAN create a sandbox (SUSPENDED -> CONNECTED is valid).

        This is the reactivation path: SUSPENDED -> CONNECTED increments generation.
        """
        agent_registry.register(
            agent_id="agent-suspended",
            owner_id="user-1",
            zone_id="zone-1",
        )
        # UNKNOWN -> CONNECTED -> SUSPENDED
        agent_registry.transition("agent-suspended", AgentState.CONNECTED)
        agent_registry.transition("agent-suspended", AgentState.SUSPENDED)

        result = await auth_service.create_sandbox(
            agent_id="agent-suspended",
            owner_id="user-1",
            zone_id="zone-1",
            name="reactivated-sandbox",
        )

        assert result.agent_record.state == AgentState.CONNECTED
        # Generation should be 2 (incremented twice: UNKNOWN->CONNECTED, SUSPENDED->CONNECTED)
        assert result.agent_record.generation == 2

    async def test_idle_agent_can_create_sandbox(
        self, auth_service, agent_registry
    ):
        """An IDLE agent CAN create a sandbox (IDLE -> CONNECTED is valid).

        This is the reconnection path.
        """
        agent_registry.register(
            agent_id="agent-idle",
            owner_id="user-1",
            zone_id="zone-1",
        )
        # UNKNOWN -> CONNECTED -> IDLE
        agent_registry.transition("agent-idle", AgentState.CONNECTED)
        agent_registry.transition("agent-idle", AgentState.IDLE)

        result = await auth_service.create_sandbox(
            agent_id="agent-idle",
            owner_id="user-1",
            zone_id="zone-1",
            name="reconnected-sandbox",
        )

        assert result.agent_record.state == AgentState.CONNECTED
        # Generation should be 2 (incremented for each new session)
        assert result.agent_record.generation == 2

    async def test_no_sandbox_created_when_transition_fails(
        self, auth_service, agent_registry, mock_sandbox_manager
    ):
        """SandboxManager should not be called when state transition is invalid."""
        agent_registry.register(
            agent_id="agent-blocked",
            owner_id="user-1",
            zone_id="zone-1",
        )
        agent_registry.transition("agent-blocked", AgentState.CONNECTED)

        with pytest.raises(InvalidTransitionError):
            await auth_service.create_sandbox(
                agent_id="agent-blocked",
                owner_id="user-1",
                zone_id="zone-1",
                name="blocked-sandbox",
            )

        mock_sandbox_manager.create_sandbox.assert_not_awaited()


# ---------------------------------------------------------------------------
# Test: zone isolation (agent in zone A cannot access sandbox in zone B)
# ---------------------------------------------------------------------------


class TestZoneIsolation:
    """Test zone-based isolation for sandbox access.

    IMPORTANT: The current SandboxAuthService does NOT enforce that the
    zone_id passed to create_sandbox matches the agent's registered zone_id.
    The zone_id is simply passed through to SandboxManager. This means zone
    isolation is NOT enforced at the auth service layer.

    These tests document the current behavior. If zone isolation is added
    to the auth service later, these tests should be updated accordingly.
    """

    async def test_agent_can_create_sandbox_in_own_zone(
        self, auth_service, agent_registry
    ):
        """Agent in zone-alpha can create a sandbox with zone_id=zone-alpha."""
        agent_registry.register(
            agent_id="agent-zone-a",
            owner_id="user-1",
            zone_id="zone-alpha",
        )

        result = await auth_service.create_sandbox(
            agent_id="agent-zone-a",
            owner_id="user-1",
            zone_id="zone-alpha",
            name="same-zone-sandbox",
        )

        assert result.sandbox["sandbox_id"] == "sb-test-001"
        assert result.agent_record.state == AgentState.CONNECTED

    async def test_agent_can_request_sandbox_in_different_zone(
        self, auth_service, agent_registry
    ):
        """Currently, zone mismatch is NOT enforced at the auth service layer.

        An agent registered in zone-alpha can request a sandbox in zone-beta.
        The zone_id is passed through to SandboxManager without validation.
        This documents the current (permissive) behavior.
        """
        agent_registry.register(
            agent_id="agent-zone-mismatch",
            owner_id="user-1",
            zone_id="zone-alpha",
        )

        # This DOES NOT raise an error in the current implementation
        result = await auth_service.create_sandbox(
            agent_id="agent-zone-mismatch",
            owner_id="user-1",
            zone_id="zone-beta",  # Different from agent's zone-alpha
            name="cross-zone-sandbox",
        )

        assert result.sandbox["sandbox_id"] == "sb-test-001"

    async def test_sandbox_manager_receives_requested_zone_not_agent_zone(
        self, auth_service, agent_registry, mock_sandbox_manager
    ):
        """SandboxManager receives the requested zone_id, not the agent's zone_id.

        This documents that the auth service passes through whatever zone_id
        the caller provides, without cross-checking against the agent's zone.
        """
        agent_registry.register(
            agent_id="agent-zone-pass",
            owner_id="user-1",
            zone_id="zone-alpha",
        )

        await auth_service.create_sandbox(
            agent_id="agent-zone-pass",
            owner_id="user-1",
            zone_id="zone-beta",  # Different from agent's zone
            name="zone-passthrough-sandbox",
        )

        call_kwargs = mock_sandbox_manager.create_sandbox.call_args[1]
        assert call_kwargs["zone_id"] == "zone-beta"

    async def test_ownership_enforced_across_zones(
        self, auth_service, agent_registry
    ):
        """Ownership is enforced regardless of zone.

        Even when requesting a sandbox in a different zone, the agent must
        be owned by the requesting user.
        """
        agent_registry.register(
            agent_id="agent-cross-zone-owned",
            owner_id="user-A",
            zone_id="zone-alpha",
        )

        with pytest.raises(PermissionError, match="[Oo]wnership"):
            await auth_service.create_sandbox(
                agent_id="agent-cross-zone-owned",
                owner_id="user-B",  # Wrong owner
                zone_id="zone-beta",
                name="stolen-sandbox",
            )


# ---------------------------------------------------------------------------
# Test: full lifecycle (create -> stop -> events)
# ---------------------------------------------------------------------------


class TestFullLifecycle:
    """Test the complete sandbox lifecycle through the auth service."""

    async def test_create_then_stop(self, auth_service, agent_registry, event_log):
        """Create a sandbox, then stop it. Verify state transitions and events."""
        agent_registry.register(
            agent_id="agent-lifecycle",
            owner_id="user-lifecycle",
            zone_id="zone-1",
        )

        # Create sandbox
        create_result = await auth_service.create_sandbox(
            agent_id="agent-lifecycle",
            owner_id="user-lifecycle",
            zone_id="zone-1",
            name="lifecycle-sandbox",
        )
        assert create_result.sandbox["status"] == "active"
        assert create_result.agent_record.state == AgentState.CONNECTED

        # Stop sandbox
        stop_result = await auth_service.stop_sandbox(
            sandbox_id="sb-test-001",
            agent_id="agent-lifecycle",
        )
        assert stop_result["status"] == "stopped"

        # Agent should be IDLE after stop
        record = agent_registry.get("agent-lifecycle")
        assert record is not None
        assert record.state == AgentState.IDLE

        # Two events should be recorded: created + stopped
        events = event_log.list_events("agent-lifecycle")
        assert len(events) == 2
        event_types = {e["event_type"] for e in events}
        assert event_types == {"sandbox.created", "sandbox.stopped"}

    async def test_create_then_connect(self, auth_service, agent_registry, event_log):
        """Create a sandbox, then connect to it. Verify events."""
        agent_registry.register(
            agent_id="agent-connect",
            owner_id="user-connect",
            zone_id="zone-1",
        )

        await auth_service.create_sandbox(
            agent_id="agent-connect",
            owner_id="user-connect",
            zone_id="zone-1",
            name="connect-sandbox",
        )

        connect_result = await auth_service.connect_sandbox(
            sandbox_id="sb-test-001",
            agent_id="agent-connect",
            nexus_url="http://localhost:2026",
            nexus_api_key="test-key-123",
        )
        assert connect_result["success"] is True

        # Two events: created + connected
        events = event_log.list_events("agent-connect")
        assert len(events) == 2
        event_types = {e["event_type"] for e in events}
        assert event_types == {"sandbox.created", "sandbox.connected"}

    async def test_create_stop_then_recreate(
        self, auth_service, agent_registry, event_log
    ):
        """Create, stop, and recreate a sandbox. Generation should increment."""
        agent_registry.register(
            agent_id="agent-recreate",
            owner_id="user-recreate",
            zone_id="zone-1",
        )

        # First sandbox
        result1 = await auth_service.create_sandbox(
            agent_id="agent-recreate",
            owner_id="user-recreate",
            zone_id="zone-1",
            name="sandbox-v1",
        )
        assert result1.agent_record.generation == 1

        # Stop -> agent becomes IDLE
        await auth_service.stop_sandbox(
            sandbox_id="sb-test-001",
            agent_id="agent-recreate",
        )

        # Second sandbox -> IDLE -> CONNECTED, generation increments
        result2 = await auth_service.create_sandbox(
            agent_id="agent-recreate",
            owner_id="user-recreate",
            zone_id="zone-1",
            name="sandbox-v2",
        )
        assert result2.agent_record.generation == 2
        assert result2.agent_record.state == AgentState.CONNECTED

        # Should have 3 events: created, stopped, created
        events = event_log.list_events("agent-recreate")
        assert len(events) == 3


# ---------------------------------------------------------------------------
# Test: ownership validation
# ---------------------------------------------------------------------------


class TestOwnershipValidation:
    """Test that ownership is strictly enforced."""

    async def test_wrong_owner_raises_permission_error(
        self, auth_service, agent_registry
    ):
        """Creating a sandbox with the wrong owner_id should fail."""
        agent_registry.register(
            agent_id="agent-owned-by-alice",
            owner_id="alice",
            zone_id="zone-1",
        )

        with pytest.raises(PermissionError, match="[Oo]wnership"):
            await auth_service.create_sandbox(
                agent_id="agent-owned-by-alice",
                owner_id="bob",  # Not the owner
                zone_id="zone-1",
                name="bobs-sandbox",
            )

    async def test_wrong_owner_never_creates_sandbox(
        self, auth_service, agent_registry, mock_sandbox_manager
    ):
        """SandboxManager should not be called when ownership fails."""
        agent_registry.register(
            agent_id="agent-owned-by-carol",
            owner_id="carol",
            zone_id="zone-1",
        )

        with pytest.raises(PermissionError):
            await auth_service.create_sandbox(
                agent_id="agent-owned-by-carol",
                owner_id="dave",
                zone_id="zone-1",
                name="daves-sandbox",
            )

        mock_sandbox_manager.create_sandbox.assert_not_awaited()

    async def test_wrong_owner_does_not_transition_agent(
        self, auth_service, agent_registry
    ):
        """Agent state should not change when ownership validation fails."""
        agent_registry.register(
            agent_id="agent-no-transition",
            owner_id="eve",
            zone_id="zone-1",
        )

        with pytest.raises(PermissionError):
            await auth_service.create_sandbox(
                agent_id="agent-no-transition",
                owner_id="frank",
                zone_id="zone-1",
                name="franks-sandbox",
            )

        # Agent should still be in UNKNOWN state
        record = agent_registry.get("agent-no-transition")
        assert record is not None
        assert record.state == AgentState.UNKNOWN
        assert record.generation == 0


# ---------------------------------------------------------------------------
# Test: budget enforcement integration
# ---------------------------------------------------------------------------


class TestBudgetEnforcement:
    """Test budget checks with real registry."""

    async def test_budget_insufficient_blocks_creation(
        self, agent_registry, mock_sandbox_manager, event_log
    ):
        """Budget check failure should prevent sandbox creation."""
        mock_pay = MagicMock()
        mock_pay.can_afford = AsyncMock(return_value=False)

        service = SandboxAuthService(
            agent_registry=agent_registry,
            sandbox_manager=mock_sandbox_manager,
            event_log=event_log,
            nexus_pay=mock_pay,
            budget_enforcement=True,
        )

        agent_registry.register(
            agent_id="agent-broke",
            owner_id="user-1",
            zone_id="zone-1",
        )

        with pytest.raises(ValueError, match="[Bb]udget"):
            await service.create_sandbox(
                agent_id="agent-broke",
                owner_id="user-1",
                zone_id="zone-1",
                name="expensive-sandbox",
            )

        mock_sandbox_manager.create_sandbox.assert_not_awaited()

    async def test_budget_sufficient_allows_creation(
        self, agent_registry, mock_sandbox_manager, event_log
    ):
        """Sufficient budget should allow sandbox creation with budget_checked=True."""
        mock_pay = MagicMock()
        mock_pay.can_afford = AsyncMock(return_value=True)

        service = SandboxAuthService(
            agent_registry=agent_registry,
            sandbox_manager=mock_sandbox_manager,
            event_log=event_log,
            nexus_pay=mock_pay,
            budget_enforcement=True,
        )

        agent_registry.register(
            agent_id="agent-funded",
            owner_id="user-1",
            zone_id="zone-1",
        )

        result = await service.create_sandbox(
            agent_id="agent-funded",
            owner_id="user-1",
            zone_id="zone-1",
            name="funded-sandbox",
        )

        assert result.budget_checked is True
        assert result.sandbox["sandbox_id"] == "sb-test-001"

    async def test_budget_not_checked_when_disabled(
        self, auth_service, agent_registry
    ):
        """Budget should not be checked when enforcement is disabled."""
        agent_registry.register(
            agent_id="agent-free",
            owner_id="user-1",
            zone_id="zone-1",
        )

        result = await auth_service.create_sandbox(
            agent_id="agent-free",
            owner_id="user-1",
            zone_id="zone-1",
            name="free-sandbox",
        )

        assert result.budget_checked is False


# ---------------------------------------------------------------------------
# Test: without optional dependencies
# ---------------------------------------------------------------------------


class TestWithoutOptionalDependencies:
    """Test behavior when optional dependencies are not provided."""

    async def test_create_without_event_log(
        self, agent_registry, mock_sandbox_manager
    ):
        """Sandbox creation should work without an event log."""
        service = SandboxAuthService(
            agent_registry=agent_registry,
            sandbox_manager=mock_sandbox_manager,
            event_log=None,
        )

        agent_registry.register(
            agent_id="agent-no-log",
            owner_id="user-1",
            zone_id="zone-1",
        )

        result = await service.create_sandbox(
            agent_id="agent-no-log",
            owner_id="user-1",
            zone_id="zone-1",
            name="no-log-sandbox",
        )

        assert result.sandbox["sandbox_id"] == "sb-test-001"

    async def test_create_without_namespace_manager(
        self, agent_registry, mock_sandbox_manager, event_log
    ):
        """Sandbox creation should work without a namespace manager."""
        service = SandboxAuthService(
            agent_registry=agent_registry,
            sandbox_manager=mock_sandbox_manager,
            namespace_manager=None,
            event_log=event_log,
        )

        agent_registry.register(
            agent_id="agent-no-ns",
            owner_id="user-1",
            zone_id="zone-1",
        )

        result = await service.create_sandbox(
            agent_id="agent-no-ns",
            owner_id="user-1",
            zone_id="zone-1",
            name="no-ns-sandbox",
        )

        assert result.mount_table == []
        assert result.sandbox["sandbox_id"] == "sb-test-001"

    async def test_stop_without_event_log(
        self, agent_registry, mock_sandbox_manager
    ):
        """Stopping a sandbox should work without an event log."""
        service = SandboxAuthService(
            agent_registry=agent_registry,
            sandbox_manager=mock_sandbox_manager,
            event_log=None,
        )

        agent_registry.register(
            agent_id="agent-stop-no-log",
            owner_id="user-1",
            zone_id="zone-1",
        )

        # Create first (to transition to CONNECTED)
        await service.create_sandbox(
            agent_id="agent-stop-no-log",
            owner_id="user-1",
            zone_id="zone-1",
            name="stop-no-log-sandbox",
        )

        # Stop should work without event log
        result = await service.stop_sandbox(
            sandbox_id="sb-test-001",
            agent_id="agent-stop-no-log",
        )

        assert result["status"] == "stopped"
