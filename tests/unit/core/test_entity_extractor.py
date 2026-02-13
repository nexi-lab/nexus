"""Tests for Entity Extraction (Issue #1025).

Tests for the SimpleMem-inspired symbolic layer that extracts
named entities at memory ingestion time for improved multi-hop queries.
"""

import pytest

from nexus.services.permissions.entity_extractor import (
    EntityExtractor,
    ExtractedEntity,
    extract_entities,
    extract_entities_as_dicts,
    get_default_extractor,
)


class TestExtractedEntity:
    """Test ExtractedEntity dataclass."""

    def test_to_dict(self):
        """Test conversion to dictionary."""
        entity = ExtractedEntity(
            text="John Smith",
            type="PERSON",
            start=0,
            end=10,
        )
        result = entity.to_dict()

        assert result == {
            "text": "John Smith",
            "type": "PERSON",
            "start": 0,
            "end": 10,
        }


class TestEntityExtractorRegex:
    """Test regex-based entity extraction."""

    @pytest.fixture
    def extractor(self):
        """Create entity extractor with regex fallback."""
        return EntityExtractor(use_spacy=False)

    def test_extract_date_iso_format(self, extractor):
        """Test extracting ISO format dates."""
        text = "The meeting is scheduled for 2024-01-15."
        entities = extractor.extract(text)

        date_entities = [e for e in entities if e.type == "DATE"]
        assert len(date_entities) == 1
        assert date_entities[0].text == "2024-01-15"

    def test_extract_date_us_format(self, extractor):
        """Test extracting US format dates."""
        text = "The deadline is 01/15/2024."
        entities = extractor.extract(text)

        date_entities = [e for e in entities if e.type == "DATE"]
        assert len(date_entities) == 1
        assert date_entities[0].text == "01/15/2024"

    def test_extract_date_written_format(self, extractor):
        """Test extracting written format dates."""
        text = "The event is on January 15, 2024."
        entities = extractor.extract(text)

        date_entities = [e for e in entities if e.type == "DATE"]
        assert len(date_entities) == 1
        assert "January" in date_entities[0].text

    def test_extract_currency(self, extractor):
        """Test extracting currency amounts."""
        text = "The project costs $1,234.56."
        entities = extractor.extract(text)

        number_entities = [e for e in entities if e.type == "NUMBER"]
        assert len(number_entities) == 1
        assert number_entities[0].text == "$1,234.56"

    def test_extract_numbers_with_units(self, extractor):
        """Test extracting numbers with units."""
        text = "Revenue increased by 15% to 2.5 million."
        entities = extractor.extract(text)

        number_entities = [e for e in entities if e.type == "NUMBER"]
        assert len(number_entities) >= 1
        # Should find "15%" or "2.5 million"
        texts = [e.text for e in number_entities]
        assert any("15%" in t or "million" in t for t in texts)

    def test_extract_email(self, extractor):
        """Test extracting email addresses."""
        text = "Contact us at support@example.com for help."
        entities = extractor.extract(text)

        email_entities = [e for e in entities if e.type == "EMAIL"]
        assert len(email_entities) == 1
        assert email_entities[0].text == "support@example.com"

    def test_extract_url(self, extractor):
        """Test extracting URLs."""
        text = "Visit https://example.com/docs for documentation."
        entities = extractor.extract(text)

        url_entities = [e for e in entities if e.type == "URL"]
        assert len(url_entities) == 1
        assert url_entities[0].text == "https://example.com/docs"

    def test_extract_organization_with_suffix(self, extractor):
        """Test extracting organizations with common suffixes."""
        text = "She works at Microsoft Corporation."
        entities = extractor.extract(text)

        org_entities = [e for e in entities if e.type == "ORG"]
        assert len(org_entities) == 1
        assert org_entities[0].text == "Microsoft Corporation"

    def test_extract_location_with_indicator(self, extractor):
        """Test extracting locations with common indicators."""
        text = "They visited Central Park yesterday."
        entities = extractor.extract(text)

        loc_entities = [e for e in entities if e.type == "LOCATION"]
        assert len(loc_entities) == 1
        assert loc_entities[0].text == "Central Park"

    def test_extract_person_two_word_name(self, extractor):
        """Test extracting person names (two words)."""
        text = "John Smith attended the meeting."
        entities = extractor.extract(text)

        person_entities = [e for e in entities if e.type == "PERSON"]
        assert len(person_entities) == 1
        assert person_entities[0].text == "John Smith"

    def test_extract_person_with_title(self, extractor):
        """Test extracting person names with titles."""
        text = "Dr. Jane Doe presented the findings."
        entities = extractor.extract(text)

        person_entities = [e for e in entities if e.type == "PERSON"]
        assert len(person_entities) >= 1
        # Should find "Jane Doe"
        names = [e.text for e in person_entities]
        assert any("Jane" in n for n in names)

    def test_extract_multiple_entities(self, extractor):
        """Test extracting multiple entities from complex text."""
        text = "John Smith from Microsoft visited New York on 2024-01-15 and spent $5,000."
        entities = extractor.extract(text)

        types = {e.type for e in entities}
        assert "PERSON" in types
        assert "DATE" in types
        assert "NUMBER" in types
        # ORG may or may not be detected depending on context

    def test_entity_positions(self, extractor):
        """Test that entity positions are correct."""
        text = "Hello John Smith!"
        entities = extractor.extract(text)

        person_entities = [e for e in entities if e.type == "PERSON"]
        assert len(person_entities) == 1

        entity = person_entities[0]
        assert text[entity.start : entity.end] == entity.text

    def test_no_duplicate_entities(self, extractor):
        """Test that overlapping entities are deduplicated."""
        text = "Contact support@example.com at Example Corp."
        entities = extractor.extract(text)

        # Each span should appear only once
        spans = [(e.start, e.end) for e in entities]
        assert len(spans) == len(set(spans))

    def test_sorted_by_position(self, extractor):
        """Test that entities are sorted by position."""
        text = "John Smith visited New York on 2024-01-15."
        entities = extractor.extract(text)

        positions = [e.start for e in entities]
        assert positions == sorted(positions)


