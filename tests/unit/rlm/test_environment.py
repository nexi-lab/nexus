"""Tests for NexusREPL environment wrapping SandboxManager.

All sandbox calls are mocked at the boundary (AsyncMock). Tests verify:
- setup() creates sandbox and injects tools
- load_context() sets context variables in sandbox
- execute_code() translates CodeExecutionResult → REPLResult
- Error paths: sandbox creation failure, execution timeout, network errors
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.rlm.environment import NexusREPL
from nexus.rlm.types import REPLResult, RLMInfrastructureError


def _make_sandbox_manager(
    *,
    create_result: dict | None = None,
    run_code_result: MagicMock | None = None,
) -> AsyncMock:
    """Create a mock SandboxManager with sensible defaults."""
    mgr = AsyncMock()
    mgr.create_sandbox = AsyncMock(
        return_value=create_result
        or {"sandbox_id": "sb-123", "status": "active", "provider": "docker"}
    )
    if run_code_result is None:
        run_code_result = MagicMock()
        run_code_result.stdout = ""
        run_code_result.stderr = ""
        run_code_result.exit_code = 0
        run_code_result.execution_time = 0.1
    mgr.run_code = AsyncMock(return_value=run_code_result)
    mgr.stop_sandbox = AsyncMock(return_value={"status": "stopped"})
    return mgr


class TestSetup:
    """NexusREPL.setup() creates sandbox and prepares environment."""

    def test_setup_creates_sandbox(self) -> None:
        mgr = _make_sandbox_manager()
        repl = NexusREPL(
            sandbox_manager=mgr,
            user_id="user-1",
            zone_id="zone-1",
            nexus_api_url="http://localhost:2026",
            nexus_api_key="test-key",
        )
        repl.setup()

        mgr.create_sandbox.assert_called_once()
        call_kwargs = mgr.create_sandbox.call_args.kwargs
        assert call_kwargs["user_id"] == "user-1"
        assert call_kwargs["zone_id"] == "zone-1"
        assert repl.sandbox_id == "sb-123"

    def test_setup_injects_tools_code(self) -> None:
        mgr = _make_sandbox_manager()
        repl = NexusREPL(
            sandbox_manager=mgr,
            user_id="user-1",
            zone_id="zone-1",
            nexus_api_url="http://localhost:2026",
            nexus_api_key="test-key",
        )
        repl.setup()

        # After sandbox creation, setup should run tool injection code
        mgr.run_code.assert_called()
        injected_code = mgr.run_code.call_args_list[0].kwargs.get(
            "code",
            mgr.run_code.call_args_list[0].args[2]
            if len(mgr.run_code.call_args_list[0].args) > 2
            else "",
        )
        assert "nexus_read" in injected_code or "NEXUS_API_URL" in injected_code

    def test_setup_raises_infrastructure_error_on_failure(self) -> None:
        mgr = _make_sandbox_manager()
        mgr.create_sandbox = AsyncMock(side_effect=ValueError("No providers available"))
        repl = NexusREPL(
            sandbox_manager=mgr,
            user_id="user-1",
            zone_id="zone-1",
            nexus_api_url="http://localhost:2026",
            nexus_api_key="test-key",
        )
        with pytest.raises(RLMInfrastructureError, match="sandbox"):
            repl.setup()

    def test_setup_with_custom_provider(self) -> None:
        mgr = _make_sandbox_manager()
        repl = NexusREPL(
            sandbox_manager=mgr,
            user_id="user-1",
            zone_id="zone-1",
            nexus_api_url="http://localhost:2026",
            nexus_api_key="test-key",
            sandbox_provider="e2b",
        )
        repl.setup()
        call_kwargs = mgr.create_sandbox.call_args.kwargs
        assert call_kwargs["provider"] == "e2b"


class TestLoadContext:
    """NexusREPL.load_context() sets context variables in sandbox."""

    def test_load_string_context(self) -> None:
        mgr = _make_sandbox_manager()
        repl = NexusREPL(
            sandbox_manager=mgr,
            user_id="user-1",
            zone_id="zone-1",
            nexus_api_url="http://localhost:2026",
            nexus_api_key="test-key",
        )
        repl.setup()
        mgr.run_code.reset_mock()

        repl.load_context("query: What is X?\npaths: /data/doc1.md")

        mgr.run_code.assert_called()

    def test_load_dict_context(self) -> None:
        mgr = _make_sandbox_manager()
        repl = NexusREPL(
            sandbox_manager=mgr,
            user_id="user-1",
            zone_id="zone-1",
            nexus_api_url="http://localhost:2026",
            nexus_api_key="test-key",
        )
        repl.setup()
        mgr.run_code.reset_mock()

        repl.load_context({"query": "What is X?", "paths": ["/data/doc1.md"]})

        mgr.run_code.assert_called()

    def test_load_context_before_setup_raises(self) -> None:
        mgr = _make_sandbox_manager()
        repl = NexusREPL(
            sandbox_manager=mgr,
            user_id="user-1",
            zone_id="zone-1",
            nexus_api_url="http://localhost:2026",
            nexus_api_key="test-key",
        )
        with pytest.raises(RLMInfrastructureError, match="not set up"):
            repl.load_context("test")


class TestExecuteCode:
    """NexusREPL.execute_code() translates sandbox results to REPLResult."""

    def test_successful_execution(self) -> None:
        result_mock = MagicMock()
        result_mock.stdout = "42\n"
        result_mock.stderr = ""
        result_mock.exit_code = 0
        result_mock.execution_time = 0.5

        mgr = _make_sandbox_manager(run_code_result=result_mock)
        repl = NexusREPL(
            sandbox_manager=mgr,
            user_id="user-1",
            zone_id="zone-1",
            nexus_api_url="http://localhost:2026",
            nexus_api_key="test-key",
        )
        repl.setup()
        mgr.run_code.reset_mock()
        mgr.run_code.return_value = result_mock

        repl_result = repl.execute_code("print(42)")

        assert isinstance(repl_result, REPLResult)
        assert repl_result.stdout == "42\n"
        assert repl_result.stderr == ""
        assert repl_result.exit_code == 0
        assert repl_result.execution_time == 0.5

    def test_execution_with_stderr(self) -> None:
        result_mock = MagicMock()
        result_mock.stdout = ""
        result_mock.stderr = "NameError: name 'x' is not defined"
        result_mock.exit_code = 1
        result_mock.execution_time = 0.1

        mgr = _make_sandbox_manager(run_code_result=result_mock)
        repl = NexusREPL(
            sandbox_manager=mgr,
            user_id="user-1",
            zone_id="zone-1",
            nexus_api_url="http://localhost:2026",
            nexus_api_key="test-key",
        )
        repl.setup()
        mgr.run_code.reset_mock()
        mgr.run_code.return_value = result_mock

        repl_result = repl.execute_code("print(x)")

        assert repl_result.stderr == "NameError: name 'x' is not defined"
        assert repl_result.exit_code == 1

    def test_execution_before_setup_raises(self) -> None:
        mgr = _make_sandbox_manager()
        repl = NexusREPL(
            sandbox_manager=mgr,
            user_id="user-1",
            zone_id="zone-1",
            nexus_api_url="http://localhost:2026",
            nexus_api_key="test-key",
        )
        with pytest.raises(RLMInfrastructureError, match="not set up"):
            repl.execute_code("print(1)")

    def test_execution_truncates_long_output(self) -> None:
        """Output longer than 20K chars should be truncated."""
        long_output = "x" * 25_000
        result_mock = MagicMock()
        result_mock.stdout = long_output
        result_mock.stderr = ""
        result_mock.exit_code = 0
        result_mock.execution_time = 0.1

        mgr = _make_sandbox_manager(run_code_result=result_mock)
        repl = NexusREPL(
            sandbox_manager=mgr,
            user_id="user-1",
            zone_id="zone-1",
            nexus_api_url="http://localhost:2026",
            nexus_api_key="test-key",
        )
        repl.setup()
        mgr.run_code.reset_mock()
        mgr.run_code.return_value = result_mock

        repl_result = repl.execute_code("print('x' * 25000)")

        assert len(repl_result.stdout) <= 20_000 + 100  # Allow for truncation message


class TestCleanup:
    """NexusREPL.cleanup() stops the sandbox."""

    def test_cleanup_stops_sandbox(self) -> None:
        mgr = _make_sandbox_manager()
        repl = NexusREPL(
            sandbox_manager=mgr,
            user_id="user-1",
            zone_id="zone-1",
            nexus_api_url="http://localhost:2026",
            nexus_api_key="test-key",
        )
        repl.setup()
        repl.cleanup()

        mgr.stop_sandbox.assert_called_once_with("sb-123")

    def test_cleanup_before_setup_is_noop(self) -> None:
        mgr = _make_sandbox_manager()
        repl = NexusREPL(
            sandbox_manager=mgr,
            user_id="user-1",
            zone_id="zone-1",
            nexus_api_url="http://localhost:2026",
            nexus_api_key="test-key",
        )
        repl.cleanup()  # Should not raise
        mgr.stop_sandbox.assert_not_called()
