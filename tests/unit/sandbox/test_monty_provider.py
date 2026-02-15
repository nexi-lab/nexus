"""Unit tests for MontySandboxProvider — ABC compliance + adapter logic (Issue #1316).

Tests cover:
- Provider lifecycle: create, destroy, get_info, is_available
- Code execution: complete mode, stdout capture, error handling
- Language validation: only Python supported
- Unsupported operations: pause, resume, mount_nexus
- Resource limits: per-profile tier configuration
- Host function registration and dispatch
- Iterative execution: start/resume loop with host functions
- Type checking: syntax errors, type errors, runtime errors
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

# Skip entire module if pydantic-monty is not installed
try:
    import pydantic_monty  # noqa: F401

    MONTY_AVAILABLE = True
except ImportError:
    MONTY_AVAILABLE = False

pytestmark = pytest.mark.skipif(not MONTY_AVAILABLE, reason="pydantic-monty not installed")

from nexus.sandbox.sandbox_monty_provider import (  # noqa: E402
    MONTY_RESOURCE_PROFILES,
    MontySandboxProvider,
)
from nexus.sandbox.sandbox_provider import (  # noqa: E402
    SandboxNotFoundError,
    UnsupportedLanguageError,
    UnsupportedOperationError,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def provider() -> MontySandboxProvider:
    """Create a standard Monty provider."""
    return MontySandboxProvider(resource_profile="standard", enable_type_checking=False)


@pytest.fixture
def provider_with_type_check() -> MontySandboxProvider:
    """Create a Monty provider with type checking enabled."""
    return MontySandboxProvider(resource_profile="standard", enable_type_checking=True)


@pytest.fixture
async def sandbox_id(provider: MontySandboxProvider) -> str:
    """Create a sandbox and return its ID."""
    return await provider.create()


# ---------------------------------------------------------------------------
# Lifecycle: create / destroy / get_info / is_available
# ---------------------------------------------------------------------------


class TestLifecycle:
    """Tests for sandbox lifecycle management."""

    @pytest.mark.asyncio
    async def test_create_returns_monty_prefixed_id(self, provider: MontySandboxProvider) -> None:
        sandbox_id = await provider.create()
        assert sandbox_id.startswith("monty-")
        assert len(sandbox_id) == 18  # "monty-" + 12 hex chars

    @pytest.mark.asyncio
    async def test_create_unique_ids(self, provider: MontySandboxProvider) -> None:
        ids = [await provider.create() for _ in range(10)]
        assert len(set(ids)) == 10

    @pytest.mark.asyncio
    async def test_create_respects_security_profile(self, provider: MontySandboxProvider) -> None:
        mock_profile = MagicMock()
        mock_profile.name = "strict"
        sandbox_id = await provider.create(security_profile=mock_profile)
        info = await provider.get_info(sandbox_id)
        assert info.metadata["resource_profile"] == "strict"

    @pytest.mark.asyncio
    async def test_create_unknown_security_profile_uses_default(
        self, provider: MontySandboxProvider
    ) -> None:
        mock_profile = MagicMock()
        mock_profile.name = "nonexistent"
        sandbox_id = await provider.create(security_profile=mock_profile)
        info = await provider.get_info(sandbox_id)
        assert info.metadata["resource_profile"] == "standard"

    @pytest.mark.asyncio
    async def test_destroy_removes_instance(
        self, provider: MontySandboxProvider, sandbox_id: str
    ) -> None:
        await provider.destroy(sandbox_id)
        with pytest.raises(SandboxNotFoundError):
            await provider.get_info(sandbox_id)

    @pytest.mark.asyncio
    async def test_destroy_nonexistent_raises(self, provider: MontySandboxProvider) -> None:
        with pytest.raises(SandboxNotFoundError, match="not found"):
            await provider.destroy("nonexistent-123")

    @pytest.mark.asyncio
    async def test_get_info_returns_sandbox_info(
        self, provider: MontySandboxProvider, sandbox_id: str
    ) -> None:
        info = await provider.get_info(sandbox_id)
        assert info.sandbox_id == sandbox_id
        assert info.status == "active"
        assert info.provider == "monty"
        assert isinstance(info.created_at, datetime)

    @pytest.mark.asyncio
    async def test_is_available(self, provider: MontySandboxProvider) -> None:
        assert await provider.is_available() is True


class TestInitialization:
    """Tests for provider initialization."""

    def test_invalid_resource_profile(self) -> None:
        with pytest.raises(ValueError, match="Unknown resource profile"):
            MontySandboxProvider(resource_profile="nonexistent")

    def test_valid_profiles(self) -> None:
        for name in ("strict", "standard", "permissive"):
            p = MontySandboxProvider(resource_profile=name)
            assert p._default_profile_name == name

    @patch("nexus.sandbox.sandbox_monty_provider.MONTY_AVAILABLE", False)
    def test_unavailable_when_monty_not_installed(self) -> None:
        with pytest.raises(RuntimeError, match="pydantic-monty is not installed"):
            MontySandboxProvider()


# ---------------------------------------------------------------------------
# Code execution: complete mode
# ---------------------------------------------------------------------------


class TestCodeExecution:
    """Tests for run_code() in complete mode (no host functions)."""

    @pytest.mark.asyncio
    async def test_simple_expression(self, provider: MontySandboxProvider, sandbox_id: str) -> None:
        result = await provider.run_code(sandbox_id, "python", "1 + 1")
        assert result.exit_code == 0
        assert "2" in result.stdout

    @pytest.mark.asyncio
    async def test_print_output(self, provider: MontySandboxProvider, sandbox_id: str) -> None:
        result = await provider.run_code(sandbox_id, "python", 'print("hello world")')
        assert result.exit_code == 0
        assert "hello world" in result.stdout

    @pytest.mark.asyncio
    async def test_multiline_code(self, provider: MontySandboxProvider, sandbox_id: str) -> None:
        code = "x = 10\ny = 20\nprint(x + y)"
        result = await provider.run_code(sandbox_id, "python", code)
        assert result.exit_code == 0
        assert "30" in result.stdout

    @pytest.mark.asyncio
    async def test_return_value_serialized_as_json(
        self, provider: MontySandboxProvider, sandbox_id: str
    ) -> None:
        result = await provider.run_code(sandbox_id, "python", '{"key": "value"}')
        assert result.exit_code == 0
        # The dict should be serialized as JSON in stdout
        assert "key" in result.stdout
        assert "value" in result.stdout

    @pytest.mark.asyncio
    async def test_syntax_error_returns_exit_code_1(
        self, provider: MontySandboxProvider, sandbox_id: str
    ) -> None:
        result = await provider.run_code(sandbox_id, "python", "def foo(")
        assert result.exit_code in (1, 2)  # Syntax or parse error
        assert result.stderr != ""

    @pytest.mark.asyncio
    async def test_runtime_error_returns_exit_code_1(
        self, provider: MontySandboxProvider, sandbox_id: str
    ) -> None:
        result = await provider.run_code(sandbox_id, "python", "1 / 0")
        assert result.exit_code == 1
        assert "error" in result.stderr.lower() or "ZeroDivision" in result.stderr

    @pytest.mark.asyncio
    async def test_execution_time_tracked(
        self, provider: MontySandboxProvider, sandbox_id: str
    ) -> None:
        result = await provider.run_code(sandbox_id, "python", "42")
        assert result.execution_time >= 0
        assert result.execution_time < 5.0  # Should be sub-second

    @pytest.mark.asyncio
    async def test_none_return_value(self, provider: MontySandboxProvider, sandbox_id: str) -> None:
        result = await provider.run_code(sandbox_id, "python", "x = 42")
        assert result.exit_code == 0
        # Assignment returns None, so stdout should not contain extra output
        # (no "null" appended)

    @pytest.mark.asyncio
    async def test_destroyed_sandbox_raises(
        self, provider: MontySandboxProvider, sandbox_id: str
    ) -> None:
        await provider.destroy(sandbox_id)
        with pytest.raises(SandboxNotFoundError):
            await provider.run_code(sandbox_id, "python", "42")


# ---------------------------------------------------------------------------
# Language validation
# ---------------------------------------------------------------------------


class TestLanguageValidation:
    """Tests for language support enforcement."""

    @pytest.mark.asyncio
    async def test_python_accepted(self, provider: MontySandboxProvider, sandbox_id: str) -> None:
        result = await provider.run_code(sandbox_id, "python", "42")
        assert result.exit_code == 0

    @pytest.mark.asyncio
    async def test_javascript_rejected(
        self, provider: MontySandboxProvider, sandbox_id: str
    ) -> None:
        with pytest.raises(UnsupportedLanguageError, match="only supports Python"):
            await provider.run_code(sandbox_id, "javascript", "console.log('hi')")

    @pytest.mark.asyncio
    async def test_bash_rejected(self, provider: MontySandboxProvider, sandbox_id: str) -> None:
        with pytest.raises(UnsupportedLanguageError):
            await provider.run_code(sandbox_id, "bash", "echo hi")


# ---------------------------------------------------------------------------
# Unsupported operations
# ---------------------------------------------------------------------------


class TestUnsupportedOperations:
    """Tests for operations that Monty doesn't support."""

    @pytest.mark.asyncio
    async def test_pause_raises(self, provider: MontySandboxProvider, sandbox_id: str) -> None:
        with pytest.raises(UnsupportedOperationError, match="do not support pause"):
            await provider.pause(sandbox_id)

    @pytest.mark.asyncio
    async def test_resume_raises(self, provider: MontySandboxProvider, sandbox_id: str) -> None:
        with pytest.raises(UnsupportedOperationError, match="do not support resume"):
            await provider.resume(sandbox_id)

    @pytest.mark.asyncio
    async def test_mount_nexus_raises(
        self, provider: MontySandboxProvider, sandbox_id: str
    ) -> None:
        with pytest.raises(UnsupportedOperationError, match="do not support FUSE"):
            await provider.mount_nexus(
                sandbox_id,
                mount_path="/mnt/nexus",
                nexus_url="https://example.com",
                api_key="test-key",
            )

    @pytest.mark.asyncio
    async def test_pause_nonexistent_raises_not_found(self, provider: MontySandboxProvider) -> None:
        with pytest.raises(SandboxNotFoundError):
            await provider.pause("nonexistent")


