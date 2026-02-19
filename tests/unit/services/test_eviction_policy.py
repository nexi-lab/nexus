"""Unit tests for EvictionPolicy (Issue #2170).

Tests cover:
- LRU selects oldest first
- LRU respects batch_size limit
- LRU handles None heartbeats (evicted first)
- LRUEvictionPolicy satisfies EvictionPolicy protocol
"""

from __future__ import annotations

import types
from datetime import UTC, datetime, timedelta

from nexus.contracts.agent_types import AgentRecord, AgentState
from nexus.services.agents.eviction_policy import (
    EvictionPolicy,
    LRUEvictionPolicy,
)


def _make_agent(agent_id: str, last_heartbeat: datetime | None = None) -> AgentRecord:
    """Create a minimal AgentRecord for testing."""
    now = datetime.now(UTC)
    return AgentRecord(
        agent_id=agent_id,
        owner_id="test-owner",
        zone_id=None,
        name=None,
        state=AgentState.CONNECTED,
        generation=1,
        last_heartbeat=last_heartbeat,
        metadata=types.MappingProxyType({}),
        created_at=now,
        updated_at=now,
    )


class TestLRUEvictionPolicy:
    """Tests for LRUEvictionPolicy."""

    def test_lru_selects_oldest_first(self):
        """LRU policy selects agents with oldest heartbeats first."""
        now = datetime.now(UTC)
        agents = [
            _make_agent("old", last_heartbeat=now - timedelta(hours=2)),
            _make_agent("medium", last_heartbeat=now - timedelta(hours=1)),
            _make_agent("new", last_heartbeat=now),
        ]

        policy = LRUEvictionPolicy()
        selected = policy.select_candidates(agents, batch_size=2)

        assert len(selected) == 2
        assert selected[0].agent_id == "old"
        assert selected[1].agent_id == "medium"

    def test_lru_respects_batch_size(self):
        """LRU policy returns at most batch_size agents."""
        now = datetime.now(UTC)
        agents = [_make_agent(f"agent-{i}", last_heartbeat=now) for i in range(10)]

        policy = LRUEvictionPolicy()
        selected = policy.select_candidates(agents, batch_size=3)

        assert len(selected) == 3

    def test_lru_handles_null_heartbeats(self):
        """Agents with None heartbeats appear first (pre-sorted by DB)."""
        now = datetime.now(UTC)
        agents = [
            _make_agent("no-heartbeat-1", last_heartbeat=None),
            _make_agent("no-heartbeat-2", last_heartbeat=None),
            _make_agent("old", last_heartbeat=now - timedelta(hours=1)),
            _make_agent("new", last_heartbeat=now),
        ]

        policy = LRUEvictionPolicy()
        selected = policy.select_candidates(agents, batch_size=2)

        assert len(selected) == 2
        assert selected[0].agent_id == "no-heartbeat-1"
        assert selected[1].agent_id == "no-heartbeat-2"

    def test_lru_empty_list(self):
        """LRU policy handles empty candidate list."""
        policy = LRUEvictionPolicy()
        selected = policy.select_candidates([], batch_size=10)
        assert selected == []

    def test_lru_satisfies_protocol(self):
        """LRUEvictionPolicy satisfies the EvictionPolicy protocol."""
        assert isinstance(LRUEvictionPolicy(), EvictionPolicy)
