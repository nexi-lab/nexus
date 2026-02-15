"""Unit tests for MemoryEvolutionDetector (#1190).

Tests the hybrid heuristic+LLM detector for memory evolution relationships
(UPDATES, EXTENDS, DERIVES).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from nexus.services.memory.evolution_detector import (
    DERIVES_MARKERS,
    EXTENDS_MARKERS,
    UPDATES_MARKERS,
    EvolutionDetectionResult,
    EvolutionResult,
    MemoryEvolutionDetector,
    _compute_cosine_similarity_pure,
    _compute_entity_overlap,
)


class TestEvolutionResult:
    """Tests for the EvolutionResult frozen dataclass."""

    def test_create_result(self):
        result = EvolutionResult(
            relationship_type="UPDATES",
            target_memory_id="mem-123",
            confidence=0.85,
            method="heuristic",
            signals=["updates_marker:correction"],
        )
        assert result.relationship_type == "UPDATES"
        assert result.target_memory_id == "mem-123"
        assert result.confidence == 0.85
        assert result.method == "heuristic"
        assert result.signals == ["updates_marker:correction"]

    def test_result_is_frozen(self):
        result = EvolutionResult(
            relationship_type="EXTENDS",
            target_memory_id="mem-456",
            confidence=0.7,
            method="heuristic",
        )
        with pytest.raises(AttributeError):
            result.relationship_type = "DERIVES"  # type: ignore[misc]

    def test_default_signals(self):
        result = EvolutionResult(
            relationship_type=None,
            target_memory_id=None,
            confidence=0.0,
            method="none",
        )
        assert result.signals == []
        assert result.elapsed_ms == 0.0

    def test_none_relationship(self):
        result = EvolutionResult(
            relationship_type=None,
            target_memory_id="mem-789",
            confidence=0.1,
            method="heuristic",
            signals=["no_signals_detected"],
        )
        assert result.relationship_type is None


class TestEvolutionDetectionResult:
    """Tests for the EvolutionDetectionResult frozen dataclass."""

    def test_create_empty_result(self):
        result = EvolutionDetectionResult()
        assert result.relationships == ()
        assert result.candidates_evaluated == 0
        assert result.elapsed_ms == 0.0

    def test_create_with_relationships(self):
        rel = EvolutionResult(
            relationship_type="UPDATES",
            target_memory_id="mem-1",
            confidence=0.9,
            method="heuristic",
        )
        result = EvolutionDetectionResult(
            relationships=(rel,),
            candidates_evaluated=5,
            elapsed_ms=15.3,
        )
        assert len(result.relationships) == 1
        assert result.candidates_evaluated == 5

    def test_result_is_frozen(self):
        result = EvolutionDetectionResult()
        with pytest.raises(AttributeError):
            result.candidates_evaluated = 10  # type: ignore[misc]


class TestRegexPatterns:
    """Tests for the pre-compiled regex patterns."""

    @pytest.mark.parametrize(
        "text",
        [
            "Alice actually works at Google",
            "Correction: the meeting is on Friday",
            "Bob no longer works at Acme",
            "She changed to a new role",
            "He is now the team lead",
            "Use Python instead of Java",
            "Alice switched to the marketing department",
            "Bob moved to London",
            "Turns out he was wrong",
            "In fact, the deadline is next week",
        ],
    )
    def test_updates_markers_match(self, text):
        assert UPDATES_MARKERS.search(text), f"Expected UPDATES match for: {text}"

    @pytest.mark.parametrize(
        "text",
        [
            "Alice also manages the backend team",
            "Additionally, Bob speaks French",
            "Furthermore, the system supports caching",
            "Moreover, we need to add logging",
            "More specifically, she handles ML pipelines",
            "In addition to Python, he knows Rust",
            "Besides coding, she does design",
            "Alice as well as Bob attended",
        ],
    )
    def test_extends_markers_match(self, text):
        assert EXTENDS_MARKERS.search(text), f"Expected EXTENDS match for: {text}"

    @pytest.mark.parametrize(
        "text",
        [
            "Therefore, we should use microservices",
            "Thus, the team needs more resources",
            "Consequently, we delayed the launch",
            "Because of the outage, we need backups",
            "Based on the data, sales are declining",
            "This implies we should hire more engineers",
            "As a result, the project was cancelled",
            "Hence, we need a new strategy",
            "Given that revenue is down, cut costs",
        ],
    )
    def test_derives_markers_match(self, text):
        assert DERIVES_MARKERS.search(text), f"Expected DERIVES match for: {text}"


class TestCosineSimilarity:
    """Tests for the pure Python cosine similarity function."""

    def test_identical_vectors(self):
        vec = [1.0, 0.0, 0.0]
        assert abs(_compute_cosine_similarity_pure(vec, vec) - 1.0) < 1e-6

    def test_orthogonal_vectors(self):
        vec_a = [1.0, 0.0, 0.0]
        vec_b = [0.0, 1.0, 0.0]
        assert abs(_compute_cosine_similarity_pure(vec_a, vec_b)) < 1e-6

    def test_opposite_vectors(self):
        vec_a = [1.0, 0.0]
        vec_b = [-1.0, 0.0]
        assert abs(_compute_cosine_similarity_pure(vec_a, vec_b) - (-1.0)) < 1e-6

    def test_zero_vector(self):
        vec_a = [0.0, 0.0]
        vec_b = [1.0, 1.0]
        assert _compute_cosine_similarity_pure(vec_a, vec_b) == 0.0

    def test_similar_vectors(self):
        vec_a = [1.0, 1.0, 0.0]
        vec_b = [1.0, 1.0, 0.1]
        sim = _compute_cosine_similarity_pure(vec_a, vec_b)
        assert sim > 0.9


class TestEntityOverlap:
    """Tests for entity overlap computation."""

    def test_full_overlap(self):
        entities = [{"text": "Alice", "label": "PERSON"}]
        overlap = _compute_entity_overlap(entities, "Alice", "PERSON")
        assert overlap > 0.0

    def test_no_overlap(self):
        entities = [{"text": "Bob", "label": "PERSON"}]
        overlap = _compute_entity_overlap(entities, "Charlie", "ORG")
        assert overlap == 0.0

    def test_empty_entities(self):
        overlap = _compute_entity_overlap([], "Alice", "PERSON")
        assert overlap == 0.0

    def test_none_existing_refs(self):
        entities = [{"text": "Alice", "label": "PERSON"}]
        overlap = _compute_entity_overlap(entities, None, None)
        assert overlap == 0.0

    def test_partial_overlap(self):
        entities = [
            {"text": "Alice", "label": "PERSON"},
            {"text": "Bob", "label": "PERSON"},
        ]
        overlap = _compute_entity_overlap(entities, "Alice", "PERSON")
        assert 0.0 < overlap <= 1.0


class TestHeuristicClassifier:
    """Tests for the heuristic classification path."""

    @pytest.fixture
    def detector(self):
        return MemoryEvolutionDetector(llm_provider=None, confidence_threshold=0.7)

    def _make_candidate(
        self, person_refs=None, entity_types=None, embedding=None, memory_id="mem-existing"
    ):
        """Create a mock MemoryModel candidate."""
        mock = MagicMock()
        mock.memory_id = memory_id
        mock.person_refs = person_refs
        mock.entity_types = entity_types
        mock.embedding = embedding
        mock.entities_json = None
        mock.superseded_by_id = None
        return mock

    # --- UPDATES cases ---

    @pytest.mark.parametrize(
        "new_text,existing_persons",
        [
            ("Alice actually works at Google now", "Alice"),
            ("Correction: Bob is the VP, not Director", "Bob"),
            ("Alice is now in the London office", "Alice"),
            ("He no longer manages the team", "He"),
            ("Alice switched to the data science team", "Alice"),
        ],
    )
    def test_updates_heuristic(self, detector, new_text, existing_persons):
        candidate = self._make_candidate(person_refs=existing_persons)
        entities = [{"text": existing_persons, "label": "PERSON"}]
        result = detector._classify_heuristic(
            new_text=new_text,
            candidate=candidate,
            similarity=0.6,
            new_entities=entities,
        )
        assert result.relationship_type == "UPDATES"
        assert result.confidence > 0.0
        assert any("updates_marker" in s for s in result.signals)

    # --- EXTENDS cases ---

    @pytest.mark.parametrize(
        "new_text,existing_persons",
        [
            ("Alice also manages the backend team", "Alice"),
            ("Additionally, Bob speaks French", "Bob"),
            ("Furthermore, the system supports real-time data", "system"),
            ("Moreover, Alice has a PhD in CS", "Alice"),
        ],
    )
    def test_extends_heuristic(self, detector, new_text, existing_persons):
        candidate = self._make_candidate(person_refs=existing_persons)
        entities = [{"text": existing_persons, "label": "PERSON"}]
        result = detector._classify_heuristic(
            new_text=new_text,
            candidate=candidate,
            similarity=0.5,
            new_entities=entities,
        )
        assert result.relationship_type == "EXTENDS"
        assert result.confidence > 0.0
        assert any("extends_marker" in s for s in result.signals)

    # --- DERIVES cases ---

    @pytest.mark.parametrize(
        "new_text",
        [
            "Therefore, we should use microservices",
            "Consequently, the project was delayed",
            "As a result, we need to hire more engineers",
            "Based on the data, we should pivot to cloud",
        ],
    )
    def test_derives_heuristic(self, detector, new_text):
        candidate = self._make_candidate(entity_types="ORG,CONCEPT")
        result = detector._classify_heuristic(
            new_text=new_text,
            candidate=candidate,
            similarity=0.3,
            new_entities=[{"text": "project", "label": "CONCEPT"}],
        )
        assert result.relationship_type == "DERIVES"
        assert result.confidence > 0.0
        assert any("derives_marker" in s for s in result.signals)

    # --- NONE cases ---

    def test_unrelated_content_returns_none(self, detector):
        candidate = self._make_candidate(person_refs="Charlie", entity_types="PERSON")
        result = detector._classify_heuristic(
            new_text="Bitcoin reached 100k today",
            candidate=candidate,
            similarity=0.1,
            new_entities=[{"text": "Bitcoin", "label": "PRODUCT"}],
        )
        # Should be None or very low confidence
        assert result.relationship_type is None or result.confidence < 0.3

    def test_no_entities_returns_none(self, detector):
        candidate = self._make_candidate()
        result = detector._classify_heuristic(
            new_text="A simple statement with no markers",
            candidate=candidate,
            similarity=0.0,
            new_entities=None,
        )
        assert result.relationship_type is None
        assert result.signals == ["no_signals_detected"]

    # --- Edge cases ---

    def test_high_embedding_similarity_boosts_updates(self, detector):
        candidate = self._make_candidate(person_refs="Alice", entity_types="PERSON")
        entities = [{"text": "Alice", "label": "PERSON"}]
        result = detector._classify_heuristic(
            new_text="Alice changed to engineering manager",
            candidate=candidate,
            similarity=0.9,
            new_entities=entities,
        )
        assert any("embedding_similarity" in s for s in result.signals)

    def test_multiple_markers_increases_confidence(self, detector):
        candidate = self._make_candidate(person_refs="Alice", entity_types="PERSON")
        entities = [{"text": "Alice", "label": "PERSON"}]
        result = detector._classify_heuristic(
            new_text="Actually, correction: Alice is now at Google instead",
            candidate=candidate,
            similarity=0.7,
            new_entities=entities,
        )
        assert result.relationship_type == "UPDATES"
        # Multiple update markers should boost confidence
        updates_signals = [s for s in result.signals if "updates_marker" in s]
        assert len(updates_signals) >= 2


class TestLLMClassifier:
    """Tests for the LLM classification path."""

    def test_valid_llm_response(self):
        mock_llm = MagicMock()
        mock_llm.generate.return_value = (
            '{"relationship": "UPDATES", "confidence": 0.9, '
            '"reasoning": "New info supersedes old employment"}'
        )

        detector = MemoryEvolutionDetector(
            llm_provider=mock_llm,
            confidence_threshold=1.0,
        )
        result = detector._classify_llm(
            new_text="Alice now works at Google",
            existing_text="People: Alice; Types: PERSON",
            candidate_memory_id="mem-123",
        )
        assert result.relationship_type == "UPDATES"
        assert result.confidence == 0.9
        assert result.method == "llm"
        assert any("llm_reasoning" in s for s in result.signals)

    def test_llm_returns_none(self):
        mock_llm = MagicMock()
        mock_llm.generate.return_value = (
            '{"relationship": "NONE", "confidence": 0.95, '
            '"reasoning": "No meaningful relationship"}'
        )

        detector = MemoryEvolutionDetector(llm_provider=mock_llm)
        result = detector._classify_llm(
            new_text="Bitcoin reached 100k",
            existing_text="People: Alice",
            candidate_memory_id="mem-123",
        )
        assert result.relationship_type is None
        assert result.confidence == 0.95

    def test_invalid_llm_response_raises(self):
        mock_llm = MagicMock()
        mock_llm.generate.return_value = "This is not valid JSON at all"

        detector = MemoryEvolutionDetector(llm_provider=mock_llm)
        with pytest.raises(ValueError, match="No JSON found"):
            detector._classify_llm(
                new_text="test",
                existing_text="test",
                candidate_memory_id="mem-123",
            )

    def test_llm_invalid_relationship_normalized(self):
        mock_llm = MagicMock()
        mock_llm.generate.return_value = (
            '{"relationship": "INVALID_TYPE", "confidence": 0.5, "reasoning": "test"}'
        )

        detector = MemoryEvolutionDetector(llm_provider=mock_llm)
        result = detector._classify_llm(
            new_text="test",
            existing_text="test",
            candidate_memory_id="mem-123",
        )
        assert result.relationship_type is None  # NONE → None

    def test_llm_confidence_clamped(self):
        mock_llm = MagicMock()
        mock_llm.generate.return_value = (
            '{"relationship": "EXTENDS", "confidence": 5.0, "reasoning": "test"}'
        )

        detector = MemoryEvolutionDetector(llm_provider=mock_llm)
        result = detector._classify_llm(
            new_text="test",
            existing_text="test",
            candidate_memory_id="mem-123",
        )
        assert result.confidence <= 1.0


class TestClassifyCandidate:
    """Tests for the combined heuristic+LLM classification flow."""

    def _make_candidate(self, person_refs=None, entity_types=None, memory_id="mem-existing"):
        mock = MagicMock()
        mock.memory_id = memory_id
        mock.person_refs = person_refs
        mock.entity_types = entity_types
        mock.embedding = None
        mock.entities_json = None
        mock.superseded_by_id = None
        return mock

    def test_high_confidence_skips_llm(self):
        mock_llm = MagicMock()
        detector = MemoryEvolutionDetector(llm_provider=mock_llm, confidence_threshold=0.5)

        candidate = self._make_candidate(person_refs="Alice")
        entities = [{"text": "Alice", "label": "PERSON"}]

        result = detector._classify_candidate(
            new_text="Correction: Alice is now at Google instead of Microsoft",
            candidate=candidate,
            similarity=0.7,
            new_entities=entities,
        )
        # Strong update markers should give high confidence, skipping LLM
        assert result.method == "heuristic"
        mock_llm.generate.assert_not_called()

    def test_low_confidence_invokes_llm(self):
        mock_llm = MagicMock()
        mock_llm.generate.return_value = (
            '{"relationship": "EXTENDS", "confidence": 0.8, "reasoning": "adds detail"}'
        )

        detector = MemoryEvolutionDetector(llm_provider=mock_llm, confidence_threshold=0.99)

        candidate = self._make_candidate(person_refs="Alice")
        entities = [{"text": "Alice", "label": "PERSON"}]

        result = detector._classify_candidate(
            new_text="Alice also speaks French",
            candidate=candidate,
            similarity=0.5,
            new_entities=entities,
        )
        assert result.method == "llm"
        mock_llm.generate.assert_called_once()

    def test_llm_failure_falls_back_to_heuristic(self):
        mock_llm = MagicMock()
        mock_llm.generate.side_effect = RuntimeError("LLM unavailable")

        detector = MemoryEvolutionDetector(llm_provider=mock_llm, confidence_threshold=0.99)

        candidate = self._make_candidate(person_refs="Alice")
        entities = [{"text": "Alice", "label": "PERSON"}]

        result = detector._classify_candidate(
            new_text="Alice also manages the team",
            candidate=candidate,
            similarity=0.5,
            new_entities=entities,
        )
        assert result.method == "error"
        assert "llm_fallback" in result.signals

    def test_no_llm_returns_heuristic(self):
        detector = MemoryEvolutionDetector(llm_provider=None, confidence_threshold=0.7)

        candidate = self._make_candidate(person_refs="Bob")
        entities = [{"text": "Bob", "label": "PERSON"}]

        result = detector._classify_candidate(
            new_text="Bob also plays guitar",
            candidate=candidate,
            similarity=0.4,
            new_entities=entities,
        )
        assert result.method == "heuristic"


class TestDetect:
    """Tests for the main detect() method with mocked DB."""

    def _make_memory_model(
        self,
        memory_id="mem-1",
        person_refs="Alice",
        entity_types="PERSON",
        embedding=None,
        state="active",
        invalid_at=None,
        superseded_by_id=None,
    ):
        mock = MagicMock()
        mock.memory_id = memory_id
        mock.person_refs = person_refs
        mock.entity_types = entity_types
        mock.embedding = embedding
        mock.entities_json = None
        mock.state = state
        mock.invalid_at = invalid_at
        mock.superseded_by_id = superseded_by_id
        mock.zone_id = "test_zone"
        return mock

    @patch("nexus.services.memory.evolution_detector.select")
    def test_detect_finds_updates(self, mock_select):
        """Test that detect() finds UPDATES relationships."""
        mock_session = MagicMock()
        existing = self._make_memory_model()
        mock_session.execute.return_value.scalars.return_value.all.return_value = [existing]

        detector = MemoryEvolutionDetector(llm_provider=None)
        result = detector.detect(
            session=mock_session,
            zone_id="test_zone",
            new_text="Correction: Alice is now at Google",
            new_entities=[{"text": "Alice", "label": "PERSON"}],
            person_refs="Alice",
            entity_types="PERSON",
        )

        assert result.candidates_evaluated > 0
        assert len(result.relationships) >= 1
        first_rel = result.relationships[0]
        assert first_rel.relationship_type == "UPDATES"
        assert first_rel.target_memory_id == "mem-1"

    @patch("nexus.services.memory.evolution_detector.select")
    def test_detect_no_candidates(self, mock_select):
        """Test graceful handling when no candidates found."""
        mock_session = MagicMock()
        mock_session.execute.return_value.scalars.return_value.all.return_value = []

        detector = MemoryEvolutionDetector(llm_provider=None)
        result = detector.detect(
            session=mock_session,
            zone_id="test_zone",
            new_text="Random new memory",
            person_refs=None,
            entity_types=None,
        )

        assert result.candidates_evaluated == 0
        assert len(result.relationships) == 0

    @patch("nexus.services.memory.evolution_detector.select")
    def test_detect_respects_timeout(self, mock_select):
        """Test that detect() respects soft timeout."""
        mock_session = MagicMock()
        # Create many candidates to potentially trigger timeout
        candidates = [self._make_memory_model(memory_id=f"mem-{i}") for i in range(20)]
        mock_session.execute.return_value.scalars.return_value.all.return_value = candidates

        detector = MemoryEvolutionDetector(llm_provider=None, max_candidates=20)
        result = detector.detect(
            session=mock_session,
            zone_id="test_zone",
            new_text="Alice correction something",
            person_refs="Alice",
            entity_types="PERSON",
            timeout_ms=0.001,  # Extremely short timeout
        )
        # Should still return without error
        assert result.elapsed_ms >= 0

    @patch("nexus.services.memory.evolution_detector.select")
    def test_detect_handles_exception_gracefully(self, mock_select):
        """Test that detect() handles exceptions gracefully."""
        mock_session = MagicMock()
        mock_session.execute.side_effect = RuntimeError("DB error")

        detector = MemoryEvolutionDetector(llm_provider=None)
        result = detector.detect(
            session=mock_session,
            zone_id="test_zone",
            new_text="Some text",
            person_refs="Alice",
            entity_types="PERSON",
        )

        assert len(result.relationships) == 0
        assert result.elapsed_ms >= 0


class TestEdgeCases:
    """Edge case tests for evolution detection."""

    def _make_candidate(self, **kwargs):
        mock = MagicMock()
        mock.memory_id = kwargs.get("memory_id", "mem-1")
        mock.person_refs = kwargs.get("person_refs")
        mock.entity_types = kwargs.get("entity_types")
        mock.embedding = kwargs.get("embedding")
        mock.entities_json = kwargs.get("entities_json")
        mock.superseded_by_id = kwargs.get("superseded_by_id")
        return mock

    def test_same_entity_different_attribute_is_updates(self):
        """Same entity, different attribute value → UPDATES."""
        detector = MemoryEvolutionDetector(llm_provider=None)
        candidate = self._make_candidate(person_refs="Alice", entity_types="PERSON")
        result = detector._classify_heuristic(
            new_text="Alice is now a senior engineer instead of junior",
            candidate=candidate,
            similarity=0.8,
            new_entities=[{"text": "Alice", "label": "PERSON"}],
        )
        assert result.relationship_type == "UPDATES"

    def test_same_entity_additional_attribute_is_extends(self):
        """Same entity, additional attribute → EXTENDS."""
        detector = MemoryEvolutionDetector(llm_provider=None)
        candidate = self._make_candidate(person_refs="Alice", entity_types="PERSON")
        result = detector._classify_heuristic(
            new_text="Additionally, Alice has a PhD in computer science",
            candidate=candidate,
            similarity=0.6,
            new_entities=[{"text": "Alice", "label": "PERSON"}],
        )
        assert result.relationship_type == "EXTENDS"

    def test_no_entity_overlap_returns_none_or_low_confidence(self):
        """No entity overlap → NONE or very low confidence."""
        detector = MemoryEvolutionDetector(llm_provider=None)
        candidate = self._make_candidate(person_refs="Charlie", entity_types="PERSON")
        result = detector._classify_heuristic(
            new_text="Bitcoin hit new highs today",
            candidate=candidate,
            similarity=0.1,
            new_entities=[{"text": "Bitcoin", "label": "PRODUCT"}],
        )
        assert result.relationship_type is None or result.confidence < 0.3

    def test_empty_new_text(self):
        """Empty text should return no signals."""
        detector = MemoryEvolutionDetector(llm_provider=None)
        candidate = self._make_candidate(person_refs="Alice")
        result = detector._classify_heuristic(
            new_text="",
            candidate=candidate,
            similarity=0.0,
            new_entities=None,
        )
        assert result.relationship_type is None

    def test_mixed_markers_highest_wins(self):
        """When multiple marker types are present, highest score wins."""
        detector = MemoryEvolutionDetector(llm_provider=None)
        candidate = self._make_candidate(person_refs="Alice", entity_types="PERSON")
        result = detector._classify_heuristic(
            new_text="Actually, correction: Alice also now runs the team",
            candidate=candidate,
            similarity=0.5,
            new_entities=[{"text": "Alice", "label": "PERSON"}],
        )
        # "actually" and "correction" = 2 UPDATES markers vs 1 EXTENDS ("also")
        assert result.relationship_type == "UPDATES"

    def test_candidate_text_extraction(self):
        """Test _get_candidate_text helper."""
        candidate = self._make_candidate(
            person_refs="Alice,Bob",
            entity_types="PERSON,ORG",
            entities_json='[{"text": "Alice"}, {"text": "Google"}]',
        )
        text = MemoryEvolutionDetector._get_candidate_text(candidate)
        assert "Alice" in text
        assert "Bob" in text
        assert "PERSON" in text

    def test_candidate_text_no_info(self):
        """Test _get_candidate_text with empty candidate."""
        candidate = self._make_candidate()
        candidate.person_refs = None
        candidate.entity_types = None
        candidate.entities_json = None
        text = MemoryEvolutionDetector._get_candidate_text(candidate)
        assert text == "<no entity info>"