class TestEntityExtractorHelpers:
    """Test helper methods."""

    @pytest.fixture
    def extractor(self):
        """Create entity extractor."""
        return EntityExtractor(use_spacy=False)

    def test_extract_as_dicts(self, extractor):
        """Test extracting entities as dictionaries."""
        text = "John Smith works at Acme Inc."
        result = extractor.extract_as_dicts(text)

        assert isinstance(result, list)
        assert all(isinstance(d, dict) for d in result)
        assert all("text" in d and "type" in d for d in result)

    def test_get_entity_types(self, extractor):
        """Test getting unique entity types."""
        text = "John Smith visited New York on 2024-01-15."
        types = extractor.get_entity_types(text)

        assert isinstance(types, set)
        assert "PERSON" in types
        assert "DATE" in types

    def test_get_entity_types_string(self, extractor):
        """Test getting entity types as comma-separated string."""
        text = "John Smith visited New York on 2024-01-15."
        types_str = extractor.get_entity_types_string(text)

        assert isinstance(types_str, str)
        assert "PERSON" in types_str
        assert "DATE" in types_str
        # Should be comma-separated
        assert "," in types_str

    def test_get_person_refs(self, extractor):
        """Test getting person references."""
        text = "John Smith met with Jane Doe yesterday."
        persons = extractor.get_person_refs(text)

        assert isinstance(persons, list)
        assert "John Smith" in persons
        assert "Jane Doe" in persons

    def test_get_person_refs_string(self, extractor):
        """Test getting person references as comma-separated string."""
        text = "John Smith met with Jane Doe yesterday."
        persons_str = extractor.get_person_refs_string(text)

        assert isinstance(persons_str, str)
        assert "John Smith" in persons_str
        assert "Jane Doe" in persons_str

    def test_empty_text(self, extractor):
        """Test handling empty text."""
        entities = extractor.extract("")
        assert entities == []

    def test_no_entities(self, extractor):
        """Test text with no extractable entities."""
        text = "the quick brown fox jumps over the lazy dog"
        entities = extractor.extract(text)

        # All lowercase, no proper nouns, dates, etc.
        assert entities == []


class TestModuleFunctions:
    """Test module-level convenience functions."""

    def test_get_default_extractor(self):
        """Test getting default extractor."""
        extractor1 = get_default_extractor()
        extractor2 = get_default_extractor()

        # Should return same instance (singleton)
        assert extractor1 is extractor2
        assert isinstance(extractor1, EntityExtractor)

    def test_extract_entities(self):
        """Test module-level extract_entities function."""
        text = "John Smith visited on 2024-01-15."
        entities = extract_entities(text)

        assert isinstance(entities, list)
        assert all(isinstance(e, ExtractedEntity) for e in entities)
        assert len(entities) >= 2  # At least person and date

    def test_extract_entities_as_dicts(self):
        """Test module-level extract_entities_as_dicts function."""
        text = "John Smith visited on 2024-01-15."
        result = extract_entities_as_dicts(text)

        assert isinstance(result, list)
        assert all(isinstance(d, dict) for d in result)


class TestEdgeCases:
    """Test edge cases and special scenarios."""

    @pytest.fixture
    def extractor(self):
        """Create entity extractor."""
        return EntityExtractor(use_spacy=False)

    def test_unicode_text(self, extractor):
        """Test handling Unicode text."""
        text = "John Smith visited Tokyo on 2024-01-15."
        entities = extractor.extract(text)

        # Should not crash
        assert isinstance(entities, list)
        # Should find the person and date
        types = {e.type for e in entities}
        assert "PERSON" in types
        assert "DATE" in types

    def test_newlines_in_text(self, extractor):
        """Test handling text with newlines."""
        text = "John Smith\nvisited\nNew York"
        entities = extractor.extract(text)

        # Should extract entities across newlines
        assert isinstance(entities, list)

    def test_special_characters(self, extractor):
        """Test handling special characters."""
        text = "Email: user+tag@example.com"
        entities = extractor.extract(text)

        email_entities = [e for e in entities if e.type == "EMAIL"]
        assert len(email_entities) == 1
        assert email_entities[0].text == "user+tag@example.com"

    def test_multiple_dates(self, extractor):
        """Test extracting multiple dates."""
        text = "From 2024-01-01 to 2024-12-31."
        entities = extractor.extract(text)

        date_entities = [e for e in entities if e.type == "DATE"]
        assert len(date_entities) == 2

    def test_company_variations(self, extractor):
        """Test extracting various company name formats."""
        texts = [
            "Works at Apple Inc.",
            "Employed by Google LLC",
            "Joined Amazon Corp",
        ]

        for text in texts:
            entities = extractor.extract(text)
            org_entities = [e for e in entities if e.type == "ORG"]
            assert len(org_entities) >= 1, f"Failed to extract org from: {text}"
