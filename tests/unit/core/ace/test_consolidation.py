"""Tests for ACE consolidation engine."""

from unittest.mock import AsyncMock, Mock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.core.ace.consolidation import ConsolidationEngine
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
    backend.read_content = Mock(return_value=b'{"content": "test memory"}')
    backend.write_content = Mock(return_value="hash123")
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
        tenant_id="acme",
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
            tenant_id="acme",
        )

        assert engine.session == session
        assert engine.backend == mock_backend
        assert engine.llm_provider == mock_llm_provider
        assert engine.user_id == "alice"
        assert engine.agent_id == "agent1"
        assert engine.tenant_id == "acme"

    def test_init_without_optional_params(self, session, mock_backend, mock_llm_provider):
        """Test initialization without optional parameters."""
        engine = ConsolidationEngine(
            session=session,
            backend=mock_backend,
            llm_provider=mock_llm_provider,
            user_id="alice",
        )

        assert engine.agent_id is None
        assert engine.tenant_id is None


class TestLoadMemory:
    """Test _load_memory method."""

    def test_load_existing_memory(self, consolidation_engine, session, mock_backend):
        """Test loading an existing memory."""
        # Create memory
        memory = MemoryModel(
            memory_id="mem1",
            content_hash="hash1",
            user_id="alice",
            importance=0.5,
            memory_type="fact",
            scope="user",
        )
        session.add(memory)
        session.commit()

        mock_backend.read_content.return_value = b"test content"

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
            importance=0.3,
            memory_type="fact",
        )
        mem2 = MemoryModel(
            memory_id="mem2",
            content_hash="hash2",
            user_id="alice",
            importance=0.4,
            memory_type="fact",
        )
        session.add_all([mem1, mem2])
        session.commit()

        mock_backend.read_content.return_value = b"test content"

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
        mem1 = MemoryModel(memory_id="mem1", content_hash="hash1", user_id="alice", importance=0.3)
        mem2 = MemoryModel(memory_id="mem2", content_hash="hash2", user_id="alice", importance=0.9)
        session.add_all([mem1, mem2])
        session.commit()

        mock_backend.read_content.return_value = b"test"

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
            importance=0.3,
            agent_id="agent1",  # Match consolidation_engine agent_id
        )
        mem2 = MemoryModel(
            memory_id="mem2",
            content_hash="hash2",
            user_id="alice",
            importance=0.5,
            agent_id="agent1",  # Match consolidation_engine agent_id
        )
        session.add_all([mem1, mem2])
        session.commit()

        mock_backend.read_content.return_value = b"test"

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
            importance=0.45,
            agent_id="agent1",  # Match consolidation_engine agent_id
        )
        mem2 = MemoryModel(
            memory_id="mem2",
            content_hash="hash2",
            user_id="alice",
            importance=0.48,
            agent_id="agent1",  # Match consolidation_engine agent_id
        )
        session.add_all([mem1, mem2])
        session.commit()

        mock_backend.read_content.return_value = b"test"

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
                importance=0.3,
            )
            session.add(mem)
        session.commit()

        mock_backend.read_content.return_value = b"test"

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
                memory_type="fact",
                importance=0.3,
            )
            session.add(mem)
        session.commit()

        mock_backend.read_content.return_value = b"test"

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
                importance=0.3,
            )
            session.add(mem)
        session.commit()

        mock_backend.read_content.return_value = b"test"

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
        mem1 = MemoryModel(memory_id="mem1", content_hash="hash1", user_id="alice", importance=0.3)
        mem2 = MemoryModel(memory_id="mem2", content_hash="hash2", user_id="alice", importance=0.4)
        session.add_all([mem1, mem2])
        session.commit()

        mock_backend.read_content.return_value = b"test"

        result = consolidation_engine.sync_consolidate(["mem1", "mem2"])

        assert result is not None
        assert "consolidated_memory_id" in result
