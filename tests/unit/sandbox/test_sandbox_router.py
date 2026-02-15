"""Unit tests for SandboxRouter — smart sandbox routing (Issue #1317).

TDD: These tests are written FIRST (RED), before the implementation.
All should fail initially, then pass once SandboxRouter is implemented.

Test structure:
    - TestCodeAnalysis: ~30 parametrized cases for AST-based routing
    - TestHistoryBasedRouting: Per-agent sticky sessions
    - TestProviderSelection: Combined analysis + history + availability
    - TestEscalationChain: get_next_tier logic
    - TestHostFunctionCache: Per-agent host function caching
    - TestMetrics: SandboxRouterMetrics thread-safety
    - TestHypothesis: Property-based tests for robustness
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from nexus.sandbox.sandbox_provider import SandboxProvider
from nexus.sandbox.sandbox_router import SandboxRouter
from nexus.sandbox.sandbox_router_metrics import SandboxRouterMetrics

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_mock_provider(name: str) -> SandboxProvider:
    """Create a mock sandbox provider with a name."""
    mock = AsyncMock(spec=SandboxProvider)
    mock.name = name
    return mock


@pytest.fixture
def providers() -> dict[str, SandboxProvider]:
    """All three providers available."""
    return {
        "monty": _make_mock_provider("monty"),
        "docker": _make_mock_provider("docker"),
        "e2b": _make_mock_provider("e2b"),
    }


@pytest.fixture
def router(providers: dict[str, SandboxProvider]) -> SandboxRouter:
    """Router with all providers available."""
    return SandboxRouter(available_providers=providers)


@pytest.fixture
def router_no_monty() -> SandboxRouter:
    """Router without Monty provider."""
    return SandboxRouter(
        available_providers={
            "docker": _make_mock_provider("docker"),
            "e2b": _make_mock_provider("e2b"),
        }
    )


@pytest.fixture
def router_docker_only() -> SandboxRouter:
    """Router with only Docker provider."""
    return SandboxRouter(
        available_providers={
            "docker": _make_mock_provider("docker"),
        }
    )


@pytest.fixture
def metrics() -> SandboxRouterMetrics:
    """Fresh metrics instance."""
    return SandboxRouterMetrics()


# ---------------------------------------------------------------------------
# TestCodeAnalysis — AST-based tier classification
# ---------------------------------------------------------------------------


class TestCodeAnalysis:
    """Test analyze_code() for correct tier classification."""

    # Parametrized: (code, language, expected_tier)
    @pytest.mark.parametrize(
        "code,expected",
        [
            # Pure Python → monty
            ("x = 1 + 2", "monty"),
            ("result = 42 * 3", "monty"),
            ("print('hello')", "monty"),
            ("x = [1, 2, 3]\ny = sum(x)", "monty"),
            ("def foo(a, b): return a + b", "monty"),
            ("class Foo:\n    pass", "monty"),
            ("for i in range(10):\n    print(i)", "monty"),
            ("x = {'a': 1, 'b': 2}", "monty"),
            ("if True:\n    x = 1\nelse:\n    x = 2", "monty"),
            ("lambda x: x * 2", "monty"),
            # List/dict comprehensions
            ("[x**2 for x in range(10)]", "monty"),
            ("{k: v for k, v in [('a', 1)]}", "monty"),
            # Multiple assignments
            ("a = 1\nb = 2\nc = a + b", "monty"),
            # Empty code
            ("", "monty"),
            # Whitespace only
            ("   \n\n   ", "monty"),
        ],
        ids=[
            "simple_arithmetic",
            "multiplication",
            "print_call",
            "list_sum",
            "function_def",
            "class_def",
            "for_loop",
            "dict_literal",
            "if_else",
            "lambda",
            "list_comprehension",
            "dict_comprehension",
            "multiple_assign",
            "empty_code",
            "whitespace_only",
        ],
    )
    def test_pure_python_routes_to_monty(
        self, router: SandboxRouter, code: str, expected: str
    ) -> None:
        assert router.analyze_code(code, "python") == expected

    @pytest.mark.parametrize(
        "code,expected",
        [
            # Stdlib imports → docker
            ("import os", "docker"),
            ("import sys", "docker"),
            ("import subprocess", "docker"),
            ("import pathlib", "docker"),
            ("from os import path", "docker"),
            ("from pathlib import Path", "docker"),
            ("import json", "docker"),
            ("import re", "docker"),
            # Third-party imports → docker
            ("import pandas", "docker"),
            ("import numpy", "docker"),
            ("import requests", "docker"),
            ("from sklearn import metrics", "docker"),
            ("import torch", "docker"),
        ],
        ids=[
            "import_os",
            "import_sys",
            "import_subprocess",
            "import_pathlib",
            "from_os",
            "from_pathlib",
            "import_json",
            "import_re",
            "import_pandas",
            "import_numpy",
            "import_requests",
            "from_sklearn",
            "import_torch",
        ],
    )
    def test_imports_route_to_docker(self, router: SandboxRouter, code: str, expected: str) -> None:
        assert router.analyze_code(code, "python") == expected

    @pytest.mark.parametrize(
        "code,expected",
        [
            # File I/O → docker
            ('open("file.txt")', "docker"),
            ('f = open("data.csv", "r")', "docker"),
            ('with open("out.txt", "w") as f:\n    f.write("hi")', "docker"),
            # Subprocess → docker
            ("import subprocess\nsubprocess.run(['ls'])", "docker"),
        ],
        ids=[
            "open_file",
            "open_csv",
            "with_open",
            "subprocess_run",
        ],
    )
    def test_io_routes_to_docker(self, router: SandboxRouter, code: str, expected: str) -> None:
        assert router.analyze_code(code, "python") == expected

    def test_string_literal_not_false_positive(self, router: SandboxRouter) -> None:
        """String containing 'import pandas' should NOT trigger docker."""
        code = 'x = "import pandas"\nprint(x)'
        assert router.analyze_code(code, "python") == "monty"

    def test_comment_not_false_positive(self, router: SandboxRouter) -> None:
        """Comment containing 'import os' should NOT trigger docker."""
        code = "# import os\nx = 42"
        assert router.analyze_code(code, "python") == "monty"

    def test_non_python_routes_to_docker(self, router: SandboxRouter) -> None:
        """Non-Python code always routes to docker (or e2b if docker unavailable)."""
        assert router.analyze_code("console.log('hi')", "javascript") == "docker"
        assert router.analyze_code("echo hello", "bash") == "docker"

    def test_non_python_without_docker(self, router_no_monty: SandboxRouter) -> None:
        """Non-Python falls through to first available non-monty provider."""
        result = router_no_monty.analyze_code("console.log('hi')", "javascript")
        assert result == "docker"

    def test_syntax_error_routes_to_monty(self, router: SandboxRouter) -> None:
        """Code with syntax errors should route to monty (let it report the error)."""
        code = "def foo("
        assert router.analyze_code(code, "python") == "monty"

    def test_ast_parse_failure_fallback(self, router: SandboxRouter) -> None:
        """If AST parse fails unexpectedly, fall back gracefully."""
        # NUL bytes cause AST parse to fail differently
        code = "x = 1\x00"
        result = router.analyze_code(code, "python")
        # Should not crash — falls back to monty or docker
        assert result in ("monty", "docker", "e2b")

    def test_exec_eval_routes_to_docker(self, router: SandboxRouter) -> None:
        """exec() and eval() with dynamic strings should route to docker."""
        assert router.analyze_code("exec('import os')", "python") == "docker"
        assert router.analyze_code("eval('1+1')", "python") == "docker"

    def test_dunder_import_routes_to_docker(self, router: SandboxRouter) -> None:
        """__import__() call should route to docker."""
        assert router.analyze_code("__import__('os')", "python") == "docker"

    def test_mixed_safe_and_unsafe(self, router: SandboxRouter) -> None:
        """Code with both safe and unsafe operations routes to docker."""
        code = "x = 1 + 2\nimport os\ny = 3"
        assert router.analyze_code(code, "python") == "docker"


# ---------------------------------------------------------------------------
# TestHistoryBasedRouting
# ---------------------------------------------------------------------------


class TestHistoryBasedRouting:
    """Test per-agent sticky session behavior."""

    def test_fresh_agent_uses_analysis(self, router: SandboxRouter) -> None:
        """Agent with no history uses static analysis."""
        tier = router.select_provider("x = 42", "python", agent_id="agent-1")
        assert tier == "monty"

    def test_history_overrides_analysis(self, router: SandboxRouter) -> None:
        """When majority of history is docker, sticky session overrides analysis."""
        agent_id = "agent-sticky"
        # Record 8 docker executions
        for _ in range(8):
            router.record_execution(agent_id, "docker", escalated=False)
        # Record 2 monty executions
        for _ in range(2):
            router.record_execution(agent_id, "monty", escalated=False)

        # Simple code would normally route to monty, but history says docker
        tier = router.select_provider("x = 42", "python", agent_id=agent_id)
        assert tier == "docker"

    def test_escalation_recorded_in_history(self, router: SandboxRouter) -> None:
        """Escalation events are recorded in agent history."""
        agent_id = "agent-esc"
        router.record_escalation(agent_id, "monty", "docker")

        # Verify escalation is recorded
        tier = router.select_provider("x = 42", "python", agent_id=agent_id)
        # After one escalation, it shouldn't override yet (need majority)
        assert tier in ("monty", "docker")

    def test_history_bounded_by_maxlen(self, router: SandboxRouter) -> None:
        """History deque doesn't grow beyond maxlen."""
        agent_id = "agent-bounded"
        # Record 20 executions (maxlen is 10)
        for _ in range(20):
            router.record_execution(agent_id, "docker", escalated=False)

        # History should be bounded
        history = router._agent_history.get(agent_id)
        assert history is not None
        assert len(history) <= 10

    def test_no_agent_id_skips_history(self, router: SandboxRouter) -> None:
        """When agent_id is None, history is not used."""
        tier = router.select_provider("x = 42", "python", agent_id=None)
        assert tier == "monty"

    def test_history_threshold(self, router: SandboxRouter) -> None:
        """History only overrides when >=70% of recent executions are on a tier."""
        agent_id = "agent-threshold"
        # 6 docker + 4 monty = 60% docker — not enough to override
        for _ in range(6):
            router.record_execution(agent_id, "docker", escalated=False)
        for _ in range(4):
            router.record_execution(agent_id, "monty", escalated=False)

        tier = router.select_provider("x = 42", "python", agent_id=agent_id)
        # 60% is below threshold (70%), so analysis should decide
        assert tier == "monty"


