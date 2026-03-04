"""TDD tests for memory enrichment pipeline (#1498).

These tests validate the enrichment behavior currently embedded in Memory.store().
Written BEFORE extracting EnrichmentPipeline to serve as a safety net during refactoring.

Each test covers one enrichment step in isolation, ensuring behavioral equivalence
after extraction.
"""

from unittest.mock import patch

import pytest

from nexus.backends.storage.local import LocalBackend
from nexus.bricks.memory.service import Memory
from nexus.bricks.rebac.entity_registry import EntityRegistry
from tests.helpers.in_memory_record_store import InMemoryRecordStore


@pytest.fixture
def record_store():
    """Create in-memory RecordStore for testing."""
    store = InMemoryRecordStore()
    yield store
    store.close()


@pytest.fixture
def session(record_store):
    """Create database session."""
    session = record_store.session_factory()
    yield session
    session.close()


@pytest.fixture
def backend(tmp_path):
    """Create local backend for content storage."""
    return LocalBackend(root_path=tmp_path)


@pytest.fixture
def entity_registry(record_store):
    """Create and populate entity registry."""
    registry = EntityRegistry(record_store)
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


class TestEntityExtraction:
    """Test entity extraction enrichment step (#1025)."""

    def test_entities_extracted_by_default(self, memory_api):
        """Entity extraction is enabled by default."""
        memory_id = memory_api.store(
            content="John Smith met with Microsoft CEO in New York",
            scope="user",
        )
        result = memory_api.get(memory_id)
        assert result is not None
        # entity_types should be populated by default NER
        # The exact value depends on the entity extractor, but it should be non-None
        # for text containing obvious entities
        assert result["memory_id"] == memory_id

    def test_entities_disabled(self, memory_api):
        """Entity extraction can be disabled."""
        memory_id = memory_api.store(
            content="John Smith met with Microsoft CEO",
            scope="user",
            extract_entities=False,
        )
        result = memory_api.get(memory_id)
        assert result is not None
        # Should still store successfully without entities

    def test_entity_extraction_failure_non_fatal(self, memory_api):
        """Entity extraction failure should not prevent memory storage.

        After EnrichmentPipeline extraction (#1498), entity extraction is
        wrapped in try/except like all other enrichment steps.
        """
        with patch(
            "nexus.bricks.rebac.entity_extractor.EntityExtractor.extract",
            side_effect=RuntimeError("NER engine crashed"),
        ):
            memory_id = memory_api.store(
                content="John Smith works at Google",
                scope="user",
                extract_entities=True,
            )
            result = memory_api.get(memory_id)
            assert result is not None
            # Entities should be None due to failure, but memory stored successfully
            assert result["memory_id"] == memory_id


class TestTemporalExtraction:
    """Test temporal metadata extraction enrichment step (#1028)."""

    def test_temporal_extracted_by_default(self, memory_api):
        """Temporal extraction is enabled by default."""
        memory_id = memory_api.store(
            content="Meeting scheduled for January 15, 2026",
            scope="user",
        )
        result = memory_api.get(memory_id)
        assert result is not None

    def test_temporal_disabled(self, memory_api):
        """Temporal extraction can be disabled."""
        memory_id = memory_api.store(
            content="Meeting scheduled for January 15, 2026",
            scope="user",
            extract_temporal=False,
        )
        result = memory_api.get(memory_id)
        assert result is not None


class TestStabilityClassification:
    """Test temporal stability classification enrichment step (#1191)."""

    def test_static_content_classified(self, memory_api):
        """Static facts should be classified with a stability value."""
        memory_id = memory_api.store(
            content="Paris is the capital of France",
            scope="user",
        )
        result = memory_api.get(memory_id)
        assert result is not None
        # Stability classifier assigns a value (exact classification depends on rules)
        assert result["temporal_stability"] in ("static", "semi_dynamic", "dynamic")
        assert result["stability_confidence"] is not None

    def test_dynamic_content_classified(self, memory_api):
        """Dynamic content should be classified with a stability value."""
        memory_id = memory_api.store(
            content="John is currently working on the Q4 report right now",
            scope="user",
        )
        result = memory_api.get(memory_id)
        assert result is not None
        assert result["temporal_stability"] in ("static", "semi_dynamic", "dynamic")
        assert result["stability_confidence"] is not None

    def test_classification_disabled(self, memory_api):
        """Classification can be disabled."""
        memory_id = memory_api.store(
            content="The Earth orbits the Sun",
            scope="user",
            classify_stability=False,
        )
        result = memory_api.get(memory_id)
        assert result is not None
        assert result["temporal_stability"] is None

    def test_classification_failure_non_fatal(self, memory_api):
        """Classification failure should not prevent memory storage."""
        with patch(
            "nexus.bricks.memory.stability_classifier.TemporalStabilityClassifier.classify",
            side_effect=RuntimeError("Classifier exploded"),
        ):
            memory_id = memory_api.store(
                content="Paris is the capital of France",
                scope="user",
                classify_stability=True,
            )
            result = memory_api.get(memory_id)
            assert result is not None
            assert result["temporal_stability"] is None


