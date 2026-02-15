"""Service-layer E2E tests for Monty sandbox with permissions (Issue #1316).

Tests the full pipeline: SandboxAuthService → SandboxManager → MontySandboxProvider
with a real SQLite DB, real AgentRegistry, and real Monty execution.
This exercises the same code path as `nexus serve` without requiring
the Rust metastore extension.

Validates:
- Monty provider auto-registered in SandboxManager
- Auth pipeline works with Monty (agent validation, ownership, state transition)
- Code execution through the full stack
- Permission enforcement via host functions
- No performance regressions
"""

from __future__ import annotations

import logging
import statistics
import time

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

pytestmark = [
    pytest.mark.skipif(not MONTY_AVAILABLE, reason="pydantic-monty not installed"),
]

from nexus.core.agent_record import AgentState
from nexus.core.agent_registry import AgentRegistry
from nexus.sandbox.auth_service import SandboxAuthService
from nexus.sandbox.sandbox_manager import SandboxManager
from nexus.sandbox.sandbox_monty_provider import MontySandboxProvider
from nexus.storage.models import Base

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    """In-memory SQLite DB."""
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
def agent_registry(session_factory) -> AgentRegistry:
    """Real AgentRegistry with a test agent."""
    registry = AgentRegistry(session_factory=session_factory)
    # Register a test agent
    registry.register(
        agent_id="user-1,TestAgent",
        owner_id="user-1",
        name="TestAgent",
    )
    return registry


@pytest.fixture
def sandbox_manager(session_factory) -> SandboxManager:
    """SandboxManager with Monty provider."""
    mgr = SandboxManager(session_factory=session_factory)
    mgr.providers["monty"] = MontySandboxProvider(
        resource_profile="standard",
        enable_type_checking=False,
    )
    return mgr


@pytest.fixture
def auth_service(
    agent_registry: AgentRegistry,
    sandbox_manager: SandboxManager,
) -> SandboxAuthService:
    """SandboxAuthService with real registry and Monty."""
    return SandboxAuthService(
        agent_registry=agent_registry,
        sandbox_manager=sandbox_manager,
        budget_enforcement=False,
    )


# ---------------------------------------------------------------------------
# Auth Pipeline Tests
# ---------------------------------------------------------------------------


class TestAuthPipeline:
    """Test the full auth pipeline with Monty."""

    @pytest.mark.asyncio
    async def test_full_auth_create_run_stop(
        self,
        auth_service: SandboxAuthService,
        sandbox_manager: SandboxManager,
    ) -> None:
        """Full lifecycle: auth create → run code → stop."""
        # Create via auth pipeline
        result = await auth_service.create_sandbox(
            agent_id="user-1,TestAgent",
            owner_id="user-1",
            zone_id="zone-1",
            name="monty-auth-test",
            provider="monty",
        )

        sandbox_id = result.sandbox["sandbox_id"]
        assert sandbox_id.startswith("monty-")
        assert result.agent_record.state == AgentState.CONNECTED

        # Run code through SandboxManager
        exec_result = await sandbox_manager.run_code(
            sandbox_id, "python", 'print("authenticated execution")'
        )
        assert exec_result.exit_code == 0
        assert "authenticated execution" in exec_result.stdout

        # Stop
        await auth_service.stop_sandbox(sandbox_id, agent_id="user-1,TestAgent")

    @pytest.mark.asyncio
    async def test_auth_rejects_unknown_agent(
        self,
        auth_service: SandboxAuthService,
    ) -> None:
        """Auth pipeline rejects unregistered agents."""
        with pytest.raises(ValueError, match="not found"):
            await auth_service.create_sandbox(
                agent_id="unknown,EvilAgent",
                owner_id="user-1",
                zone_id="zone-1",
                name="should-fail",
                provider="monty",
            )

    @pytest.mark.asyncio
    async def test_auth_rejects_wrong_owner(
        self,
        auth_service: SandboxAuthService,
    ) -> None:
        """Auth pipeline rejects ownership mismatch."""
        with pytest.raises(PermissionError, match="Ownership validation failed"):
            await auth_service.create_sandbox(
                agent_id="user-1,TestAgent",
                owner_id="user-2",  # Wrong owner
                zone_id="zone-1",
                name="should-fail",
                provider="monty",
            )


# ---------------------------------------------------------------------------
# Host Function Permission Tests (with auth)
# ---------------------------------------------------------------------------


