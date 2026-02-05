"""Tests for hierarchical memory abstraction (Issue #1029).

Tests the HierarchicalMemoryManager that builds multi-level memory hierarchies
from atomic memories through progressive consolidation.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

try:
    from datetime import UTC
except ImportError:
    # Python 3.10 compatibility
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

import pytest

from nexus.core.ace.memory_hierarchy import (
    HierarchicalMemoryManager,
    HierarchyLevel,
    HierarchyResult,
    HierarchyRetrievalResult,
    build_hierarchy,
)
from nexus.storage.models import MemoryModel


class MockMemoryModel:
    """Mock MemoryModel for testing without database."""

    def __init__(
        self,
        memory_id: str,
        embedding: list[float] | None = None,
        created_at: datetime | None = None,
        importance: float | None = None,
        memory_type: str | None = None,
        abstraction_level: int = 0,
        parent_memory_id: str | None = None,
        child_memory_ids: str | None = None,
        is_archived: bool = False,
        zone_id: str = "default",
    ):
        self.memory_id = memory_id
        self.embedding = json.dumps(embedding) if embedding else None
        self.created_at = created_at or datetime.now(UTC)
        self.importance = importance
        self.memory_type = memory_type
        self.abstraction_level = abstraction_level
        self.parent_memory_id = parent_memory_id
        self.child_memory_ids = child_memory_ids
        self.is_archived = is_archived
        self.zone_id = zone_id


def create_mock_memories(count: int = 5) -> list[MockMemoryModel]:
    """Create mock memories with embeddings for testing."""
    memories = []
    base_time = datetime.now(UTC)

    for i in range(count):
        # Create similar embeddings for testing clustering
        embedding = [1.0 - i * 0.1, i * 0.1, 0.0]
        memories.append(
            MockMemoryModel(
                memory_id=f"mem_{i}",
                embedding=embedding,
                created_at=base_time - timedelta(hours=i),
                importance=0.3 + i * 0.1,
                memory_type="fact",
            )
        )

    return memories


class TestHierarchyLevel:
    """Test HierarchyLevel dataclass."""

    def test_creation(self):
        """Should create HierarchyLevel with default values."""
        memories = create_mock_memories(2)
        level = HierarchyLevel(level=0, memories=memories)  # type: ignore

        assert level.level == 0
        assert len(level.memories) == 2
        assert level.cluster_count == 0

    def test_with_cluster_count(self):
        """Should track cluster count."""
        memories = create_mock_memories(3)
        level = HierarchyLevel(level=1, memories=memories, cluster_count=2)  # type: ignore

        assert level.level == 1
        assert level.cluster_count == 2


class TestHierarchyResult:
    """Test HierarchyResult dataclass."""

    def test_level_summary(self):
        """Should return correct level summary."""
        memories_l0 = create_mock_memories(5)
        memories_l1 = create_mock_memories(2)

        result = HierarchyResult(
            levels={
                0: HierarchyLevel(level=0, memories=memories_l0),  # type: ignore
                1: HierarchyLevel(level=1, memories=memories_l1),  # type: ignore
            },
            total_memories=5,
            total_abstracts_created=2,
            max_level_reached=1,
        )

        assert result.level_summary == {0: 5, 1: 2}

    def test_empty_result(self):
        """Should handle empty result."""
        result = HierarchyResult(
            levels={},
            total_memories=0,
            total_abstracts_created=0,
            max_level_reached=0,
        )

        assert result.level_summary == {}
        assert result.total_abstracts_created == 0


class TestHierarchyRetrievalResult:
    """Test HierarchyRetrievalResult dataclass."""

    def test_creation(self):
        """Should create retrieval result."""
        memories = create_mock_memories(3)
        result = HierarchyRetrievalResult(
            memories=memories,  # type: ignore
            abstracts_used=1,
            atomics_used=2,
            expanded_from_abstracts=1,
        )

        assert len(result.memories) == 3
        assert result.abstracts_used == 1
        assert result.atomics_used == 2
        assert result.expanded_from_abstracts == 1


class TestHierarchicalMemoryManager:
    """Test HierarchicalMemoryManager class."""

    @pytest.fixture
    def mock_session(self):
        """Create mock SQLAlchemy session."""
        session = MagicMock()
        session.execute.return_value.scalars.return_value.all.return_value = []
        session.execute.return_value.scalar_one_or_none.return_value = None
        return session

    @pytest.fixture
    def mock_consolidation_engine(self):
        """Create mock ConsolidationEngine."""
        engine = MagicMock()
        engine.consolidate_async = AsyncMock(
            return_value={
                "consolidated_memory_id": "consolidated_1",
                "source_memory_ids": ["mem_0", "mem_1"],
                "memories_consolidated": 2,
            }
        )
        return engine

    @pytest.fixture
    def manager(self, mock_consolidation_engine, mock_session):
        """Create HierarchicalMemoryManager instance."""
        return HierarchicalMemoryManager(
            consolidation_engine=mock_consolidation_engine,
            session=mock_session,
            zone_id="test-zone",
        )

    def test_init(self, mock_consolidation_engine, mock_session):
        """Should initialize with required dependencies."""
        manager = HierarchicalMemoryManager(
            consolidation_engine=mock_consolidation_engine,
            session=mock_session,
            zone_id="my-zone",
        )

        assert manager.engine == mock_consolidation_engine
        assert manager.session == mock_session
        assert manager.zone_id == "my-zone"

    @pytest.mark.asyncio
    async def test_build_hierarchy_no_memories(self, manager):
        """Should raise error when no memories provided."""
        with pytest.raises(ValueError, match="Either memories or memory_ids"):
            await manager.build_hierarchy_async(memories=None, memory_ids=None)

    @pytest.mark.asyncio
    async def test_build_hierarchy_empty_memory_ids(self, manager):
        """Should raise error for empty memory_ids."""
        with pytest.raises(ValueError, match="Either memories or memory_ids"):
            await manager.build_hierarchy_async(memories=None, memory_ids=[])

    @pytest.mark.asyncio
    async def test_build_hierarchy_too_few_memories(self, manager):
        """Should return early with too few memories."""
        memories = [create_mock_memories(1)[0]]

        result = await manager.build_hierarchy_async(
            memories=memories,
            min_cluster_size=2,  # type: ignore
        )

        assert result.total_memories == 1
        assert result.total_abstracts_created == 0
        assert result.max_level_reached == 0

    @pytest.mark.asyncio
    async def test_build_hierarchy_with_memories(
        self, manager, mock_session, mock_consolidation_engine
    ):
        """Should build hierarchy from provided memories."""
        memories = create_mock_memories(4)

        # Mock getting consolidated memory
        consolidated_memory = MockMemoryModel(
            memory_id="consolidated_1",
            embedding=[0.5, 0.5, 0.0],
            abstraction_level=1,
        )
        mock_session.execute.return_value.scalar_one_or_none.return_value = consolidated_memory

        result = await manager.build_hierarchy_async(
            memories=memories,  # type: ignore
            max_levels=2,
            cluster_threshold=0.5,
            min_cluster_size=2,
        )

        assert result.total_memories == 4
        assert 0 in result.levels  # Level 0 should always exist

    def test_to_memory_vectors(self, manager):
        """Should convert MemoryModel to MemoryVector."""
        memories = create_mock_memories(3)

        vectors = manager._to_memory_vectors(memories)  # type: ignore

        assert len(vectors) == 3
        assert vectors[0].memory_id == "mem_0"
        assert len(vectors[0].embedding) == 3

    def test_to_memory_vectors_skips_no_embedding(self, manager):
        """Should skip memories without embeddings."""
        memories = create_mock_memories(2)
        memories[0].embedding = None

        vectors = manager._to_memory_vectors(memories)  # type: ignore

        assert len(vectors) == 1
        assert vectors[0].memory_id == "mem_1"

    def test_to_memory_vectors_skips_invalid_embedding(self, manager):
        """Should skip memories with invalid embeddings."""
        memories = create_mock_memories(2)
        memories[0].embedding = "not_valid_json"

        vectors = manager._to_memory_vectors(memories)  # type: ignore

        assert len(vectors) == 1

    def test_get_children(self, manager, mock_session):
        """Should get children of a memory."""
        parent = MockMemoryModel(
            memory_id="parent",
            child_memory_ids=json.dumps(["child_1", "child_2"]),
        )

        children = [
            MockMemoryModel(memory_id="child_1"),
            MockMemoryModel(memory_id="child_2"),
        ]
        mock_session.execute.return_value.scalars.return_value.all.return_value = children

        result = manager._get_children(parent)  # type: ignore

        assert len(result) == 2

    def test_get_children_no_children(self, manager):
        """Should return empty list for memory without children."""
        parent = MockMemoryModel(memory_id="parent", child_memory_ids=None)

        result = manager._get_children(parent)  # type: ignore

        assert result == []

    def test_get_children_invalid_json(self, manager):
        """Should return empty list for invalid JSON."""
        parent = MockMemoryModel(memory_id="parent", child_memory_ids="invalid")

        result = manager._get_children(parent)  # type: ignore

        assert result == []


class TestRetrieveWithHierarchy:
    """Test hierarchy-aware retrieval."""

    @pytest.fixture
    def manager_with_data(self):
        """Create manager with mock search results."""
        session = MagicMock()
        engine = MagicMock()
        manager = HierarchicalMemoryManager(engine, session, "test")

        # Mock _search_by_level to return test data
        return manager

    def test_retrieve_prefers_abstracts(self):
        """Should prefer higher-level abstracts."""
        session = MagicMock()
        engine = MagicMock()
        manager = HierarchicalMemoryManager(engine, session, "test")

        # Create mock abstracts and atomics
        abstract = MockMemoryModel(
            memory_id="abstract_1",
            abstraction_level=2,
            embedding=[1.0, 0.0, 0.0],
            child_memory_ids=json.dumps(["atom_1", "atom_2"]),
        )
        atomic = MockMemoryModel(
            memory_id="atom_1",
            abstraction_level=0,
            embedding=[0.9, 0.1, 0.0],
        )

        # Mock search results
        with patch.object(manager, "_search_by_level") as mock_search:
            mock_search.side_effect = [
                [(abstract, 0.9)],  # First call for abstracts
                [(atomic, 0.8)],  # Second call for atomics
            ]

            with patch.object(manager, "_get_children") as mock_children:
                mock_children.return_value = [atomic]

                result = manager.retrieve_with_hierarchy(
                    query_embedding=[1.0, 0.0, 0.0],
                    max_results=5,
                    prefer_abstracts=True,
                    expand_threshold=0.8,
                )

        assert result.abstracts_used >= 1

    def test_retrieve_expands_high_scoring_abstracts(self):
        """Should expand abstracts that score above threshold."""
        session = MagicMock()
        engine = MagicMock()
        manager = HierarchicalMemoryManager(engine, session, "test")

        abstract = MockMemoryModel(
            memory_id="abstract_1",
            abstraction_level=2,
            embedding=[1.0, 0.0, 0.0],
            child_memory_ids=json.dumps(["atom_1"]),
        )
        child = MockMemoryModel(
            memory_id="atom_1",
            abstraction_level=0,
            embedding=[0.9, 0.1, 0.0],
        )

        with patch.object(manager, "_search_by_level") as mock_search:
            mock_search.side_effect = [
                [(abstract, 0.95)],  # High score - should expand
                [],
            ]

            with patch.object(manager, "_get_children") as mock_children:
                mock_children.return_value = [child]

                result = manager.retrieve_with_hierarchy(
                    query_embedding=[1.0, 0.0, 0.0],
                    max_results=10,
                    expand_threshold=0.9,
                )

        assert result.expanded_from_abstracts >= 1


class TestGetHierarchyForMemory:
    """Test hierarchy tree retrieval."""

    def test_get_hierarchy_with_ancestors(self):
        """Should retrieve ancestor chain."""
        session = MagicMock()
        engine = MagicMock()
        manager = HierarchicalMemoryManager(engine, session, "test")

        child = MockMemoryModel(
            memory_id="child",
            abstraction_level=0,
            parent_memory_id="parent",
            is_archived=True,
        )
        parent = MockMemoryModel(
            memory_id="parent",
            abstraction_level=1,
            parent_memory_id="grandparent",
        )
        grandparent = MockMemoryModel(
            memory_id="grandparent",
            abstraction_level=2,
            parent_memory_id=None,
        )

        def mock_get_memory(memory_id):
            memories = {
                "child": child,
                "parent": parent,
                "grandparent": grandparent,
            }
            return memories.get(memory_id)

        with (
            patch.object(manager, "_get_memory", side_effect=mock_get_memory),
            patch.object(manager, "_get_children_recursive", return_value=[]),
        ):
            result = manager.get_hierarchy_for_memory("child")

        assert result["memory_id"] == "child"
        assert result["abstraction_level"] == 0
        assert len(result["ancestors"]) == 2
        assert result["ancestors"][0]["memory_id"] == "parent"
        assert result["ancestors"][1]["memory_id"] == "grandparent"

    def test_get_hierarchy_not_found(self):
        """Should return empty dict for non-existent memory."""
        session = MagicMock()
        engine = MagicMock()
        manager = HierarchicalMemoryManager(engine, session, "test")

        with patch.object(manager, "_get_memory", return_value=None):
            result = manager.get_hierarchy_for_memory("non_existent")

        assert result == {}


class TestBuildHierarchySyncWrapper:
    """Test synchronous wrapper function."""

    def test_build_hierarchy_sync(self):
        """Should provide sync wrapper for build_hierarchy_async."""
        session = MagicMock()
        engine = MagicMock()
        engine.consolidate_async = AsyncMock(
            return_value={"consolidated_memory_id": "c1", "memories_consolidated": 2}
        )

        memories = create_mock_memories(2)

        # The sync wrapper should work
        with patch(
            "nexus.core.ace.memory_hierarchy.HierarchicalMemoryManager.build_hierarchy_async"
        ) as mock_async:
            mock_async.return_value = HierarchyResult(
                levels={0: HierarchyLevel(0, memories)},  # type: ignore
                total_memories=2,
                total_abstracts_created=0,
                max_level_reached=0,
            )

            result = build_hierarchy(
                consolidation_engine=engine,
                session=session,
                memories=memories,  # type: ignore
                zone_id="test",
            )

        assert result.total_memories == 2


class TestMemoryModelHierarchyFields:
    """Test that MemoryModel has required hierarchy fields."""

    def test_has_abstraction_level(self):
        """MemoryModel should have abstraction_level field."""
        assert hasattr(MemoryModel, "abstraction_level")

    def test_has_parent_memory_id(self):
        """MemoryModel should have parent_memory_id field."""
        assert hasattr(MemoryModel, "parent_memory_id")

    def test_has_child_memory_ids(self):
        """MemoryModel should have child_memory_ids field."""
        assert hasattr(MemoryModel, "child_memory_ids")

    def test_has_is_archived(self):
        """MemoryModel should have is_archived field."""
        assert hasattr(MemoryModel, "is_archived")