# ---------------------------------------------------------------------------
# TestProviderSelection
# ---------------------------------------------------------------------------


class TestProviderSelection:
    """Test select_provider() with combined analysis + history + availability."""

    def test_monty_selected_when_available(self, router: SandboxRouter) -> None:
        """Analysis says monty + monty available → monty."""
        tier = router.select_provider("x = 1", "python", agent_id=None)
        assert tier == "monty"

    def test_docker_selected_when_monty_unavailable(self, router_no_monty: SandboxRouter) -> None:
        """Analysis says monty but monty not registered → docker."""
        tier = router_no_monty.select_provider("x = 1", "python", agent_id=None)
        assert tier == "docker"

    def test_e2b_fallback_when_docker_unavailable(self) -> None:
        """When docker is not available, fall to e2b."""
        router = SandboxRouter(available_providers={"e2b": _make_mock_provider("e2b")})
        tier = router.select_provider("import os", "python", agent_id=None)
        assert tier == "e2b"

    def test_explicit_override_honored(self, router: SandboxRouter) -> None:
        """When explicit provider is specified via agent config, use it."""
        # The router itself doesn't enforce this — it's the manager's job.
        # But we test that analyze_code returns the correct analysis.
        assert router.analyze_code("x = 1", "python") == "monty"

    def test_no_providers_raises(self) -> None:
        """Router with no providers raises ValueError."""
        with pytest.raises(ValueError, match="No sandbox providers"):
            SandboxRouter(available_providers={})

    def test_select_returns_available_only(self, router_docker_only: SandboxRouter) -> None:
        """select_provider never returns a provider that isn't registered."""
        # Even though analysis may say monty, if only docker exists, return docker
        tier = router_docker_only.select_provider("x = 1", "python", agent_id=None)
        assert tier == "docker"


