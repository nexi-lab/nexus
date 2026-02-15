"""Integration tests for memory evolution detection (#1190).

Tests the full pipeline: MemoryModel → EvolutionDetector → apply_evolution_results
using in-memory SQLite for isolation.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from nexus.services.memory.evolution_detector import (
    EvolutionDetectionResult,
    EvolutionResult,
    MemoryEvolutionDetector,
    apply_evolution_results,
)
from nexus.storage.models._base import Base
from nexus.storage.models.memory import MemoryModel


@pytest.fixture
def db_session():
    """Create an in-memory SQLite session with MemoryModel tables."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    yield session
    session.close()
    engine.dispose()


def _create_memory(
    session: Session,
    content_hash: str = "abc123",
    zone_id: str = "test_zone",
    person_refs: str | None = None,
    entity_types: str | None = None,
    embedding: str | None = None,
    entities_json: str | None = None,
    state: str = "active",
    superseded_by_id: str | None = None,
    invalid_at=None,
) -> MemoryModel:
    """Helper to create a memory in the test DB."""
    memory = MemoryModel(
        content_hash=content_hash,
        zone_id=zone_id,
        scope="user",
        state=state,
        person_refs=person_refs,
        entity_types=entity_types,
        embedding=embedding,
        entities_json=entities_json,
        superseded_by_id=superseded_by_id,
        invalid_at=invalid_at,
    )
    session.add(memory)
    session.commit()
    return memory


class TestFindCandidates:
    """Tests for candidate finding with real SQLite."""

    def test_finds_matching_person_refs(self, db_session):
        existing = _create_memory(
            db_session,
            content_hash="hash1",
            person_refs="Alice,Bob",
            entity_types="PERSON",
        )

        detector = MemoryEvolutionDetector()
        candidates = detector._find_candidates(
            session=db_session,
            zone_id="test_zone",
            person_refs="Alice",
            entity_types=None,
        )

        assert len(candidates) >= 1
        memory_ids = [c[0].memory_id for c in candidates]
        assert existing.memory_id in memory_ids

    def test_excludes_superseded_memories(self, db_session):
        superseded = _create_memory(
            db_session,
            content_hash="hash2",
            person_refs="Alice",
            entity_types="PERSON",
            superseded_by_id="some-new-id",
        )

        detector = MemoryEvolutionDetector()
        candidates = detector._find_candidates(
            session=db_session,
            zone_id="test_zone",
            person_refs="Alice",
        )

        memory_ids = [c[0].memory_id for c in candidates]
        assert superseded.memory_id not in memory_ids

    def test_excludes_invalidated_memories(self, db_session):
        from datetime import UTC, datetime

        invalidated = _create_memory(
            db_session,
            content_hash="hash3",
            person_refs="Alice",
            entity_types="PERSON",
            invalid_at=datetime.now(UTC),
        )

        detector = MemoryEvolutionDetector()
        candidates = detector._find_candidates(
            session=db_session,
            zone_id="test_zone",
            person_refs="Alice",
        )

        memory_ids = [c[0].memory_id for c in candidates]
        assert invalidated.memory_id not in memory_ids

    def test_zone_isolation(self, db_session):
        _create_memory(
            db_session,
            content_hash="hash4",
            zone_id="zone_a",
            person_refs="Alice",
            entity_types="PERSON",
        )

        detector = MemoryEvolutionDetector()
        candidates = detector._find_candidates(
            session=db_session,
            zone_id="zone_b",
            person_refs="Alice",
        )

        assert len(candidates) == 0

    def test_excludes_self(self, db_session):
        existing = _create_memory(
            db_session,
            content_hash="hash5",
            person_refs="Alice",
            entity_types="PERSON",
        )

        detector = MemoryEvolutionDetector()
        candidates = detector._find_candidates(
            session=db_session,
            zone_id="test_zone",
            person_refs="Alice",
            exclude_memory_id=existing.memory_id,
        )

        memory_ids = [c[0].memory_id for c in candidates]
        assert existing.memory_id not in memory_ids

    def test_no_entity_filters_returns_empty(self, db_session):
        _create_memory(
            db_session,
            content_hash="hash6",
            person_refs="Alice",
        )

        detector = MemoryEvolutionDetector()
        candidates = detector._find_candidates(
            session=db_session,
            zone_id="test_zone",
            person_refs=None,
            entity_types=None,
        )

        assert len(candidates) == 0

    def test_embedding_reranking(self, db_session):
        vec1 = json.dumps([1.0, 0.0, 0.0])
        vec2 = json.dumps([0.9, 0.1, 0.0])

        mem1 = _create_memory(
            db_session,
            content_hash="hash7a",
            person_refs="Alice",
            entity_types="PERSON",
            embedding=vec1,
        )
        _create_memory(
            db_session,
            content_hash="hash7b",
            person_refs="Alice",
            entity_types="PERSON",
            embedding=vec2,
        )

        detector = MemoryEvolutionDetector()
        candidates = detector._find_candidates(
            session=db_session,
            zone_id="test_zone",
            person_refs="Alice",
            embedding_vec=[1.0, 0.0, 0.0],
        )

        # mem1 should be ranked higher (exact match)
        assert len(candidates) == 2
        assert candidates[0][0].memory_id == mem1.memory_id
        assert candidates[0][1] > candidates[1][1]


