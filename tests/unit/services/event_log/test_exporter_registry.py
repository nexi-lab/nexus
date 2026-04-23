"""Unit tests for ExporterRegistry — parallel dispatch, circuit breaker, and timeout.

Issue #1138: Event Stream Export.
Issue #2750: Per-exporter circuit breaker and timeout.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, PropertyMock

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.lib.circuit_breaker import CircuitState
from nexus.services.event_bus.types import FileEvent, FileEventType
from nexus.services.event_log.exporter_registry import ExporterRegistry


def _make_event(event_id: str = "evt-1", path: str = "/test.txt") -> FileEvent:
    return FileEvent(
        type=FileEventType.FILE_WRITE,
        path=path,
        zone_id=ROOT_ZONE_ID,
        event_id=event_id,
    )


def _make_mock_exporter(name: str, fail_ids: list[str] | None = None):
    """Create a mock exporter that implements the protocol."""
    exporter = MagicMock()
    type(exporter).name = PropertyMock(return_value=name)
    exporter.publish = AsyncMock()
    exporter.publish_batch = AsyncMock(return_value=fail_ids or [])
    exporter.close = AsyncMock()
    exporter.health_check = AsyncMock(return_value=True)
    return exporter


class TestExporterRegistry:
    """Test ExporterRegistry register/unregister/dispatch."""

    def test_register_and_unregister(self) -> None:
        registry = ExporterRegistry()
        exporter = _make_mock_exporter("kafka")

        registry.register(exporter)
        assert "kafka" in registry.exporter_names

        registry.unregister("kafka")
        assert "kafka" not in registry.exporter_names

    def test_unregister_nonexistent_is_noop(self) -> None:
        registry = ExporterRegistry()
        registry.unregister("nonexistent")  # Should not raise

    def test_dispatch_batch_empty_events(self) -> None:
        registry = ExporterRegistry()
        exporter = _make_mock_exporter("kafka")
        registry.register(exporter)

        result = asyncio.run(registry.dispatch_batch([]))
        assert result == {}
        exporter.publish_batch.assert_not_called()

    def test_dispatch_batch_empty_registry(self) -> None:
        registry = ExporterRegistry()
        events = [_make_event()]

        result = asyncio.run(registry.dispatch_batch(events))
        assert result == {}

    def test_dispatch_batch_success(self) -> None:
        registry = ExporterRegistry()
        exporter = _make_mock_exporter("kafka")
        registry.register(exporter)

        events = [_make_event("e1"), _make_event("e2")]
        result = asyncio.run(registry.dispatch_batch(events))

        assert result == {}  # No failures
        exporter.publish_batch.assert_called_once_with(events)

    def test_dispatch_batch_partial_failure(self) -> None:
        registry = ExporterRegistry()
        exporter = _make_mock_exporter("kafka", fail_ids=["e2"])
        registry.register(exporter)

        events = [_make_event("e1"), _make_event("e2")]
        result = asyncio.run(registry.dispatch_batch(events))

        assert result == {"kafka": ["e2"]}

    def test_dispatch_batch_exporter_exception(self) -> None:
        registry = ExporterRegistry()
        exporter = _make_mock_exporter("kafka")
        exporter.publish_batch = AsyncMock(side_effect=ConnectionError("down"))
        registry.register(exporter)

        events = [_make_event("e1"), _make_event("e2")]
        result = asyncio.run(registry.dispatch_batch(events))

        # All events should be reported as failed
        assert "kafka" in result
        assert set(result["kafka"]) == {"e1", "e2"}

    def test_parallel_dispatch_to_multiple_exporters(self) -> None:
        registry = ExporterRegistry()
        kafka = _make_mock_exporter("kafka")
        nats = _make_mock_exporter("nats", fail_ids=["e1"])
        registry.register(kafka)
        registry.register(nats)

        events = [_make_event("e1"), _make_event("e2")]
        result = asyncio.run(registry.dispatch_batch(events))

        # Kafka succeeded, NATS failed for e1
        assert "kafka" not in result
        assert result == {"nats": ["e1"]}
        kafka.publish_batch.assert_called_once()
        nats.publish_batch.assert_called_once()

    def test_close_all(self) -> None:
        registry = ExporterRegistry()
        kafka = _make_mock_exporter("kafka")
        nats = _make_mock_exporter("nats")
        registry.register(kafka)
        registry.register(nats)

        asyncio.run(registry.close_all())

        kafka.close.assert_called_once()
        nats.close.assert_called_once()
        assert registry.exporter_names == []

    def test_health_check(self) -> None:
        registry = ExporterRegistry()
        kafka = _make_mock_exporter("kafka")
        kafka.health_check = AsyncMock(return_value=True)
        nats = _make_mock_exporter("nats")
        nats.health_check = AsyncMock(return_value=False)
        registry.register(kafka)
        registry.register(nats)

        result = asyncio.run(registry.health_check())

        assert result == {"kafka": True, "nats": False}

    def test_health_check_exception_returns_false(self) -> None:
        registry = ExporterRegistry()
        exporter = _make_mock_exporter("kafka")
        exporter.health_check = AsyncMock(side_effect=Exception("boom"))
        registry.register(exporter)

        result = asyncio.run(registry.health_check())
        assert result == {"kafka": False}


class TestExporterRegistryCircuitBreaker:
    """Test per-exporter circuit breaker behavior (Issue #2750)."""

    def test_register_creates_breaker(self) -> None:
        """Each registered exporter gets its own circuit breaker."""
        registry = ExporterRegistry()
        exporter = _make_mock_exporter("kafka")
        registry.register(exporter)

        assert "kafka" in registry.breaker_states
        assert registry.breaker_states["kafka"] == CircuitState.CLOSED.value

    def test_unregister_removes_breaker(self) -> None:
        """Unregistering an exporter removes its circuit breaker."""
        registry = ExporterRegistry()
        exporter = _make_mock_exporter("kafka")
        registry.register(exporter)
        registry.unregister("kafka")

        assert "kafka" not in registry.breaker_states

    def test_custom_failure_threshold(self) -> None:
        """Registry accepts custom failure_threshold parameter."""
        registry = ExporterRegistry(failure_threshold=5)
        assert registry._failure_threshold == 5

    def test_custom_reset_timeout(self) -> None:
        """Registry accepts custom reset_timeout parameter."""
        registry = ExporterRegistry(reset_timeout=120.0)
        assert registry._reset_timeout == 120.0

    def test_custom_exporter_timeout(self) -> None:
        """Registry accepts custom exporter_timeout parameter."""
        registry = ExporterRegistry(exporter_timeout=15.0)
        assert registry._exporter_timeout == 15.0

    def test_success_keeps_breaker_closed(self) -> None:
        """Successful dispatches keep the circuit breaker CLOSED."""
        registry = ExporterRegistry()
        exporter = _make_mock_exporter("kafka")
        registry.register(exporter)

        events = [_make_event("e1")]
        asyncio.run(registry.dispatch_batch(events))

        assert registry.breaker_states["kafka"] == CircuitState.CLOSED.value

    async def test_breaker_trips_after_threshold_failures(self) -> None:
        """Circuit breaker opens after failure_threshold consecutive failures."""

        async def _run():
            registry = ExporterRegistry(failure_threshold=3)
            exporter = _make_mock_exporter("kafka")
            exporter.publish_batch = AsyncMock(side_effect=ConnectionError("down"))
            registry.register(exporter)

            events = [_make_event("e1")]
            for _ in range(3):
                await registry.dispatch_batch(events)

            assert registry.breaker_states["kafka"] == CircuitState.OPEN.value

        asyncio.run(_run())

    async def test_open_breaker_fast_fails(self) -> None:
        """OPEN circuit breaker returns all event IDs without calling exporter."""

        async def _run():
            registry = ExporterRegistry(failure_threshold=3)
            exporter = _make_mock_exporter("kafka")
            exporter.publish_batch = AsyncMock(side_effect=ConnectionError("down"))
            registry.register(exporter)

            events = [_make_event("e1")]
            for _ in range(3):
                await registry.dispatch_batch(events)

            # Reset the mock to verify it's not called during fast-fail
            exporter.publish_batch.reset_mock()

            result = await registry.dispatch_batch([_make_event("e2"), _make_event("e3")])

            assert result == {"kafka": ["e2", "e3"]}
            exporter.publish_batch.assert_not_called()

        asyncio.run(_run())

    async def test_partial_failure_records_failure_on_breaker(self) -> None:
        """Partial failures (some event IDs returned) record as failure."""

        async def _run():
            registry = ExporterRegistry(failure_threshold=3)
            exporter = _make_mock_exporter("kafka", fail_ids=["e1"])
            registry.register(exporter)

            events = [_make_event("e1"), _make_event("e2")]
            for _ in range(3):
                await registry.dispatch_batch(events)

            assert registry.breaker_states["kafka"] == CircuitState.OPEN.value

        asyncio.run(_run())

    async def test_success_records_success_on_breaker(self) -> None:
        """Successful dispatch after failures keeps breaker CLOSED."""

        async def _run():
            registry = ExporterRegistry(failure_threshold=3)
            exporter = _make_mock_exporter("kafka")
            registry.register(exporter)

            events = [_make_event("e1")]

            # 2 failures then 1 success
            exporter.publish_batch = AsyncMock(side_effect=ConnectionError("down"))
            for _ in range(2):
                await registry.dispatch_batch(events)

            exporter.publish_batch = AsyncMock(return_value=[])
            await registry.dispatch_batch(events)

            assert registry.breaker_states["kafka"] == CircuitState.CLOSED.value

        asyncio.run(_run())

    async def test_independent_breakers_per_exporter(self) -> None:
        """Each exporter has its own independent circuit breaker."""

        async def _run():
            registry = ExporterRegistry(failure_threshold=3)
            kafka = _make_mock_exporter("kafka")
            kafka.publish_batch = AsyncMock(side_effect=ConnectionError("down"))
            nats = _make_mock_exporter("nats")  # succeeds
            registry.register(kafka)
            registry.register(nats)

            events = [_make_event("e1")]
            for _ in range(3):
                await registry.dispatch_batch(events)

            assert registry.breaker_states["kafka"] == CircuitState.OPEN.value
            assert registry.breaker_states["nats"] == CircuitState.CLOSED.value

        asyncio.run(_run())


class TestExporterRegistryTimeout:
    """Test per-exporter timeout behavior (Issue #2750)."""

    async def test_timeout_returns_all_ids_as_failed(self) -> None:
        """Exporter timeout returns all event IDs as failed."""

        async def _run():
            registry = ExporterRegistry(exporter_timeout=0.05)
            exporter = _make_mock_exporter("kafka")

            async def slow_publish(events):  # noqa: ARG001
                await asyncio.sleep(1.0)
                return []

            exporter.publish_batch = slow_publish
            registry.register(exporter)

            events = [_make_event("e1"), _make_event("e2")]
            result = await registry.dispatch_batch(events)

            assert result == {"kafka": ["e1", "e2"]}

        asyncio.run(_run())

    async def test_timeout_records_failure_on_breaker(self) -> None:
        """Timeout records a failure on the circuit breaker."""

        async def _run():
            registry = ExporterRegistry(failure_threshold=3, exporter_timeout=0.05)
            exporter = _make_mock_exporter("kafka")

            async def slow_publish(events):  # noqa: ARG001
                await asyncio.sleep(1.0)
                return []

            exporter.publish_batch = slow_publish
            registry.register(exporter)

            events = [_make_event("e1")]
            for _ in range(3):
                await registry.dispatch_batch(events)

            assert registry.breaker_states["kafka"] == CircuitState.OPEN.value

        asyncio.run(_run())

    async def test_slow_exporter_does_not_block_fast_exporter(self) -> None:
        """A slow exporter does not block delivery to a fast exporter."""

        async def _run():
            registry = ExporterRegistry(exporter_timeout=0.05)

            slow = _make_mock_exporter("slow")

            async def slow_publish(events):  # noqa: ARG001
                await asyncio.sleep(1.0)
                return []

            slow.publish_batch = slow_publish

            fast = _make_mock_exporter("fast")
            registry.register(slow)
            registry.register(fast)

            events = [_make_event("e1")]
            result = await registry.dispatch_batch(events)

            assert result == {"slow": ["e1"]}
            fast.publish_batch.assert_called_once()

        asyncio.run(_run())

    def test_default_timeout_is_30s(self) -> None:
        """Default exporter timeout is 30 seconds."""
        registry = ExporterRegistry()
        assert registry._exporter_timeout == 30.0

    def test_default_failure_threshold_is_3(self) -> None:
        """Default failure threshold is 3."""
        registry = ExporterRegistry()
        assert registry._failure_threshold == 3

    def test_default_reset_timeout_is_60(self) -> None:
        """Default reset timeout is 60 seconds."""
        registry = ExporterRegistry()
        assert registry._reset_timeout == 60.0