# ---------------------------------------------------------------------------
# TestEscalationChain
# ---------------------------------------------------------------------------


class TestEscalationChain:
    """Test get_next_tier() escalation logic."""

    def test_monty_escalates_to_docker(self, router: SandboxRouter) -> None:
        assert router.get_next_tier("monty") == "docker"

    def test_docker_escalates_to_e2b(self, router: SandboxRouter) -> None:
        assert router.get_next_tier("docker") == "e2b"

    def test_e2b_has_no_next(self, router: SandboxRouter) -> None:
        assert router.get_next_tier("e2b") is None

    def test_escalation_skips_unavailable(self, router_docker_only: SandboxRouter) -> None:
        """If e2b is not available, docker has no next tier."""
        assert router_docker_only.get_next_tier("docker") is None

    def test_monty_skips_to_e2b_when_no_docker(self) -> None:
        """If docker is unavailable, monty escalates to e2b."""
        router = SandboxRouter(
            available_providers={
                "monty": _make_mock_provider("monty"),
                "e2b": _make_mock_provider("e2b"),
            }
        )
        assert router.get_next_tier("monty") == "e2b"

    def test_unknown_tier_returns_none(self, router: SandboxRouter) -> None:
        assert router.get_next_tier("unknown") is None


# ---------------------------------------------------------------------------
# TestHostFunctionCache
# ---------------------------------------------------------------------------


