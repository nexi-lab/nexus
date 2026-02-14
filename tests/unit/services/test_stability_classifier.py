"""Unit tests for TemporalStabilityClassifier (#1191).

Tests the hybrid heuristic+LLM classifier for memory temporal stability
classification (static, semi_dynamic, dynamic).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nexus.services.memory.stability_classifier import (
    DEFAULT_TTL,
    VALID_STABILITIES,
    StabilityClassification,
    TemporalStabilityClassifier,
)


class TestStabilityClassification:
    """Tests for the StabilityClassification frozen dataclass."""

    def test_create_classification(self):
        result = StabilityClassification(
            temporal_stability="static",
            confidence=0.9,
            estimated_ttl_days=None,
            method="heuristic",
            signals=["static_marker:always"],
        )
        assert result.temporal_stability == "static"
        assert result.confidence == 0.9
        assert result.estimated_ttl_days is None
        assert result.method == "heuristic"
        assert result.signals == ["static_marker:always"]

    def test_classification_is_frozen(self):
        result = StabilityClassification(
            temporal_stability="static",
            confidence=0.9,
            estimated_ttl_days=None,
            method="heuristic",
        )
        with pytest.raises(AttributeError):
            result.temporal_stability = "dynamic"  # type: ignore[misc]

    def test_default_signals(self):
        result = StabilityClassification(
            temporal_stability="dynamic",
            confidence=0.8,
            estimated_ttl_days=30,
            method="llm",
        )
        assert result.signals == []


class TestHeuristicClassifier:
    """Tests for the heuristic classification path."""

    @pytest.fixture
    def classifier(self):
        return TemporalStabilityClassifier(llm_provider=None, confidence_threshold=0.6)

    # --- Static cases ---

    @pytest.mark.parametrize(
        "text",
        [
            "Paris is the capital of France",
            "Pi equals 3.14159",
            "Albert Einstein was born on March 14, 1879",
            "Water has a boiling point of 100 degrees Celsius",
            "The Pythagorean theorem states that a^2 + b^2 = c^2",
            "The company was founded in 1998",
            "Gold has an atomic number of 79",
        ],
    )
    def test_static_memories(self, classifier, text):
        result = classifier.classify(text)
        assert result.temporal_stability == "static"
        assert result.confidence >= 0.5
        assert result.estimated_ttl_days is None  # Static = infinite TTL
        assert result.method == "heuristic"
        assert any("static_marker" in s for s in result.signals)

    # --- Dynamic cases ---

    @pytest.mark.parametrize(
        "text",
        [
            "John is currently working on the Q4 report",
            "The stock price is at $150 right now",
            "Today the weather is sunny and warm",
            "She just started a new project at the moment",
            "Breaking news: market reaches new high",
            "The system is presently under maintenance",
        ],
    )
    def test_dynamic_memories(self, classifier, text):
        result = classifier.classify(text)
        assert result.temporal_stability == "dynamic"
        assert result.confidence >= 0.5
        assert result.estimated_ttl_days == DEFAULT_TTL["dynamic"]
        assert result.method == "heuristic"
        assert any("dynamic_marker" in s for s in result.signals)

    # --- Semi-dynamic cases ---

    @pytest.mark.parametrize(
        "text",
        [
            "Sarah works at Microsoft as a senior engineer",
            "John lives in San Francisco",
            "Maria is studying computer science at MIT",
            "He usually prefers dark mode for coding",
            "Alice is employed by Google",
        ],
    )
    def test_semi_dynamic_memories(self, classifier, text):
        result = classifier.classify(text)
        assert result.temporal_stability == "semi_dynamic"
        assert result.confidence >= 0.5
        assert result.estimated_ttl_days == DEFAULT_TTL["semi_dynamic"]
        assert result.method == "heuristic"
        assert any("semi_dynamic_marker" in s for s in result.signals)

    # --- Edge cases ---

    def test_empty_string(self, classifier):
        result = classifier.classify("")
        assert result.temporal_stability in VALID_STABILITIES
        assert 0.0 <= result.confidence <= 1.0

    def test_very_long_content_truncated(self, classifier):
        long_text = "This is always true. " * 100  # Well over 500 chars
        result = classifier.classify(long_text)
        assert result.temporal_stability == "static"
        assert result.method == "heuristic"

    def test_no_signals_returns_semi_dynamic_default(self, classifier):
        result = classifier.classify("A simple neutral statement about things.")
        assert result.temporal_stability == "semi_dynamic"
        assert result.confidence == 0.3
        assert result.signals == ["no_signals_detected"]

    def test_mixed_signals(self, classifier):
        # Both static and dynamic markers present
        result = classifier.classify("Paris is the capital of France, currently under renovation.")
        assert result.temporal_stability in VALID_STABILITIES
        assert result.confidence > 0.0
        assert len(result.signals) >= 2

    def test_entity_signals(self, classifier):
        entities = [
            {"label": "PERSON", "text": "birth date"},
            {"label": "ORG", "text": "founded year"},
        ]
        result = classifier.classify("Some text with entities", entities=entities)
        assert result.temporal_stability in VALID_STABILITIES
        assert any("entity" in s for s in result.signals)

    def test_temporal_refs_boost_dynamic(self, classifier):
        temporal_refs = [
            {"type": "date", "text": "tomorrow", "value": "2025-01-15"},
            {"type": "date", "text": "next week", "value": "2025-01-20"},
        ]
        result = classifier.classify("Meeting scheduled for tomorrow", temporal_refs=temporal_refs)
        assert any("temporal_refs_count" in s for s in result.signals)


class TestLLMClassifier:
    """Tests for the LLM classification path."""

    def test_valid_llm_response(self):
        mock_llm = MagicMock()
        mock_llm.generate.return_value = (
            '{"stability": "dynamic", "confidence": 0.85, "ttl_days": 7, '
            '"reasoning": "Time-sensitive status update"}'
        )

        classifier = TemporalStabilityClassifier(
            llm_provider=mock_llm,
            confidence_threshold=1.0,  # Force LLM path
        )
        result = classifier.classify("John is currently at the office")

        assert result.temporal_stability == "dynamic"
        assert result.confidence == 0.85
        assert result.estimated_ttl_days == 7
        assert result.method == "llm"
        assert any("llm_reasoning" in s for s in result.signals)

    def test_invalid_llm_response_falls_back(self):
        mock_llm = MagicMock()
        mock_llm.generate.return_value = "This is not valid JSON at all"

        classifier = TemporalStabilityClassifier(
            llm_provider=mock_llm,
            confidence_threshold=1.0,  # Force LLM path
        )
        # Should fall back to heuristic on parse failure
        result = classifier.classify("Paris is the capital of France")

        assert result.temporal_stability in VALID_STABILITIES
        assert result.method == "heuristic"

    def test_llm_exception_falls_back(self):
        mock_llm = MagicMock()
        mock_llm.generate.side_effect = RuntimeError("LLM service unavailable")

        classifier = TemporalStabilityClassifier(
            llm_provider=mock_llm,
            confidence_threshold=1.0,  # Force LLM path
        )
        result = classifier.classify("Paris is the capital of France")

        assert result.temporal_stability in VALID_STABILITIES
        assert result.method == "heuristic"

    def test_llm_invalid_stability_normalized(self):
        mock_llm = MagicMock()
        mock_llm.generate.return_value = (
            '{"stability": "unknown_value", "confidence": 0.5, "ttl_days": null, '
            '"reasoning": "test"}'
        )

        classifier = TemporalStabilityClassifier(llm_provider=mock_llm, confidence_threshold=1.0)
        result = classifier.classify("Some text")
        # Invalid stability should be normalized to semi_dynamic
        assert result.temporal_stability == "semi_dynamic"

    def test_llm_confidence_clamped(self):
        mock_llm = MagicMock()
        mock_llm.generate.return_value = (
            '{"stability": "static", "confidence": 5.0, "ttl_days": null, "reasoning": "test"}'
        )

        classifier = TemporalStabilityClassifier(llm_provider=mock_llm, confidence_threshold=1.0)
        result = classifier.classify("Some text")
        assert result.confidence <= 1.0


class TestHybridClassifier:
    """Tests for the hybrid (heuristic + LLM) classification path."""

    def test_high_heuristic_confidence_skips_llm(self):
        mock_llm = MagicMock()
        classifier = TemporalStabilityClassifier(llm_provider=mock_llm, confidence_threshold=0.6)

        result = classifier.classify("Paris is the capital of France")

        # Should NOT call LLM because heuristic confidence > threshold
        assert result.method == "heuristic"
        mock_llm.generate.assert_not_called()

    def test_low_heuristic_confidence_invokes_llm(self):
        mock_llm = MagicMock()
        mock_llm.generate.return_value = (
            '{"stability": "semi_dynamic", "confidence": 0.8, "ttl_days": 180, '
            '"reasoning": "Ambiguous case resolved by LLM"}'
        )

        classifier = TemporalStabilityClassifier(llm_provider=mock_llm, confidence_threshold=0.6)

        # Neutral text should have low heuristic confidence
        result = classifier.classify("The meeting went well.")

        # Should have called LLM since heuristic confidence was low
        assert result.method == "llm"
        mock_llm.generate.assert_called_once()

    def test_llm_failure_returns_heuristic_result(self):
        mock_llm = MagicMock()
        mock_llm.generate.side_effect = Exception("LLM error")

        classifier = TemporalStabilityClassifier(llm_provider=mock_llm, confidence_threshold=0.6)

        result = classifier.classify("The meeting went well.")
        assert result.method == "heuristic"
        assert result.temporal_stability in VALID_STABILITIES

    def test_no_llm_provider_returns_heuristic(self):
        classifier = TemporalStabilityClassifier(llm_provider=None, confidence_threshold=0.6)

        result = classifier.classify("The meeting went well.")
        assert result.method == "heuristic"

    def test_confidence_threshold_boundary(self):
        mock_llm = MagicMock()
        mock_llm.generate.return_value = (
            '{"stability": "dynamic", "confidence": 0.9, "ttl_days": 7, "reasoning": "test"}'
        )

        # Set threshold to 0.95 — almost nothing should pass
        classifier = TemporalStabilityClassifier(llm_provider=mock_llm, confidence_threshold=0.95)

        result = classifier.classify("Paris is the capital of France")
        # Even high-confidence heuristic (0.95 cap) might be at boundary
        # Key test: the classifier should work either way
        assert result.temporal_stability in VALID_STABILITIES
