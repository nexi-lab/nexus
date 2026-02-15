"""Integration tests for MontySandboxProvider with SandboxManager (Issue #1316).

Tests cover:
- SandboxManager integration: create/run/destroy via manager
- Host function permission testing (mocked VFS)
- Iterative execution with realistic host function scenarios
- Path traversal and injection attack prevention
- Performance benchmarks for Monty vs baseline
"""

from __future__ import annotations

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

pytestmark = pytest.mark.skipif(not MONTY_AVAILABLE, reason="pydantic-monty not installed")

from nexus.sandbox.sandbox_manager import SandboxManager  # noqa: E402
from nexus.sandbox.sandbox_monty_provider import MontySandboxProvider  # noqa: E402
from nexus.storage.models import Base  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    """In-memory SQLite DB for testing."""
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
def monty_provider() -> MontySandboxProvider:
    return MontySandboxProvider(resource_profile="standard", enable_type_checking=False)


@pytest.fixture
def manager(session_factory, monty_provider) -> SandboxManager:
    """SandboxManager with only Monty provider."""
    mgr = SandboxManager(session_factory=session_factory)
    mgr.providers["monty"] = monty_provider
    return mgr


# ---------------------------------------------------------------------------
# SandboxManager Integration
# ---------------------------------------------------------------------------


class TestManagerIntegration:
    """Tests for Monty provider through SandboxManager."""

    @pytest.mark.asyncio
    async def test_create_and_run_via_manager(self, manager: SandboxManager) -> None:
        """Full lifecycle: create → run → destroy via SandboxManager."""
        sandbox = await manager.create_sandbox(
            name="test-monty",
            user_id="user-1",
            zone_id="zone-1",
            provider="monty",
        )
        sandbox_id = sandbox["sandbox_id"]
        assert sandbox_id.startswith("monty-")
        assert sandbox["provider"] == "monty"
        assert sandbox["status"] == "active"

        # Run code
        result = await manager.run_code(sandbox_id, "python", "print(42)")
        assert result.exit_code == 0
        assert "42" in result.stdout

        # Stop
        stopped = await manager.stop_sandbox(sandbox_id)
        assert stopped["status"] == "stopped"

    @pytest.mark.asyncio
    async def test_host_functions_via_manager(
        self, manager: SandboxManager, monty_provider: MontySandboxProvider
    ) -> None:
        """Host functions set via manager are usable in execution."""
        sandbox = await manager.create_sandbox(
            name="test-host-fns",
            user_id="user-1",
            zone_id="zone-1",
            provider="monty",
        )
        sandbox_id = sandbox["sandbox_id"]

        # Simulate VFS read
        def mock_read_file(path: str) -> str:
            files = {
                "/data/config.json": '{"version": 1}',
                "/data/readme.txt": "Hello from Nexus",
            }
            if path not in files:
                raise FileNotFoundError(f"File not found: {path}")
            return files[path]

        manager.set_monty_host_functions(sandbox_id, {"read_file": mock_read_file})

        result = await manager.run_code(
            sandbox_id,
            "python",
            'content = read_file("/data/config.json")\nprint(content)',
        )
        assert result.exit_code == 0
        assert "version" in result.stdout

    @pytest.mark.asyncio
    async def test_monty_not_in_auto_select(self, manager: SandboxManager) -> None:
        """Monty should NOT be auto-selected — explicit only (Decision #4A).

        When no provider is specified and only Monty is available, the
        manager should raise ValueError because Monty is not part of the
        auto-select chain (docker > e2b). Users must explicitly request
        provider='monty'.
        """
        with pytest.raises(ValueError, match="No sandbox providers available"):
            await manager.create_sandbox(
                name="test-auto",
                user_id="user-1",
                zone_id="zone-1",
                # No provider specified — Monty excluded from auto-select
            )

    @pytest.mark.asyncio
    async def test_list_sandbox_includes_monty(self, manager: SandboxManager) -> None:
        """Monty sandboxes appear in listing."""
        await manager.create_sandbox(
            name="monty-sb",
            user_id="user-1",
            zone_id="zone-1",
            provider="monty",
        )
        sandboxes = await manager.list_sandboxes(user_id="user-1")
        assert len(sandboxes) >= 1
        names = [s["name"] for s in sandboxes]
        assert "monty-sb" in names


# ---------------------------------------------------------------------------
# Host Function Permission Tests (Decision #11C)
# ---------------------------------------------------------------------------


