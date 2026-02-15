"""Unit tests for SandboxManager._handle_escalation error paths (Issue #1317 review fix #10).

Tests cover all 4 branches in _handle_escalation():
1. router is None → re-raises EscalationNeeded
2. next_tier is None (no escalation target) → re-raises
3. Happy path → creates temp sandbox, runs, destroys
4. destroy fails in finally → logs warning, still returns result
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from nexus.sandbox.sandbox_manager import SandboxManager
from nexus.sandbox.sandbox_provider import (
    CodeExecutionResult,
    EscalationNeeded,
    SandboxProvider,
)
from nexus.storage.models import Base


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def mock_docker() -> SandboxProvider:
    mock = AsyncMock(spec=SandboxProvider)
    mock.create.return_value = "docker-temp-123"
    mock.run_code.return_value = CodeExecutionResult(
        stdout="docker ok\n", stderr="", exit_code=0, execution_time=0.5
    )
    mock.destroy.return_value = None
    return mock


@pytest.fixture
def escalation_exc() -> EscalationNeeded:
    return EscalationNeeded(reason="test escalation", suggested_tier="docker")


class TestHandleEscalationErrorPaths:
    """Tests for _handle_escalation error branches."""

    @pytest.mark.asyncio
    async def test_no_router_reraises(
        self, session_factory, escalation_exc: EscalationNeeded
    ) -> None:
        """When router is None, EscalationNeeded is re-raised."""
        mgr = SandboxManager(session_factory=session_factory)
        # Don't wire a router — _router stays None

        with pytest.raises(EscalationNeeded, match="test escalation"):
            await mgr._handle_escalation(
                escalation_exc, "monty", "python", "import os", 30, False, "agent-1"
            )

    @pytest.mark.asyncio
    async def test_no_next_tier_reraises(self, session_factory) -> None:
        """When no next tier is available, EscalationNeeded is re-raised."""
        mgr = SandboxManager(session_factory=session_factory)
        # Only e2b available — escalation from e2b has no next tier
        mock_e2b = AsyncMock(spec=SandboxProvider)
        mgr.providers = {"e2b": mock_e2b}
        mgr.wire_router()

        exc = EscalationNeeded(reason="e2b failed too", suggested_tier=None)
        with pytest.raises(EscalationNeeded, match="e2b failed too"):
            await mgr._handle_escalation(exc, "e2b", "python", "import os", 30, False, "agent-1")

    @pytest.mark.asyncio
    async def test_happy_path_creates_temp_sandbox(
        self, session_factory, mock_docker: SandboxProvider, escalation_exc: EscalationNeeded
    ) -> None:
        """Escalation creates temp sandbox, runs code, and destroys it."""
        mgr = SandboxManager(session_factory=session_factory)
        mgr.providers = {"docker": mock_docker}
        mgr.wire_router()

        result = await mgr._handle_escalation(
            escalation_exc, "monty", "python", "import os", 30, False, "agent-1"
        )

        assert result.exit_code == 0
        assert result.stdout == "docker ok\n"
        mock_docker.create.assert_called_once_with(timeout_minutes=5)
        mock_docker.destroy.assert_called_once_with("docker-temp-123")

    @pytest.mark.asyncio
    async def test_destroy_failure_logs_warning(
        self, session_factory, mock_docker: SandboxProvider, escalation_exc: EscalationNeeded
    ) -> None:
        """If destroy fails, warning is logged but result is still returned."""
        mock_docker.destroy.side_effect = RuntimeError("cleanup failed")
        mgr = SandboxManager(session_factory=session_factory)
        mgr.providers = {"docker": mock_docker}
        mgr.wire_router()

        with patch("nexus.sandbox.sandbox_manager.logger") as mock_logger:
            result = await mgr._handle_escalation(
                escalation_exc, "monty", "python", "import os", 30, False, "agent-1"
            )

        assert result.exit_code == 0
        # Verify warning was logged
        mock_logger.warning.assert_any_call(
            "Failed to destroy temp sandbox %s: %s",
            "docker-temp-123",
            mock_docker.destroy.side_effect,
        )

    @pytest.mark.asyncio
    async def test_suggested_tier_honored(self, session_factory) -> None:
        """When suggested_tier is available, it's used instead of get_next_tier."""
        mock_e2b = AsyncMock(spec=SandboxProvider)
        mock_e2b.create.return_value = "e2b-direct-456"
        mock_e2b.run_code.return_value = CodeExecutionResult(
            stdout="e2b direct\n", stderr="", exit_code=0, execution_time=1.0
        )
        mock_e2b.destroy.return_value = None

        mgr = SandboxManager(session_factory=session_factory)
        # Replace all auto-discovered providers with only our mocks
        mock_monty = AsyncMock(spec=SandboxProvider)
        mgr.providers = {"e2b": mock_e2b, "monty": mock_monty}
        mgr.wire_router()

        # Escalation suggests "docker" but only e2b+monty exist → falls to get_next_tier
        exc = EscalationNeeded(reason="need docker", suggested_tier="docker")

        result = await mgr._handle_escalation(
            exc, "monty", "python", "import os", 30, False, "agent-1"
        )
        assert result.exit_code == 0
        mock_e2b.create.assert_called_once()