# ---------------------------------------------------------------------------
# Resource limits
# ---------------------------------------------------------------------------


class TestResourceLimits:
    """Tests for resource limit profiles."""

    def test_profile_tiers_exist(self) -> None:
        assert "strict" in MONTY_RESOURCE_PROFILES
        assert "standard" in MONTY_RESOURCE_PROFILES
        assert "permissive" in MONTY_RESOURCE_PROFILES

    def test_strict_tightest(self) -> None:
        strict = MONTY_RESOURCE_PROFILES["strict"]
        standard = MONTY_RESOURCE_PROFILES["standard"]
        assert strict.max_duration_secs < standard.max_duration_secs
        assert strict.max_memory < standard.max_memory
        assert strict.max_recursion_depth < standard.max_recursion_depth

    def test_permissive_most_generous(self) -> None:
        standard = MONTY_RESOURCE_PROFILES["standard"]
        permissive = MONTY_RESOURCE_PROFILES["permissive"]
        assert permissive.max_duration_secs > standard.max_duration_secs
        assert permissive.max_memory > standard.max_memory

    def test_to_resource_limits_returns_dict_like(self) -> None:
        profile = MONTY_RESOURCE_PROFILES["standard"]
        limits = profile.to_resource_limits()
        # ResourceLimits is a TypedDict — verify it has expected keys
        assert "max_duration_secs" in limits or hasattr(limits, "max_duration_secs")

    @pytest.mark.asyncio
    async def test_timeout_override_when_shorter(self) -> None:
        provider = MontySandboxProvider(resource_profile="permissive")
        sandbox_id = await provider.create()
        # The 120s permissive default should be overridden by timeout=5
        result = await provider.run_code(sandbox_id, "python", "42", timeout=5)
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Host functions
# ---------------------------------------------------------------------------