class TestApplyEvolutionResults:
    """Tests for applying evolution results to the database."""

    def test_apply_updates_sets_supersedes(self, db_session):
        existing = _create_memory(db_session, content_hash="hash_old", person_refs="Alice")
        new_mem = _create_memory(db_session, content_hash="hash_new", person_refs="Alice")

        rel = EvolutionResult(
            relationship_type="UPDATES",
            target_memory_id=existing.memory_id,
            confidence=0.9,
            method="heuristic",
        )
        results = EvolutionDetectionResult(relationships=(rel,), candidates_evaluated=1)

        apply_evolution_results(
            session=db_session,
            new_memory_id=new_mem.memory_id,
            results=results,
        )

        db_session.refresh(new_mem)
        db_session.refresh(existing)

        assert new_mem.supersedes_id == existing.memory_id
        assert existing.superseded_by_id == new_mem.memory_id
        assert existing.invalid_at is not None

    def test_apply_extends_sets_ids(self, db_session):
        existing = _create_memory(db_session, content_hash="hash_base", person_refs="Alice")
        new_mem = _create_memory(db_session, content_hash="hash_ext", person_refs="Alice")

        rel = EvolutionResult(
            relationship_type="EXTENDS",
            target_memory_id=existing.memory_id,
            confidence=0.85,
            method="heuristic",
        )
        results = EvolutionDetectionResult(relationships=(rel,), candidates_evaluated=1)

        apply_evolution_results(
            session=db_session,
            new_memory_id=new_mem.memory_id,
            results=results,
        )

        db_session.refresh(new_mem)
        db_session.refresh(existing)

        # New memory should have extends_ids
        assert new_mem.extends_ids is not None
        extends_list = json.loads(new_mem.extends_ids)
        assert existing.memory_id in extends_list

        # Existing memory should have extended_by_ids
        assert existing.extended_by_ids is not None
        extended_by_list = json.loads(existing.extended_by_ids)
        assert new_mem.memory_id in extended_by_list

    def test_apply_derives_sets_ids(self, db_session):
        existing = _create_memory(db_session, content_hash="hash_src", person_refs="Alice")
        new_mem = _create_memory(db_session, content_hash="hash_deriv", person_refs="Alice")

        rel = EvolutionResult(
            relationship_type="DERIVES",
            target_memory_id=existing.memory_id,
            confidence=0.8,
            method="heuristic",
        )
        results = EvolutionDetectionResult(relationships=(rel,), candidates_evaluated=1)

        apply_evolution_results(
            session=db_session,
            new_memory_id=new_mem.memory_id,
            results=results,
        )

        db_session.refresh(new_mem)

        # New memory should have derived_from_ids
        assert new_mem.derived_from_ids is not None
        derived_list = json.loads(new_mem.derived_from_ids)
        assert existing.memory_id in derived_list

    def test_apply_empty_results_is_noop(self, db_session):
        new_mem = _create_memory(db_session, content_hash="hash_noop")

        results = EvolutionDetectionResult()

        apply_evolution_results(
            session=db_session,
            new_memory_id=new_mem.memory_id,
            results=results,
        )

        db_session.refresh(new_mem)
        assert new_mem.supersedes_id is None
        assert new_mem.extends_ids is None
        assert new_mem.derived_from_ids is None

    def test_updates_skips_already_superseded(self, db_session):
        existing = _create_memory(
            db_session,
            content_hash="hash_already",
            person_refs="Alice",
            superseded_by_id="some-other-id",
        )
        new_mem = _create_memory(db_session, content_hash="hash_new2")

        rel = EvolutionResult(
            relationship_type="UPDATES",
            target_memory_id=existing.memory_id,
            confidence=0.9,
            method="heuristic",
        )
        results = EvolutionDetectionResult(relationships=(rel,), candidates_evaluated=1)

        apply_evolution_results(
            session=db_session,
            new_memory_id=new_mem.memory_id,
            results=results,
        )

        db_session.refresh(new_mem)
        # Should NOT set supersedes_id because target is already superseded
        assert new_mem.supersedes_id is None

    def test_multiple_extends_accumulate(self, db_session):
        existing1 = _create_memory(db_session, content_hash="hash_e1", person_refs="Alice")
        existing2 = _create_memory(db_session, content_hash="hash_e2", person_refs="Bob")
        new_mem = _create_memory(db_session, content_hash="hash_multi")

        rel1 = EvolutionResult(
            relationship_type="EXTENDS",
            target_memory_id=existing1.memory_id,
            confidence=0.8,
            method="heuristic",
        )
        rel2 = EvolutionResult(
            relationship_type="EXTENDS",
            target_memory_id=existing2.memory_id,
            confidence=0.7,
            method="heuristic",
        )
        results = EvolutionDetectionResult(relationships=(rel1, rel2), candidates_evaluated=2)

        apply_evolution_results(
            session=db_session,
            new_memory_id=new_mem.memory_id,
            results=results,
        )

        db_session.refresh(new_mem)
        extends_list = json.loads(new_mem.extends_ids)
        assert len(extends_list) == 2
        assert existing1.memory_id in extends_list
        assert existing2.memory_id in extends_list


