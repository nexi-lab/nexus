"""Tests for event ordering guarantees (Issue #2755).

Verifies that:
- Events within a zone are delivered in sequence_number order
- sequence_number is included in FileEvent payloads
- Kafka exporter uses zone_id as partition key
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from nexus.core.file_events import FileEvent, FileEventType
from nexus.services.event_log.delivery import EventDeliveryWorker


def _make_record(
    operation_id: str,
    zone_id: str,
    sequence_number: int,
    operation_type: str = "write",
    path: str = "/test.txt",
) -> Mock:
    """Create a mock OperationLogModel record."""
    record = Mock()
    record.operation_id = operation_id
    record.zone_id = zone_id
    record.sequence_number = sequence_number
    record.operation_type = operation_type
    record.path = path
    record.new_path = None
    record.agent_id = "agent-1"
    record.created_at = Mock(isoformat=Mock(return_value="2025-01-01T00:00:00"))
    record.delivered = False
    return record


class TestFileEventSequenceNumber:
    """Test that sequence_number is carried through FileEvent."""

    def test_sequence_number_in_to_dict(self):
        """sequence_number appears in serialized dict when set."""
        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/a.txt",
            zone_id="zone-1",
            sequence_number=42,
        )
        d = event.to_dict()
        assert d["sequence_number"] == 42

    def test_sequence_number_absent_when_none(self):
        """sequence_number omitted from dict when None."""
        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/a.txt",
            zone_id="zone-1",
        )
        assert "sequence_number" not in event.to_dict()

    def test_sequence_number_roundtrip(self):
        """sequence_number survives to_dict -> from_dict roundtrip."""
        original = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/a.txt",
            zone_id="zone-1",
            sequence_number=99,
        )
        restored = FileEvent.from_dict(original.to_dict())
        assert restored.sequence_number == 99

    def test_from_dict_without_sequence_number(self):
        """from_dict handles missing sequence_number gracefully."""
        event = FileEvent.from_dict({"type": "file_write", "path": "/a.txt"})
        assert event.sequence_number is None


class TestDeliveryWorkerOrdering:
    """Test that EventDeliveryWorker dispatches in sequence_number order per zone."""

    def _make_worker(self, event_bus=None, sub_manager_getter=None):
        """Create a worker with a mock record store."""
        mock_record_store = Mock()
        mock_record_store.session_factory = MagicMock()

        return EventDeliveryWorker(
            record_store=mock_record_store,
            event_bus=event_bus,
            subscription_manager_getter=sub_manager_getter,
            batch_size=50,
        )

    def test_build_file_event_includes_sequence_number(self):
        """_build_file_event copies sequence_number from record."""
        record = _make_record("op-1", "zone-1", sequence_number=7)
        worker = self._make_worker()

        event = worker._build_file_event(record)
        assert event.sequence_number == 7

    def test_per_zone_dispatch_order(self):
        """Events dispatched in sequence_number order within each zone."""
        # Two zones, interleaved sequence numbers
        records = [
            _make_record("op-1", "zone-a", sequence_number=1, path="/a1.txt"),
            _make_record("op-3", "zone-b", sequence_number=3, path="/b1.txt"),
            _make_record("op-2", "zone-a", sequence_number=2, path="/a2.txt"),
            _make_record("op-4", "zone-b", sequence_number=4, path="/b2.txt"),
        ]

        dispatch_log: list[tuple[str, int]] = []
        worker = self._make_worker()

        def tracking_dispatch(event, record):  # noqa: ARG001
            dispatch_log.append((event.zone_id, event.sequence_number))

        worker._dispatch_event_internal = tracking_dispatch

        # Build events from records, then simulate the grouping logic
        # from _poll_and_dispatch without needing a real DB session.
        import itertools

        from nexus.contracts.constants import ROOT_ZONE_ID

        events_with_records = [(worker._build_file_event(r), r) for r in records]

        def zone_key(pair):
            return pair[1].zone_id or ROOT_ZONE_ID

        sorted_pairs = sorted(events_with_records, key=zone_key)
        for _zone_id, zone_group in itertools.groupby(sorted_pairs, key=zone_key):
            for event, record in zone_group:
                worker._dispatch_event_internal(event, record)

        # Within zone-a: seq 1 before seq 2
        zone_a = [(z, s) for z, s in dispatch_log if z == "zone-a"]
        assert zone_a == [("zone-a", 1), ("zone-a", 2)]

        # Within zone-b: seq 3 before seq 4
        zone_b = [(z, s) for z, s in dispatch_log if z == "zone-b"]
        assert zone_b == [("zone-b", 3), ("zone-b", 4)]

    @pytest.mark.asyncio
    async def test_sequence_number_in_broadcast_data(self):
        """Webhook broadcast includes sequence_number in event data."""
        record = _make_record("op-1", "zone-1", sequence_number=42)

        broadcast_calls: list[dict] = []
        mock_sub_manager = Mock()

        async def capture_broadcast(event_type, data, zone_id):  # noqa: ARG001
            broadcast_calls.append(data)

        mock_sub_manager.broadcast = capture_broadcast

        worker = self._make_worker(
            sub_manager_getter=lambda: mock_sub_manager,
        )

        # Issue #3193: _dispatch_event_internal is now async (no _run_async bridge)
        await worker._dispatch_event_internal(worker._build_file_event(record), record)

        assert len(broadcast_calls) == 1
        assert broadcast_calls[0]["sequence_number"] == 42


class TestKafkaExporterPartitionKey:
    """Test that Kafka exporter uses zone_id as partition key (#2755)."""

    def test_publish_uses_zone_id_key(self):
        """Single publish uses zone_id as Kafka key."""
        from nexus.services.event_log.exporters.kafka_exporter import (
            KafkaExporter,
        )

        config = Mock()
        config.topic_prefix = "nexus.events"
        config.send_timeout = 10.0

        exporter = KafkaExporter(config)
        mock_producer = AsyncMock()
        exporter._producer = mock_producer

        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/test.txt",
            zone_id="zone-42",
            sequence_number=10,
        )

        asyncio.run(exporter.publish(event))

        mock_producer.send_and_wait.assert_called_once_with(
            "nexus.events.zone-42",
            value=event.to_dict(),
            key="zone-42",
        )

    def test_publish_batch_uses_zone_id_key(self):
        """Batch publish uses zone_id as Kafka key for each event."""
        from nexus.services.event_log.exporters.kafka_exporter import (
            KafkaExporter,
        )

        config = Mock()
        config.topic_prefix = "nexus.events"
        config.batch_size = 100
        config.send_timeout = 10.0

        exporter = KafkaExporter(config)
        mock_producer = AsyncMock()
        exporter._producer = mock_producer

        events = [
            FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/a.txt",
                zone_id="zone-1",
                sequence_number=1,
            ),
            FileEvent(
                type=FileEventType.FILE_DELETE,
                path="/b.txt",
                zone_id="zone-2",
                sequence_number=2,
            ),
        ]

        asyncio.run(exporter.publish_batch(events))

        calls = mock_producer.send_and_wait.call_args_list
        assert len(calls) == 2
        # Each event should use its own zone_id as the partition key
        assert calls[0] == (
            ("nexus.events.zone-1",),
            {"value": events[0].to_dict(), "key": "zone-1"},
        )
        assert calls[1] == (
            ("nexus.events.zone-2",),
            {"value": events[1].to_dict(), "key": "zone-2"},
        )
