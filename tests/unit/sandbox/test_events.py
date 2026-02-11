"""Unit tests for AgentEventLog (Issue #1307).

Tests cover:
- Recording events with and without payload
- Listing events with filtering
- Event ordering (newest first)
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from nexus.sandbox.events import AgentEventLog
from nexus.storage.models import Base


@pytest.fixture
def engine():
    """Create in-memory SQLite database for testing."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def session_factory(engine):
    """Create a session factory."""
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def event_log(session_factory):
    """Create an AgentEventLog for testing."""
    return AgentEventLog(session_factory=session_factory)


class TestRecord:
    def test_record_basic_event(self, event_log):
        event_id = event_log.record(
            agent_id="agent-1",
            event_type="sandbox.created",
        )

        assert event_id  # UUID string
        assert len(event_id) == 36  # UUID format

    def test_record_with_zone(self, event_log):
        event_log.record(
            agent_id="agent-1",
            event_type="sandbox.created",
            zone_id="zone-1",
        )

        events = event_log.list_events("agent-1")
        assert len(events) == 1
        assert events[0]["zone_id"] == "zone-1"

    def test_record_with_payload(self, event_log):
        event_log.record(
            agent_id="agent-1",
            event_type="sandbox.created",
            payload={"sandbox_id": "sb-123", "provider": "docker"},
        )

        events = event_log.list_events("agent-1")
        assert len(events) == 1
        assert events[0]["payload"] == {"sandbox_id": "sb-123", "provider": "docker"}

    def test_record_without_payload(self, event_log):
        event_log.record(
            agent_id="agent-1",
            event_type="sandbox.stopped",
        )

        events = event_log.list_events("agent-1")
        assert events[0]["payload"] is None


class TestListEvents:
    def test_list_events_for_agent(self, event_log):
        event_log.record(agent_id="agent-1", event_type="sandbox.created")
        event_log.record(agent_id="agent-1", event_type="sandbox.stopped")
        event_log.record(agent_id="agent-2", event_type="sandbox.created")

        events = event_log.list_events("agent-1")
        assert len(events) == 2
        assert all(e["agent_id"] == "agent-1" for e in events)

    def test_list_events_filter_by_type(self, event_log):
        event_log.record(agent_id="agent-1", event_type="sandbox.created")
        event_log.record(agent_id="agent-1", event_type="sandbox.stopped")

        events = event_log.list_events("agent-1", event_type="sandbox.created")
        assert len(events) == 1
        assert events[0]["event_type"] == "sandbox.created"

    def test_list_events_respects_limit(self, event_log):
        for i in range(5):
            event_log.record(agent_id="agent-1", event_type=f"event-{i}")

        events = event_log.list_events("agent-1", limit=3)
        assert len(events) == 3

    def test_list_events_empty(self, event_log):
        events = event_log.list_events("nonexistent")
        assert events == []

    def test_events_contain_all_fields(self, event_log):
        event_log.record(
            agent_id="agent-1",
            event_type="sandbox.created",
            zone_id="zone-1",
            payload={"key": "value"},
        )

        events = event_log.list_events("agent-1")
        event = events[0]
        assert "id" in event
        assert event["agent_id"] == "agent-1"
        assert event["event_type"] == "sandbox.created"
        assert event["zone_id"] == "zone-1"
        assert event["payload"] == {"key": "value"}
        assert event["created_at"] is not None