class TestFullDetectPipeline:
    """End-to-end tests for the full detect() pipeline on real SQLite."""

    def test_detect_updates_on_real_db(self, db_session):
        _create_memory(
            db_session,
            content_hash="hash_old_job",
            person_refs="Alice",
            entity_types="PERSON",
        )

        detector = MemoryEvolutionDetector(llm_provider=None)
        result = detector.detect(
            session=db_session,
            zone_id="test_zone",
            new_text="Correction: Alice is now at Google instead of Microsoft",
            new_entities=[{"text": "Alice", "label": "PERSON"}],
            person_refs="Alice",
            entity_types="PERSON",
        )

        assert result.candidates_evaluated >= 1
        assert len(result.relationships) >= 1
        assert result.relationships[0].relationship_type == "UPDATES"
        assert result.elapsed_ms >= 0

    def test_detect_extends_on_real_db(self, db_session):
        _create_memory(
            db_session,
            content_hash="hash_base_info",
            person_refs="Bob",
            entity_types="PERSON",
        )

        detector = MemoryEvolutionDetector(llm_provider=None)
        result = detector.detect(
            session=db_session,
            zone_id="test_zone",
            new_text="Additionally, Bob also speaks French and German",
            new_entities=[{"text": "Bob", "label": "PERSON"}],
            person_refs="Bob",
            entity_types="PERSON",
        )

        assert result.candidates_evaluated >= 1
        assert len(result.relationships) >= 1
        assert result.relationships[0].relationship_type == "EXTENDS"

    def test_detect_derives_on_real_db(self, db_session):
        _create_memory(
            db_session,
            content_hash="hash_premise",
            entity_types="ORG,CONCEPT",
        )

        detector = MemoryEvolutionDetector(llm_provider=None)
        result = detector.detect(
            session=db_session,
            zone_id="test_zone",
            new_text="Therefore, we should reduce headcount immediately",
            new_entities=[{"text": "headcount", "label": "CONCEPT"}],
            entity_types="CONCEPT",
        )

        assert result.candidates_evaluated >= 1
        if result.relationships:
            assert result.relationships[0].relationship_type == "DERIVES"

    def test_detect_no_match(self, db_session):
        _create_memory(
            db_session,
            content_hash="hash_unrelated",
            person_refs="Charlie",
            entity_types="PERSON",
        )

        detector = MemoryEvolutionDetector(llm_provider=None)
        result = detector.detect(
            session=db_session,
            zone_id="test_zone",
            new_text="Bitcoin price update today",
            new_entities=[{"text": "Bitcoin", "label": "PRODUCT"}],
            person_refs="Bitcoin",
            entity_types="PRODUCT",
        )

        # Should find no candidates (no entity overlap)
        assert len(result.relationships) == 0

    def test_performance_under_500ms(self, db_session):
        """Assert integration test response time < 500ms."""
        # Create 50 candidate memories
        for i in range(50):
            _create_memory(
                db_session,
                content_hash=f"hash_perf_{i}",
                person_refs="Alice",
                entity_types="PERSON",
            )

        detector = MemoryEvolutionDetector(llm_provider=None)
        result = detector.detect(
            session=db_session,
            zone_id="test_zone",
            new_text="Alice is now working at Google",
            new_entities=[{"text": "Alice", "label": "PERSON"}],
            person_refs="Alice",
            entity_types="PERSON",
        )

        assert result.elapsed_ms < 500, f"Detection took {result.elapsed_ms:.1f}ms (>500ms)"


class TestMemoryModelColumns:
    """Tests for the new MemoryModel columns."""

    def test_new_columns_exist(self, db_session):
        memory = _create_memory(db_session, content_hash="hash_cols")

        assert memory.extends_ids is None
        assert memory.extended_by_ids is None
        assert memory.derived_from_ids is None

    def test_columns_accept_json(self, db_session):
        memory = _create_memory(db_session, content_hash="hash_json")
        memory.extends_ids = json.dumps(["mem-1", "mem-2"])
        memory.extended_by_ids = json.dumps(["mem-3"])
        memory.derived_from_ids = json.dumps(["mem-4"])
        db_session.commit()

        db_session.refresh(memory)
        assert json.loads(memory.extends_ids) == ["mem-1", "mem-2"]
        assert json.loads(memory.extended_by_ids) == ["mem-3"]
        assert json.loads(memory.derived_from_ids) == ["mem-4"]
