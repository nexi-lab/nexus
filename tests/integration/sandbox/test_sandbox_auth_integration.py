"""Integration tests for SandboxAuthService (Issue #1307).

Uses REAL AgentRegistry and AgentEventLog with in-memory SQLite,
and a MOCK SandboxManager (requires Docker/E2B providers in production).

Verifies the full pipeline:
    register agent → create sandbox → verify state transitions → verify events
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from nexus.sandbox.auth_service import SandboxAuthService
from nexus.sandbox.events import AgentEventLog
from nexus.services.agents.agent_record import AgentState
from nexus.services.agents.agent_registry import AgentRegistry, InvalidTransitionError
from nexus.storage.models import Base

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    """Create in-memory SQLite database with all tables."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def session_factory(engine):
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
    """Mock SandboxManager (providers need Docker/E2B)."""
    mgr = MagicMock()
    mgr.create_sandbox = AsyncMock(
        return_value={
            "sandbox_id": "sb-integ-001",
            "name": "integration-sandbox",
            "status": "active",
            "provider": "docker",
        }
    )
    mgr.stop_sandbox = AsyncMock(
        return_value={
            "sandbox_id": "sb-integ-001",
            "status": "stopped",
        }
    )
    mgr.connect_sandbox = AsyncMock(
        return_value={
            "success": True,
            "sandbox_id": "sb-integ-001",
            "mount_path": "/mnt/nexus",
        }
    )
    return mgr


@pytest.fixture
def auth_service(agent_registry, mock_sandbox_manager, event_log):
    """SandboxAuthService wired with real registry + real event log."""
    return SandboxAuthService(
        agent_registry=agent_registry,
        sandbox_manager=mock_sandbox_manager,
        namespace_manager=None,
        event_log=event_log,
        budget_enforcement=False,
    )


# ---------------------------------------------------------------------------
# Full Pipeline Tests
# ---------------------------------------------------------------------------


class TestFullPipeline:
    """End-to-end pipeline: register → create → verify."""

    @pytest.mark.asyncio
    async def test_register_then_create_sandbox(self, auth_service, agent_registry, event_log):
        """Full pipeline: register agent, create sandbox, verify state + events."""
        # Step 1: Register agent (starts in UNKNOWN state)
        record = agent_registry.register(
            agent_id="agent-integ-1",
            owner_id="user-integ-1",
            zone_id="zone-1",
        )
        assert record.state == AgentState.UNKNOWN
        assert record.generation == 0

        # Step 2: Create sandbox through auth service
        result = await auth_service.create_sandbox(
            agent_id="agent-integ-1",
            owner_id="user-integ-1",
            zone_id="zone-1",
            name="test-sandbox",
        )

        # Step 3: Verify sandbox was created
        assert result.sandbox["sandbox_id"] == "sb-integ-001"
        assert result.sandbox["status"] == "active"

        # Step 4: Verify agent transitioned to CONNECTED with incremented generation
        assert result.agent_record.state == AgentState.CONNECTED
        assert result.agent_record.generation == 1

        # Step 5: Verify event was recorded in the real event log
        events = event_log.list_events("agent-integ-1")
        assert len(events) == 1
        assert events[0]["event_type"] == "sandbox.created"
        assert events[0]["payload"]["sandbox_id"] == "sb-integ-001"

    @pytest.mark.asyncio
    async def test_create_then_stop_sandbox(self, auth_service, agent_registry, event_log):
        """Pipeline: register → create → stop → verify IDLE state + 2 events."""
        # Register and create
        agent_registry.register(
            agent_id="agent-integ-2",
            owner_id="user-integ-1",
            zone_id="zone-1",
        )
        await auth_service.create_sandbox(
            agent_id="agent-integ-2",
            owner_id="user-integ-1",
            zone_id="zone-1",
            name="stop-test-sandbox",
        )

        # Stop sandbox
        stop_result = await auth_service.stop_sandbox(
            sandbox_id="sb-integ-001",
            agent_id="agent-integ-2",
        )
        assert stop_result["status"] == "stopped"

        # Agent should be IDLE after stop
        agent = agent_registry.get("agent-integ-2")
        assert agent is not None
        assert agent.state == AgentState.IDLE

        # Should have 2 events: created + stopped
        events = event_log.list_events("agent-integ-2")
        assert len(events) == 2
        event_types = {e["event_type"] for e in events}
        assert event_types == {"sandbox.created", "sandbox.stopped"}

    @pytest.mark.asyncio
    async def test_connect_sandbox_records_event(self, auth_service, agent_registry, event_log):
        """Pipeline: register → create → connect → verify 2 events."""
        agent_registry.register(
            agent_id="agent-integ-3",
            owner_id="user-integ-1",
            zone_id="zone-1",
        )
        await auth_service.create_sandbox(
            agent_id="agent-integ-3",
            owner_id="user-integ-1",
            zone_id="zone-1",
            name="connect-test-sandbox",
        )

        # Connect sandbox
        connect_result = await auth_service.connect_sandbox(
            sandbox_id="sb-integ-001",
            agent_id="agent-integ-3",
            nexus_url="http://localhost:2026",
            nexus_api_key="test-key",
        )
        assert connect_result["success"] is True

        # Should have 2 events: created + connected
        events = event_log.list_events("agent-integ-3")
        assert len(events) == 2
        event_types = {e["event_type"] for e in events}
        assert event_types == {"sandbox.created", "sandbox.connected"}


