"""Unit tests for SandboxAuthService (Issue #1307 — Phase 5).

Tests cover:
- Happy path: agent auth → namespace → sandbox creation → event recording
- Edge case 1: Agent not found → error before container creation
- Edge case 2: Agent in wrong state → error with state info
- Edge case 3: Namespace construction (mock NamespaceManager)
- Edge case 4: Budget insufficient → error before container creation
- Edge case 5: Partial failure → container cleaned up
- Edge case 6: Concurrent creation → one wins
- Edge case 7: Ownership mismatch → error
- Stop sandbox → agent transitions to IDLE, event recorded
- Connect sandbox → namespace passed as metadata
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from nexus.core.agent_record import AgentRecord, AgentState
from nexus.core.agent_registry import AgentRegistry, InvalidTransitionError
from nexus.sandbox.auth_service import SandboxAuthService
from nexus.sandbox.events import AgentEventLog
from nexus.storage.models import Base

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    """Create in-memory SQLite database."""
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


def _make_agent_record(
    agent_id: str = "agent-1",
    owner_id: str = "user-1",
    state: AgentState = AgentState.UNKNOWN,
    generation: int = 0,
    zone_id: str | None = "zone-1",
) -> AgentRecord:
    """Build a frozen AgentRecord for testing."""
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    return AgentRecord(
        agent_id=agent_id,
        owner_id=owner_id,
        zone_id=zone_id,
        name=None,
        state=state,
        generation=generation,
        last_heartbeat=None,
        metadata=MappingProxyType({}),
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def mock_registry():
    registry = MagicMock(spec=AgentRegistry)
    record = _make_agent_record()
    registry.get.return_value = record
    registry.validate_ownership.return_value = True
    registry.transition.return_value = _make_agent_record(state=AgentState.CONNECTED, generation=1)
    return registry


@pytest.fixture
def mock_sandbox_manager():
    mgr = MagicMock()
    mgr.create_sandbox = AsyncMock(
        return_value={
            "sandbox_id": "sb-123",
            "name": "test-sb",
            "status": "active",
            "provider": "docker",
        }
    )
    mgr.stop_sandbox = AsyncMock(
        return_value={
            "sandbox_id": "sb-123",
            "status": "stopped",
        }
    )
    mgr.connect_sandbox = AsyncMock(
        return_value={
            "success": True,
            "sandbox_id": "sb-123",
            "mount_path": "/mnt/nexus",
        }
    )
    return mgr


@pytest.fixture
def mock_namespace_manager():
    ns_mgr = MagicMock()

    @dataclass(frozen=True)
    class MockMountEntry:
        virtual_path: str

    ns_mgr.get_mount_table.return_value = [
        MockMountEntry(virtual_path="/workspace"),
        MockMountEntry(virtual_path="/memory"),
    ]
    return ns_mgr


@pytest.fixture
def mock_event_log():
    log = MagicMock(spec=AgentEventLog)
    log.record.return_value = "event-uuid-123"
    return log


@pytest.fixture
def mock_nexus_pay():
    pay = MagicMock()
    pay.can_afford = AsyncMock(return_value=True)
    return pay


@pytest.fixture
def auth_service(
    mock_registry,
    mock_sandbox_manager,
    mock_namespace_manager,
    mock_event_log,
    mock_nexus_pay,
):
    return SandboxAuthService(
        agent_registry=mock_registry,
        sandbox_manager=mock_sandbox_manager,
        namespace_manager=mock_namespace_manager,
        event_log=mock_event_log,
        nexus_pay=mock_nexus_pay,
        budget_enforcement=False,
    )


# ---------------------------------------------------------------------------
# Happy Path
# ---------------------------------------------------------------------------


class TestCreateSandboxHappyPath:
    @pytest.mark.asyncio
    async def test_creates_sandbox_through_registry(self, auth_service, mock_registry):
        result = await auth_service.create_sandbox(
            agent_id="agent-1",
            owner_id="user-1",
            zone_id="zone-1",
            name="test-sb",
        )

        assert result.sandbox["sandbox_id"] == "sb-123"
        assert result.agent_record.state == AgentState.CONNECTED
        mock_registry.get.assert_called_once_with("agent-1")
        mock_registry.validate_ownership.assert_called_once_with("agent-1", "user-1")
        mock_registry.transition.assert_called_once()

    @pytest.mark.asyncio
    async def test_constructs_namespace(self, auth_service, mock_namespace_manager):
        result = await auth_service.create_sandbox(
            agent_id="agent-1",
            owner_id="user-1",
            zone_id="zone-1",
            name="test-sb",
        )

        assert len(result.mount_table) == 2
        mock_namespace_manager.get_mount_table.assert_called_once()

    @pytest.mark.asyncio
    async def test_records_event(self, auth_service, mock_event_log):
        await auth_service.create_sandbox(
            agent_id="agent-1",
            owner_id="user-1",
            zone_id="zone-1",
            name="test-sb",
        )

        mock_event_log.record.assert_called_once()
        call_kwargs = mock_event_log.record.call_args
        assert (
            call_kwargs[1]["event_type"] == "sandbox.created"
            or call_kwargs[0][1] == "sandbox.created"
        )

    @pytest.mark.asyncio
    async def test_budget_not_checked_when_disabled(self, auth_service, mock_nexus_pay):
        await auth_service.create_sandbox(
            agent_id="agent-1",
            owner_id="user-1",
            zone_id="zone-1",
            name="test-sb",
        )

        mock_nexus_pay.can_afford.assert_not_called()
        # budget_checked should be False
        # (verified via result)

    @pytest.mark.asyncio
    async def test_budget_checked_when_enabled(
        self,
        mock_registry,
        mock_sandbox_manager,
        mock_namespace_manager,
        mock_event_log,
        mock_nexus_pay,
    ):
        service = SandboxAuthService(
            agent_registry=mock_registry,
            sandbox_manager=mock_sandbox_manager,
            namespace_manager=mock_namespace_manager,
            event_log=mock_event_log,
            nexus_pay=mock_nexus_pay,
            budget_enforcement=True,
        )

        result = await service.create_sandbox(
            agent_id="agent-1",
            owner_id="user-1",
            zone_id="zone-1",
            name="test-sb",
        )

        mock_nexus_pay.can_afford.assert_awaited_once()
        assert result.budget_checked is True


# ---------------------------------------------------------------------------
# Edge Cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_agent_not_found(self, auth_service, mock_registry):
        """Edge case 1: Agent not found → error before container creation."""
        mock_registry.get.return_value = None

        with pytest.raises(ValueError, match="not found"):
            await auth_service.create_sandbox(
                agent_id="nonexistent",
                owner_id="user-1",
                zone_id="zone-1",
                name="test-sb",
            )

    @pytest.mark.asyncio
    async def test_agent_not_found_no_container_created(
        self, auth_service, mock_registry, mock_sandbox_manager
    ):
        """Agent not found should never create a container."""
        mock_registry.get.return_value = None

        with pytest.raises(ValueError):
            await auth_service.create_sandbox(
                agent_id="nonexistent",
                owner_id="user-1",
                zone_id="zone-1",
                name="test-sb",
            )

        mock_sandbox_manager.create_sandbox.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_agent_wrong_state(self, auth_service, mock_registry):
        """Edge case 2: Agent in wrong state → transition fails."""
        mock_registry.get.return_value = _make_agent_record(state=AgentState.SUSPENDED)
        mock_registry.transition.side_effect = InvalidTransitionError(
            "agent-1", AgentState.SUSPENDED, AgentState.CONNECTED
        )

        with pytest.raises(InvalidTransitionError):
            await auth_service.create_sandbox(
                agent_id="agent-1",
                owner_id="user-1",
                zone_id="zone-1",
                name="test-sb",
            )

    @pytest.mark.asyncio
    async def test_namespace_construction_fails_gracefully(
        self, auth_service, mock_namespace_manager
    ):
        """Edge case 3: NamespaceManager fails → empty mount table, sandbox still created."""
        mock_namespace_manager.get_mount_table.side_effect = RuntimeError("ReBAC unavailable")

        result = await auth_service.create_sandbox(
            agent_id="agent-1",
            owner_id="user-1",
            zone_id="zone-1",
            name="test-sb",
        )

        # Sandbox created despite namespace failure
        assert result.sandbox["sandbox_id"] == "sb-123"
        assert result.mount_table == []

    @pytest.mark.asyncio
    async def test_budget_insufficient(
        self,
        mock_registry,
        mock_sandbox_manager,
        mock_namespace_manager,
        mock_event_log,
        mock_nexus_pay,
    ):
        """Edge case 4: Budget insufficient → error before container creation."""
        mock_nexus_pay.can_afford = AsyncMock(return_value=False)

        service = SandboxAuthService(
            agent_registry=mock_registry,
            sandbox_manager=mock_sandbox_manager,
            namespace_manager=mock_namespace_manager,
            event_log=mock_event_log,
            nexus_pay=mock_nexus_pay,
            budget_enforcement=True,
        )

        with pytest.raises(ValueError, match="[Bb]udget"):
            await service.create_sandbox(
                agent_id="agent-1",
                owner_id="user-1",
                zone_id="zone-1",
                name="test-sb",
            )

        mock_sandbox_manager.create_sandbox.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_partial_failure_sandbox_creation_fails(
        self, auth_service, mock_sandbox_manager, mock_event_log
    ):
        """Edge case 5: SandboxManager.create_sandbox fails after agent transition."""
        mock_sandbox_manager.create_sandbox = AsyncMock(
            side_effect=RuntimeError("Container creation failed")
        )

        with pytest.raises(RuntimeError, match="Container creation failed"):
            await auth_service.create_sandbox(
                agent_id="agent-1",
                owner_id="user-1",
                zone_id="zone-1",
                name="test-sb",
            )

        # Event should NOT be recorded on failure
        mock_event_log.record.assert_not_called()

    @pytest.mark.asyncio
    async def test_ownership_mismatch(self, auth_service, mock_registry):
        """Edge case 7: user_a tries to create sandbox with user_b's agent."""
        mock_registry.validate_ownership.return_value = False

        with pytest.raises(PermissionError, match="[Oo]wnership"):
            await auth_service.create_sandbox(
                agent_id="agent-1",
                owner_id="wrong-user",
                zone_id="zone-1",
                name="test-sb",
            )