class TestEmbeddingGeneration:
    """Test embedding generation enrichment step (#406)."""

    def test_embedding_generation_without_provider(self, memory_api):
        """Without an embedding provider, store should succeed without embeddings."""
        memory_id = memory_api.store(
            content="Test content for embedding",
            scope="user",
            generate_embedding=True,
        )
        result = memory_api.get(memory_id)
        assert result is not None

    def test_embedding_disabled(self, memory_api):
        """Embedding generation can be disabled."""
        memory_id = memory_api.store(
            content="Test content",
            scope="user",
            generate_embedding=False,
        )
        result = memory_api.get(memory_id)
        assert result is not None

    def test_binary_content_skips_embedding(self, memory_api):
        """Binary content should not attempt embedding generation."""
        memory_id = memory_api.store(
            content=b"\x00\x01\x02\x03",
            scope="user",
            generate_embedding=True,
        )
        result = memory_api.get(memory_id)
        assert result is not None


class TestRelationshipExtraction:
    """Test relationship extraction enrichment step (#1038)."""

    def test_relationships_disabled_by_default(self, memory_api):
        """Relationship extraction is disabled by default (opt-in)."""
        memory_id = memory_api.store(
            content="John works for Microsoft in Seattle",
            scope="user",
        )
        result = memory_api.get(memory_id)
        assert result is not None
        # Relationships extraction is off by default


class TestCoreferenceResolution:
    """Test coreference resolution enrichment step (#1027)."""

    def test_coreference_disabled_by_default(self, memory_api):
        """Coreference resolution is disabled by default."""
        memory_id = memory_api.store(
            content="He went to the store",
            scope="user",
        )
        result = memory_api.get(memory_id)
        assert result is not None
        assert result["content"] == "He went to the store"  # Not resolved


class TestTemporalResolution:
    """Test temporal expression resolution enrichment step (#1027)."""

    def test_temporal_resolution_disabled_by_default(self, memory_api):
        """Temporal resolution is disabled by default."""
        memory_id = memory_api.store(
            content="Meeting tomorrow at 2pm",
            scope="user",
        )
        result = memory_api.get(memory_id)
        assert result is not None
        assert "tomorrow" in result["content"]  # Not resolved


class TestEvolutionDetection:
    """Test memory evolution detection enrichment step (#1190)."""

    def test_evolution_disabled_by_default(self, memory_api):
        """Evolution detection is disabled by default (opt-in)."""
        memory_id = memory_api.store(
            content="Paris is the capital of France",
            scope="user",
        )
        result = memory_api.get(memory_id)
        assert result is not None


class TestEnrichmentPipelineIntegration:
    """Test multiple enrichment steps working together."""

    def test_all_default_enrichments_work(self, memory_api):
        """Default enrichments (entities, temporal, stability) should all work together."""
        memory_id = memory_api.store(
            content="John Smith joined Microsoft in January 2026",
            scope="user",
        )
        result = memory_api.get(memory_id)
        assert result is not None
        assert result["memory_id"] == memory_id
        # Should have at least stability classification
        assert result["temporal_stability"] is not None

    def test_all_enrichments_disabled(self, memory_api):
        """All enrichments disabled should produce minimal metadata."""
        memory_id = memory_api.store(
            content="Simple text content",
            scope="user",
            generate_embedding=False,
            extract_entities=False,
            extract_temporal=False,
            classify_stability=False,
        )
        result = memory_api.get(memory_id)
        assert result is not None
        assert result["temporal_stability"] is None

    def test_dict_content_enrichment(self, memory_api):
        """Dict content should be serialized and enriched."""
        memory_id = memory_api.store(
            content={"key": "value", "fact": "Paris is in France"},
            scope="user",
        )
        result = memory_api.get(memory_id)
        assert result is not None

    def test_empty_string_skips_enrichment(self, memory_api):
        """Empty/whitespace content should skip text-based enrichment."""
        memory_id = memory_api.store(
            content="   ",
            scope="user",
        )
        result = memory_api.get(memory_id)
        assert result is not None
        # Whitespace content should store but skip NLP enrichment
        assert result["temporal_stability"] is None


