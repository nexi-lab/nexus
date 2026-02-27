"""Tests for ObservabilityRegistry and LifecycleComponent protocol.

Issue #2072: Unified observability lifecycle management.
"""

import pytest

from nexus.server.observability.registry import (
    ComponentStatus,
    LifecycleComponent,
    ObservabilityRegistry,
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class FakeComponent:
    """Minimal LifecycleComponent for testing."""

    def __init__(
        self, component_name: str = "fake", *, fail_start: bool = False, fail_shutdown: bool = False
    ) -> None:
        self._name = component_name
        self._fail_start = fail_start
        self._fail_shutdown = fail_shutdown
        self._started = False
        self._shutdown_called = False
        self.start_order: int | None = None
        self.shutdown_order: int | None = None

    @property
    def name(self) -> str:
        return self._name

    async def start(self) -> None:
        if self._fail_start:
            raise RuntimeError(f"{self._name} start failed")
        self._started = True

    async def shutdown(self, timeout_ms: int = 5000) -> None:
        if self._fail_shutdown:
            raise RuntimeError(f"{self._name} shutdown failed")
        self._shutdown_called = True
        self._started = False

    def is_healthy(self) -> bool:
        return self._started


# Track ordering across components
_order_counter = 0


class OrderedComponent(FakeComponent):
    """FakeComponent that records start/shutdown order."""

    async def start(self) -> None:
        global _order_counter
        await super().start()
        _order_counter += 1
        self.start_order = _order_counter

    async def shutdown(self, timeout_ms: int = 5000) -> None:
        global _order_counter
        await super().shutdown(timeout_ms)
        _order_counter += 1
        self.shutdown_order = _order_counter


@pytest.fixture(autouse=True)
def _reset_order_counter() -> None:
    global _order_counter
    _order_counter = 0


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLifecycleComponentProtocol:
    """Verify that FakeComponent satisfies the LifecycleComponent protocol."""

    def test_fake_component_is_lifecycle_component(self) -> None:
        comp = FakeComponent()
        assert isinstance(comp, LifecycleComponent)


class TestObservabilityRegistry:
    """Tests for ObservabilityRegistry."""

    def test_register_component(self) -> None:
        registry = ObservabilityRegistry()
        comp = FakeComponent("test")
        registry.register("test", comp)
        assert len(registry._components) == 1
        assert registry._components[0][0] == "test"

    @pytest.mark.asyncio
    async def test_start_all_calls_components_in_order(self) -> None:
        registry = ObservabilityRegistry()
        c1 = OrderedComponent("first")
        c2 = OrderedComponent("second")
        c3 = OrderedComponent("third")
        registry.register("first", c1)
        registry.register("second", c2)
        registry.register("third", c3)

        statuses = await registry.start_all()

        assert len(statuses) == 3
        assert all(s.started for s in statuses)
        assert c1.start_order == 1
        assert c2.start_order == 2
        assert c3.start_order == 3

    @pytest.mark.asyncio
    async def test_shutdown_all_calls_components_in_reverse_order(self) -> None:
        registry = ObservabilityRegistry()
        c1 = OrderedComponent("first")
        c2 = OrderedComponent("second")
        c3 = OrderedComponent("third")
        registry.register("first", c1)
        registry.register("second", c2)
        registry.register("third", c3)

        await registry.start_all()
        # Reset counter to track shutdown order starting from 1
        global _order_counter
        _order_counter = 0

        await registry.shutdown_all()

        # Reverse order: third=1, second=2, first=3
        assert c3.shutdown_order == 1
        assert c2.shutdown_order == 2
        assert c1.shutdown_order == 3

    @pytest.mark.asyncio
    async def test_shutdown_continues_on_component_error(self) -> None:
        registry = ObservabilityRegistry()
        c1 = FakeComponent("first")
        c2 = FakeComponent("second", fail_shutdown=True)
        c3 = FakeComponent("third")
        registry.register("first", c1)
        registry.register("second", c2)
        registry.register("third", c3)

        await registry.start_all()
        await registry.shutdown_all()

        # c2 shutdown raised, but c1 and c3 should still shut down
        assert c1._shutdown_called
        assert c3._shutdown_called

    @pytest.mark.asyncio
    async def test_shutdown_idempotent(self) -> None:
        registry = ObservabilityRegistry()
        comp = FakeComponent("test")
        registry.register("test", comp)

        await registry.start_all()
        await registry.shutdown_all()
        assert comp._shutdown_called

        # Reset and call again — should be a no-op
        comp._shutdown_called = False
        await registry.shutdown_all()
        assert not comp._shutdown_called

    @pytest.mark.asyncio
    async def test_shutdown_safe_without_start(self) -> None:
        registry = ObservabilityRegistry()
        comp = FakeComponent("test")
        registry.register("test", comp)

        # Should not raise
        await registry.shutdown_all()
        assert not comp._shutdown_called

    @pytest.mark.asyncio
    async def test_required_component_failure_aborts_startup(self) -> None:
        registry = ObservabilityRegistry()
        c1 = FakeComponent("first")
        c2 = FakeComponent("failing", fail_start=True)
        c3 = FakeComponent("never-reached")
        registry.register("first", c1)
        registry.register("failing", c2, required=True)
        registry.register("never-reached", c3)

        with pytest.raises(RuntimeError, match="Required component 'failing' failed to start"):
            await registry.start_all()

        # c1 should have been rolled back (shutdown called)
        assert c1._shutdown_called
        # c3 should never have started
        assert not c3._started

    @pytest.mark.asyncio
    async def test_optional_component_failure_continues(self) -> None:
        registry = ObservabilityRegistry()
        c1 = FakeComponent("first")
        c2 = FakeComponent("optional-fail", fail_start=True)
        c3 = FakeComponent("third")
        registry.register("first", c1)
        registry.register("optional-fail", c2, required=False)
        registry.register("third", c3)

        statuses = await registry.start_all()

        assert c1._started
        assert not c2._started
        assert c3._started
        # Status should reflect the failure
        assert statuses[1].started is False
        assert statuses[1].error is not None

    def test_status_reports_all_components(self) -> None:
        registry = ObservabilityRegistry()
        c1 = FakeComponent("healthy")
        c1._started = True
        c2 = FakeComponent("not-started")
        registry.register("healthy", c1)
        registry.register("not-started", c2)
        # Simulate c1 being in _started list
        registry._started.append("healthy")

        result = registry.status()

        assert len(result) == 2
        assert result[0] == ComponentStatus(name="healthy", started=True, healthy=True)
        assert result[1] == ComponentStatus(name="not-started", started=False, healthy=False)

    @pytest.mark.asyncio
    async def test_lifespan_context_manager(self) -> None:
        registry = ObservabilityRegistry()
        comp = FakeComponent("test")
        registry.register("test", comp)

        async with registry.lifespan():
            assert comp._started

        assert comp._shutdown_called


# ---------------------------------------------------------------------------
# FunctionPairComponent tests (Issue #2072)
# ---------------------------------------------------------------------------


class TestFunctionPairComponent:
    """Tests for the generic FunctionPairComponent adapter."""

    def test_satisfies_lifecycle_protocol(self) -> None:
        from nexus.server.observability.components import FunctionPairComponent

        comp = FunctionPairComponent("test", start_fn=lambda: None)
        assert isinstance(comp, LifecycleComponent)

    @pytest.mark.asyncio
    async def test_start_calls_start_fn(self) -> None:
        from nexus.server.observability.components import FunctionPairComponent

        called = []
        comp = FunctionPairComponent("test", start_fn=lambda: called.append("start"))
        await comp.start()
        assert called == ["start"]
        assert comp.is_healthy()

    @pytest.mark.asyncio
    async def test_shutdown_calls_stop_fn(self) -> None:
        from nexus.server.observability.components import FunctionPairComponent

        called = []
        comp = FunctionPairComponent(
            "test", start_fn=lambda: None, stop_fn=lambda: called.append("stop")
        )
        await comp.start()
        await comp.shutdown()
        assert called == ["stop"]
        assert not comp.is_healthy()

    @pytest.mark.asyncio
    async def test_shutdown_noop_before_start(self) -> None:
        from nexus.server.observability.components import FunctionPairComponent

        called = []
        comp = FunctionPairComponent(
            "test", start_fn=lambda: None, stop_fn=lambda: called.append("stop")
        )
        await comp.shutdown()
        assert called == []

    @pytest.mark.asyncio
    async def test_shutdown_graceful_when_stop_fn_none(self) -> None:
        from nexus.server.observability.components import FunctionPairComponent

        comp = FunctionPairComponent("test", start_fn=lambda: None, stop_fn=None)
        await comp.start()
        await comp.shutdown()  # Should not raise
        assert not comp.is_healthy()

    @pytest.mark.asyncio
    async def test_start_kwargs_forwarded(self) -> None:
        from nexus.server.observability.components import FunctionPairComponent

        captured = {}

        def _start(env: str = "prod") -> None:
            captured["env"] = env

        comp = FunctionPairComponent("test", start_fn=_start, start_kwargs={"env": "dev"})
        await comp.start()
        assert captured["env"] == "dev"

    @pytest.mark.asyncio
    async def test_shutdown_error_does_not_raise(self) -> None:
        from nexus.server.observability.components import FunctionPairComponent

        def _bad_stop() -> None:
            raise RuntimeError("stop failed")

        comp = FunctionPairComponent("test", start_fn=lambda: None, stop_fn=_bad_stop)
        await comp.start()
        await comp.shutdown()  # Should not raise
        assert not comp.is_healthy()


# ---------------------------------------------------------------------------
# create_registry() wiring tests (Issue #2072)
# ---------------------------------------------------------------------------


class TestCreateRegistry:
    """Tests for create_registry() wiring (Issue #2072)."""

    def test_registers_all_providers(self) -> None:
        """create_registry() should register 5 observability providers."""
        from nexus.server.lifespan.observability import create_registry

        registry = create_registry()
        names = [name for name, _, _ in registry._components]
        assert "logging" in names
        assert "otel-tracing" in names
        assert "sentry" in names
        assert "pyroscope" in names
        assert "prometheus" in names
        assert len(names) == 5

    def test_registration_order_matches_dependency_order(self) -> None:
        from nexus.server.lifespan.observability import create_registry

        registry = create_registry()
        names = [name for name, _, _ in registry._components]
        # Logging must be first (other components may log during startup)
        assert names[0] == "logging"

    @pytest.mark.asyncio
    async def test_start_all_does_not_raise_on_missing_deps(self) -> None:
        """start_all() should gracefully handle missing optional deps."""
        from nexus.server.lifespan.observability import create_registry

        registry = create_registry()
        # All components are optional (required=False), so even if
        # otel/sentry/pyroscope aren't installed, start should succeed
        statuses = await registry.start_all()
        # At minimum logging should start
        assert any(s.started for s in statuses)
        await registry.shutdown_all()


# ---------------------------------------------------------------------------
# Registry performance benchmark (Issue #2072)
# ---------------------------------------------------------------------------


class TestRegistryPerformance:
    """Benchmark registry overhead (Issue #2072)."""

    @pytest.mark.asyncio
    async def test_start_shutdown_overhead_under_100ms(self) -> None:
        """Registry with 10 mock components should start+shutdown in <100ms."""
        import time

        registry = ObservabilityRegistry()
        for i in range(10):
            registry.register(f"mock-{i}", FakeComponent(f"mock-{i}"))

        start = time.perf_counter()
        await registry.start_all()
        await registry.shutdown_all()
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert elapsed_ms < 100, f"Registry overhead: {elapsed_ms:.1f}ms"
