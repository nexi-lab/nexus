"""Pre-extraction unit tests for Memory API core CRUD (#2035).

These tests validate the Memory class behavior BEFORE brick extraction.
They serve as a safety net ensuring behavioral equivalence after the
Skills module is restructured into the brick layout.

Tests cover: store, get, retrieve, delete, list, query, search.
"""

import importlib
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.backends.storage.cas_local import CASLocalBackend
from nexus.bricks.memory.service import Memory
from nexus.storage.models import Base

_er_mod = importlib.import_module("nexus.bricks.rebac.entity_registry")
EntityRegistry = _er_mod.EntityRegistry

# ---------------------------------------------------------------------------
# Fixtures (shared with existing test_enrichment_pipeline.py pattern)
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    """Create in-memory SQLite database for testing."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def session(engine):
    """Create database session."""
    session_cls = sessionmaker(bind=engine)
    session = session_cls()
    yield session
    session.close()


@pytest.fixture
def backend(tmp_path):
    """Create local backend for content storage."""
    return CASLocalBackend(root_path=tmp_path)


@pytest.fixture
def entity_registry(session):
    """Create and populate entity registry."""
    registry = EntityRegistry(
        SimpleNamespace(session_factory=lambda: session)  # type: ignore[arg-type]
    )
    registry.register_entity("zone", "acme")
    registry.register_entity("user", "alice", parent_type="zone", parent_id="acme")
    registry.register_entity("agent", "agent1", parent_type="user", parent_id="alice")
    return registry


@pytest.fixture
def memory_api(session, backend, entity_registry):
    """Create Memory API instance."""
    return Memory(
        session=session,
        backend=backend,
        zone_id="acme",
        user_id="alice",
        agent_id="agent1",
        entity_registry=entity_registry,
    )


# ---------------------------------------------------------------------------
# TestMemoryStore
# ---------------------------------------------------------------------------


class TestMemoryStore:
    """Test Memory.store() for various content types and options."""

    def test_store_text_content(self, memory_api):
        """Storing plain text returns a non-empty memory_id."""
        memory_id = memory_api.store(content="Hello, world!", scope="user")
        assert memory_id is not None
        assert isinstance(memory_id, str)
        assert len(memory_id) > 0

    def test_store_dict_content(self, memory_api):
        """Storing a dict serialises it as JSON and returns a valid ID."""
        memory_id = memory_api.store(
            content={"key": "value", "nested": {"a": 1}},
            scope="user",
        )
        assert memory_id is not None
        result = memory_api.get(memory_id)
        assert result is not None
        # Content should be the JSON-serialised string
        assert "key" in result["content"]
        assert "value" in result["content"]

    def test_store_binary_content(self, memory_api):
        """Storing raw bytes succeeds and is retrievable."""
        memory_id = memory_api.store(
            content=b"\x00\x01\x02\xff",
            scope="user",
        )
        assert memory_id is not None
        result = memory_api.get(memory_id)
        assert result is not None
        # Binary content is hex-encoded on retrieval
        assert result["content"] == "000102ff"

    def test_store_with_namespace_and_path_key(self, memory_api):
        """Storing with namespace and path_key populates those fields."""
        memory_id = memory_api.store(
            content="namespace test",
            scope="user",
            namespace="user/prefs",
            path_key="theme",
        )
        result = memory_api.get(memory_id)
        assert result is not None
        assert result["namespace"] == "user/prefs"
        assert result["path_key"] == "theme"

    def test_store_upsert_creates_new_version(self, memory_api):
        """Storing with same path_key creates a new superseding memory."""
        id1 = memory_api.store(
            content="version 1",
            scope="user",
            namespace="ns",
            path_key="key1",
        )
        id2 = memory_api.store(
            content="version 2",
            scope="user",
            namespace="ns",
            path_key="key1",
        )
        # IDs should differ (append-only; new row created)
        assert id1 != id2

        # New version should be retrievable; old one should be superseded
        result = memory_api.get(id2)
        assert result is not None
        assert "version 2" in result["content"]

        # Superseded memory follows chain to current
        result_old = memory_api.get(id1)
        # get() follows superseded chain, so should return current version
        assert result_old is not None
        assert result_old["memory_id"] == id2

    def test_store_with_inactive_state(self, memory_api):
        """Memories stored with state='inactive' should have that state."""
        memory_id = memory_api.store(
            content="pending review",
            scope="user",
            state="inactive",
        )
        result = memory_api.get(memory_id)
        assert result is not None
        assert result["state"] == "inactive"


# ---------------------------------------------------------------------------
# TestMemoryGet
# ---------------------------------------------------------------------------


class TestMemoryGet:
    """Test Memory.get() retrieval behaviour."""

    def test_get_existing_memory(self, memory_api):
        """get() returns a dict with expected fields for an existing memory."""
        memory_id = memory_api.store(content="Hello", scope="user")
        result = memory_api.get(memory_id)
        assert result is not None
        assert result["memory_id"] == memory_id
        assert result["content"] == "Hello"
        assert result["scope"] == "user"

    def test_get_nonexistent_returns_none(self, memory_api):
        """get() returns None for a non-existent memory ID."""
        result = memory_api.get("nonexistent-id-12345")
        assert result is None

    def test_get_deleted_returns_none(self, memory_api):
        """get() returns None for a soft-deleted memory."""
        memory_id = memory_api.store(content="to delete", scope="user")
        memory_api.delete(memory_id)
        result = memory_api.get(memory_id)
        assert result is None

    def test_get_follows_superseded_chain(self, memory_api):
        """get() on a superseded memory follows the chain to the current version."""
        id1 = memory_api.store(
            content="old",
            scope="user",
            namespace="ns",
            path_key="chain",
        )
        id2 = memory_api.store(
            content="current",
            scope="user",
            namespace="ns",
            path_key="chain",
        )
        result = memory_api.get(id1)
        assert result is not None
        assert result["memory_id"] == id2
        assert "current" in result["content"]

    def test_get_tracks_access(self, memory_api):
        """get() increments access_count when track_access=True (default)."""
        memory_id = memory_api.store(content="tracked", scope="user")

        # First access
        r1 = memory_api.get(memory_id)
        assert r1 is not None
        count_1 = r1["access_count"]

        # Second access
        r2 = memory_api.get(memory_id)
        assert r2 is not None
        count_2 = r2["access_count"]
        assert count_2 > count_1


# ---------------------------------------------------------------------------
# TestMemoryRetrieve
# ---------------------------------------------------------------------------


class TestMemoryRetrieve:
    """Test Memory.retrieve() by namespace + path_key."""

    def test_retrieve_by_namespace_path_key(self, memory_api):
        """retrieve() finds a memory by namespace and path_key."""
        memory_api.store(
            content={"setting": "dark"},
            scope="user",
            namespace="user/prefs",
            path_key="theme",
        )
        result = memory_api.retrieve(namespace="user/prefs", path_key="theme")
        assert result is not None
        assert result["content"] == {"setting": "dark"}

    def test_retrieve_by_combined_path(self, memory_api):
        """retrieve(path=...) splits into namespace/path_key."""
        memory_api.store(
            content="combined path test",
            scope="user",
            namespace="knowledge/geo",
            path_key="france",
        )
        result = memory_api.retrieve(path="knowledge/geo/france")
        assert result is not None
        assert "combined path test" in result["content"]

    def test_retrieve_nonexistent_returns_none(self, memory_api):
        """retrieve() returns None when no matching memory exists."""
        result = memory_api.retrieve(namespace="missing", path_key="key")
        assert result is None


# ---------------------------------------------------------------------------
# TestMemoryDelete
# ---------------------------------------------------------------------------


class TestMemoryDelete:
    """Test Memory.delete() soft-delete behaviour."""

    def test_delete_existing_memory(self, memory_api):
        """delete() returns True and the memory is no longer retrievable via get()."""
        memory_id = memory_api.store(content="to delete", scope="user")
        assert memory_api.delete(memory_id) is True
        assert memory_api.get(memory_id) is None

    def test_delete_nonexistent_returns_false(self, memory_api):
        """delete() returns False for a non-existent memory."""
        assert memory_api.delete("nonexistent-id-xyz") is False


# ---------------------------------------------------------------------------
# TestMemoryList
# ---------------------------------------------------------------------------


class TestMemoryList:
    """Test Memory.list() lightweight listing."""

    def test_list_returns_lightweight_fields(self, memory_api):
        """list() results should contain base fields but NOT heavy fields."""
        memory_api.store(content="list test", scope="user")
        results = memory_api.list()
        assert len(results) > 0
        item = results[0]
        # Should have base fields
        assert "memory_id" in item
        assert "scope" in item
        assert "state" in item
        # Should NOT have heavy enrichment fields
        assert "temporal_stability" not in item
        assert "importance_effective" not in item

    def test_list_filters_by_scope(self, memory_api):
        """list() with scope filter only returns memories of that scope."""
        memory_api.store(content="user scope", scope="user")
        memory_api.store(content="agent scope", scope="agent")

        user_results = memory_api.list(scope="user")
        for item in user_results:
            assert item["scope"] == "user"

    def test_list_filters_by_namespace(self, memory_api):
        """list() with namespace filter returns only matching namespace."""
        memory_api.store(
            content="geo fact",
            scope="user",
            namespace="knowledge/geo",
            path_key="fact1",
        )
        memory_api.store(
            content="bio fact",
            scope="user",
            namespace="knowledge/bio",
            path_key="fact2",
        )

        results = memory_api.list(namespace="knowledge/geo")
        assert len(results) >= 1
        for item in results:
            assert item["namespace"] == "knowledge/geo"

    def test_list_respects_limit(self, memory_api):
        """list() with limit returns at most that many results."""
        for i in range(5):
            memory_api.store(content=f"item {i}", scope="user")

        results = memory_api.list(limit=3)
        assert len(results) <= 3


# ---------------------------------------------------------------------------
# TestMemoryQuery
# ---------------------------------------------------------------------------


class TestMemoryQuery:
    """Test Memory.query() with enriched fields."""

    def test_query_returns_enriched_fields(self, memory_api):
        """query() results include enrichment metadata fields."""
        memory_api.store(content="Paris is the capital of France", scope="user")
        results = memory_api.query()
        assert len(results) > 0
        item = results[0]
        # query() should include enrichment fields
        assert "memory_id" in item
        assert "content" in item
        assert "importance_effective" in item
        assert "temporal_stability" in item

    def test_query_filters_by_state(self, memory_api):
        """query() with state filter only returns memories in that state."""
        memory_api.store(content="active memory", scope="user", state="active")
        memory_api.store(content="inactive memory", scope="user", state="inactive")

        active_results = memory_api.query(state="active")
        for item in active_results:
            assert item["state"] == "active"

        inactive_results = memory_api.query(state="inactive")
        for item in inactive_results:
            assert item["state"] == "inactive"

    def test_query_supports_pagination(self, memory_api):
        """query() with limit and offset supports pagination.

        NOTE: The current implementation passes limit to the SQL query
        AND applies offset/limit in-memory after permission filtering.
        To paginate correctly, callers must request a large enough SQL
        limit to cover offset + page_size.
        """
        for i in range(5):
            memory_api.store(content=f"page item {i}", scope="user")

        # Request enough from SQL to cover offset + page
        page1 = memory_api.query(limit=5, offset=0)
        page2 = memory_api.query(limit=5, offset=2)

        assert len(page1) == 5
        assert len(page2) == 3  # 5 total - offset 2 = 3 remaining

        # First two IDs of page1 should NOT appear in page2 (they were skipped)
        page1_first2 = {item["memory_id"] for item in page1[:2]}
        page2_ids = {item["memory_id"] for item in page2}
        assert page1_first2.isdisjoint(page2_ids)


# ---------------------------------------------------------------------------
# TestMemorySearch
# ---------------------------------------------------------------------------


class TestMemorySearch:
    """Test Memory.search() keyword fallback."""

    def test_keyword_search_returns_scores(self, memory_api):
        """search() returns results with a 'score' field for matching content."""
        memory_api.store(content="Python is a great programming language", scope="user")
        memory_api.store(content="JavaScript is also popular", scope="user")

        results = memory_api.search("Python")
        assert len(results) > 0
        for item in results:
            assert "score" in item
            assert "memory_id" in item
            assert "content" in item
        # The Python result should score higher
        assert any("Python" in r["content"] for r in results)

    def test_keyword_search_no_match_returns_empty(self, memory_api):
        """search() returns empty list when nothing matches."""
        memory_api.store(content="Completely unrelated content", scope="user")
        results = memory_api.search("xyznonexistent")
        assert results == []