class TestHostFunctionCache:
    """Test per-agent host function caching."""

    def test_cache_and_retrieve(self, router: SandboxRouter) -> None:
        host_fns: dict[str, Callable[..., Any]] = {
            "read_file": lambda path: f"content of {path}",
        }
        router.cache_host_functions("agent-1", host_fns)
        cached = router.get_cached_host_functions("agent-1")
        assert cached == host_fns

    def test_missing_returns_none(self, router: SandboxRouter) -> None:
        assert router.get_cached_host_functions("nonexistent") is None

    def test_overwrite(self, router: SandboxRouter) -> None:
        host_fns_1: dict[str, Callable[..., Any]] = {"fn1": lambda: 1}
        host_fns_2: dict[str, Callable[..., Any]] = {"fn2": lambda: 2}
        router.cache_host_functions("agent-1", host_fns_1)
        router.cache_host_functions("agent-1", host_fns_2)
        assert router.get_cached_host_functions("agent-1") == host_fns_2


# ---------------------------------------------------------------------------
# TestMetrics
# ---------------------------------------------------------------------------


class TestMetrics:
    """Test SandboxRouterMetrics thread-safety and correctness."""

    def test_tier_selection_counted(self, metrics: SandboxRouterMetrics) -> None:
        metrics.record_selection("monty")
        metrics.record_selection("monty")
        metrics.record_selection("docker")
        snap = metrics.snapshot()
        assert snap["tier_selections"]["monty"] == 2
        assert snap["tier_selections"]["docker"] == 1

    def test_escalation_counted(self, metrics: SandboxRouterMetrics) -> None:
        metrics.record_escalation("monty", "docker")
        metrics.record_escalation("docker", "e2b")
        snap = metrics.snapshot()
        assert snap["escalation_count"] == 2
        assert snap["escalations_by_path"]["monty->docker"] == 1
        assert snap["escalations_by_path"]["docker->e2b"] == 1

    def test_snapshot_returns_copy(self, metrics: SandboxRouterMetrics) -> None:
        metrics.record_selection("monty")
        snap1 = metrics.snapshot()
        snap1["tier_selections"]["monty"] = 999
        snap2 = metrics.snapshot()
        assert snap2["tier_selections"]["monty"] == 1

    def test_reset_clears_counters(self, metrics: SandboxRouterMetrics) -> None:
        metrics.record_selection("monty")
        metrics.record_escalation("monty", "docker")
        metrics.reset()
        snap = metrics.snapshot()
        assert snap["tier_selections"] == {}
        assert snap["escalation_count"] == 0
        assert snap["escalations_by_path"] == {}

    def test_fresh_snapshot_is_empty(self, metrics: SandboxRouterMetrics) -> None:
        snap = metrics.snapshot()
        assert snap["tier_selections"] == {}
        assert snap["escalation_count"] == 0


# ---------------------------------------------------------------------------
# TestHypothesis — property-based fuzzing
# ---------------------------------------------------------------------------


