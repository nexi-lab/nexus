"""Server-level E2E tests for smart sandbox routing (Issue #1317).

Validates the full routing pipeline as it would run in `nexus serve`:
- Router auto-wiring (same logic as fastapi_server.py lifespan)
- Routing decisions for different code patterns
- Real Monty execution + mocked Docker/E2B escalation
- EscalationNeeded exception flow through SandboxManager.run_code
- Metrics accumulation over multiple runs
- Host function re-wiring on escalation
- No performance regression

These tests use real SandboxManager + real MontySandboxProvider +
AsyncMock Docker/E2B to simulate the production server environment
without needing external Docker daemon or E2B API keys.
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
from nexus.storage.models import Base  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures — mirror server startup logic
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
    """Mock Docker provider that tracks calls."""
    mock = AsyncMock(spec=SandboxProvider)
    mock.create.return_value = "docker-server-test-1"
    mock.run_code.return_value = CodeExecutionResult(
        stdout="docker: os.getcwd() = /workspace\n",
        stderr="",
        exit_code=0,
        execution_time=0.8,
    )
    mock.get_info.return_value = AsyncMock(status="active")
    mock.is_available.return_value = True
    mock.destroy.return_value = None
    return mock


@pytest.fixture
def mock_e2b() -> SandboxProvider:
    """Mock E2B provider."""
    mock = AsyncMock(spec=SandboxProvider)
    mock.create.return_value = "e2b-server-test-1"
    mock.run_code.return_value = CodeExecutionResult(
        stdout="e2b output\n",
        stderr="",
        exit_code=0,
        execution_time=2.5,
    )
    mock.get_info.return_value = AsyncMock(status="active")
    mock.is_available.return_value = True
    mock.destroy.return_value = None
    return mock


@pytest.fixture
def server_stack(session_factory, mock_docker, mock_e2b):
    """Simulate server startup: SandboxManager + Router (mirrors fastapi_server.py).

    This replicates the exact wiring from:
      src/nexus/server/fastapi_server.py lines 876-889
      src/nexus/core/nexus_fs.py lines 6140-6153
    """
    # Step 1: Create SandboxManager (same as server)
    mgr = SandboxManager(session_factory=session_factory)

    # Step 2: Manually register providers (server does this via env vars)
    monty = MontySandboxProvider(resource_profile="standard", enable_type_checking=False)
    mgr.providers["monty"] = monty
    mgr.providers["docker"] = mock_docker
    mgr.providers["e2b"] = mock_e2b

    # Step 3: Attach router (same logic as server wiring via wire_router())
    mgr.wire_router()

    return mgr, mgr._router, monty, mock_docker, mock_e2b


# ---------------------------------------------------------------------------
# Test: Full routing pipeline as server would execute
# ---------------------------------------------------------------------------


class TestServerRoutingPipeline:
    """Validate routing as it runs in production server."""

    @pytest.mark.asyncio
    async def test_router_is_wired_on_startup(self, server_stack) -> None:
        """Router is properly attached to SandboxManager after startup."""
        mgr, router, _, _, _ = server_stack
        assert router is not None
        assert hasattr(mgr, "_router")
        assert mgr._router is router
        assert "monty" in router._providers
        assert "docker" in router._providers
        assert "e2b" in router._providers

    @pytest.mark.asyncio
    async def test_simple_python_runs_on_monty(self, server_stack) -> None:
        """Simple Python code goes to Monty and executes successfully."""
        mgr, router, _, mock_docker, _ = server_stack

        sandbox = await mgr.create_sandbox(
            name="srv-simple",
            user_id="user-1",
            zone_id="zone-1",
            provider="monty",
        )
        sid = sandbox["sandbox_id"]
        assert sid.startswith("monty-")

        result = await mgr.run_code(sid, "python", "x = 2 ** 10\nprint(x)")
        assert result.exit_code == 0
        assert "1024" in result.stdout

        # Docker should NOT have been called
        mock_docker.run_code.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_import_code_triggers_escalation_modulenotfound(
        self, server_stack, caplog
    ) -> None:
        """Code with `import json` escalates from Monty to Docker.

        Monty raises ModuleNotFoundError -> EscalationNeeded.
        SandboxManager catches it, creates temp Docker sandbox, retries.
        """
        mgr, router, _, mock_docker, _ = server_stack

        sandbox = await mgr.create_sandbox(
            name="srv-escalate-mnf",
            user_id="user-1",
            zone_id="zone-1",
            provider="monty",
            agent_id="user-1,agent-escalate-mnf",
        )
        sid = sandbox["sandbox_id"]

        with caplog.at_level(logging.INFO, logger="nexus.sandbox"):
            result = await mgr.run_code(sid, "python", "import json\nprint(json.dumps({'a':1}))")

        # Result comes from mock Docker provider (escalation succeeded)
        assert result.exit_code == 0

        # Docker was called as escalation target
        mock_docker.create.assert_awaited()
        mock_docker.run_code.assert_awaited()
        # Temp sandbox was cleaned up
        mock_docker.destroy.assert_awaited()

        # Metrics reflect escalation
        snap = router.metrics.snapshot()
        assert snap["escalation_count"] >= 1
        assert "monty->docker" in snap["escalations_by_path"]

    @pytest.mark.asyncio
    async def test_module_attr_error_triggers_escalation(self, server_stack) -> None:
        """Code using os.getcwd() escalates (Monty has os stub but not getcwd).

        Monty's partial `os` module succeeds on import but fails on
        `os.getcwd()` with AttributeError. This should escalate.
        """
        mgr, router, _, mock_docker, _ = server_stack

        sandbox = await mgr.create_sandbox(
            name="srv-escalate-attr",
            user_id="user-1",
            zone_id="zone-1",
            provider="monty",
            agent_id="user-1,agent-escalate-attr",
        )
        sid = sandbox["sandbox_id"]

        result = await mgr.run_code(sid, "python", "import os\nprint(os.getcwd())")

        # Escalation: Monty -> Docker
        assert result.exit_code == 0
        mock_docker.create.assert_awaited()
        mock_docker.run_code.assert_awaited()

        snap = router.metrics.snapshot()
        assert snap["escalation_count"] >= 1

    @pytest.mark.asyncio
    async def test_syntax_error_stays_on_monty(self, server_stack) -> None:
        """Syntax errors are reported by Monty, NOT escalated."""
        mgr, _, _, mock_docker, _ = server_stack

        sandbox = await mgr.create_sandbox(
            name="srv-syntax",
            user_id="user-1",
            zone_id="zone-1",
            provider="monty",
        )
        sid = sandbox["sandbox_id"]

        result = await mgr.run_code(sid, "python", "def foo(")
        # Monty reports the syntax error — should NOT escalate
        assert result.exit_code != 0
        assert "syntax" in result.stderr.lower() or "error" in result.stderr.lower()
        mock_docker.run_code.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_runtime_error_stays_on_monty(self, server_stack) -> None:
        """Runtime errors (ZeroDivision, TypeError) are NOT escalated."""
        mgr, _, _, mock_docker, _ = server_stack

        sandbox = await mgr.create_sandbox(
            name="srv-runtime",
            user_id="user-1",
            zone_id="zone-1",
            provider="monty",
        )
        sid = sandbox["sandbox_id"]

        result = await mgr.run_code(sid, "python", "1 / 0")
        assert result.exit_code != 0
        assert "zero" in result.stderr.lower() or "error" in result.stderr.lower()
        mock_docker.run_code.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_explicit_docker_bypasses_routing(self, server_stack) -> None:
        """Explicitly requesting docker bypasses the router entirely."""
        mgr, _, _, mock_docker, _ = server_stack

        sandbox = await mgr.create_sandbox(
            name="srv-explicit-docker",
            user_id="user-1",
            zone_id="zone-1",
            provider="docker",
        )
        assert sandbox["provider"] == "docker"

        result = await mgr.run_code(sandbox["sandbox_id"], "python", "print('hello from docker')")
        assert result.exit_code == 0
        mock_docker.run_code.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_host_functions_work_on_monty(self, server_stack) -> None:
        """Host functions are properly wired and callable from Monty code."""
        mgr, router, _, _, _ = server_stack

        sandbox = await mgr.create_sandbox(
            name="srv-hostfns",
            user_id="user-1",
            zone_id="zone-1",
            provider="monty",
            agent_id="user-1,agent-hf",
        )
        sid = sandbox["sandbox_id"]

        # Set host functions via manager (as server would do)
        vfs_data = {"/workspace/config.json": '{"key": "value"}'}

        def mock_read(path: str) -> str:
            if path not in vfs_data:
                raise FileNotFoundError(f"Not found: {path}")
            return vfs_data[path]

        def mock_write(path: str, content: str) -> str:
            vfs_data[path] = content
            return "ok"

        mgr.set_monty_host_functions(sid, {"read_file": mock_read, "write_file": mock_write})

        # Test read
        result = await mgr.run_code(
            sid, "python", 'data = read_file("/workspace/config.json")\nprint(data)'
        )
        assert result.exit_code == 0
        assert "key" in result.stdout

        # Test write
        result = await mgr.run_code(
            sid,
            "python",
            'write_file("/workspace/output.txt", "hello")\nprint("written")',
        )
        assert result.exit_code == 0
        assert "written" in result.stdout
        assert vfs_data["/workspace/output.txt"] == "hello"

        # Verify router cached host functions for escalation re-wiring
        cached = router.get_cached_host_functions("user-1,agent-hf")
        assert cached is not None
        assert "read_file" in cached
        assert "write_file" in cached

    @pytest.mark.asyncio
    async def test_no_provider_auto_select_excludes_monty(self, server_stack) -> None:
        """When provider=None, auto-select uses docker>e2b, NOT monty.

        This preserves backward compatibility (Decision #4A from #1316).
        """
        mgr, _, _, _, _ = server_stack

        # Auto-select should pick docker (not monty)
        sandbox = await mgr.create_sandbox(
            name="srv-autoselect",
            user_id="user-1",
            zone_id="zone-1",
            # No provider specified
        )
        assert sandbox["provider"] == "docker"


# ---------------------------------------------------------------------------
# Test: Metrics accumulation over session
# ---------------------------------------------------------------------------


class TestServerMetrics:
    """Validate metrics accumulate correctly over a server session."""

    @pytest.mark.asyncio
    async def test_metrics_across_multiple_runs(self, server_stack) -> None:
        """Metrics accumulate over multiple sandbox runs."""
        mgr, router, _, _, _ = server_stack

        # Create sandbox
        sandbox = await mgr.create_sandbox(
            name="srv-metrics",
            user_id="user-1",
            zone_id="zone-1",
            provider="monty",
            agent_id="user-1,agent-m",
        )
        sid = sandbox["sandbox_id"]

        # Run 5 simple python expressions on monty
        for i in range(5):
            result = await mgr.run_code(sid, "python", f"print({i})")
            assert result.exit_code == 0

        snap = router.metrics.snapshot()
        assert snap["tier_selections"].get("monty", 0) >= 5

    @pytest.mark.asyncio
    async def test_escalation_metrics(self, server_stack) -> None:
        """Escalation events are properly counted."""
        mgr, router, _, _, _ = server_stack

        sandbox = await mgr.create_sandbox(
            name="srv-esc-metrics",
            user_id="user-1",
            zone_id="zone-1",
            provider="monty",
            agent_id="user-1,agent-esc",
        )
        sid = sandbox["sandbox_id"]

        # import json triggers ModuleNotFoundError -> escalation
        await mgr.run_code(sid, "python", "import json\nprint(json.dumps({'a':1}))")

        snap = router.metrics.snapshot()
        assert snap["escalation_count"] >= 1

    @pytest.mark.asyncio
    async def test_metrics_snapshot_is_isolated(self, server_stack) -> None:
        """Mutating snapshot dict does not affect metrics."""
        _, router, _, _, _ = server_stack

        router.record_execution("test-agent", "monty", escalated=False)
        snap1 = router.metrics.snapshot()
        snap1["tier_selections"]["monty"] = 9999

        snap2 = router.metrics.snapshot()
        assert snap2["tier_selections"]["monty"] == 1


# ---------------------------------------------------------------------------
# Test: Sticky session history
# ---------------------------------------------------------------------------


class TestServerStickySession:
    """Validate per-agent sticky session learning."""

    @pytest.mark.asyncio
    async def test_agent_learns_from_escalations(self, server_stack) -> None:
        """After repeated escalations, agent sticks to docker."""
        mgr, router, _, _, _ = server_stack

        agent_id = "user-1,agent-learner"

        # Simulate 8 docker executions (as if escalated)
        for _ in range(8):
            router.record_execution(agent_id, "docker", escalated=True)

        # Now even simple code should route to docker (sticky session)
        tier = router.select_provider("x = 1", "python", agent_id=agent_id)
        assert tier == "docker"

    @pytest.mark.asyncio
    async def test_different_agents_independent(self, server_stack) -> None:
        """Each agent has independent history."""
        _, router, _, _, _ = server_stack

        # Agent A uses docker heavily
        for _ in range(8):
            router.record_execution("agent-A", "docker", escalated=False)

        # Agent B has no history — should use analysis
        tier = router.select_provider("x = 1", "python", agent_id="agent-B")
        assert tier == "monty"

        # Agent A should stick to docker
        tier = router.select_provider("x = 1", "python", agent_id="agent-A")
        assert tier == "docker"


# ---------------------------------------------------------------------------
# Test: Performance validation
# ---------------------------------------------------------------------------


class TestServerPerformance:
    """Ensure no performance regression with routing enabled."""

    @pytest.mark.asyncio
    async def test_routing_adds_less_than_half_ms(self, server_stack) -> None:
        """Router overhead is <0.5ms per routing decision."""
        _, router, _, _, _ = server_stack

        times = []
        for _ in range(200):
            start = time.perf_counter_ns()
            router.select_provider("x = 1 + 2\nprint(x)", "python", agent_id="perf-agent")
            elapsed_ns = time.perf_counter_ns() - start
            times.append(elapsed_ns)

        median_ms = statistics.median(times) / 1_000_000
        p99_ms = sorted(times)[int(len(times) * 0.99)] / 1_000_000
        assert median_ms < 0.5, f"select_provider median {median_ms:.3f}ms > 0.5ms"
        assert p99_ms < 2.0, f"select_provider p99 {p99_ms:.3f}ms > 2ms"

    @pytest.mark.asyncio
    async def test_monty_execution_under_10ms(self, server_stack) -> None:
        """Monty execution still sub-10ms with routing overhead."""
        mgr, _, _, _, _ = server_stack

        sandbox = await mgr.create_sandbox(
            name="srv-perf",
            user_id="user-1",
            zone_id="zone-1",
            provider="monty",
        )
        sid = sandbox["sandbox_id"]

        times = []
        for _ in range(50):
            start = time.perf_counter_ns()
            result = await mgr.run_code(sid, "python", "42 * 2")
            elapsed_ns = time.perf_counter_ns() - start
            times.append(elapsed_ns)
            assert result.exit_code == 0

        median_ms = statistics.median(times) / 1_000_000
        assert median_ms < 10, f"run_code median {median_ms:.1f}ms > 10ms"

    @pytest.mark.asyncio
    async def test_analyze_code_under_1ms(self, server_stack) -> None:
        """AST analysis is <1ms even for complex code."""
        _, router, _, _, _ = server_stack

        complex_code = """
class Calculator:
    def __init__(self):
        self.history = []

    def add(self, a, b):
        result = a + b
        self.history.append(('add', a, b, result))
        return result

    def multiply(self, a, b):
        result = a * b
        self.history.append(('mul', a, b, result))
        return result

calc = Calculator()
results = [calc.add(i, i*2) for i in range(100)]
total = sum(results)
print(f"Total: {total}")
"""
        times = []
        for _ in range(100):
            start = time.perf_counter_ns()
            tier = router.analyze_code(complex_code, "python")
            elapsed_ns = time.perf_counter_ns() - start
            times.append(elapsed_ns)
            assert tier == "monty"  # No imports, should be monty

        median_ms = statistics.median(times) / 1_000_000
        assert median_ms < 1.0, f"analyze_code median {median_ms:.3f}ms > 1ms"


# ---------------------------------------------------------------------------
# Test: Edge cases in routing
# ---------------------------------------------------------------------------


class TestServerEdgeCases:
    """Edge cases that could occur in production."""

    @pytest.mark.asyncio
    async def test_empty_code_runs_on_monty(self, server_stack) -> None:
        """Empty code doesn't crash the router."""
        mgr, _, _, _, _ = server_stack

        sandbox = await mgr.create_sandbox(
            name="srv-empty",
            user_id="user-1",
            zone_id="zone-1",
            provider="monty",
        )
        result = await mgr.run_code(sandbox["sandbox_id"], "python", "")
        assert result.exit_code == 0

    @pytest.mark.asyncio
    async def test_multiline_imports_escalate(self, server_stack) -> None:
        """Code with imports buried in multiline still escalates."""
        mgr, _, _, mock_docker, _ = server_stack

        sandbox = await mgr.create_sandbox(
            name="srv-multiline",
            user_id="user-1",
            zone_id="zone-1",
            provider="monty",
            agent_id="user-1,agent-ml",
        )
        code = """
x = 1
y = 2
import json
data = json.dumps({"a": x, "b": y})
print(data)
"""
        result = await mgr.run_code(sandbox["sandbox_id"], "python", code)
        # Should have escalated to docker
        assert result.exit_code == 0
        mock_docker.run_code.assert_awaited()

    @pytest.mark.asyncio
    async def test_string_with_import_does_not_escalate(self, server_stack) -> None:
        """String containing 'import' does NOT trigger escalation."""
        mgr, _, _, mock_docker, _ = server_stack

        sandbox = await mgr.create_sandbox(
            name="srv-string-import",
            user_id="user-1",
            zone_id="zone-1",
            provider="monty",
        )
        code = 'msg = "to import data, use the import button"\nprint(msg)'
        result = await mgr.run_code(sandbox["sandbox_id"], "python", code)
        assert result.exit_code == 0
        assert "import" in result.stdout
        mock_docker.run_code.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_comment_with_import_does_not_escalate(self, server_stack) -> None:
        """Comments with 'import' don't trigger escalation."""
        mgr, _, _, mock_docker, _ = server_stack

        sandbox = await mgr.create_sandbox(
            name="srv-comment-import",
            user_id="user-1",
            zone_id="zone-1",
            provider="monty",
        )
        code = "# TODO: import pandas for data analysis\nx = 42\nprint(x)"
        result = await mgr.run_code(sandbox["sandbox_id"], "python", code)
        assert result.exit_code == 0
        assert "42" in result.stdout
        mock_docker.run_code.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_multiple_sandboxes_independent(self, server_stack) -> None:
        """Multiple sandboxes for same user don't interfere."""
        mgr, _, _, _, _ = server_stack

        sb1 = await mgr.create_sandbox(
            name="srv-sb1", user_id="user-1", zone_id="zone-1", provider="monty"
        )
        sb2 = await mgr.create_sandbox(
            name="srv-sb2", user_id="user-1", zone_id="zone-1", provider="monty"
        )

        r1 = await mgr.run_code(sb1["sandbox_id"], "python", "print('one')")
        r2 = await mgr.run_code(sb2["sandbox_id"], "python", "print('two')")

        assert r1.exit_code == 0 and "one" in r1.stdout
        assert r2.exit_code == 0 and "two" in r2.stdout

    @pytest.mark.asyncio
    async def test_sandbox_lifecycle_with_routing(self, server_stack) -> None:
        """Full lifecycle: create → run → stop works with routing."""
        mgr, _, _, _, _ = server_stack

        sandbox = await mgr.create_sandbox(
            name="srv-lifecycle",
            user_id="user-1",
            zone_id="zone-1",
            provider="monty",
        )
        sid = sandbox["sandbox_id"]
        assert sandbox["status"] == "active"

        result = await mgr.run_code(sid, "python", "print('alive')")
        assert result.exit_code == 0

        stopped = await mgr.stop_sandbox(sid)
        assert stopped["status"] == "stopped"