# ---------------------------------------------------------------------------
# Error Path Integration Tests
# ---------------------------------------------------------------------------


class TestErrorPathIntegration:
    """Error paths with real registry (not mocks)."""

    @pytest.mark.asyncio
    async def test_unregistered_agent_raises(self, auth_service):
        """Agent not in registry → ValueError before sandbox creation."""
        with pytest.raises(ValueError, match="not found"):
            await auth_service.create_sandbox(
                agent_id="nonexistent-agent",
                owner_id="user-1",
                zone_id="zone-1",
                name="should-fail",
            )

    @pytest.mark.asyncio
    async def test_wrong_owner_raises(self, auth_service, agent_registry):
        """Agent owned by user-A, but user-B tries to create sandbox."""
        agent_registry.register(
            agent_id="agent-owned",
            owner_id="user-A",
            zone_id="zone-1",
        )

        with pytest.raises(PermissionError, match="[Oo]wnership"):
            await auth_service.create_sandbox(
                agent_id="agent-owned",
                owner_id="user-B",
                zone_id="zone-1",
                name="should-fail",
            )

    @pytest.mark.asyncio
    async def test_already_connected_agent_cannot_create_sandbox(
        self, auth_service, agent_registry
    ):
        """CONNECTED → CONNECTED is invalid (no self-transitions)."""
        agent_registry.register(
            agent_id="agent-connected",
            owner_id="user-1",
            zone_id="zone-1",
        )
        # Transition: UNKNOWN → CONNECTED
        agent_registry.transition("agent-connected", AgentState.CONNECTED)

        # Trying to create a sandbox when already CONNECTED fails
        # (CONNECTED → CONNECTED is not in VALID_TRANSITIONS)
        with pytest.raises(InvalidTransitionError):
            await auth_service.create_sandbox(
                agent_id="agent-connected",
                owner_id="user-1",
                zone_id="zone-1",
                name="should-fail",
            )

    @pytest.mark.asyncio
    async def test_sandbox_failure_no_event_recorded(self, agent_registry, event_log):
        """If SandboxManager.create_sandbox fails, no event should be recorded."""
        failing_sandbox_mgr = MagicMock()
        failing_sandbox_mgr.create_sandbox = AsyncMock(
            side_effect=RuntimeError("Provider unavailable")
        )

        service = SandboxAuthService(
            agent_registry=agent_registry,
            sandbox_manager=failing_sandbox_mgr,
            event_log=event_log,
        )

        agent_registry.register(
            agent_id="agent-fail",
            owner_id="user-1",
            zone_id="zone-1",
        )

        with pytest.raises(RuntimeError, match="Provider unavailable"):
            await service.create_sandbox(
                agent_id="agent-fail",
                owner_id="user-1",
                zone_id="zone-1",
                name="should-fail",
            )

        # No events should have been recorded
        events = event_log.list_events("agent-fail")
        assert len(events) == 0


# ---------------------------------------------------------------------------
# Budget Enforcement Integration
# ---------------------------------------------------------------------------


class TestBudgetEnforcementIntegration:
    @pytest.mark.asyncio
    async def test_budget_check_blocks_sandbox_creation(
        self, agent_registry, mock_sandbox_manager, event_log
    ):
        """Budget check with real registry — insufficient budget blocks creation."""
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
                name="should-fail",
            )

        # Sandbox manager should never be called
        mock_sandbox_manager.create_sandbox.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_budget_check_allows_sandbox_creation(
        self, agent_registry, mock_sandbox_manager, event_log
    ):
        """Budget check passes → sandbox created and budget_checked=True."""
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
        assert result.sandbox["sandbox_id"] == "sb-integ-001"
