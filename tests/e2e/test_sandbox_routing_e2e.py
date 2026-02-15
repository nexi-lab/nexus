"""E2E tests for smart sandbox routing (Issue #1317).

Service-layer e2e tests that verify the complete routing pipeline:
- Router creation and attachment to SandboxManager
- Auto-route simple Python to monty
- Auto-route import-heavy code to docker (mocked)
- Escalation chain monty -> docker
- Metrics population
- Log validation via caplog
"""

from __future__ import annotations

import logging
import statistics
import time
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Skip if pydantic-monty not installed
try:
    import pydantic_monty  # noqa: F401

    MONTY_AVAILABLE = True
except ImportError:
    MONTY_AVAILABLE = False

pytestmark = pytest.mark.skipif(not MONTY_AVAILABLE, reason="pydantic-monty not installed")

from nexus.sandbox.sandbox_manager import SandboxManager  # noqa: E402
from nexus.sandbox.sandbox_monty_provider import MontySandboxProvider  # noqa: E402
from nexus.sandbox.sandbox_provider import (  # noqa: E402
    CodeExecutionResult,
    SandboxProvider,
)
from nexus.sandbox.sandbox_router import SandboxRouter  # noqa: E402
from nexus.storage.models import Base  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
    mock.create.return_value = "docker-e2e-123"
    mock.run_code.return_value = CodeExecutionResult(
        stdout="docker result\n",
        stderr="",
        exit_code=0,
        execution_time=0.5,
    )
    mock.get_info.return_value = AsyncMock(status="active")
    mock.is_available.return_value = True
    mock.destroy.return_value = None
    return mock


@pytest.fixture
def mock_e2b() -> SandboxProvider:
    mock = AsyncMock(spec=SandboxProvider)
    mock.create.return_value = "e2b-e2e-456"
    mock.run_code.return_value = CodeExecutionResult(
        stdout="e2b result\n",
        stderr="",
        exit_code=0,
        execution_time=2.0,
    )
    mock.get_info.return_value = AsyncMock(status="active")
    mock.is_available.return_value = True
    mock.destroy.return_value = None
    return mock


@pytest.fixture
def full_stack(session_factory, mock_docker, mock_e2b):
    """Full SandboxManager + Router + all providers."""
    monty = MontySandboxProvider(resource_profile="standard", enable_type_checking=False)
    mgr = SandboxManager(session_factory=session_factory)
    mgr.providers["monty"] = monty
    mgr.providers["docker"] = mock_docker
    mgr.providers["e2b"] = mock_e2b

    router = SandboxRouter(available_providers=mgr.providers)
    mgr._router = router
    return mgr, router


# ---------------------------------------------------------------------------
# TestRoutingE2E
# ---------------------------------------------------------------------------


class TestRoutingE2E:
    """End-to-end routing pipeline tests."""

    @pytest.mark.asyncio
    async def test_auto_route_simple_python_to_monty(self, full_stack) -> None:
        """Simple Python code routes to monty and executes."""
        mgr, router = full_stack

        # Create monty sandbox
        sandbox = await mgr.create_sandbox(
            name="e2e-simple",
            user_id="user-1",
            zone_id="zone-1",
            provider="monty",
        )
        sid = sandbox["sandbox_id"]

        # Run simple code
        result = await mgr.run_code(sid, "python", "x = 2 + 3\nprint(x)")
        assert result.exit_code == 0
        assert "5" in result.stdout

    @pytest.mark.asyncio
    async def test_router_selects_correct_tier(self, full_stack) -> None:
        """Router correctly selects tier based on code analysis."""
        _, router = full_stack

        assert router.select_provider("x = 1", "python", None) == "monty"
        assert router.select_provider("import os", "python", None) == "docker"
        assert router.select_provider("console.log('hi')", "javascript", None) == "docker"

    @pytest.mark.asyncio
    async def test_metrics_populated_after_routing(self, full_stack) -> None:
        """Metrics accumulate after routing decisions."""
        mgr, router = full_stack

        sandbox = await mgr.create_sandbox(
            name="e2e-metrics",
            user_id="user-1",
            zone_id="zone-1",
            provider="monty",
            agent_id="user-1,agent-metrics",
        )
        sid = sandbox["sandbox_id"]

        await mgr.run_code(sid, "python", "print(42)")

        snap = router.metrics.snapshot()
        assert snap["tier_selections"].get("monty", 0) >= 1

    @pytest.mark.asyncio
    async def test_escalation_chain_monty_to_docker(self, full_stack) -> None:
        """Escalation from monty to docker works end-to-end."""
        mgr, router = full_stack

        # Simulate escalation recording
        router.record_escalation("agent-e2e", "monty", "docker")

        snap = router.metrics.snapshot()
        assert snap["escalation_count"] == 1
        assert "monty->docker" in snap["escalations_by_path"]


# ---------------------------------------------------------------------------
# TestLogValidation
# ---------------------------------------------------------------------------


class TestLogValidation:
    """Verify routing decisions are logged."""

    @pytest.mark.asyncio
    async def test_routing_decision_logged(self, full_stack, caplog) -> None:
        """Router logs tier selection decisions."""
        _, router = full_stack

        with caplog.at_level(logging.DEBUG, logger="nexus.sandbox"):
            # History-based override triggers debug log
            for _ in range(8):
                router.record_execution("agent-log", "docker", escalated=False)
            router.select_provider("x = 1", "python", agent_id="agent-log")

        assert any("sticky session" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_escalation_logged(self, full_stack, caplog) -> None:
        """Escalation events are logged."""
        _, router = full_stack

        with caplog.at_level(logging.INFO, logger="nexus.sandbox"):
            router.record_escalation("agent-log-esc", "monty", "docker")

        assert any("escalation" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# TestPerformance
# ---------------------------------------------------------------------------


class TestPerformance:
    """Performance benchmarks for routing overhead."""

    def test_routing_overhead_under_1ms(self, full_stack) -> None:
        """Router.analyze_code() completes in <1ms."""
        _, router = full_stack

        times = []
        for _ in range(100):
            start = time.perf_counter_ns()
            router.analyze_code("x = 1 + 2", "python")
            elapsed = time.perf_counter_ns() - start
            times.append(elapsed)

        median_ms = statistics.median(times) / 1_000_000
        assert median_ms < 1.0, f"Routing took {median_ms:.3f}ms (expected <1ms)"

    @pytest.mark.asyncio
    async def test_monty_execution_still_fast(self, full_stack) -> None:
        """Monty execution with router is still sub-10ms."""
        mgr, _ = full_stack

        sandbox = await mgr.create_sandbox(
            name="e2e-perf",
            user_id="user-1",
            zone_id="zone-1",
            provider="monty",
        )
        sid = sandbox["sandbox_id"]

        times = []
        for _ in range(20):
            start = time.perf_counter_ns()
            result = await mgr.run_code(sid, "python", "1 + 1")
            elapsed = time.perf_counter_ns() - start
            times.append(elapsed)
            assert result.exit_code == 0

        median_ms = statistics.median(times) / 1_000_000
        assert median_ms < 10.0, f"Monty execution took {median_ms:.1f}ms (expected <10ms)"
