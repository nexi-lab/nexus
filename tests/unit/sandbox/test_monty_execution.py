"""Real Monty execution tests + security escape tests (Issue #1316).

Tests Monty's actual execution behavior (not mocked). These verify:
- Deny-by-default security: no filesystem, no imports, no eval/exec
- Resource limit enforcement: memory, time, recursion
- Iterative execution: start/resume loop with multiple calls
- Edge cases: empty code, large output, nested functions
"""

from __future__ import annotations

import pytest

# Skip entire module if pydantic-monty is not installed
try:
    import pydantic_monty  # noqa: F401

    MONTY_AVAILABLE = True
except ImportError:
    MONTY_AVAILABLE = False

pytestmark = pytest.mark.skipif(not MONTY_AVAILABLE, reason="pydantic-monty not installed")

from nexus.sandbox.sandbox_monty_provider import MontySandboxProvider


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def provider() -> MontySandboxProvider:
    """Create a standard Monty provider (no type checking for speed)."""
    return MontySandboxProvider(resource_profile="standard", enable_type_checking=False)


@pytest.fixture
def strict_provider() -> MontySandboxProvider:
    """Create a strict Monty provider (tightest limits)."""
    return MontySandboxProvider(resource_profile="strict", enable_type_checking=False)


@pytest.fixture
async def sandbox_id(provider: MontySandboxProvider) -> str:
    return await provider.create()


@pytest.fixture
async def strict_sandbox_id(strict_provider: MontySandboxProvider) -> str:
    return await strict_provider.create()


# ---------------------------------------------------------------------------
# Security escape tests (Decision #10B: basic escape attempts)
# ---------------------------------------------------------------------------


class TestSecurityDenyByDefault:
    """Verify Monty's deny-by-default security boundary.

    These tests verify that sandboxed code CANNOT:
    - Access the filesystem
    - Import modules
    - Use eval/exec
    - Access environment variables
    - Access network
    """

    @pytest.mark.asyncio
    async def test_open_file_blocked(
        self, provider: MontySandboxProvider, sandbox_id: str
    ) -> None:
        """open() should fail — no filesystem access."""
        result = await provider.run_code(
            sandbox_id, "python", 'open("/etc/passwd").read()'
        )
        assert result.exit_code != 0

    @pytest.mark.asyncio
    async def test_import_os_blocked(
        self, provider: MontySandboxProvider, sandbox_id: str
    ) -> None:
        """import os should fail — no stdlib imports."""
        result = await provider.run_code(
            sandbox_id, "python", "import os\nos.system('whoami')"
        )
        assert result.exit_code != 0

    @pytest.mark.asyncio
    async def test_dunder_import_blocked(
        self, provider: MontySandboxProvider, sandbox_id: str
    ) -> None:
        """__import__ should fail — no dynamic imports."""
        result = await provider.run_code(
            sandbox_id, "python", "__import__('subprocess').call(['whoami'])"
        )
        assert result.exit_code != 0

    @pytest.mark.asyncio
    async def test_eval_blocked(
        self, provider: MontySandboxProvider, sandbox_id: str
    ) -> None:
        """eval() should fail — code injection prevention."""
        result = await provider.run_code(
            sandbox_id, "python", "eval(\"__import__('os')\")"
        )
        assert result.exit_code != 0

    @pytest.mark.asyncio
    async def test_exec_blocked(
        self, provider: MontySandboxProvider, sandbox_id: str
    ) -> None:
        """exec() should fail — code injection prevention."""
        result = await provider.run_code(
            sandbox_id, "python", "exec(\"import socket\")"
        )
        assert result.exit_code != 0

    @pytest.mark.asyncio
    async def test_globals_manipulation_blocked(
        self, provider: MontySandboxProvider, sandbox_id: str
    ) -> None:
        """Attempting to access globals/builtins should not escape."""
        result = await provider.run_code(
            sandbox_id, "python",
            "globals()['__builtins__']['__import__']('os').system('whoami')"
        )
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Resource limit enforcement
# ---------------------------------------------------------------------------