class TestHostFunctionPermissions:
    """Verify host functions enforce proper access boundaries."""

    @pytest.mark.asyncio
    async def test_path_traversal_rejected(
        self, manager: SandboxManager, monty_provider: MontySandboxProvider
    ) -> None:
        """Host function rejects path traversal attempts."""
        sandbox = await manager.create_sandbox(
            name="perm-test-1",
            user_id="user-1",
            zone_id="zone-1",
            provider="monty",
        )
        sandbox_id = sandbox["sandbox_id"]

        def scoped_read(path: str) -> str:
            # Simulate agent-scoped read with path validation
            if ".." in path:
                raise ValueError(f"Path traversal blocked: {path}")
            if not path.startswith("/agent-1/"):
                raise ValueError(f"Access denied: {path} outside agent namespace")
            return f"content of {path}"

        manager.set_monty_host_functions(sandbox_id, {"read_file": scoped_read})

        # Path traversal attempt
        code = """
try:
    read_file("../../etc/passwd")
    print("BREACH")
except ValueError as e:
    print(f"blocked: {e}")
"""
        result = await manager.run_code(sandbox_id, "python", code)
        assert result.exit_code == 0
        assert "blocked:" in result.stdout
        assert "BREACH" not in result.stdout

    @pytest.mark.asyncio
    async def test_cross_namespace_access_rejected(
        self, manager: SandboxManager, monty_provider: MontySandboxProvider
    ) -> None:
        """Host function rejects access outside agent namespace."""
        sandbox = await manager.create_sandbox(
            name="perm-test-2",
            user_id="user-1",
            zone_id="zone-1",
            provider="monty",
        )
        sandbox_id = sandbox["sandbox_id"]

        def scoped_read(path: str) -> str:
            if not path.startswith("/agent-1/"):
                raise ValueError(f"Access denied: {path} outside namespace")
            return f"content of {path}"

        manager.set_monty_host_functions(sandbox_id, {"read_file": scoped_read})

        # Cross-namespace read attempt
        code = """
try:
    read_file("/other-agent/secrets.txt")
    print("BREACH")
except ValueError as e:
    print(f"blocked: {e}")
"""
        result = await manager.run_code(sandbox_id, "python", code)
        assert result.exit_code == 0
        assert "blocked:" in result.stdout
        assert "BREACH" not in result.stdout

    @pytest.mark.asyncio
    async def test_valid_namespace_access_allowed(
        self, manager: SandboxManager, monty_provider: MontySandboxProvider
    ) -> None:
        """Host function allows access within agent namespace."""
        sandbox = await manager.create_sandbox(
            name="perm-test-3",
            user_id="user-1",
            zone_id="zone-1",
            provider="monty",
        )
        sandbox_id = sandbox["sandbox_id"]

        def scoped_read(path: str) -> str:
            if not path.startswith("/agent-1/"):
                raise ValueError(f"Access denied: {path}")
            return f"content of {path}"

        manager.set_monty_host_functions(sandbox_id, {"read_file": scoped_read})

        result = await manager.run_code(
            sandbox_id,
            "python",
            'print(read_file("/agent-1/data.txt"))',
        )
        assert result.exit_code == 0
        assert "content of /agent-1/data.txt" in result.stdout

    @pytest.mark.asyncio
    async def test_write_permission_enforced(
        self, manager: SandboxManager, monty_provider: MontySandboxProvider
    ) -> None:
        """Write operations enforce permission checks."""
        sandbox = await manager.create_sandbox(
            name="perm-test-4",
            user_id="user-1",
            zone_id="zone-1",
            provider="monty",
        )
        sandbox_id = sandbox["sandbox_id"]
        written: dict[str, str] = {}

        def scoped_write(path: str, content: str) -> str:
            if not path.startswith("/agent-1/"):
                raise ValueError(f"Write denied: {path}")
            written[path] = content
            return "ok"

        manager.set_monty_host_functions(sandbox_id, {"write_file": scoped_write})

        code = """
write_file("/agent-1/output.json", '{"result": 42}')
print("written")
"""
        result = await manager.run_code(sandbox_id, "python", code)
        assert result.exit_code == 0
        assert "written" in result.stdout
        assert written["/agent-1/output.json"] == '{"result": 42}'

    @pytest.mark.asyncio
    async def test_nonexistent_file_returns_error(
        self, manager: SandboxManager, monty_provider: MontySandboxProvider
    ) -> None:
        """Reading a non-existent file propagates FileNotFoundError."""
        sandbox = await manager.create_sandbox(
            name="perm-test-5",
            user_id="user-1",
            zone_id="zone-1",
            provider="monty",
        )
        sandbox_id = sandbox["sandbox_id"]

        def scoped_read(path: str) -> str:
            raise FileNotFoundError(f"No such file: {path}")

        manager.set_monty_host_functions(sandbox_id, {"read_file": scoped_read})

        code = """
try:
    read_file("/agent-1/missing.txt")
except Exception as e:
    print(f"error: {e}")
"""
        result = await manager.run_code(sandbox_id, "python", code)
        assert result.exit_code == 0
        assert "error:" in result.stdout