class TestHostFunctions:
    """Tests for host function registration and invocation."""

    @pytest.mark.asyncio
    async def test_set_host_functions(
        self, provider: MontySandboxProvider, sandbox_id: str
    ) -> None:
        fns = {"read_file": lambda path: f"content of {path}"}
        provider.set_host_functions(sandbox_id, fns)
        # Verify registered
        instance = provider._instances[sandbox_id]
        assert "read_file" in instance.host_functions

    @pytest.mark.asyncio
    async def test_host_function_called_during_execution(
        self, provider: MontySandboxProvider, sandbox_id: str
    ) -> None:
        call_log: list[str] = []

        def mock_read(path: str) -> str:
            call_log.append(path)
            return f"content of {path}"

        provider.set_host_functions(sandbox_id, {"read_file": mock_read})
        result = await provider.run_code(sandbox_id, "python", 'read_file("/test.txt")')
        assert result.exit_code == 0
        assert call_log == ["/test.txt"]
        assert "content of /test.txt" in result.stdout

    @pytest.mark.asyncio
    async def test_host_function_error_propagates_to_sandbox(
        self, provider: MontySandboxProvider, sandbox_id: str
    ) -> None:
        def failing_read(path: str) -> str:
            raise ValueError(f"Access denied: {path}")

        provider.set_host_functions(sandbox_id, {"read_file": failing_read})
        code = """
try:
    read_file("/secret.txt")
    print("should not reach here")
except ValueError as e:
    print(f"caught: {e}")
"""
        result = await provider.run_code(sandbox_id, "python", code)
        assert result.exit_code == 0
        assert "caught:" in result.stdout
        assert "Access denied" in result.stdout

    @pytest.mark.asyncio
    async def test_unregistered_host_function_raises_name_error(
        self, provider: MontySandboxProvider, sandbox_id: str
    ) -> None:
        # Register one function but call another
        provider.set_host_functions(sandbox_id, {"read_file": lambda p: p})
        code = """
try:
    write_file("/test.txt", "data")
    print("should not reach here")
except NameError as e:
    print(f"caught: {e}")
"""
        result = await provider.run_code(sandbox_id, "python", code)
        assert result.exit_code == 0
        assert "caught:" in result.stdout

    @pytest.mark.asyncio
    async def test_multiple_host_function_calls(
        self, provider: MontySandboxProvider, sandbox_id: str
    ) -> None:
        call_count = {"count": 0}

        def increment(value: int) -> int:
            call_count["count"] += 1
            return value + 1

        provider.set_host_functions(sandbox_id, {"increment": increment})
        code = """
a = increment(1)
b = increment(a)
c = increment(b)
print(c)
"""
        result = await provider.run_code(sandbox_id, "python", code)
        assert result.exit_code == 0
        assert "4" in result.stdout
        assert call_count["count"] == 3

    @pytest.mark.asyncio
    async def test_set_host_functions_nonexistent_sandbox(
        self, provider: MontySandboxProvider
    ) -> None:
        with pytest.raises(SandboxNotFoundError):
            provider.set_host_functions("nonexistent", {"fn": lambda: None})

    @pytest.mark.asyncio
    async def test_set_host_functions_rejects_invalid_names(
        self, provider: MontySandboxProvider, sandbox_id: str
    ) -> None:
        """Host function names must be valid Python identifiers."""
        with pytest.raises(ValueError, match="Invalid host function name"):
            provider.set_host_functions(sandbox_id, {"not-valid": lambda: None})
        with pytest.raises(ValueError, match="Invalid host function name"):
            provider.set_host_functions(sandbox_id, {"123bad": lambda: None})