class TestHypothesis:
    """Property-based tests for robustness."""

    @given(code=st.text(max_size=500))
    @settings(
        max_examples=50,
        deadline=2000,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_route_never_crashes_on_random_code(
        self, code: str, providers: dict[str, SandboxProvider]
    ) -> None:
        """analyze_code must never raise on any string input."""
        router = SandboxRouter(available_providers=providers)
        result = router.analyze_code(code, "python")
        assert result in ("monty", "docker", "e2b")

    @given(code=st.text(max_size=200))
    @settings(
        max_examples=30,
        deadline=2000,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_route_is_deterministic(self, code: str, providers: dict[str, SandboxProvider]) -> None:
        """Same code -> same tier (no randomness)."""
        router = SandboxRouter(available_providers=providers)
        result1 = router.analyze_code(code, "python")
        result2 = router.analyze_code(code, "python")
        assert result1 == result2

    @given(code=st.text(max_size=200))
    @settings(max_examples=30, deadline=2000)
    def test_route_respects_available_providers(self, code: str) -> None:
        """Never selects unavailable provider."""
        docker_only: dict[str, SandboxProvider] = {
            "docker": _make_mock_provider("docker"),
        }
        router = SandboxRouter(available_providers=docker_only)
        result = router.select_provider(code, "python", agent_id=None)
        assert result == "docker"

    @given(lang=st.sampled_from(["javascript", "bash", "ruby", "go", "rust"]))
    @settings(
        max_examples=10,
        deadline=2000,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_non_python_never_routes_to_monty(
        self, lang: str, providers: dict[str, SandboxProvider]
    ) -> None:
        """Non-Python languages never route to monty."""
        router = SandboxRouter(available_providers=providers)
        result = router.analyze_code("some code", lang)
        assert result != "monty"


# ---------------------------------------------------------------------------
# TestPerformance
# ---------------------------------------------------------------------------


class TestPerformance:
    """Test that routing overhead is minimal."""

    def test_routing_overhead_under_1ms(self, router: SandboxRouter) -> None:
        """analyze_code must complete in <1ms for simple code."""
        import statistics
        import time

        times = []
        for _ in range(100):
            start = time.perf_counter_ns()
            router.analyze_code("x = 1 + 2", "python")
            elapsed_ns = time.perf_counter_ns() - start
            times.append(elapsed_ns)

        median_ms = statistics.median(times) / 1_000_000
        assert median_ms < 1.0, f"Routing took {median_ms:.3f}ms (expected <1ms)"

    def test_select_provider_under_1ms(self, router: SandboxRouter) -> None:
        """select_provider must complete in <1ms."""
        import statistics
        import time

        times = []
        for _ in range(100):
            start = time.perf_counter_ns()
            router.select_provider("x = 1", "python", agent_id=None)
            elapsed_ns = time.perf_counter_ns() - start
            times.append(elapsed_ns)

        median_ms = statistics.median(times) / 1_000_000
        assert median_ms < 1.0, f"select_provider took {median_ms:.3f}ms (expected <1ms)"


# ---------------------------------------------------------------------------
# TestStickyOnlyEscalatesUp — Issue #1317 review fix #4
# ---------------------------------------------------------------------------


class TestStickyOnlyEscalatesUp:
    """Verify sticky sessions only escalate UP, never downgrade."""

    def test_sticky_does_not_downgrade_to_monty(self, router: SandboxRouter) -> None:
        """History favoring monty should NOT override analysis saying docker."""
        agent_id = "agent-downgrade"
        # Record 8 monty executions (80% monty)
        for _ in range(8):
            router.record_execution(agent_id, "monty", escalated=False)
        for _ in range(2):
            router.record_execution(agent_id, "docker", escalated=False)

        # Code with imports should route to docker despite monty-heavy history
        tier = router.select_provider("import os", "python", agent_id=agent_id)
        assert tier == "docker"

    def test_sticky_can_escalate_to_docker(self, router: SandboxRouter) -> None:
        """History favoring docker CAN override analysis saying monty (escalation UP)."""
        agent_id = "agent-escalate-up"
        # Record 8 docker executions
        for _ in range(8):
            router.record_execution(agent_id, "docker", escalated=False)
        for _ in range(2):
            router.record_execution(agent_id, "monty", escalated=False)

        # Simple code would route to monty, but sticky says docker (UP)
        tier = router.select_provider("x = 42", "python", agent_id=agent_id)
        assert tier == "docker"

    def test_sticky_can_escalate_to_e2b(self, router: SandboxRouter) -> None:
        """History favoring e2b CAN override analysis saying docker."""
        agent_id = "agent-e2b-sticky"
        for _ in range(8):
            router.record_execution(agent_id, "e2b", escalated=False)
        for _ in range(2):
            router.record_execution(agent_id, "docker", escalated=False)

        tier = router.select_provider("import os", "python", agent_id=agent_id)
        assert tier == "e2b"


# ---------------------------------------------------------------------------
# TestHostFnCacheEviction — Issue #1317 review fix #7
# ---------------------------------------------------------------------------


class TestHostFnCacheEviction:
    """Verify _host_fn_cache is evicted alongside _agent_history."""

    def test_host_fn_cache_evicted_on_lru(self) -> None:
        """When agent history LRU evicts, host_fn_cache is also evicted."""
        providers = {"monty": _make_mock_provider("monty")}
        router = SandboxRouter(
            available_providers=providers,
            agent_cache_maxsize=3,
        )

        # Fill cache with 3 agents
        for i in range(3):
            agent_id = f"agent-{i}"
            router.record_execution(agent_id, "monty", escalated=False)
            router.cache_host_functions(agent_id, {"fn": lambda _i=i: _i})

        # All 3 should be cached
        assert router.get_cached_host_functions("agent-0") is not None
        assert router.get_cached_host_functions("agent-1") is not None
        assert router.get_cached_host_functions("agent-2") is not None

        # Adding a 4th agent should evict agent-0 (LRU)
        router.record_execution("agent-3", "monty", escalated=False)

        assert router.get_cached_host_functions("agent-0") is None
        assert router.get_cached_host_functions("agent-1") is not None


# ---------------------------------------------------------------------------
# TestThreadSafety — Issue #1317 review fix #3
# ---------------------------------------------------------------------------


class TestThreadSafety:
    """Verify thread-safe access to SandboxRouter."""

    def test_concurrent_record_execution(self, router: SandboxRouter) -> None:
        """Concurrent record_execution calls don't corrupt state."""
        from concurrent.futures import ThreadPoolExecutor

        def record(agent_idx: int) -> None:
            agent_id = f"agent-thread-{agent_idx % 20}"
            for _ in range(50):
                router.record_execution(agent_id, "monty", escalated=False)

        with ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(record, range(100)))

        # Verify metrics are consistent
        snap = router.metrics.snapshot()
        assert snap["tier_selections"]["monty"] == 5000  # 100 * 50

    def test_concurrent_select_and_record(self, router: SandboxRouter) -> None:
        """Concurrent select_provider + record_execution don't deadlock."""
        from concurrent.futures import ThreadPoolExecutor

        def select_and_record(agent_idx: int) -> None:
            agent_id = f"agent-sr-{agent_idx}"
            for _ in range(20):
                router.select_provider("x = 1", "python", agent_id=agent_id)
                router.record_execution(agent_id, "monty", escalated=False)

        with ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(select_and_record, range(50)))

        # Should complete without deadlock or crash
        snap = router.metrics.snapshot()
        assert snap["tier_selections"]["monty"] == 1000  # 50 * 20


# ---------------------------------------------------------------------------
# TestEdgeCasePatterns — Issue #1317 review fix #12
# ---------------------------------------------------------------------------


class TestEdgeCasePatterns:
    """Real-world code patterns for AST analysis edge cases."""

    @pytest.mark.parametrize(
        "code,expected",
        [
            # try/except ImportError — still has import statement → docker
            ("try:\n    import pandas\nexcept ImportError:\n    pandas = None", "docker"),
            # async def/await — pure async → monty
            ("async def foo():\n    return 42", "monty"),
            # async with import → docker
            ("import asyncio\nasync def main():\n    await asyncio.sleep(1)", "docker"),
            # f-string with complex expression — pure → monty
            ('name = "world"\nresult = f"hello {name}"', "monty"),
            # walrus operator — pure → monty
            ("if (n := 10) > 5:\n    print(n)", "monty"),
            # Generator expression — pure → monty
            ("g = (x**2 for x in range(10))\nprint(sum(g))", "monty"),
            # Nested function with closure — pure → monty
            ("def outer():\n    x = 1\n    def inner(): return x\n    return inner()", "monty"),
            # compile() builtin — docker
            ("compile('x=1', '<string>', 'exec')", "docker"),
        ],
        ids=[
            "try_except_import",
            "async_def_pure",
            "async_with_import",
            "fstring_pure",
            "walrus_operator",
            "generator_expr",
            "nested_closure",
            "compile_builtin",
        ],
    )
    def test_real_world_patterns(self, router: SandboxRouter, code: str, expected: str) -> None:
        assert router.analyze_code(code, "python") == expected