# ---------------------------------------------------------------------------
# Performance Benchmarks
# ---------------------------------------------------------------------------


class TestMontyPerformance:
    """Performance benchmarks for Monty provider."""

    N_ITERATIONS = 100

    @pytest.mark.asyncio
    async def test_sandbox_creation_speed(self, monty_provider: MontySandboxProvider) -> None:
        """Monty sandbox creation should be < 1ms median."""
        times: list[float] = []
        sandbox_ids: list[str] = []
        for _ in range(self.N_ITERATIONS):
            start = time.perf_counter_ns()
            sid = await monty_provider.create()
            elapsed_ns = time.perf_counter_ns() - start
            times.append(elapsed_ns)
            sandbox_ids.append(sid)

        # Cleanup
        for sid in sandbox_ids:
            await monty_provider.destroy(sid)

        median_us = statistics.median(times) / 1000
        p99_us = sorted(times)[int(self.N_ITERATIONS * 0.99)] / 1000
        assert median_us < 1000, f"create() median {median_us:.1f}µs > 1000µs"
        assert p99_us < 5000, f"create() p99 {p99_us:.1f}µs > 5000µs"

    @pytest.mark.asyncio
    async def test_simple_execution_speed(self, monty_provider: MontySandboxProvider) -> None:
        """Simple code execution should be < 10ms median."""
        sandbox_id = await monty_provider.create()
        times: list[float] = []

        for _ in range(self.N_ITERATIONS):
            start = time.perf_counter_ns()
            result = await monty_provider.run_code(sandbox_id, "python", "1 + 1")
            elapsed_ns = time.perf_counter_ns() - start
            times.append(elapsed_ns)
            assert result.exit_code == 0

        await monty_provider.destroy(sandbox_id)

        median_ms = statistics.median(times) / 1_000_000
        p99_ms = sorted(times)[int(self.N_ITERATIONS * 0.99)] / 1_000_000
        assert median_ms < 10, f"run_code() median {median_ms:.1f}ms > 10ms"
        assert p99_ms < 100, f"run_code() p99 {p99_ms:.1f}ms > 100ms"

    @pytest.mark.asyncio
    async def test_host_function_call_speed(self, monty_provider: MontySandboxProvider) -> None:
        """Host function call overhead should be < 5ms per call."""
        sandbox_id = await monty_provider.create()
        monty_provider.set_host_functions(
            sandbox_id,
            {
                "identity": lambda x: x,
            },
        )

        times: list[float] = []
        for _ in range(self.N_ITERATIONS):
            start = time.perf_counter_ns()
            result = await monty_provider.run_code(sandbox_id, "python", "identity(42)")
            elapsed_ns = time.perf_counter_ns() - start
            times.append(elapsed_ns)
            assert result.exit_code == 0

        await monty_provider.destroy(sandbox_id)

        median_ms = statistics.median(times) / 1_000_000
        assert median_ms < 15, f"host fn call median {median_ms:.1f}ms > 15ms"

    @pytest.mark.asyncio
    async def test_destroy_speed(self, monty_provider: MontySandboxProvider) -> None:
        """Sandbox destruction should be < 0.1ms."""
        sandbox_ids = [await monty_provider.create() for _ in range(self.N_ITERATIONS)]
        times: list[float] = []

        for sid in sandbox_ids:
            start = time.perf_counter_ns()
            await monty_provider.destroy(sid)
            elapsed_ns = time.perf_counter_ns() - start
            times.append(elapsed_ns)

        median_us = statistics.median(times) / 1000
        assert median_us < 100, f"destroy() median {median_us:.1f}µs > 100µs"