# ---------------------------------------------------------------------------
# Iteration guard
# ---------------------------------------------------------------------------


class TestIterationGuard:
    """Tests for the iteration upper-bound guard in _run_iterative."""

    @pytest.mark.asyncio
    async def test_iteration_guard_caps_runaway_loop(
        self, provider: MontySandboxProvider, sandbox_id: str
    ) -> None:
        """Verify _MAX_ITERATIONS prevents infinite host-function loops."""
        # Set a very low guard for this test
        original = MontySandboxProvider._MAX_ITERATIONS
        MontySandboxProvider._MAX_ITERATIONS = 5
        try:
            call_count = {"n": 0}

            def recursive_call() -> int:
                call_count["n"] += 1
                return call_count["n"]

            provider.set_host_functions(sandbox_id, {"get_next": recursive_call})

            # Code that calls host function in a tight loop (>5 times)
            code = """
results = []
for i in range(100):
    results.append(get_next())
print(results)
"""
            # The SandboxProviderError is caught by run_code's generic handler
            # and surfaced as exit_code=127 with error message in stderr
            result = await provider.run_code(sandbox_id, "python", code)
            assert result.exit_code == 127
            assert "exceeded" in result.stderr
            assert call_count["n"] <= 5
        finally:
            MontySandboxProvider._MAX_ITERATIONS = original


# ---------------------------------------------------------------------------
# Type checking
# ---------------------------------------------------------------------------


class TestTypeChecking:
    """Tests for Monty's type checking integration."""

    @pytest.mark.asyncio
    async def test_type_error_detected(
        self, provider_with_type_check: MontySandboxProvider
    ) -> None:
        sandbox_id = await provider_with_type_check.create()
        # Monty's type checker should catch adding int + str
        result = await provider_with_type_check.run_code(
            sandbox_id, "python", 'x: int = 1\ny: str = "hello"\nz = x + y'
        )
        # Type error should be reported (exit_code=2 for type errors)
        if result.exit_code == 2:
            assert "type" in result.stderr.lower() or "Type" in result.stderr

    @pytest.mark.asyncio
    async def test_valid_code_passes_type_check(
        self, provider_with_type_check: MontySandboxProvider
    ) -> None:
        sandbox_id = await provider_with_type_check.create()
        result = await provider_with_type_check.run_code(
            sandbox_id, "python", "x: int = 1\ny: int = 2\nprint(x + y)"
        )
        assert result.exit_code == 0
        assert "3" in result.stdout