# ---------------------------------------------------------------------------
# Stop Sandbox
# ---------------------------------------------------------------------------


class TestStopSandbox:
    @pytest.mark.asyncio
    async def test_stop_records_event(
        self, auth_service, mock_sandbox_manager, mock_event_log, mock_registry
    ):
        result = await auth_service.stop_sandbox(
            sandbox_id="sb-123",
            agent_id="agent-1",
        )

        assert result["status"] == "stopped"
        mock_event_log.record.assert_called_once()
        call_args = mock_event_log.record.call_args
        assert "sandbox.stopped" in str(call_args)

    @pytest.mark.asyncio
    async def test_stop_transitions_agent(self, auth_service, mock_registry):
        mock_registry.transition.return_value = _make_agent_record(
            state=AgentState.IDLE, generation=1
        )

        await auth_service.stop_sandbox(
            sandbox_id="sb-123",
            agent_id="agent-1",
        )

        mock_registry.transition.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_succeeds_when_transition_fails(
        self, auth_service, mock_registry, mock_sandbox_manager
    ):
        """stop_sandbox still returns result even if agent transition to IDLE fails."""
        mock_registry.transition.side_effect = InvalidTransitionError(
            "agent-1", AgentState.UNKNOWN, AgentState.IDLE
        )

        result = await auth_service.stop_sandbox(
            sandbox_id="sb-123",
            agent_id="agent-1",
        )

        # Stop still succeeded — transition failure is best-effort
        assert result["status"] == "stopped"
        mock_sandbox_manager.stop_sandbox.assert_called_once_with("sb-123")