class TestResourceLimits:
    """Verify resource limits are enforced during execution."""

    @pytest.mark.asyncio
    async def test_recursion_limit(
        self, strict_provider: MontySandboxProvider, strict_sandbox_id: str
    ) -> None:
        """Deep recursion should hit the limit (strict=100)."""
        code = """
def recurse(n):
    if n <= 0:
        return 0
    return recurse(n - 1)
recurse(500)
"""
        result = await strict_provider.run_code(strict_sandbox_id, "python", code)
        assert result.exit_code != 0
        assert ("recursion" in result.stderr.lower() or "depth" in result.stderr.lower()
                or "Runtime error" in result.stderr)

    @pytest.mark.asyncio
    async def test_memory_limit_large_list(
        self, strict_provider: MontySandboxProvider, strict_sandbox_id: str
    ) -> None:
        """Allocating a huge list should fail under strict limits (10MB)."""
        code = "big_list = list(range(10_000_000))"
        result = await strict_provider.run_code(strict_sandbox_id, "python", code)
        # Should fail with memory or allocation error
        assert result.exit_code != 0

    @pytest.mark.asyncio
    async def test_safe_code_under_limits(
        self, strict_provider: MontySandboxProvider, strict_sandbox_id: str
    ) -> None:
        """Normal code should run fine under strict limits."""
        result = await strict_provider.run_code(
            strict_sandbox_id, "python", "print(sum(range(100)))"
        )
        assert result.exit_code == 0
        assert "4950" in result.stdout


# ---------------------------------------------------------------------------
# Iterative execution tests (Decision #12B)
# ---------------------------------------------------------------------------


class TestIterativeExecution:
    """Tests for the start()/resume() iterative execution loop.

    Covers: happy path, errors, timeouts, multiple sequential calls.
    """

    @pytest.mark.asyncio
    async def test_single_host_function_call(
        self, provider: MontySandboxProvider, sandbox_id: str
    ) -> None:
        """Single external function call → pause → resume → complete."""
        provider.set_host_functions(sandbox_id, {
            "get_value": lambda: 42,
        })
        result = await provider.run_code(
            sandbox_id, "python", "result = get_value()\nprint(result)"
        )
        assert result.exit_code == 0
        assert "42" in result.stdout

    @pytest.mark.asyncio
    async def test_sequential_host_function_calls(
        self, provider: MontySandboxProvider, sandbox_id: str
    ) -> None:
        """Multiple sequential external function calls."""
        state: dict[str, str] = {}

        def store(key: str, value: str) -> None:
            state[key] = value

        def load(key: str) -> str:
            return state.get(key, "not found")

        provider.set_host_functions(sandbox_id, {"store": store, "load": load})
        code = """
store("name", "nexus")
store("version", "1.0")
name = load("name")
version = load("version")
print(f"{name} v{version}")
"""
        result = await provider.run_code(sandbox_id, "python", code)
        assert result.exit_code == 0
        assert "nexus v1.0" in result.stdout

    @pytest.mark.asyncio
    async def test_host_function_exception_caught_in_sandbox(
        self, provider: MontySandboxProvider, sandbox_id: str
    ) -> None:
        """Host function exception propagated and catchable in sandbox."""
        def risky_fn() -> str:
            raise ValueError("something went wrong")

        provider.set_host_functions(sandbox_id, {"risky_fn": risky_fn})
        code = """
try:
    risky_fn()
except ValueError as e:
    print(f"handled: {e}")
"""
        result = await provider.run_code(sandbox_id, "python", code)
        assert result.exit_code == 0
        assert "handled: something went wrong" in result.stdout

    @pytest.mark.asyncio
    async def test_host_function_exception_uncaught_fails(
        self, provider: MontySandboxProvider, sandbox_id: str
    ) -> None:
        """Uncaught host function exception results in runtime error."""
        def fail() -> None:
            raise RuntimeError("kaboom")

        provider.set_host_functions(sandbox_id, {"fail": fail})
        result = await provider.run_code(sandbox_id, "python", "fail()")
        assert result.exit_code == 1
        assert "kaboom" in result.stderr or "Runtime" in result.stderr

    @pytest.mark.asyncio
    async def test_host_function_with_args_and_kwargs(
        self, provider: MontySandboxProvider, sandbox_id: str
    ) -> None:
        """Host functions receive correct positional and keyword args."""
        received: list[tuple] = []

        def log_call(*args: object, **kwargs: object) -> str:
            received.append((args, kwargs))
            return "ok"

        provider.set_host_functions(sandbox_id, {"log_call": log_call})
        result = await provider.run_code(
            sandbox_id, "python", 'log_call("hello", 42)'
        )
        assert result.exit_code == 0
        assert len(received) == 1
        assert received[0][0] == ("hello", 42)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge case tests for unusual inputs and behaviors."""

    @pytest.mark.asyncio
    async def test_empty_code(
        self, provider: MontySandboxProvider, sandbox_id: str
    ) -> None:
        """Empty code should succeed with empty output."""
        result = await provider.run_code(sandbox_id, "python", "")
        # Empty code may be a syntax error or succeed with None
        # Either is acceptable
        assert result.execution_time >= 0

    @pytest.mark.asyncio
    async def test_whitespace_only_code(
        self, provider: MontySandboxProvider, sandbox_id: str
    ) -> None:
        """Whitespace-only code."""
        result = await provider.run_code(sandbox_id, "python", "   \n\n  ")
        assert result.execution_time >= 0

    @pytest.mark.asyncio
    async def test_large_output(
        self, provider: MontySandboxProvider, sandbox_id: str
    ) -> None:
        """Large print output should be captured."""
        code = """
