"""Tests for EventLogProtocol, KernelEvent, and EventId (Issue #1383)."""

from __future__ import annotations

import dataclasses

import pytest

from nexus.core.protocols.event_log import EventId, EventLogProtocol, KernelEvent

# ---------------------------------------------------------------------------
# EventId frozen dataclass tests
# ---------------------------------------------------------------------------


class TestEventId:
    """Verify EventId is a proper frozen, slots dataclass."""

    def test_frozen(self) -> None:
        eid = EventId(id="evt-1", sequence=42)
        with pytest.raises(dataclasses.FrozenInstanceError):
            eid.id = "changed"  # type: ignore[misc]

    def test_slots(self) -> None:
        assert hasattr(EventId, "__slots__")

    def test_fields(self) -> None:
        fields = {f.name for f in dataclasses.fields(EventId)}
        assert fields == {"id", "sequence"}

    def test_equality(self) -> None:
        assert EventId(id="a", sequence=1) == EventId(id="a", sequence=1)

    def test_zero_sequence(self) -> None:
        eid = EventId(id="x", sequence=0)
        assert eid.sequence == 0


# ---------------------------------------------------------------------------
# KernelEvent frozen dataclass tests
# ---------------------------------------------------------------------------


class TestKernelEvent:
    """Verify KernelEvent is a proper frozen, slots dataclass."""

    def test_frozen(self) -> None:
        evt = KernelEvent(
            type="file_write",
            source="vfs_router",
            zone_id=None,
            timestamp="2024-01-01T00:00:00Z",
            event_id="evt-1",
            payload={},
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            evt.type = "changed"  # type: ignore[misc]

    def test_slots(self) -> None:
        assert hasattr(KernelEvent, "__slots__")

    def test_fields(self) -> None:
        fields = {f.name for f in dataclasses.fields(KernelEvent)}
        assert fields == {
            "type",
            "source",
            "zone_id",
            "timestamp",
            "event_id",
            "payload",
        }

    def test_none_zone(self) -> None:
        evt = KernelEvent(
            type="t",
            source="s",
            zone_id=None,
            timestamp="ts",
            event_id="e1",
            payload={},
        )
        assert evt.zone_id is None

    def test_payload_accessible(self) -> None:
        payload = {"key": "value", "count": 42}
        evt = KernelEvent(
            type="t",
            source="s",
            zone_id="z1",
            timestamp="ts",
            event_id="e1",
            payload=payload,
        )
        assert evt.payload == {"key": "value", "count": 42}

    def test_equality(self) -> None:
        kwargs = {
            "type": "t",
            "source": "s",
            "zone_id": None,
            "timestamp": "ts",
            "event_id": "e1",
            "payload": {},
        }
        assert KernelEvent(**kwargs) == KernelEvent(**kwargs)


# ---------------------------------------------------------------------------
# Protocol structural tests (no conformance — new protocol, no impl yet)
# ---------------------------------------------------------------------------


class TestEventLogProtocol:
    """Verify the protocol is runtime-checkable and has expected methods."""

    def test_expected_methods(self) -> None:
        expected = {"append", "read", "subscribe"}
        actual = {
            name
            for name in dir(EventLogProtocol)
            if not name.startswith("_") and callable(getattr(EventLogProtocol, name))
        }
        assert expected <= actual