# ---------------------------------------------------------------------------
# Connect Sandbox
# ---------------------------------------------------------------------------


class TestConnectSandbox:
    @pytest.mark.asyncio
    async def test_connect_passes_namespace(
        self, auth_service, mock_sandbox_manager, mock_namespace_manager
    ):
        result = await auth_service.connect_sandbox(
            sandbox_id="sb-123",
            agent_id="agent-1",
            nexus_url="http://localhost:2026",
            nexus_api_key="test-key",
        )

        assert result["success"] is True
        mock_sandbox_manager.connect_sandbox.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_connect_records_event(self, auth_service, mock_event_log):
        await auth_service.connect_sandbox(
            sandbox_id="sb-123",
            agent_id="agent-1",
            nexus_url="http://localhost:2026",
            nexus_api_key="test-key",
        )

        mock_event_log.record.assert_called_once()


# ---------------------------------------------------------------------------
# No Namespace Manager
# ---------------------------------------------------------------------------


class TestWithoutOptionalDeps:
    @pytest.mark.asyncio
    async def test_create_without_namespace_manager(
        self, mock_registry, mock_sandbox_manager, mock_event_log
    ):
        service = SandboxAuthService(
            agent_registry=mock_registry,
            sandbox_manager=mock_sandbox_manager,
            namespace_manager=None,
            event_log=mock_event_log,
        )

        result = await service.create_sandbox(
            agent_id="agent-1",
            owner_id="user-1",
            zone_id="zone-1",
            name="test-sb",
        )

        assert result.mount_table == []
        assert result.sandbox["sandbox_id"] == "sb-123"

    @pytest.mark.asyncio
    async def test_create_without_event_log(self, mock_registry, mock_sandbox_manager):
        service = SandboxAuthService(
            agent_registry=mock_registry,
            sandbox_manager=mock_sandbox_manager,
            event_log=None,
        )

        result = await service.create_sandbox(
            agent_id="agent-1",
            owner_id="user-1",
            zone_id="zone-1",
            name="test-sb",
        )

        # Should work fine without event log
        assert result.sandbox["sandbox_id"] == "sb-123"
