"""Unit tests for ExporterRegistry — parallel dispatch and failure collection.

Issue #1138: Event Stream Export.
"""

from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.services.event_subsystem.log.exporter_registry import ExporterRegistry
from nexus.services.event_subsystem.types import FileEvent, FileEventType


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

    @pytest.mark.asyncio
    async def test_dispatch_batch_empty_events(self) -> None:
        registry = ExporterRegistry()
        exporter = _make_mock_exporter("kafka")
        registry.register(exporter)

        result = await registry.dispatch_batch([])
        assert result == {}
        exporter.publish_batch.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatch_batch_empty_registry(self) -> None:
        registry = ExporterRegistry()
        events = [_make_event()]

        result = await registry.dispatch_batch(events)
        assert result == {}

    @pytest.mark.asyncio
    async def test_dispatch_batch_success(self) -> None:
        registry = ExporterRegistry()
        exporter = _make_mock_exporter("kafka")
        registry.register(exporter)

        events = [_make_event("e1"), _make_event("e2")]
        result = await registry.dispatch_batch(events)

        assert result == {}  # No failures
        exporter.publish_batch.assert_called_once_with(events)

    @pytest.mark.asyncio
    async def test_dispatch_batch_partial_failure(self) -> None:
        registry = ExporterRegistry()
        exporter = _make_mock_exporter("kafka", fail_ids=["e2"])
        registry.register(exporter)

        events = [_make_event("e1"), _make_event("e2")]
        result = await registry.dispatch_batch(events)

        assert result == {"kafka": ["e2"]}

    @pytest.mark.asyncio
    async def test_dispatch_batch_exporter_exception(self) -> None:
        registry = ExporterRegistry()
        exporter = _make_mock_exporter("kafka")
        exporter.publish_batch = AsyncMock(side_effect=ConnectionError("down"))
        registry.register(exporter)

        events = [_make_event("e1"), _make_event("e2")]
        result = await registry.dispatch_batch(events)

        # All events should be reported as failed
        assert "kafka" in result
        assert set(result["kafka"]) == {"e1", "e2"}

    @pytest.mark.asyncio
    async def test_parallel_dispatch_to_multiple_exporters(self) -> None:
        registry = ExporterRegistry()
        kafka = _make_mock_exporter("kafka")
        nats = _make_mock_exporter("nats", fail_ids=["e1"])
        registry.register(kafka)
        registry.register(nats)

        events = [_make_event("e1"), _make_event("e2")]
        result = await registry.dispatch_batch(events)

        # Kafka succeeded, NATS failed for e1
        assert "kafka" not in result
        assert result == {"nats": ["e1"]}
        kafka.publish_batch.assert_called_once()
        nats.publish_batch.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_all(self) -> None:
        registry = ExporterRegistry()
        kafka = _make_mock_exporter("kafka")
        nats = _make_mock_exporter("nats")
        registry.register(kafka)
        registry.register(nats)

        await registry.close_all()

        kafka.close.assert_called_once()
        nats.close.assert_called_once()
        assert registry.exporter_names == []

    @pytest.mark.asyncio
    async def test_health_check(self) -> None:
        registry = ExporterRegistry()
        kafka = _make_mock_exporter("kafka")
        kafka.health_check = AsyncMock(return_value=True)
        nats = _make_mock_exporter("nats")
        nats.health_check = AsyncMock(return_value=False)
        registry.register(kafka)
        registry.register(nats)

        result = await registry.health_check()

        assert result == {"kafka": True, "nats": False}

    @pytest.mark.asyncio
    async def test_health_check_exception_returns_false(self) -> None:
        registry = ExporterRegistry()
        exporter = _make_mock_exporter("kafka")
        exporter.health_check = AsyncMock(side_effect=Exception("boom"))
        registry.register(exporter)

        result = await registry.health_check()
        assert result == {"kafka": False}
