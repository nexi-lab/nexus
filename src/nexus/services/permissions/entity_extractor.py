"""Entity extraction for memory ingestion (Issue #1025).

Implements lightweight named entity extraction inspired by SimpleMem's
symbolic layer for improved multi-hop query performance.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

# Entity type definitions
EntityType = Literal["PERSON", "ORG", "LOCATION", "DATE", "NUMBER", "EMAIL", "URL", "ENTITY"]


@dataclass
class ExtractedEntity:
    """Extracted named entity with position information."""

    text: str
    type: EntityType
    start: int
    end: int

    def to_dict(self) -> dict[str, str | int]:
        """Convert to dictionary for JSON serialization."""
        return {
            "text": self.text,
            "type": self.type,
            "start": self.start,
            "end": self.end,
        }


class EntityExtractor:
    """Extract named entities using lightweight NER.

    Provides both spaCy-based extraction (when available) and a regex-based
    fallback for environments without spaCy.

    Examples:
        >>> extractor = EntityExtractor()
        >>> entities = extractor.extract("John Smith from Microsoft visited New York on 2024-01-15.")
        >>> for e in entities:
        ...     print(f"{e.type}: {e.text}")
        PERSON: John Smith
        ORG: Microsoft
        LOCATION: New York
        DATE: 2024-01-15
    """

    # Regex patterns for lightweight entity extraction
    ENTITY_PATTERNS: dict[EntityType, re.Pattern[str]] = {
        # Dates in various formats
        "DATE": re.compile(
            r"\b(?:"
            r"\d{4}-\d{2}-\d{2}"  # ISO format: 2024-01-15
            r"|\d{1,2}/\d{1,2}/\d{2,4}"  # US format: 01/15/2024
            r"|\d{1,2}-\d{1,2}-\d{2,4}"  # EU format: 15-01-2024
            r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}"  # Jan 15, 2024
            r"|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4}"  # 15 January 2024
            r")\b",
            re.IGNORECASE,
        ),
        # Numbers with units or currency
        "NUMBER": re.compile(
            r"(?:"
            r"\$[\d,]+(?:\.\d+)?"  # Currency: $1,234 or $1,234.56
            r"|[\d,]+(?:\.\d+)?\s*(?:million|billion|trillion|k|m|b|%|percent)"  # Numbers with units
            r")",
            re.IGNORECASE,
        ),
        # Email addresses
        "EMAIL": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"),
        # URLs
        "URL": re.compile(r"https?://[^\s<>\"{}|\\^`\[\]]+"),
    }

    # Patterns for proper nouns (used for PERSON, ORG, LOCATION detection)
    # Matches: "John Smith", "Apple Inc", "Google LLC", "New York City"
    # Each word starts with uppercase, followed by lowercase OR all uppercase (for suffixes like LLC, Inc)
    PROPER_NOUN_PATTERN = re.compile(r"\b[A-Z][a-z]+(?:\s+(?:[A-Z][a-z]+|[A-Z]{2,}))*\b")

    # Common organization suffixes
    ORG_SUFFIXES = frozenset(
        [
            "Inc",
            "Corp",
            "Corporation",
            "LLC",
            "Ltd",
            "Company",
            "Co",
            "Group",
            "Holdings",
            "International",
            "Foundation",
            "Institute",
            "University",
            "College",
            "Bank",
            "Hospital",
            "Association",
        ]
    )

    # Common location indicators
    LOCATION_INDICATORS = frozenset(
        [
            "City",
            "County",
            "State",
            "Country",
            "Street",
            "Avenue",
            "Road",
            "Boulevard",
            "Drive",
            "Place",
            "Park",
            "River",
            "Lake",
            "Mountain",
            "Island",
            "Beach",
            "Valley",
            "Bay",
            "Ocean",
            "Sea",
        ]
    )

    # Common person name patterns (titles, suffixes)
    PERSON_INDICATORS = frozenset(
        [
            "Mr",
            "Mrs",
            "Ms",
            "Dr",
            "Prof",
            "Jr",
            "Sr",
            "II",
            "III",
        ]
    )

    def __init__(self, use_spacy: bool = False, spacy_model: str = "en_core_web_sm"):
        """Initialize entity extractor.

        Args:
            use_spacy: Whether to use spaCy for NER (requires spacy package).
            spacy_model: spaCy model to load (default: "en_core_web_sm").
        """
        self.use_spacy = use_spacy
        self.nlp = None

        if use_spacy:
            try:
                import spacy

                self.nlp = spacy.load(spacy_model)
            except ImportError:
                # spaCy not available, fall back to regex
                self.use_spacy = False
                self.nlp = None
            except OSError:
                # Model not downloaded, fall back to regex
                self.use_spacy = False
                self.nlp = None

    def extract(self, text: str) -> list[ExtractedEntity]:
        """Extract named entities from text.

        Args:
            text: Input text to extract entities from.

        Returns:
            List of extracted entities with position information.
        """
        if self.nlp is not None:
            return self._spacy_extract(text)
        return self._regex_extract(text)

    def extract_as_dicts(self, text: str) -> list[dict[str, str | int]]:
        """Extract entities and return as list of dictionaries.

        Convenience method for JSON serialization.

        Args:
            text: Input text to extract entities from.

        Returns:
            List of entity dictionaries.
        """
        return [e.to_dict() for e in self.extract(text)]

    def get_entity_types(self, text: str) -> set[str]:
        """Get unique entity types found in text.

        Args:
            text: Input text to analyze.

        Returns:
            Set of entity type strings (e.g., {"PERSON", "ORG", "DATE"}).
        """
        return {e.type for e in self.extract(text)}

    def get_entity_types_string(self, text: str) -> str:
        """Get comma-separated string of unique entity types.

        Args:
            text: Input text to analyze.

        Returns:
            Comma-separated entity types (e.g., "PERSON,ORG,DATE").
        """
        types = sorted(self.get_entity_types(text))
        return ",".join(types)

    def get_person_refs(self, text: str) -> list[str]:
        """Get list of person references found in text.

        Args:
            text: Input text to analyze.

        Returns:
            List of person names for quick filtering.
        """
        return [e.text for e in self.extract(text) if e.type == "PERSON"]

    def get_person_refs_string(self, text: str) -> str:
        """Get comma-separated string of person references.

        Args:
            text: Input text to analyze.

        Returns:
            Comma-separated person names (e.g., "John Smith,Jane Doe").
        """
        return ",".join(self.get_person_refs(text))

    def _spacy_extract(self, text: str) -> list[ExtractedEntity]:
        """Extract entities using spaCy NER.

        Args:
            text: Input text.

        Returns:
            List of extracted entities.
        """
        assert self.nlp is not None
        doc = self.nlp(text)

        entities = []
        for ent in doc.ents:
            # Map spaCy labels to our entity types
            entity_type = self._map_spacy_label(ent.label_)
            entities.append(
                ExtractedEntity(
                    text=ent.text,
                    type=entity_type,
                    start=ent.start_char,
                    end=ent.end_char,
                )
            )

        return entities

    def _map_spacy_label(self, label: str) -> EntityType:
        """Map spaCy entity label to our entity type.

        Args:
            label: spaCy entity label.

        Returns:
            Mapped entity type.
        """
        label_map: dict[str, EntityType] = {
            "PERSON": "PERSON",
            "PER": "PERSON",
            "ORG": "ORG",
            "GPE": "LOCATION",
            "LOC": "LOCATION",
            "FAC": "LOCATION",
            "DATE": "DATE",
            "TIME": "DATE",
            "MONEY": "NUMBER",
            "PERCENT": "NUMBER",
            "QUANTITY": "NUMBER",
            "CARDINAL": "NUMBER",
            "ORDINAL": "NUMBER",
        }
        return label_map.get(label, "ENTITY")

    def _regex_extract(self, text: str) -> list[ExtractedEntity]:
        """Extract entities using regex patterns.

        Args:
            text: Input text.

        Returns:
            List of extracted entities.
        """
        entities: list[ExtractedEntity] = []
        seen_spans: set[tuple[int, int]] = set()

        # Extract typed entities first (dates, numbers, emails, URLs)
        for entity_type, pattern in self.ENTITY_PATTERNS.items():
            for match in pattern.finditer(text):
                span = (match.start(), match.end())
                if span not in seen_spans:
                    seen_spans.add(span)
                    entities.append(
                        ExtractedEntity(
                            text=match.group(),
                            type=entity_type,
                            start=match.start(),
                            end=match.end(),
                        )
                    )

        # Extract proper nouns and classify them
        for match in self.PROPER_NOUN_PATTERN.finditer(text):
            span = (match.start(), match.end())
            if span in seen_spans:
                continue

            # Check if this span overlaps with any existing entity
            overlaps = False
            for existing_span in seen_spans:
                if span[0] < existing_span[1] and span[1] > existing_span[0]:
                    overlaps = True
                    break

            if overlaps:
                continue

            entity_text = match.group()
            entity_type = self._classify_proper_noun(entity_text, text, match.start())

            seen_spans.add(span)
            entities.append(
                ExtractedEntity(
                    text=entity_text,
                    type=entity_type,
                    start=match.start(),
                    end=match.end(),
                )
            )

        # Sort by position
        entities.sort(key=lambda e: e.start)
        return entities

    def _classify_proper_noun(self, text: str, context: str, position: int) -> EntityType:
        """Classify a proper noun as PERSON, ORG, LOCATION, or generic ENTITY.

        Uses context clues and pattern matching for classification.

        Args:
            text: The proper noun text.
            context: Full text context.
            position: Position in context.

        Returns:
            Classified entity type.
        """
        words = text.split()

        # Check for organization indicators
        for word in words:
            if word in self.ORG_SUFFIXES:
                return "ORG"

        # Check for location indicators
        for word in words:
            if word in self.LOCATION_INDICATORS:
                return "LOCATION"

        # Check for person indicators (titles before the name)
        prefix_start = max(0, position - 10)
        prefix_text = context[prefix_start:position].strip()
        for indicator in self.PERSON_INDICATORS:
            if prefix_text.endswith(indicator) or prefix_text.endswith(indicator + "."):
                return "PERSON"

        # Heuristic: 2-3 word proper nouns are often person names
        # Check if all words look like name parts (not too long, not all caps)
        if (len(words) == 2 or len(words) == 3) and all(
            len(w) < 15 and not w.isupper() for w in words
        ):
            return "PERSON"

        # Default to generic entity
        return "ENTITY"


# Module-level convenience functions
_default_extractor: EntityExtractor | None = None


def get_default_extractor() -> EntityExtractor:
    """Get or create the default entity extractor.

    Returns:
        Default EntityExtractor instance (regex-based).
    """
    global _default_extractor
    if _default_extractor is None:
        _default_extractor = EntityExtractor(use_spacy=False)
    return _default_extractor


def extract_entities(text: str) -> list[ExtractedEntity]:
    """Extract entities from text using default extractor.

    Args:
        text: Input text.

    Returns:
        List of extracted entities.
    """
    return get_default_extractor().extract(text)


def extract_entities_as_dicts(text: str) -> list[dict[str, str | int]]:
    """Extract entities as dictionaries using default extractor.

    Args:
        text: Input text.

    Returns:
        List of entity dictionaries.
    """
    return get_default_extractor().extract_as_dicts(text)