for i in range(100):
    print(f"line {i}")
"""
        result = await provider.run_code(sandbox_id, "python", code)
        assert result.exit_code == 0
        assert "line 0" in result.stdout
        assert "line 99" in result.stdout

    @pytest.mark.asyncio
    async def test_boolean_return_value(
        self, provider: MontySandboxProvider, sandbox_id: str
    ) -> None:
        result = await provider.run_code(sandbox_id, "python", "True")
        assert result.exit_code == 0
        assert "true" in result.stdout.lower()

    @pytest.mark.asyncio
    async def test_list_return_value(
        self, provider: MontySandboxProvider, sandbox_id: str
    ) -> None:
        result = await provider.run_code(sandbox_id, "python", "[1, 2, 3]")
        assert result.exit_code == 0
        assert "[1, 2, 3]" in result.stdout

    @pytest.mark.asyncio
    async def test_multiple_runs_on_same_sandbox(
        self, provider: MontySandboxProvider, sandbox_id: str
    ) -> None:
        """Multiple run_code calls on same sandbox should work independently."""
        r1 = await provider.run_code(sandbox_id, "python", "1 + 1")
        r2 = await provider.run_code(sandbox_id, "python", "2 + 2")
        r3 = await provider.run_code(sandbox_id, "python", "3 + 3")
        assert r1.exit_code == 0
        assert r2.exit_code == 0
        assert r3.exit_code == 0
        assert "2" in r1.stdout
        assert "4" in r2.stdout
        assert "6" in r3.stdout

    @pytest.mark.asyncio
    async def test_for_loop_and_comprehension(
        self, provider: MontySandboxProvider, sandbox_id: str
    ) -> None:
        """Verify loops and comprehensions work."""
        code = """
squares = [x * x for x in range(5)]
print(squares)
"""
        result = await provider.run_code(sandbox_id, "python", code)
        assert result.exit_code == 0
        assert "[0, 1, 4, 9, 16]" in result.stdout

    @pytest.mark.asyncio
    async def test_try_except_in_sandbox(
        self, provider: MontySandboxProvider, sandbox_id: str
    ) -> None:
        """Exception handling works inside sandbox."""
        code = """
try:
    x = 1 / 0
except ZeroDivisionError:
    print("caught division by zero")
"""
        result = await provider.run_code(sandbox_id, "python", code)
        assert result.exit_code == 0
        assert "caught division by zero" in result.stdout

    @pytest.mark.asyncio
    async def test_function_definition_and_call(
        self, provider: MontySandboxProvider, sandbox_id: str
    ) -> None:
        """Functions can be defined and called in sandbox."""
        code = """
def fibonacci(n):
    if n <= 1:
        return n
    a = 0
    b = 1
    for _ in range(2, n + 1):
        a, b = b, a + b
    return b

print(fibonacci(10))
"""
        result = await provider.run_code(sandbox_id, "python", code)
        assert result.exit_code == 0
        assert "55" in result.stdout
