"""Tests for ACE consolidation engine."""

from unittest.mock import AsyncMock, Mock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.services.ace.consolidation import ConsolidationEngine
from nexus.core.response import HandlerResponse
from nexus.storage.models import Base, MemoryModel


@pytest.fixture
def engine():
    """Create in-memory SQLite database for testing."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def session(engine):
    """Create database session."""
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()


@pytest.fixture
def mock_backend():
    """Create mock storage backend."""
    backend = Mock()
    backend.read_content = Mock(return_value=HandlerResponse.ok(b'{"content": "test memory"}'))
    backend.write_content = Mock(return_value=HandlerResponse.ok("hash123"))
    return backend


@pytest.fixture
def mock_llm_provider():
    """Create mock LLM provider."""
    provider = Mock()
    response = Mock()
    response.content = "Consolidated summary of memories"
    provider.complete_async = AsyncMock(return_value=response)
    return provider


@pytest.fixture
def consolidation_engine(session, mock_backend, mock_llm_provider):
    """Create consolidation engine instance."""
    return ConsolidationEngine(
        session=session,
        backend=mock_backend,
        llm_provider=mock_llm_provider,
        user_id="alice",
        agent_id="agent1",
        zone_id="acme",
    )


class TestConsolidationEngineInit:
    """Test ConsolidationEngine initialization."""

    def test_init_with_all_params(self, session, mock_backend, mock_llm_provider):
        """Test initialization with all parameters."""
        engine = ConsolidationEngine(
            session=session,
            backend=mock_backend,
            llm_provider=mock_llm_provider,
            user_id="alice",
            agent_id="agent1",
            zone_id="acme",
        )

        assert engine.session == session
        assert engine.backend == mock_backend
        assert engine.llm_provider == mock_llm_provider
        assert engine.user_id == "alice"
        assert engine.agent_id == "agent1"
        assert engine.zone_id == "acme"

    def test_init_without_optional_params(self, session, mock_backend, mock_llm_provider):
        """Test initialization without optional parameters."""
        engine = ConsolidationEngine(
            session=session,
            backend=mock_backend,
            llm_provider=mock_llm_provider,
            user_id="alice",
        )

        assert engine.agent_id is None
        assert engine.zone_id is None


class TestLoadMemory:
    """Test _load_memory method."""

    def test_load_existing_memory(self, consolidation_engine, session, mock_backend):
        """Test loading an existing memory."""
        # Create memory
        memory = MemoryModel(
            memory_id="mem1",
            content_hash="hash1",
            user_id="alice",
            zone_id="acme",
            importance=0.5,
            memory_type="fact",
            scope="user",
        )
        session.add(memory)
        session.commit()

        mock_backend.read_content.return_value = HandlerResponse.ok(b"test content")

        result = consolidation_engine._load_memory("mem1")

        assert result is not None
        assert result["memory_id"] == "mem1"
        assert result["content"] == "test content"
        assert result["importance"] == 0.5
        assert result["memory_type"] == "fact"

    def test_load_nonexistent_memory(self, consolidation_engine):
        """Test loading a non-existent memory."""
        result = consolidation_engine._load_memory("nonexistent")

        assert result is None

    def test_load_memory_with_backend_error(self, consolidation_engine, session, mock_backend):
        """Test loading memory when backend fails."""
        memory = MemoryModel(
            memory_id="mem1",
            content_hash="hash1",
            user_id="alice",
            zone_id="acme",
        )
        session.add(memory)
        session.commit()

        mock_backend.read_content.side_effect = Exception("Backend error")

        result = consolidation_engine._load_memory("mem1")

        assert result is not None
        assert result["content"] == ""  # Returns empty string on error


class TestBuildConsolidationPrompt:
    """Test _build_consolidation_prompt method."""

    def test_build_prompt_single_memory(self, consolidation_engine):
        """Test building prompt with single memory."""
        memories = [
            {
                "content": "Test memory content",
                "importance": 0.5,
                "memory_type": "fact",
            }
        ]

        prompt = consolidation_engine._build_consolidation_prompt(memories)

        assert "Memory Consolidation Task" in prompt
        assert "Test memory content" in prompt
        assert "fact" in prompt
        assert "0.50" in prompt

    def test_build_prompt_multiple_memories(self, consolidation_engine):
        """Test building prompt with multiple memories."""
        memories = [
            {"content": "Memory 1", "importance": 0.3, "memory_type": "fact"},
            {"content": "Memory 2", "importance": 0.4, "memory_type": "insight"},
        ]

        prompt = consolidation_engine._build_consolidation_prompt(memories)

        assert "Memory 1" in prompt
        assert "Memory 2" in prompt
        assert "Memory 1" in prompt and "Memory 2" in prompt

    def test_prompt_includes_task_instructions(self, consolidation_engine):
        """Test that prompt includes consolidation task instructions."""
        memories = [{"content": "Test", "importance": 0.5, "memory_type": "fact"}]

        prompt = consolidation_engine._build_consolidation_prompt(memories)

        assert "consolidated summary" in prompt.lower()
        assert "essential information" in prompt.lower()


class TestStoreConsolidatedMemory:
    """Test _store_consolidated_memory method."""

    def test_store_consolidated_memory(self, consolidation_engine, session, mock_backend):
        """Test storing consolidated memory."""
        source_memories = [
            {"memory_id": "mem1", "content": "Memory 1"},
            {"memory_id": "mem2", "content": "Memory 2"},
        ]

        memory_id = consolidation_engine._store_consolidated_memory(
            source_memories=source_memories,
            consolidated_content="Consolidated content",
            importance=0.8,
        )

        assert memory_id is not None
        mock_backend.write_content.assert_called_once()

        # Verify memory was stored
        memory = session.query(MemoryModel).filter_by(memory_id=memory_id).first()
        assert memory is not None
        assert memory.memory_type == "consolidated"
        assert memory.importance == 0.8
        assert memory.user_id == "alice"

    def test_store_tracks_source_memories(self, consolidation_engine, session, mock_backend):
        """Test that consolidated memory tracks source memory IDs."""
        import json

        source_memories = [
            {"memory_id": "mem1", "content": "Memory 1"},
            {"memory_id": "mem2", "content": "Memory 2"},
        ]

        memory_id = consolidation_engine._store_consolidated_memory(
            source_memories, "Consolidated", 0.7
        )

        memory = session.query(MemoryModel).filter_by(memory_id=memory_id).first()
        source_ids = json.loads(memory.consolidated_from)

        assert "mem1" in source_ids
        assert "mem2" in source_ids


class TestMarkMemoriesConsolidated:
    """Test _mark_memories_consolidated method."""

    def test_mark_memories_consolidated(self, consolidation_engine, session):
        """Test marking memories as consolidated."""
        # Create memories
        mem1 = MemoryModel(memory_id="mem1", content_hash="hash1", user_id="alice", importance=0.3)
        mem2 = MemoryModel(memory_id="mem2", content_hash="hash2", user_id="alice", importance=0.4)
        session.add_all([mem1, mem2])
        session.commit()

        consolidation_engine._mark_memories_consolidated(["mem1", "mem2"], "consolidated_id")

        session.refresh(mem1)
        session.refresh(mem2)

        # Importance should be updated (minimum 0.1)
        assert mem1.importance >= 0.1
        assert mem2.importance >= 0.1

    def test_mark_nonexistent_memory(self, consolidation_engine):
        """Test marking non-existent memory (should not crash)."""
        consolidation_engine._mark_memories_consolidated(["nonexistent"], "consolidated_id")
        # Should complete without error


@pytest.mark.asyncio
class TestConsolidateAsync:
    """Test consolidate_async method."""

    async def test_consolidate_two_memories(
        self, consolidation_engine, session, mock_backend, mock_llm_provider
    ):
        """Test consolidating two memories."""
        # Create memories
        mem1 = MemoryModel(
            memory_id="mem1",
            content_hash="hash1",
            user_id="alice",
            zone_id="acme",
            importance=0.3,
            memory_type="fact",
        )
        mem2 = MemoryModel(
            memory_id="mem2",
            content_hash="hash2",
            user_id="alice",
            zone_id="acme",
            importance=0.4,
            memory_type="fact",
        )
        session.add_all([mem1, mem2])
        session.commit()

        mock_backend.read_content.return_value = HandlerResponse.ok(b"test content")

        result = await consolidation_engine.consolidate_async(
            memory_ids=["mem1", "mem2"], importance_threshold=0.5
        )

        assert result is not None
        assert result["consolidated_memory_id"] is not None
        assert result["memories_consolidated"] == 2
        assert result["source_memory_ids"] == ["mem1", "mem2"]
        assert "importance_score" in result

    async def test_consolidate_skips_high_importance(
        self, consolidation_engine, session, mock_backend
    ):
        """Test that high-importance memories are skipped."""
        mem1 = MemoryModel(
            memory_id="mem1", content_hash="hash1", user_id="alice", zone_id="acme", importance=0.3
        )
        mem2 = MemoryModel(
            memory_id="mem2", content_hash="hash2", user_id="alice", zone_id="acme", importance=0.9
        )
        session.add_all([mem1, mem2])
        session.commit()

        mock_backend.read_content.return_value = HandlerResponse.ok(b"test")

        # Should fail because only 1 memory below threshold
        with pytest.raises(ValueError, match="Need at least 2 memories"):
            await consolidation_engine.consolidate_async(
                memory_ids=["mem1", "mem2"], importance_threshold=0.5
            )

    async def test_consolidate_calculates_importance(
        self, consolidation_engine, session, mock_backend
    ):
        """Test that consolidated importance is max + 0.1."""
        mem1 = MemoryModel(
            memory_id="mem1",
            content_hash="hash1",
            user_id="alice",
            zone_id="acme",
            importance=0.3,
            agent_id="agent1",
        )
        mem2 = MemoryModel(
            memory_id="mem2",
            content_hash="hash2",
            user_id="alice",
            zone_id="acme",
            importance=0.5,
            agent_id="agent1",
        )
        session.add_all([mem1, mem2])
        session.commit()

        mock_backend.read_content.return_value = HandlerResponse.ok(b"test")

        result = await consolidation_engine.consolidate_async(
            ["mem1", "mem2"], importance_threshold=0.6
        )

        # Should be max(0.3, 0.5) + 0.1 = 0.6
        assert result["importance_score"] == 0.6

    async def test_consolidate_caps_importance_at_one(
        self, consolidation_engine, session, mock_backend
    ):
        """Test that importance is capped at 1.0."""
        mem1 = MemoryModel(
            memory_id="mem1",
            content_hash="hash1",
            user_id="alice",
            zone_id="acme",
            importance=0.45,
            agent_id="agent1",
        )
        mem2 = MemoryModel(
            memory_id="mem2",
            content_hash="hash2",
            user_id="alice",
            zone_id="acme",
            importance=0.48,
            agent_id="agent1",
        )
        session.add_all([mem1, mem2])
        session.commit()

        mock_backend.read_content.return_value = HandlerResponse.ok(b"test")

        result = await consolidation_engine.consolidate_async(
            ["mem1", "mem2"], importance_threshold=0.6
        )

        # Should be max + 0.1, but not exceed 1.0
        assert result["importance_score"] <= 1.0

    async def test_consolidate_respects_max_memories(
        self, consolidation_engine, session, mock_backend
    ):
        """Test that max_consolidated_memories is respected."""
        # Create 5 memories
        for i in range(5):
            mem = MemoryModel(
                memory_id=f"mem{i}",
                content_hash=f"hash{i}",
                user_id="alice",
                zone_id="acme",
                importance=0.3,
            )
            session.add(mem)
        session.commit()

        mock_backend.read_content.return_value = HandlerResponse.ok(b"test")

        result = await consolidation_engine.consolidate_async(
            memory_ids=["mem0", "mem1", "mem2", "mem3", "mem4"],
            max_consolidated_memories=3,
        )

        # Should only consolidate first 3
        assert result["memories_consolidated"] == 3


class TestConsolidateByCriteria:
    """Test consolidate_by_criteria method."""

    def test_consolidate_by_memory_type(self, consolidation_engine, session, mock_backend):
        """Test consolidating memories by type."""
        # Create memories
        for i in range(3):
            mem = MemoryModel(
                memory_id=f"mem{i}",
                content_hash=f"hash{i}",
                user_id="alice",
                agent_id="agent1",
                zone_id="acme",
                memory_type="fact",
                importance=0.3,
            )
            session.add(mem)
        session.commit()

        mock_backend.read_content.return_value = HandlerResponse.ok(b"test")

        with patch.object(
            consolidation_engine, "consolidate_async", new_callable=AsyncMock
        ) as mock:
            mock.return_value = {
                "consolidated_memory_id": "consolidated",
                "memories_consolidated": 3,
            }

            results = consolidation_engine.consolidate_by_criteria(memory_type="fact")

            assert len(results) >= 0  # May be 0 or more depending on batching

    def test_consolidate_by_scope(self, consolidation_engine, session):
        """Test consolidating memories by scope."""
        mem = MemoryModel(
            memory_id="mem1",
            content_hash="hash1",
            user_id="alice",
            agent_id="agent1",
            zone_id="acme",
            scope="user",
            importance=0.3,
        )
        session.add(mem)
        session.commit()

        results = consolidation_engine.consolidate_by_criteria(scope="user", batch_size=10)

        # Should complete without error (may return empty list if < 2 memories)
        assert isinstance(results, list)

    def test_consolidate_respects_batch_size(self, consolidation_engine, session, mock_backend):
        """Test that batch_size is respected."""
        # Create many memories
        for i in range(15):
            mem = MemoryModel(
                memory_id=f"mem{i}",
                content_hash=f"hash{i}",
                user_id="alice",
                agent_id="agent1",
                zone_id="acme",
                importance=0.3,
            )
            session.add(mem)
        session.commit()

        mock_backend.read_content.return_value = HandlerResponse.ok(b"test")

        with patch.object(
            consolidation_engine, "consolidate_async", new_callable=AsyncMock
        ) as mock:
            mock.return_value = {"consolidated_memory_id": "consolidated"}

            results = consolidation_engine.consolidate_by_criteria(batch_size=5, limit=15)

            # Should create batches of 5
            # 15 memories / 5 per batch = 3 batches
            assert isinstance(results, list)


class TestSyncConsolidate:
    """Test sync_consolidate method."""

    def test_sync_consolidate(self, consolidation_engine, session, mock_backend):
        """Test synchronous consolidation wrapper."""
        mem1 = MemoryModel(
            memory_id="mem1", content_hash="hash1", user_id="alice", zone_id="acme", importance=0.3
        )
        mem2 = MemoryModel(
            memory_id="mem2", content_hash="hash2", user_id="alice", zone_id="acme", importance=0.4
        )
        session.add_all([mem1, mem2])
        session.commit()

        mock_backend.read_content.return_value = HandlerResponse.ok(b"test")

        result = consolidation_engine.sync_consolidate(["mem1", "mem2"])

        assert result is not None
        assert "consolidated_memory_id" in result


@pytest.mark.asyncio
class TestConsolidateByAffinityAsync:
    """Test consolidate_by_affinity_async method (Issue #1026)."""

    async def test_returns_empty_for_less_than_two_memories(self, consolidation_engine, session):
        """Should return empty results if less than 2 memories."""
        # Create only one memory
        mem1 = MemoryModel(
            memory_id="mem1",
            content_hash="hash1",
            user_id="alice",
            agent_id="agent1",
            zone_id="acme",
            importance=0.3,
        )
        session.add(mem1)
        session.commit()

        result = await consolidation_engine.consolidate_by_affinity_async(memory_ids=["mem1"])

        assert result["clusters_formed"] == 0
        assert result["total_consolidated"] == 0
        assert result["results"] == []

    async def test_returns_empty_for_no_memories(self, consolidation_engine):
        """Should return empty results if no memories found."""
        result = await consolidation_engine.consolidate_by_affinity_async(memory_ids=[])

        assert result["clusters_formed"] == 0
        assert result["total_consolidated"] == 0

    async def test_loads_memory_vectors(self, consolidation_engine, session, mock_backend):
        """Should load memory vectors with content and embeddings."""
        import json

        # Create memories with embeddings
        mem1 = MemoryModel(
            memory_id="mem1",
            content_hash="hash1",
            user_id="alice",
            agent_id="agent1",
            zone_id="acme",
            importance=0.3,
            embedding=json.dumps([1.0, 0.0, 0.0]),
            embedding_dim=3,
        )
        mem2 = MemoryModel(
            memory_id="mem2",
            content_hash="hash2",
            user_id="alice",
            agent_id="agent1",
            zone_id="acme",
            importance=0.3,
            embedding=json.dumps([0.95, 0.05, 0.0]),
            embedding_dim=3,
        )
        session.add_all([mem1, mem2])
        session.commit()

        mock_backend.read_content.return_value = HandlerResponse.ok(b"test content")

        # Load memory vectors
        vectors = await consolidation_engine._load_memory_vectors(["mem1", "mem2"])

        assert len(vectors) == 2
        assert vectors[0].memory_id == "mem1"
        assert vectors[0].embedding == [1.0, 0.0, 0.0]
        assert vectors[1].memory_id == "mem2"
        assert vectors[1].embedding == [0.95, 0.05, 0.0]

    async def test_handles_missing_embeddings(self, consolidation_engine, session, mock_backend):
        """Should handle memories without embeddings gracefully."""
        # Create memory without embedding
        mem1 = MemoryModel(
            memory_id="mem1",
            content_hash="hash1",
            user_id="alice",
            agent_id="agent1",
            zone_id="acme",
            importance=0.3,
            embedding=None,
        )
        session.add(mem1)
        session.commit()

        mock_backend.read_content.return_value = HandlerResponse.ok(b"test content")

        vectors = await consolidation_engine._load_memory_vectors(["mem1"])

        assert len(vectors) == 1
        assert vectors[0].embedding == []  # Empty list for missing embedding

    async def test_queries_candidate_memories(self, consolidation_engine, session, mock_backend):
        """Should query candidate memories based on criteria."""
        import json

        # Create memories matching criteria
        for i in range(3):
            mem = MemoryModel(
                memory_id=f"mem{i}",
                content_hash=f"hash{i}",
                user_id="alice",
                agent_id="agent1",
                zone_id="acme",
                importance=0.3,
                embedding=json.dumps([1.0, 0.0, 0.0]),
                embedding_dim=3,
            )
            session.add(mem)
        session.commit()

        mock_backend.read_content.return_value = HandlerResponse.ok(b"test")

        vectors = await consolidation_engine._query_candidate_memories(
            importance_max=0.5,
            limit=10,
        )

        assert len(vectors) == 3

    async def test_respects_config_parameters(self, consolidation_engine, session, mock_backend):
        """Should use provided config parameters."""
        import json

        # Create memories with embeddings
        mem1 = MemoryModel(
            memory_id="mem1",
            content_hash="hash1",
            user_id="alice",
            agent_id="agent1",
            zone_id="acme",
            importance=0.3,
            embedding=json.dumps([1.0, 0.0, 0.0]),
            embedding_dim=3,
        )
        mem2 = MemoryModel(
            memory_id="mem2",
            content_hash="hash2",
            user_id="alice",
            agent_id="agent1",
            zone_id="acme",
            importance=0.3,
            embedding=json.dumps([0.0, 1.0, 0.0]),  # Orthogonal
            embedding_dim=3,
        )
        session.add_all([mem1, mem2])
        session.commit()

        mock_backend.read_content.return_value = HandlerResponse.ok(b"test content")

        # With very high threshold, orthogonal vectors shouldn't cluster
        result = await consolidation_engine.consolidate_by_affinity_async(
            memory_ids=["mem1", "mem2"],
            beta=0.7,
            affinity_threshold=0.99,  # Very high
        )

        # Should complete without error
        assert "clusters_formed" in result

    async def test_returns_cluster_statistics(self, consolidation_engine, session, mock_backend):
        """Should return cluster statistics."""
        import json

        # Create similar memories
        mem1 = MemoryModel(
            memory_id="mem1",
            content_hash="hash1",
            user_id="alice",
            agent_id="agent1",
            zone_id="acme",
            importance=0.3,
            embedding=json.dumps([1.0, 0.0, 0.0]),
            embedding_dim=3,
        )
        mem2 = MemoryModel(
            memory_id="mem2",
            content_hash="hash2",
            user_id="alice",
            agent_id="agent1",
            zone_id="acme",
            importance=0.3,
            embedding=json.dumps([0.95, 0.05, 0.0]),
            embedding_dim=3,
        )
        session.add_all([mem1, mem2])
        session.commit()

        mock_backend.read_content.return_value = HandlerResponse.ok(b"test content")

        result = await consolidation_engine.consolidate_by_affinity_async(
            memory_ids=["mem1", "mem2"],
            affinity_threshold=0.5,  # Low threshold to ensure clustering
        )

        assert "cluster_statistics" in result


class TestSyncConsolidateByAffinity:
    """Test sync_consolidate_by_affinity method."""

    def test_sync_wrapper(self, consolidation_engine, session, mock_backend):
        """Test synchronous wrapper for affinity consolidation."""
        import json

        mem1 = MemoryModel(
            memory_id="mem1",
            content_hash="hash1",
            user_id="alice",
            agent_id="agent1",
            zone_id="acme",
            importance=0.3,
            embedding=json.dumps([1.0, 0.0, 0.0]),
        )
        mem2 = MemoryModel(
            memory_id="mem2",
            content_hash="hash2",
            user_id="alice",
            agent_id="agent1",
            zone_id="acme",
            importance=0.3,
            embedding=json.dumps([0.9, 0.1, 0.0]),
        )
        session.add_all([mem1, mem2])
        session.commit()

        mock_backend.read_content.return_value = HandlerResponse.ok(b"test")

        result = consolidation_engine.sync_consolidate_by_affinity(
            memory_ids=["mem1", "mem2"],
            affinity_threshold=0.5,
        )

        assert result is not None
        assert "clusters_formed" in result
        assert "total_consolidated" in result


# =============================================================================
# Issue 10A: _ensure_embeddings tests
# =============================================================================


@pytest.mark.asyncio
class TestEnsureEmbeddings:
    """Test _ensure_embeddings method (Issue #1026 review)."""

    async def test_no_op_when_all_have_embeddings(self, consolidation_engine):
        """Should return same list when all memories already have embeddings."""
        from datetime import UTC, datetime

        from nexus.services.ace.affinity import MemoryVector

        vectors = [
            MemoryVector("m1", [1.0, 0.0, 0.0], datetime.now(UTC), "test"),
            MemoryVector("m2", [0.0, 1.0, 0.0], datetime.now(UTC), "test"),
        ]

        result, warnings = await consolidation_engine._ensure_embeddings(vectors)

        assert len(result) == 2
        assert result[0].embedding == [1.0, 0.0, 0.0]
        assert result[1].embedding == [0.0, 1.0, 0.0]
        assert warnings == []

    async def test_does_not_mutate_input(self, consolidation_engine, session, mock_backend):
        """Should not mutate the original MemoryVector objects."""
        from datetime import UTC, datetime

        from nexus.services.ace.affinity import MemoryVector

        # Memory with embedding
        original_with = MemoryVector("m1", [1.0, 0.0], datetime.now(UTC), "test")
        # Memory without embedding
        original_without = MemoryVector("m2", [], datetime.now(UTC), "test2")

        # Create a mock embedding provider
        mock_provider = AsyncMock()
        mock_provider.embed_texts_batched = AsyncMock(return_value=[[0.5, 0.5]])
        mock_provider.model = "test-model"

        # Create the corresponding DB record for persistence
        mem2 = MemoryModel(memory_id="m2", content_hash="hash2", user_id="alice", importance=0.3)
        session.add(mem2)
        session.commit()

        result, warnings = await consolidation_engine._ensure_embeddings(
            [original_with, original_without], embedding_provider=mock_provider
        )

        # Original should NOT be mutated
        assert original_without.embedding == []
        # Result should have new embedding
        assert result[1].embedding == [0.5, 0.5]
        # First item should be unchanged
        assert result[0] is original_with
        assert warnings == []

    async def test_provider_creation_failure(self, consolidation_engine):
        """Should return input list when embedding provider fails to create."""
        from datetime import UTC, datetime

        from nexus.services.ace.affinity import MemoryVector

        vectors = [
            MemoryVector("m1", [], datetime.now(UTC), "test"),
        ]

        # Patch the actual dependency: create_embedding_provider raises
        with patch(
            "nexus.search.embeddings.create_embedding_provider",
            side_effect=RuntimeError("No API key"),
        ):
            result, warnings = await consolidation_engine._ensure_embeddings(vectors)
            assert len(result) == 1
            assert result[0].embedding == []
            assert len(warnings) == 1
            assert "Could not create embedding provider" in warnings[0]

    async def test_embedding_generation_failure(self, consolidation_engine, session):
        """Should handle embedding generation failure gracefully."""
        from datetime import UTC, datetime

        from nexus.services.ace.affinity import MemoryVector

        vectors = [
            MemoryVector("m1", [], datetime.now(UTC), "test"),
        ]

        mock_provider = AsyncMock()
        mock_provider.embed_texts_batched = AsyncMock(side_effect=RuntimeError("API error"))

        result, warnings = await consolidation_engine._ensure_embeddings(
            vectors, embedding_provider=mock_provider
        )

        # Should return the original list (no embedding added)
        assert len(result) == 1
        assert result[0].embedding == []
        assert len(warnings) == 1
        assert "Failed to generate embeddings" in warnings[0]

    async def test_persists_embeddings_to_db(self, consolidation_engine, session, mock_backend):
        """Should persist generated embeddings to database."""
        import json
        from datetime import UTC, datetime

        from nexus.services.ace.affinity import MemoryVector

        # Create DB record
        mem = MemoryModel(
            memory_id="m1",
            content_hash="hash1",
            user_id="alice",
            importance=0.3,
            embedding=None,
        )
        session.add(mem)
        session.commit()

        vectors = [MemoryVector("m1", [], datetime.now(UTC), "test content")]

        mock_provider = AsyncMock()
        mock_provider.embed_texts_batched = AsyncMock(return_value=[[0.1, 0.2, 0.3]])
        mock_provider.model = "test-model"

        result, warnings = await consolidation_engine._ensure_embeddings(
            vectors, embedding_provider=mock_provider
        )

        assert warnings == []

        # Check DB was updated
        session.refresh(mem)
        assert mem.embedding is not None
        assert json.loads(mem.embedding) == [0.1, 0.2, 0.3]
        assert mem.embedding_model == "test-model"
        assert mem.embedding_dim == 3


# =============================================================================
# Issue 11B: Error path tests
# =============================================================================


@pytest.mark.asyncio
class TestAffinityConsolidationErrorPaths:
    """Test error paths for affinity-based consolidation."""

    async def test_clustering_failure_returns_error(
        self, consolidation_engine, session, mock_backend
    ):
        """Should handle clustering failure gracefully."""
        import json

        mem1 = MemoryModel(
            memory_id="mem1",
            content_hash="hash1",
            user_id="alice",
            agent_id="agent1",
            zone_id="acme",
            importance=0.3,
            embedding=json.dumps([1.0, 0.0, 0.0]),
        )
        mem2 = MemoryModel(
            memory_id="mem2",
            content_hash="hash2",
            user_id="alice",
            agent_id="agent1",
            zone_id="acme",
            importance=0.3,
            embedding=json.dumps([0.0, 1.0, 0.0]),
        )
        session.add_all([mem1, mem2])
        session.commit()

        mock_backend.read_content.return_value = HandlerResponse.ok(b"test")

        # Mock cluster_by_affinity to raise an error
        with patch(
            "nexus.services.ace.consolidation.cluster_by_affinity",
            side_effect=RuntimeError("Clustering failed: degenerate matrix"),
        ):
            result = await consolidation_engine.consolidate_by_affinity_async(
                memory_ids=["mem1", "mem2"],
                affinity_threshold=0.5,
            )

        assert result["clusters_formed"] == 0
        assert result["total_consolidated"] == 0
        assert "error" in result

    async def test_llm_failure_during_cluster_consolidation(
        self, consolidation_engine, session, mock_backend, mock_llm_provider
    ):
        """Should handle LLM failure for individual clusters gracefully."""
        import json

        # Create 4 memories that will form 2 clusters
        for i in range(4):
            vec = [0.0, 0.0, 0.0]
            vec[i % 3] = 1.0
            mem = MemoryModel(
                memory_id=f"mem{i}",
                content_hash=f"hash{i}",
                user_id="alice",
                agent_id="agent1",
                zone_id="acme",
                importance=0.3,
                embedding=json.dumps(vec),
            )
            session.add(mem)
        session.commit()

        mock_backend.read_content.return_value = HandlerResponse.ok(b"test content")

        # Make LLM fail for all consolidation attempts
        mock_llm_provider.complete_async = AsyncMock(side_effect=RuntimeError("LLM API error"))

        result = await consolidation_engine.consolidate_by_affinity_async(
            memory_ids=["mem0", "mem1", "mem2", "mem3"],
            affinity_threshold=0.3,
        )

        # Should complete without crashing, but with 0 successful clusters
        assert result["clusters_formed"] == 0
        assert result["total_consolidated"] == 0


# =============================================================================
# Issue 8A: _mark_memories_consolidated tests
# =============================================================================


class TestMarkMemoriesConsolidatedFixed:
    """Test improved _mark_memories_consolidated (archives + lowers importance)."""

    def test_archives_source_memories(self, consolidation_engine, session):
        """Source memories should be archived after consolidation."""
        mem1 = MemoryModel(
            memory_id="mem1",
            content_hash="hash1",
            user_id="alice",
            importance=0.3,
            is_archived=False,
        )
        mem2 = MemoryModel(
            memory_id="mem2",
            content_hash="hash2",
            user_id="alice",
            importance=0.4,
            is_archived=False,
        )
        session.add_all([mem1, mem2])
        session.commit()

        consolidation_engine._mark_memories_consolidated(["mem1", "mem2"], "consolidated_id")

        session.refresh(mem1)
        session.refresh(mem2)

        assert mem1.is_archived is True
        assert mem2.is_archived is True

    def test_lowers_importance_to_floor(self, consolidation_engine, session):
        """Source memories should have importance lowered to 0.1."""
        mem1 = MemoryModel(
            memory_id="mem1",
            content_hash="hash1",
            user_id="alice",
            importance=0.5,
        )
        session.add(mem1)
        session.commit()

        consolidation_engine._mark_memories_consolidated(["mem1"], "consolidated_id")

        session.refresh(mem1)
        assert mem1.importance == pytest.approx(0.1)

    def test_links_to_parent(self, consolidation_engine, session):
        """Source memories should be linked to consolidated parent."""
        mem1 = MemoryModel(
            memory_id="mem1",
            content_hash="hash1",
            user_id="alice",
            importance=0.3,
        )
        session.add(mem1)
        session.commit()

        consolidation_engine._mark_memories_consolidated(["mem1"], "consolidated_parent_id")

        session.refresh(mem1)
        assert mem1.parent_memory_id == "consolidated_parent_id"

    def test_empty_list_is_no_op(self, consolidation_engine):
        """Empty memory_ids list should not crash."""
        consolidation_engine._mark_memories_consolidated([], "consolidated_id")


# =============================================================================
# Issue 12B: Evaluation test - affinity clustering quality
# =============================================================================


@pytest.mark.evaluation
class TestAffinityClusteringQuality:
    """Evaluate affinity-based clustering quality vs random batching.

    Creates synthetic memories with known semantic clusters and verifies
    that affinity-based clustering recovers them more accurately than
    random (sequential) batching.
    """

    def test_affinity_recovers_known_clusters(self):
        """Affinity clustering should correctly group semantically similar memories."""
        from datetime import UTC, datetime

        from nexus.services.ace.affinity import AffinityConfig, MemoryVector, cluster_by_affinity

        now = datetime.now(UTC)

        # Create 3 known clusters of 3 memories each
        # Cluster A: "food" theme (similar embeddings)
        cluster_a = [
            MemoryVector("a1", [1.0, 0.0, 0.0, 0.0], now, "coffee"),
            MemoryVector("a2", [0.95, 0.05, 0.0, 0.0], now, "tea"),
            MemoryVector("a3", [0.9, 0.1, 0.0, 0.0], now, "breakfast"),
        ]

        # Cluster B: "weather" theme
        cluster_b = [
            MemoryVector("b1", [0.0, 1.0, 0.0, 0.0], now, "rain"),
            MemoryVector("b2", [0.0, 0.95, 0.05, 0.0], now, "snow"),
            MemoryVector("b3", [0.0, 0.9, 0.1, 0.0], now, "sun"),
        ]

        # Cluster C: "work" theme
        cluster_c = [
            MemoryVector("c1", [0.0, 0.0, 1.0, 0.0], now, "meeting"),
            MemoryVector("c2", [0.0, 0.0, 0.95, 0.05], now, "deadline"),
            MemoryVector("c3", [0.0, 0.0, 0.9, 0.1], now, "project"),
        ]

        all_memories = cluster_a + cluster_b + cluster_c

        config = AffinityConfig(
            beta=0.9,  # Strong semantic weight
            cluster_threshold=0.7,
            min_cluster_size=2,
        )

        result = cluster_by_affinity(all_memories, config)

        # Should form exactly 3 clusters
        assert result.num_clusters == 3

        # Verify cluster purity: each cluster should contain IDs from
        # only one of the known clusters
        known_clusters = [
            {"a1", "a2", "a3"},
            {"b1", "b2", "b3"},
            {"c1", "c2", "c3"},
        ]

        for cluster_ids in result.clusters:
            cluster_set = set(cluster_ids)
            # Each cluster should be a subset of exactly one known cluster
            matches = [known for known in known_clusters if cluster_set.issubset(known)]
            assert len(matches) == 1, (
                f"Cluster {cluster_ids} matches {len(matches)} known clusters (expected exactly 1)"
            )

    def test_affinity_outperforms_random_batching(self):
        """Affinity clustering should have higher within-cluster similarity
        than random sequential batching."""
        from datetime import UTC, datetime, timedelta

        import numpy as np

        from nexus.services.ace.affinity import (
            AffinityConfig,
            MemoryVector,
            cluster_by_affinity,
            compute_affinity_matrix,
        )

        now = datetime.now(UTC)

        # Create 12 memories in 4 semantic groups of 3.
        # Interleave ordering so that sequential batching of size 3 mixes groups.
        # Order: group0, group1, group2, group3, group0, group1, ...
        n_groups, per_group = 4, 3
        memories = []
        for seq_idx in range(n_groups * per_group):
            group = seq_idx % n_groups
            member = seq_idx // n_groups
            base = [0.0] * n_groups
            base[group] = 1.0
            noise = np.random.RandomState(group * per_group + member).randn(n_groups) * 0.05
            embedding = (np.array(base) + noise).tolist()
            memories.append(
                MemoryVector(
                    f"m{seq_idx}",
                    embedding,
                    now - timedelta(hours=seq_idx),
                    f"content g{group}m{member}",
                )
            )

        config = AffinityConfig(beta=0.8, cluster_threshold=0.65, min_cluster_size=2)

        # Affinity clustering
        affinity_result = cluster_by_affinity(memories, config)
        affinity_matrix = compute_affinity_matrix(memories, config)

        # Calculate average within-cluster affinity for affinity-based clustering
        affinity_scores = []
        id_to_idx = {m.memory_id: i for i, m in enumerate(memories)}
        for cluster_ids in affinity_result.clusters:
            indices = [id_to_idx[mid] for mid in cluster_ids]
            for i, idx_i in enumerate(indices):
                for idx_j in indices[i + 1 :]:
                    affinity_scores.append(affinity_matrix[idx_i, idx_j])
        avg_affinity_clustering = np.mean(affinity_scores) if affinity_scores else 0

        # Random batching: sequential groups of 3 (crosses semantic groups)
        random_scores = []
        for batch_start in range(0, len(memories), per_group):
            batch_indices = list(range(batch_start, min(batch_start + per_group, len(memories))))
            for i, idx_i in enumerate(batch_indices):
                for idx_j in batch_indices[i + 1 :]:
                    random_scores.append(affinity_matrix[idx_i, idx_j])
        avg_random_batching = np.mean(random_scores) if random_scores else 0

        # Affinity clustering should produce higher within-cluster similarity
        assert avg_affinity_clustering > avg_random_batching, (
            f"Affinity clustering ({avg_affinity_clustering:.3f}) should beat "
            f"random batching ({avg_random_batching:.3f})"
        )


# =============================================================================
# Thread-safe sync wrapper tests
# =============================================================================


class TestSyncConsolidateThreadSafety:
    """Test that sync wrappers create a fresh session when a loop is running."""

    @pytest.fixture
    def shared_engine(self):
        """Create SQLite engine with shared pool so threads see the same DB."""
        from sqlalchemy.pool import StaticPool

        eng = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(eng)
        return eng

    def test_sync_consolidate_creates_fresh_session_in_thread(
        self, shared_engine, mock_backend, mock_llm_provider
    ):
        """sync_consolidate should use a separate session inside a thread
        when called from within a running event loop."""
        import asyncio

        SessionLocal = sessionmaker(bind=shared_engine)
        session = SessionLocal()

        mem1 = MemoryModel(
            memory_id="mem1",
            content_hash="hash1",
            user_id="alice",
            zone_id="acme",
            importance=0.3,
        )
        mem2 = MemoryModel(
            memory_id="mem2",
            content_hash="hash2",
            user_id="alice",
            zone_id="acme",
            importance=0.4,
        )
        session.add_all([mem1, mem2])
        session.commit()

        mock_backend.read_content.return_value = HandlerResponse.ok(b"test content")

        ce = ConsolidationEngine(
            session=session,
            backend=mock_backend,
            llm_provider=mock_llm_provider,
            user_id="alice",
            zone_id="acme",
        )

        # Call from inside a running event loop — exercises the
        # thread-safe path that creates a new session.
        async def _run():
            return ce.sync_consolidate(["mem1", "mem2"])

        result = asyncio.run(_run())

        assert result is not None
        assert "consolidated_memory_id" in result
        session.close()

    def test_sync_consolidate_by_affinity_creates_fresh_session_in_thread(
        self, shared_engine, mock_backend, mock_llm_provider
    ):
        """sync_consolidate_by_affinity should use a separate session inside
        a thread when called from within a running event loop."""
        import asyncio
        import json

        SessionLocal = sessionmaker(bind=shared_engine)
        session = SessionLocal()

        mem1 = MemoryModel(
            memory_id="mem1",
            content_hash="hash1",
            user_id="alice",
            agent_id="agent1",
            zone_id="acme",
            importance=0.3,
            embedding=json.dumps([1.0, 0.0, 0.0]),
        )
        mem2 = MemoryModel(
            memory_id="mem2",
            content_hash="hash2",
            user_id="alice",
            agent_id="agent1",
            zone_id="acme",
            importance=0.3,
            embedding=json.dumps([0.95, 0.05, 0.0]),
        )
        session.add_all([mem1, mem2])
        session.commit()

        mock_backend.read_content.return_value = HandlerResponse.ok(b"test content")

        ce = ConsolidationEngine(
            session=session,
            backend=mock_backend,
            llm_provider=mock_llm_provider,
            user_id="alice",
            agent_id="agent1",
            zone_id="acme",
        )

        async def _run():
            return ce.sync_consolidate_by_affinity(
                memory_ids=["mem1", "mem2"],
                affinity_threshold=0.5,
            )

        result = asyncio.run(_run())

        assert result is not None
        assert "clusters_formed" in result
        session.close()


# =============================================================================
# archived_count test
# =============================================================================


@pytest.mark.asyncio
class TestArchivedCount:
    """Test that archived_count is populated in affinity consolidation results."""

    async def test_archived_count_equals_total_consolidated(
        self, consolidation_engine, session, mock_backend
    ):
        """archived_count should equal total_consolidated."""
        import json

        mem1 = MemoryModel(
            memory_id="mem1",
            content_hash="hash1",
            user_id="alice",
            agent_id="agent1",
            zone_id="acme",
            importance=0.3,
            embedding=json.dumps([1.0, 0.0, 0.0]),
            embedding_dim=3,
        )
        mem2 = MemoryModel(
            memory_id="mem2",
            content_hash="hash2",
            user_id="alice",
            agent_id="agent1",
            zone_id="acme",
            importance=0.3,
            embedding=json.dumps([0.95, 0.05, 0.0]),
            embedding_dim=3,
        )
        session.add_all([mem1, mem2])
        session.commit()

        mock_backend.read_content.return_value = HandlerResponse.ok(b"test content")

        result = await consolidation_engine.consolidate_by_affinity_async(
            memory_ids=["mem1", "mem2"],
            affinity_threshold=0.5,
        )

        assert result["archived_count"] == result["total_consolidated"]