class TestResponseModels:
    """Test that Pydantic response models produce correct output (#1498)."""

    def test_get_returns_all_detail_fields(self, memory_api):
        """get() should return all MemoryDetailResponse fields."""
        memory_id = memory_api.store(
            content="Detail test",
            scope="user",
            importance=0.8,
        )
        result = memory_api.get(memory_id)
        assert result is not None
        # Verify all expected fields from MemoryDetailResponse
        expected_fields = {
            "memory_id",
            "content",
            "content_hash",
            "zone_id",
            "user_id",
            "agent_id",
            "scope",
            "visibility",
            "memory_type",
            "importance",
            "importance_original",
            "importance_effective",
            "access_count",
            "last_accessed_at",
            "state",
            "namespace",
            "path_key",
            "created_at",
            "updated_at",
            "valid_at",
            "invalid_at",
            "is_current",
            "temporal_stability",
            "stability_confidence",
            "estimated_ttl_days",
            "supersedes_id",
            "superseded_by_id",
            "extends_ids",
            "extended_by_ids",
            "derived_from_ids",
        }
        assert expected_fields.issubset(set(result.keys())), (
            f"Missing fields: {expected_fields - set(result.keys())}"
        )

    def test_query_returns_all_query_fields(self, memory_api):
        """query() should return all MemoryQueryResponse fields."""
        memory_api.store(content="Query test", scope="user")
        results = memory_api.query()
        assert len(results) > 0
        result = results[0]
        expected_fields = {
            "memory_id",
            "content",
            "content_hash",
            "zone_id",
            "user_id",
            "agent_id",
            "scope",
            "visibility",
            "memory_type",
            "importance",
            "importance_effective",
            "state",
            "namespace",
            "path_key",
            "entity_types",
            "person_refs",
            "temporal_refs_json",
            "earliest_date",
            "latest_date",
            "relationships_json",
            "relationship_count",
            "temporal_stability",
            "stability_confidence",
            "estimated_ttl_days",
            "extends_ids",
            "extended_by_ids",
            "derived_from_ids",
            "created_at",
            "updated_at",
            "valid_at",
            "invalid_at",
            "is_current",
        }
        assert expected_fields.issubset(set(result.keys())), (
            f"Missing fields: {expected_fields - set(result.keys())}"
        )

    def test_list_returns_lightweight_fields(self, memory_api):
        """list() should return MemoryListResponse (no content, no enrichment)."""
        memory_api.store(content="List test", scope="user")
        results = memory_api.list()
        assert len(results) > 0
        result = results[0]
        expected_fields = {
            "memory_id",
            "content_hash",
            "zone_id",
            "user_id",
            "agent_id",
            "scope",
            "visibility",
            "memory_type",
            "importance",
            "state",
            "namespace",
            "path_key",
            "created_at",
            "updated_at",
        }
        assert expected_fields.issubset(set(result.keys())), (
            f"Missing fields: {expected_fields - set(result.keys())}"
        )
        # list() should NOT include heavy fields
        assert "temporal_stability" not in result
        assert "importance_effective" not in result

    def test_retrieve_returns_content(self, memory_api):
        """retrieve() should return MemoryRetrieveResponse with content."""
        memory_api.store(
            content={"setting": "dark_mode"},
            scope="user",
            namespace="user/prefs",
            path_key="theme",
        )
        result = memory_api.retrieve(namespace="user/prefs", path_key="theme")
        assert result is not None
        assert result["content"] == {"setting": "dark_mode"}

    def test_search_returns_scores(self, memory_api):
        """search() should return MemorySearchResponse with scores."""
        memory_api.store(content="Python is great", scope="user")
        results = memory_api.search("Python")
        assert len(results) > 0
        result = results[0]
        assert "score" in result
        assert "memory_id" in result
        assert "content" in result


class TestHelperMethods:
    """Test the new helper methods (#1498)."""

    def test_read_content_text(self, memory_api):
        """_read_content should decode UTF-8 text."""
        content_hash = memory_api.backend.write_content(b"Hello world").content_hash
        result = memory_api._read_content(content_hash)
        assert result == "Hello world"

    def test_read_content_binary(self, memory_api):
        """_read_content should hex-encode binary content."""
        content_hash = memory_api.backend.write_content(b"\x00\x01\xff").content_hash
        result = memory_api._read_content(content_hash)
        assert result == "0001ff"

    def test_read_content_missing(self, memory_api):
        """_read_content should return placeholder for missing content."""
        result = memory_api._read_content("nonexistent_hash")
        assert "<content not available" in result

    def test_read_content_parse_json(self, memory_api):
        """_read_content with parse_json should return dict for JSON content."""
        import json

        content_hash = memory_api.backend.write_content(
            json.dumps({"key": "value"}).encode()
        ).content_hash
        result = memory_api._read_content(content_hash, parse_json=True)
        assert result == {"key": "value"}

    def test_read_content_parse_json_fallback(self, memory_api):
        """_read_content with parse_json should fall back to text for non-JSON."""
        content_hash = memory_api.backend.write_content(b"Not JSON").content_hash
        result = memory_api._read_content(content_hash, parse_json=True)
        assert result == "Not JSON"

    def test_batch_operation(self, memory_api):
        """_batch_operation should correctly partition results."""
        ids = [memory_api.store(content=f"Batch {i}", scope="user") for i in range(3)]
        result = memory_api._batch_operation(ids, memory_api.approve, success_key="approved")
        assert result["approved"] == 3
        assert result["failed"] == 0
        assert len(result["approved_ids"]) == 3
        assert len(result["failed_ids"]) == 0

    def test_batch_operation_with_failures(self, memory_api):
        """_batch_operation should track failures."""
        ids = ["nonexistent_1", "nonexistent_2"]
        result = memory_api._batch_operation(ids, memory_api.delete, success_key="deleted")
        assert result["deleted"] == 0
        assert result["failed"] == 2