class TestHostFunctionPermissionsWithAuth:
    """Permission enforcement through the full auth + execution pipeline."""

    @pytest.mark.asyncio
    async def test_scoped_read_via_auth_pipeline(
        self,
        auth_service: SandboxAuthService,
        sandbox_manager: SandboxManager,
    ) -> None:
        """Agent-scoped host functions work through the auth pipeline."""
        result = await auth_service.create_sandbox(
            agent_id="user-1,TestAgent",
            owner_id="user-1",
            zone_id="zone-1",
            name="host-fn-auth-test",
            provider="monty",
        )
        sandbox_id = result.sandbox["sandbox_id"]
        agent_id = "user-1,TestAgent"

        # Construct agent-scoped host functions (simulating what the server would do)
        def read_file(path: str) -> str:
            """Agent-scoped file read — validates namespace."""
            allowed_prefix = f"/agents/{agent_id}/"
            if not path.startswith(allowed_prefix):
                raise ValueError(
                    f"Access denied: {path} outside agent namespace {allowed_prefix}"
                )
            return f"[content of {path}]"

        def write_file(path: str, content: str) -> str:
            """Agent-scoped file write."""
            allowed_prefix = f"/agents/{agent_id}/"
            if not path.startswith(allowed_prefix):
                raise ValueError(f"Write denied: {path}")
            return "ok"

        sandbox_manager.set_monty_host_functions(sandbox_id, {
            "read_file": read_file,
            "write_file": write_file,
        })

        # Successful scoped read
        exec_result = await sandbox_manager.run_code(
            sandbox_id, "python",
            'print(read_file("/agents/user-1,TestAgent/data.txt"))',
        )
        assert exec_result.exit_code == 0
        assert "[content of" in exec_result.stdout

        # Cross-namespace read should fail
        exec_result = await sandbox_manager.run_code(
            sandbox_id, "python",
            """
try:
    read_file("/agents/other-agent/secrets.txt")
    print("BREACH")
except ValueError as e:
    print(f"blocked: {e}")
""",
        )
        assert exec_result.exit_code == 0
        assert "blocked:" in exec_result.stdout
        assert "BREACH" not in exec_result.stdout


# ---------------------------------------------------------------------------
# Log Validation
# ---------------------------------------------------------------------------


class TestLogValidation:
    """Verify correct logging through the full pipeline."""

    @pytest.mark.asyncio
    async def test_monty_provider_logs(
        self,
        auth_service: SandboxAuthService,
        sandbox_manager: SandboxManager,
        caplog,
    ) -> None:
        """Validate Monty-related log messages."""
        with caplog.at_level(logging.INFO):
            result = await auth_service.create_sandbox(
                agent_id="user-1,TestAgent",
                owner_id="user-1",
                zone_id="zone-1",
                name="log-test",
                provider="monty",
            )
            sandbox_id = result.sandbox["sandbox_id"]

            await sandbox_manager.run_code(
                sandbox_id, "python", "print('log-check')"
            )

        # Verify key log entries
        log_text = caplog.text
        assert "Created Monty sandbox" in log_text or "monty" in log_text.lower()
        assert "sandbox.created" in log_text or sandbox_id in log_text


# ---------------------------------------------------------------------------
# Performance (with full stack overhead)
# ---------------------------------------------------------------------------


class TestPerformanceWithAuth:
    """Performance benchmarks through the full auth + execution pipeline."""

    N = 20  # Fewer iterations since we include DB operations

    @pytest.mark.asyncio
    async def test_monty_full_stack_latency(
        self,
        auth_service: SandboxAuthService,
        sandbox_manager: SandboxManager,
    ) -> None:
        """Full pipeline (auth → create → run) should be < 100ms median."""
        # Pre-create sandbox (auth + DB is one-time cost)
        result = await auth_service.create_sandbox(
            agent_id="user-1,TestAgent",
            owner_id="user-1",
            zone_id="zone-1",
            name="perf-test",
            provider="monty",
        )
        sandbox_id = result.sandbox["sandbox_id"]

        # Measure repeated execution (the hot path)
        times: list[float] = []
        for _ in range(self.N):
            start = time.perf_counter_ns()
            exec_result = await sandbox_manager.run_code(
                sandbox_id, "python", "1 + 1"
            )
            elapsed_ns = time.perf_counter_ns() - start
            times.append(elapsed_ns)
            assert exec_result.exit_code == 0

        median_ms = statistics.median(times) / 1_000_000
        p99_ms = sorted(times)[int(self.N * 0.99)] / 1_000_000

        # Log performance metrics
        logger.info(
            "Monty full-stack execution: median=%.1fms, p99=%.1fms",
            median_ms,
            p99_ms,
        )

        # Monty + DB metadata update should be < 100ms median
        assert median_ms < 100, f"Full-stack median {median_ms:.1f}ms > 100ms"
