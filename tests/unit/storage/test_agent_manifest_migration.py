"""Tests for context_manifest column migration and serialization (Issue #1427).

Covers:
1. Fresh schema includes context_manifest column
2. Default value is empty JSON array
3. Round-trip serialization (write → read → verify)
4. _safe_json_loads edge cases (corrupt, None, empty, valid)
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from nexus.services.agents.agent_registry import AgentRegistry, _safe_json_loads
from nexus.storage.models import Base
from nexus.storage.models.agents import AgentRecordModel


@pytest.fixture
def engine():
    """In-memory SQLite for migration tests."""
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def registry(session_factory):
    return AgentRegistry(session_factory=session_factory)


# ---------------------------------------------------------------------------
# Test 1: Fresh schema has context_manifest column
# ---------------------------------------------------------------------------


class TestFreshSchema:
    def test_fresh_schema_has_context_manifest_column(self, engine):
        """create_all() includes the context_manifest column."""
        from sqlalchemy import inspect

        inspector = inspect(engine)
        columns = {col["name"] for col in inspector.get_columns("agent_records")}
        assert "context_manifest" in columns


# ---------------------------------------------------------------------------
# Test 2: Default value is empty list
# ---------------------------------------------------------------------------


class TestDefaultValue:
    def test_default_value_is_empty_list(self, session_factory):
        """Insert without manifest → default '[]'."""
        session = session_factory()
        from datetime import UTC, datetime

        model = AgentRecordModel(
            agent_id="test-1",
            owner_id="owner-1",
            state="UNKNOWN",
            generation=0,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(model)
        session.commit()

        result = session.execute(
            select(AgentRecordModel).where(AgentRecordModel.agent_id == "test-1")
        ).scalar_one()
        assert result.context_manifest == "[]"


# ---------------------------------------------------------------------------
# Test 3: Round-trip serialization
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_round_trip_serialization(self, registry):
        """Write manifest via registry → read → verify tuple[dict]."""
        sources = [
            {"type": "file_glob", "pattern": "*.py", "max_files": 10},
            {"type": "memory_query", "query": "auth context", "top_k": 5},
        ]
        registry.register("agent-rt", "owner-1")
        updated = registry.update_manifest("agent-rt", sources)

        assert len(updated.context_manifest) == 2
        assert updated.context_manifest[0]["type"] == "file_glob"
        assert updated.context_manifest[0]["pattern"] == "*.py"
        assert updated.context_manifest[1]["type"] == "memory_query"

        # Re-read via get()
        fetched = registry.get("agent-rt")
        assert fetched is not None
        assert fetched.context_manifest == tuple(sources)


# ---------------------------------------------------------------------------
# Test 4: _safe_json_loads edge cases
# ---------------------------------------------------------------------------


class TestSafeJsonLoads:
    def test_none_returns_default(self):
        assert _safe_json_loads(None, "agent_metadata", "a1") == {}
        assert _safe_json_loads(None, "context_manifest", "a1") == []

    def test_empty_string_returns_default(self):
        assert _safe_json_loads("", "agent_metadata", "a1") == {}
        assert _safe_json_loads("", "context_manifest", "a1") == []

    def test_corrupt_json_returns_default(self):
        assert _safe_json_loads("{invalid", "agent_metadata", "a1") == {}
        assert _safe_json_loads("[broken", "context_manifest", "a1") == []

    def test_valid_json(self):
        assert _safe_json_loads('{"key": "val"}', "agent_metadata", "a1") == {"key": "val"}
        assert _safe_json_loads('[{"type": "file_glob"}]', "context_manifest", "a1") == [
            {"type": "file_glob"}
        ]
