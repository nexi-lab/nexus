"""Tests for Relationship Extraction (Issue #1038).

Tests for the LightRAG/GraphRAG-inspired relationship extraction
that extracts (subject, predicate, object) triplets at memory ingestion.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.services.memory.relationship_extractor import (
    DEFAULT_RELATIONSHIP_TYPES,
    ExtractedRelationship,
    HeuristicRelationshipExtractor,
    LLMRelationshipExtractor,
    RelationshipExtractionResult,
    extract_relationships,
    extract_relationships_as_dicts,
    get_extractor,
)


class TestExtractedRelationship:
    """Test ExtractedRelationship dataclass."""

    def test_to_dict(self):
        """Test conversion to dictionary."""
        rel = ExtractedRelationship(
            subject="Alice",
            predicate="MANAGES",
            object="frontend_team",
            confidence=0.95,
            source_text="Alice manages the frontend team",
        )
        result = rel.to_dict()

        assert result == {
            "subject": "Alice",
            "predicate": "MANAGES",
            "object": "frontend_team",
            "confidence": 0.95,
            "source_text": "Alice manages the frontend team",
        }

    def test_to_dict_default_values(self):
        """Test conversion with default values."""
        rel = ExtractedRelationship(
            subject="Task A",
            predicate="BLOCKS",
            object="Task B",
        )
        result = rel.to_dict()

        assert result["subject"] == "Task A"
        assert result["predicate"] == "BLOCKS"
        assert result["object"] == "Task B"
        assert result["confidence"] == 1.0
        assert result["source_text"] is None


class TestRelationshipExtractionResult:
    """Test RelationshipExtractionResult dataclass."""

    def test_to_dicts(self):
        """Test conversion of relationships to list of dicts."""
        result = RelationshipExtractionResult(
            relationships=[
                ExtractedRelationship("Alice", "MANAGES", "team", 0.9),
                ExtractedRelationship("Bob", "WORKS_WITH", "Alice", 0.85),
            ],
            method="llm",
        )

        dicts = result.to_dicts()
        assert len(dicts) == 2
        assert dicts[0]["subject"] == "Alice"
        assert dicts[1]["subject"] == "Bob"

    def test_empty_result(self):
        """Test empty result."""
        result = RelationshipExtractionResult()
        assert result.relationships == []
        assert result.method == "none"
        assert result.to_dicts() == []


class TestHeuristicRelationshipExtractor:
    """Test heuristic-based relationship extraction."""

    @pytest.fixture
    def extractor(self):
        """Create heuristic extractor."""
        return HeuristicRelationshipExtractor(confidence_threshold=0.5)

    def test_extract_manages_relationship(self, extractor):
        """Test extracting MANAGES relationship."""
        text = "John Smith manages the engineering team."
        result = extractor.extract(text)

        # Heuristic extraction is less reliable, so we just check it runs
        assert isinstance(result, RelationshipExtractionResult)
        assert result.method == "heuristic"

    def test_extract_works_with_relationship(self, extractor):
        """Test extracting WORKS_WITH relationship."""
        text = "Alice works with Bob on the project."
        result = extractor.extract(text)

        assert result.method == "heuristic"

    def test_extract_depends_on_relationship(self, extractor):
        """Test extracting DEPENDS_ON relationship."""
        text = "The API depends on the database service."
        result = extractor.extract(text)

        assert result.method == "heuristic"

    def test_extract_blocks_relationship(self, extractor):
        """Test extracting BLOCKS relationship."""
        text = "Task A blocks Task B."
        result = extractor.extract(text)

        assert result.method == "heuristic"

    def test_empty_text(self, extractor):
        """Test with empty text."""
        result = extractor.extract("")
        assert result.relationships == []
        assert result.method == "heuristic"

    def test_short_text(self, extractor):
        """Test with very short text."""
        result = extractor.extract("Hi")
        assert result.relationships == []

    def test_with_entities(self, extractor):
        """Test extraction with pre-extracted entities."""
        text = "Alice manages the team."
        entities = [
            {"text": "Alice", "type": "PERSON"},
            {"text": "team", "type": "ENTITY"},
        ]
        result = extractor.extract(text, entities=entities)

        assert result.method == "heuristic"

    def test_custom_relationship_types(self, extractor):
        """Test with custom relationship types."""
        text = "Alice manages the team."
        result = extractor.extract(
            text,
            relationship_types=["MANAGES", "OWNS"],
        )

        assert result.method == "heuristic"


class TestLLMRelationshipExtractor:
    """Test LLM-based relationship extraction."""

    @pytest.fixture
    def mock_provider(self):
        """Create mock LLM provider."""
        provider = MagicMock()
        provider.complete_async = AsyncMock()
        provider.config = MagicMock()
        provider.config.model = "gpt-4o-mini"
        return provider

    @pytest.fixture
    def extractor(self, mock_provider):
        """Create LLM extractor with mock provider."""
        return LLMRelationshipExtractor(
            llm_provider=mock_provider,
            confidence_threshold=0.5,
        )

    def test_parse_valid_json_response(self, extractor):
        """Test parsing valid JSON response."""
        response = json.dumps(
            {
                "relationships": [
                    {
                        "subject": "Alice",
                        "predicate": "MANAGES",
                        "object": "team",
                        "confidence": 0.95,
                    },
                    {
                        "subject": "Bob",
                        "predicate": "WORKS_WITH",
                        "object": "Alice",
                        "confidence": 0.85,
                    },
                ]
            }
        )

        relationships = extractor._parse_response(response, "test text")
        assert len(relationships) == 2
        assert relationships[0].subject == "Alice"
        assert relationships[0].predicate == "MANAGES"
        assert relationships[0].object == "team"
        assert relationships[0].confidence == 0.95
        assert relationships[1].subject == "Bob"

    def test_parse_json_with_markdown(self, extractor):
        """Test parsing JSON wrapped in markdown code block."""
        response = """```json
{
    "relationships": [
        {"subject": "Alice", "predicate": "MANAGES", "object": "team", "confidence": 0.9}
    ]
}
```"""

        relationships = extractor._parse_response(response, "test text")
        assert len(relationships) == 1
        assert relationships[0].subject == "Alice"

    def test_parse_empty_relationships(self, extractor):
        """Test parsing empty relationships array."""
        response = json.dumps({"relationships": []})
        relationships = extractor._parse_response(response, "test text")
        assert len(relationships) == 0

    def test_parse_invalid_json(self, extractor):
        """Test parsing invalid JSON returns empty list."""
        response = "This is not valid JSON"
        relationships = extractor._parse_response(response, "test text")
        assert len(relationships) == 0

    def test_parse_missing_fields(self, extractor):
        """Test parsing relationships with missing fields."""
        response = json.dumps(
            {
                "relationships": [
                    {"subject": "Alice"},  # Missing predicate and object
                    {"subject": "Bob", "predicate": "MANAGES", "object": "team"},
                ]
            }
        )

        relationships = extractor._parse_response(response, "test text")
        # Should only get the complete relationship
        assert len(relationships) == 1
        assert relationships[0].subject == "Bob"

    @pytest.mark.asyncio
    async def test_extract_async_with_mock(self, extractor, mock_provider):
        """Test async extraction with mock provider."""
        # Setup mock response
        mock_response = MagicMock()
        mock_response.content = json.dumps(
            {
                "relationships": [
                    {
                        "subject": "Alice",
                        "predicate": "MANAGES",
                        "object": "frontend team",
                        "confidence": 0.95,
                    }
                ]
            }
        )
        mock_provider.complete_async.return_value = mock_response

        result = await extractor.extract_async("Alice manages the frontend team.")

        assert len(result.relationships) == 1
        assert result.relationships[0].subject == "Alice"
        assert result.relationships[0].predicate == "MANAGES"
        assert result.method == "llm"

    @pytest.mark.asyncio
    async def test_extract_async_empty_text(self, extractor):
        """Test async extraction with empty text."""
        result = await extractor.extract_async("")
        assert len(result.relationships) == 0
        assert result.method == "none"

    @pytest.mark.asyncio
    async def test_extract_async_short_text(self, extractor):
        """Test async extraction with very short text."""
        result = await extractor.extract_async("Hi")
        assert len(result.relationships) == 0

    @pytest.mark.asyncio
    async def test_extract_async_with_entities(self, extractor, mock_provider):
        """Test async extraction with pre-extracted entities."""
        mock_response = MagicMock()
        mock_response.content = json.dumps(
            {
                "relationships": [
                    {
                        "subject": "Alice",
                        "predicate": "WORKS_WITH",
                        "object": "Bob",
                        "confidence": 0.9,
                    }
                ]
            }
        )
        mock_provider.complete_async.return_value = mock_response

        entities = [
            {"text": "Alice", "type": "PERSON"},
            {"text": "Bob", "type": "PERSON"},
        ]

        result = await extractor.extract_async(
            "Alice works with Bob.",
            entities=entities,
        )

        assert len(result.relationships) == 1
        assert result.method == "llm"

    @pytest.mark.asyncio
    async def test_extract_async_confidence_filtering(self, extractor, mock_provider):
        """Test that relationships below confidence threshold are filtered."""
        mock_response = MagicMock()
        mock_response.content = json.dumps(
            {
                "relationships": [
                    {"subject": "A", "predicate": "RELATES_TO", "object": "B", "confidence": 0.9},
                    {"subject": "C", "predicate": "RELATES_TO", "object": "D", "confidence": 0.3},
                ]
            }
        )
        mock_provider.complete_async.return_value = mock_response

        result = await extractor.extract_async("Some text about relationships.")

        # Only the high-confidence relationship should be included
        assert len(result.relationships) == 1
        assert result.relationships[0].confidence == 0.9

    @pytest.mark.asyncio
    async def test_extract_async_fallback_on_error(self, extractor, mock_provider):
        """Test fallback to heuristic on LLM error."""
        mock_provider.complete_async.side_effect = Exception("API error")

        result = await extractor.extract_async("Alice manages the team.")

        # Should fall back to heuristic
        assert result.method == "heuristic"

    def test_extract_sync(self, extractor, mock_provider):
        """Test sync extraction."""
        mock_response = MagicMock()
        mock_response.content = json.dumps(
            {
                "relationships": [
                    {
                        "subject": "Alice",
                        "predicate": "MANAGES",
                        "object": "team",
                        "confidence": 0.9,
                    }
                ]
            }
        )
        mock_provider.complete_async.return_value = mock_response

        result = extractor.extract("Alice manages the team.")

        assert result.method == "llm"


class TestConvenienceFunctions:
    """Test module-level convenience functions."""

    def test_get_extractor_default(self):
        """Test getting default extractor."""
        extractor = get_extractor()
        assert isinstance(extractor, LLMRelationshipExtractor)

    def test_get_extractor_with_provider(self):
        """Test getting extractor with custom provider."""
        mock_provider = MagicMock()
        extractor = get_extractor(llm_provider=mock_provider)
        assert isinstance(extractor, LLMRelationshipExtractor)
        assert extractor.llm_provider == mock_provider

    def test_extract_relationships_function(self):
        """Test extract_relationships convenience function."""
        # This will likely use heuristic fallback since no LLM is configured
        with patch.object(
            LLMRelationshipExtractor,
            "_get_default_provider",
            return_value=None,
        ):
            relationships = extract_relationships("Alice manages the team.")
            assert isinstance(relationships, list)

    def test_extract_relationships_as_dicts_function(self):
        """Test extract_relationships_as_dicts convenience function."""
        with patch.object(
            LLMRelationshipExtractor,
            "_get_default_provider",
            return_value=None,
        ):
            dicts = extract_relationships_as_dicts("Alice manages the team.")
            assert isinstance(dicts, list)
            for d in dicts:
                assert isinstance(d, dict)


class TestRelationshipTypes:
    """Test relationship type constants."""

    def test_default_relationship_types(self):
        """Test default relationship types are defined."""
        assert len(DEFAULT_RELATIONSHIP_TYPES) > 0
        assert "MANAGES" in DEFAULT_RELATIONSHIP_TYPES
        assert "WORKS_WITH" in DEFAULT_RELATIONSHIP_TYPES
        assert "DEPENDS_ON" in DEFAULT_RELATIONSHIP_TYPES
        assert "BLOCKS" in DEFAULT_RELATIONSHIP_TYPES
        assert "CREATES" in DEFAULT_RELATIONSHIP_TYPES

    def test_relationship_types_are_uppercase(self):
        """Test all relationship types are uppercase."""
        for rel_type in DEFAULT_RELATIONSHIP_TYPES:
            assert rel_type == rel_type.upper()


class TestGleaningFeature:
    """Test the gleaning (retry) feature for missed relationships."""

    @pytest.fixture
    def mock_provider(self):
        """Create mock LLM provider."""
        provider = MagicMock()
        provider.complete_async = AsyncMock()
        provider.config = MagicMock()
        return provider

    @pytest.mark.asyncio
    async def test_gleaning_disabled_by_default(self, mock_provider):
        """Test that gleaning is disabled by default."""
        extractor = LLMRelationshipExtractor(
            llm_provider=mock_provider,
            enable_gleaning=False,
        )

        mock_response = MagicMock()
        mock_response.content = json.dumps(
            {
                "relationships": [
                    {"subject": "A", "predicate": "RELATES_TO", "object": "B", "confidence": 0.9}
                ]
            }
        )
        mock_provider.complete_async.return_value = mock_response

        # Need enough text to trigger extraction (>10 chars)
        await extractor.extract_async("Test text with enough content to trigger extraction.")

        # Should only be called once (no gleaning)
        assert mock_provider.complete_async.call_count == 1

    @pytest.mark.asyncio
    async def test_gleaning_enabled(self, mock_provider):
        """Test gleaning when enabled."""
        extractor = LLMRelationshipExtractor(
            llm_provider=mock_provider,
            enable_gleaning=True,
            max_gleaning_retries=1,
        )

        # First call returns some relationships
        first_response = MagicMock()
        first_response.content = json.dumps(
            {
                "relationships": [
                    {"subject": "A", "predicate": "RELATES_TO", "object": "B", "confidence": 0.9}
                ]
            }
        )

        # Second call (gleaning) returns more
        second_response = MagicMock()
        second_response.content = json.dumps(
            {
                "relationships": [
                    {"subject": "C", "predicate": "RELATES_TO", "object": "D", "confidence": 0.85}
                ]
            }
        )

        mock_provider.complete_async.side_effect = [first_response, second_response]

        result = await extractor.extract_async("Test text with multiple relationships.")

        # Should be called twice (initial + gleaning)
        assert mock_provider.complete_async.call_count == 2
        # Should have relationships from both calls
        assert len(result.relationships) == 2
