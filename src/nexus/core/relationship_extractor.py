"""Relationship extraction for memory ingestion (Issue #1038).

Implements LLM-based relationship extraction inspired by LightRAG/GraphRAG
for graph-based retrieval and multi-hop reasoning.

Extracts (subject, predicate, object) triplets with confidence scores.

References:
- LightRAG Paper: https://arxiv.org/abs/2410.05779
- SimpleMem Paper: https://arxiv.org/abs/2601.02553
- Issue #1038: LLM-based relationship extraction at ingestion

Example:
    "Alice manages the frontend team" -> (Alice, MANAGES, frontend_team, 0.95)
    "Task X blocks Task Y" -> (Task X, BLOCKS, Task Y, 0.90)
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

# Relationship type definitions
RelationshipType = Literal[
    "WORKS_WITH",
    "MANAGES",
    "REPORTS_TO",
    "CREATES",
    "MODIFIES",
    "OWNS",
    "DEPENDS_ON",
    "BLOCKS",
    "RELATES_TO",
    "MENTIONS",
    "REFERENCES",
    "LOCATED_IN",
    "PART_OF",
    "HAS",
    "USES",
    "OTHER",
]

# Default relationship types
DEFAULT_RELATIONSHIP_TYPES: list[str] = [
    "WORKS_WITH",
    "MANAGES",
    "REPORTS_TO",
    "CREATES",
    "MODIFIES",
    "OWNS",
    "DEPENDS_ON",
    "BLOCKS",
    "RELATES_TO",
    "MENTIONS",
    "REFERENCES",
    "LOCATED_IN",
    "PART_OF",
    "HAS",
    "USES",
]


@dataclass
class ExtractedRelationship:
    """Extracted relationship triplet with metadata."""

    subject: str
    predicate: str
    object: str
    confidence: float = 1.0
    source_text: str | None = None  # Original text that supports this relationship

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "subject": self.subject,
            "predicate": self.predicate,
            "object": self.object,
            "confidence": self.confidence,
            "source_text": self.source_text,
        }


@dataclass
class RelationshipExtractionResult:
    """Result of relationship extraction."""

    relationships: list[ExtractedRelationship] = field(default_factory=list)
    method: str = "none"  # "llm", "heuristic", "none"
    model_used: str | None = None
    tokens_used: int = 0

    def to_dicts(self) -> list[dict[str, Any]]:
        """Convert relationships to list of dictionaries."""
        return [r.to_dict() for r in self.relationships]


class RelationshipExtractor(ABC):
    """Abstract base class for relationship extractors."""

    @abstractmethod
    def extract(
        self,
        text: str,
        entities: list[dict[str, Any]] | None = None,
        relationship_types: list[str] | None = None,
    ) -> RelationshipExtractionResult:
        """Extract relationships from text.

        Args:
            text: Text to extract relationships from.
            entities: Optional pre-extracted entities from EntityExtractor.
            relationship_types: Optional list of relationship types to extract.
                              Defaults to DEFAULT_RELATIONSHIP_TYPES.

        Returns:
            RelationshipExtractionResult with extracted relationships.
        """
        pass

    @abstractmethod
    async def extract_async(
        self,
        text: str,
        entities: list[dict[str, Any]] | None = None,
        relationship_types: list[str] | None = None,
    ) -> RelationshipExtractionResult:
        """Extract relationships from text (async version).

        Args:
            text: Text to extract relationships from.
            entities: Optional pre-extracted entities.
            relationship_types: Optional relationship types to extract.

        Returns:
            RelationshipExtractionResult with extracted relationships.
        """
        pass


class LLMRelationshipExtractor(RelationshipExtractor):
    """LLM-based relationship extractor (recommended approach).

    Uses a language model with structured JSON output for accurate
    relationship extraction. Supports OpenRouter models for cost-effective
    extraction.

    Examples:
        >>> extractor = LLMRelationshipExtractor(llm_provider)
        >>> result = extractor.extract(
        ...     "Alice manages the frontend team and works with Bob."
        ... )
        >>> for rel in result.relationships:
        ...     print(f"({rel.subject}, {rel.predicate}, {rel.object})")
        (Alice, MANAGES, frontend team)
        (Alice, WORKS_WITH, Bob)
    """

    # Extraction prompt with few-shot examples
    EXTRACTION_PROMPT = """You are a relationship extraction system. Extract relationships between entities from the given text.

## Instructions
1. Identify all entities (people, organizations, things, concepts)
2. Find explicit relationships between entities
3. Return relationships as JSON triplets
4. Only extract relationships that are explicitly stated, not inferred
5. Assign confidence scores based on how clear the relationship is

## Relationship Types
{relationship_types}

## Output Format
Return ONLY valid JSON in this exact format (no markdown, no explanation):
{{"relationships": [
  {{"subject": "entity1", "predicate": "RELATIONSHIP_TYPE", "object": "entity2", "confidence": 0.95}},
  ...
]}}

## Examples

Text: "John Smith manages the engineering team at Google."
Output: {{"relationships": [
  {{"subject": "John Smith", "predicate": "MANAGES", "object": "engineering team", "confidence": 0.95}},
  {{"subject": "John Smith", "predicate": "WORKS_WITH", "object": "Google", "confidence": 0.85}}
]}}

Text: "The API depends on the database service, which was created by Alice."
Output: {{"relationships": [
  {{"subject": "API", "predicate": "DEPENDS_ON", "object": "database service", "confidence": 0.95}},
  {{"subject": "Alice", "predicate": "CREATES", "object": "database service", "confidence": 0.90}}
]}}

Text: "Task A blocks Task B. Bob owns Task A."
Output: {{"relationships": [
  {{"subject": "Task A", "predicate": "BLOCKS", "object": "Task B", "confidence": 0.95}},
  {{"subject": "Bob", "predicate": "OWNS", "object": "Task A", "confidence": 0.90}}
]}}

## Your Task

{entity_context}

Text to analyze:
{text}

Output:"""

    # Gleaning prompt for retry (LightRAG pattern)
    GLEANING_PROMPT = """Some relationships may have been missed. Review the text again and extract any additional relationships not already found.

Already extracted: {existing_relationships}

Text: {text}

Return ONLY new relationships not in the existing list, in JSON format:
{{"relationships": [...]}}

If no additional relationships are found, return: {{"relationships": []}}

Output:"""

    def __init__(
        self,
        llm_provider: Any = None,
        model: str | None = None,
        confidence_threshold: float = 0.5,
        enable_gleaning: bool = False,
        max_gleaning_retries: int = 1,
    ):
        """Initialize LLM relationship extractor.

        Args:
            llm_provider: LLM provider instance with complete_async() method.
                         If None, will attempt to use default provider.
            model: Model name to use (e.g., "google/gemini-2.5-flash").
                  If None, uses provider's default.
            confidence_threshold: Minimum confidence to include relationships.
            enable_gleaning: Enable retry mechanism to catch missed relationships.
            max_gleaning_retries: Maximum gleaning retries (default: 1).
        """
        self.llm_provider = llm_provider
        self.model = model
        self.confidence_threshold = confidence_threshold
        self.enable_gleaning = enable_gleaning
        self.max_gleaning_retries = max_gleaning_retries

    def extract(
        self,
        text: str,
        entities: list[dict[str, Any]] | None = None,
        relationship_types: list[str] | None = None,
    ) -> RelationshipExtractionResult:
        """Extract relationships using LLM (sync version).

        Args:
            text: Text to extract relationships from.
            entities: Optional pre-extracted entities.
            relationship_types: Optional relationship types to extract.

        Returns:
            RelationshipExtractionResult with extracted relationships.
        """
        import asyncio

        try:
            asyncio.get_running_loop()
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(
                    asyncio.run,
                    self.extract_async(text, entities, relationship_types),
                )
                return future.result()
        except RuntimeError:
            return asyncio.run(self.extract_async(text, entities, relationship_types))

    async def extract_async(
        self,
        text: str,
        entities: list[dict[str, Any]] | None = None,
        relationship_types: list[str] | None = None,
    ) -> RelationshipExtractionResult:
        """Extract relationships using LLM (async version).

        Args:
            text: Text to extract relationships from.
            entities: Optional pre-extracted entities.
            relationship_types: Optional relationship types to extract.

        Returns:
            RelationshipExtractionResult with extracted relationships.
        """
        # Skip if text is too short
        if not text or len(text.strip()) < 10:
            return RelationshipExtractionResult(
                relationships=[],
                method="none",
            )

        # Get or create LLM provider
        provider = self.llm_provider
        if provider is None:
            provider = self._get_default_provider()

        if provider is None:
            # Fall back to heuristic if no LLM available
            fallback = HeuristicRelationshipExtractor()
            result = fallback.extract(text, entities, relationship_types)
            result.method = "heuristic"
            return result

        # Use default relationship types if not specified
        rel_types = relationship_types or DEFAULT_RELATIONSHIP_TYPES

        # Build entity context if entities provided
        entity_context = ""
        if entities:
            entity_list = ", ".join(
                f"{e.get('text', e.get('name', 'unknown'))} ({e.get('type', 'ENTITY')})"
                for e in entities
            )
            entity_context = f"Known entities: {entity_list}\n"

        # Build prompt
        prompt = self.EXTRACTION_PROMPT.format(
            relationship_types=", ".join(rel_types),
            entity_context=entity_context,
            text=text,
        )

        try:
            # Call LLM
            response = await self._call_llm(provider, prompt)
            relationships = self._parse_response(response, text)

            # Apply gleaning if enabled
            if self.enable_gleaning and self.max_gleaning_retries > 0:
                for _ in range(self.max_gleaning_retries):
                    additional = await self._glean_relationships(provider, text, relationships)
                    if not additional:
                        break
                    relationships.extend(additional)

            # Filter by confidence threshold
            filtered = [r for r in relationships if r.confidence >= self.confidence_threshold]

            return RelationshipExtractionResult(
                relationships=filtered,
                method="llm",
                model_used=self.model or getattr(provider, "config", {}).get("model"),
            )

        except Exception as e:
            import logging

            logging.getLogger(__name__).warning(
                f"LLM relationship extraction failed: {e}, falling back to heuristic"
            )
            fallback = HeuristicRelationshipExtractor()
            result = fallback.extract(text, entities, relationship_types)
            result.method = "heuristic"
            return result

    async def _call_llm(self, provider: Any, prompt: str) -> str:
        """Call LLM provider and get response."""
        if hasattr(provider, "complete_async"):
            from nexus.llm import Message, MessageRole

            messages = [Message(role=MessageRole.USER, content=prompt)]
            response = await provider.complete_async(messages)
            return str(response.content) if response.content else ""
        elif hasattr(provider, "acomplete"):
            response = await provider.acomplete(prompt)
            return str(response)
        elif hasattr(provider, "complete"):
            if hasattr(provider, "config"):
                from nexus.llm import Message, MessageRole

                messages = [Message(role=MessageRole.USER, content=prompt)]
                response = provider.complete(messages)
                return str(response.content) if response.content else ""
            else:
                response = provider.complete(prompt)
                return str(response)
        else:
            raise ValueError("LLM provider must have complete() or complete_async() method")

    def _parse_response(self, response: str, source_text: str) -> list[ExtractedRelationship]:
        """Parse LLM response into relationships."""
        relationships = []

        # Clean response - remove markdown code blocks if present
        response = response.strip()
        if response.startswith("```"):
            # Remove markdown code block
            lines = response.split("\n")
            # Find the JSON content between ``` markers
            json_lines = []
            in_block = False
            for line in lines:
                if line.startswith("```"):
                    in_block = not in_block
                    continue
                if in_block or not line.startswith("```"):
                    json_lines.append(line)
            response = "\n".join(json_lines).strip()

        try:
            data = json.loads(response)
            rel_list = data.get("relationships", [])

            for rel in rel_list:
                if not isinstance(rel, dict):
                    continue

                subject = rel.get("subject", "").strip()
                predicate = rel.get("predicate", "").strip().upper()
                obj = rel.get("object", "").strip()
                confidence = float(rel.get("confidence", 0.8))

                if subject and predicate and obj:
                    relationships.append(
                        ExtractedRelationship(
                            subject=subject,
                            predicate=predicate,
                            object=obj,
                            confidence=confidence,
                            source_text=source_text[:200] if source_text else None,
                        )
                    )

        except json.JSONDecodeError:
            # Try to extract JSON from response
            json_match = re.search(r"\{.*\}", response, re.DOTALL)
            if json_match:
                try:
                    data = json.loads(json_match.group())
                    return self._parse_response(json.dumps(data), source_text)
                except json.JSONDecodeError:
                    pass

        return relationships

    async def _glean_relationships(
        self,
        provider: Any,
        text: str,
        existing: list[ExtractedRelationship],
    ) -> list[ExtractedRelationship]:
        """Retry extraction to find missed relationships (gleaning)."""
        existing_str = json.dumps([r.to_dict() for r in existing])

        prompt = self.GLEANING_PROMPT.format(
            existing_relationships=existing_str,
            text=text,
        )

        try:
            response = await self._call_llm(provider, prompt)
            return self._parse_response(response, text)
        except Exception:
            return []

    def _get_default_provider(self) -> Any:
        """Try to get a default LLM provider."""
        import os

        try:
            from pydantic import SecretStr

            from nexus.llm import LiteLLMProvider, LLMConfig

            # Prefer OpenRouter for cost-effective extraction
            if os.environ.get("OPENROUTER_API_KEY"):
                model = self.model or "google/gemini-2.5-flash"
                config = LLMConfig(
                    model=model,
                    api_key=SecretStr(os.environ["OPENROUTER_API_KEY"]),
                    custom_llm_provider="openrouter",
                    max_output_tokens=2048,
                    temperature=0.1,  # Low temperature for consistent extraction
                )
                return LiteLLMProvider(config)
            # Fall back to other providers
            elif os.environ.get("ANTHROPIC_API_KEY"):
                config = LLMConfig(
                    model="claude-3-5-haiku-20241022",
                    api_key=SecretStr(os.environ["ANTHROPIC_API_KEY"]),
                    max_output_tokens=2048,
                    temperature=0.1,
                )
                return LiteLLMProvider(config)
            elif os.environ.get("OPENAI_API_KEY"):
                config = LLMConfig(
                    model="gpt-4o-mini",
                    api_key=SecretStr(os.environ["OPENAI_API_KEY"]),
                    max_output_tokens=2048,
                    temperature=0.1,
                )
                return LiteLLMProvider(config)
        except Exception:
            pass

        return None


class HeuristicRelationshipExtractor(RelationshipExtractor):
    """Simple heuristic-based relationship extractor (fallback only).

    Uses pattern matching to extract common relationships when no LLM
    is available. Less accurate than LLM-based extraction.
    """

    # Verb patterns that indicate relationships
    RELATIONSHIP_PATTERNS: dict[str, list[str]] = {
        "MANAGES": [r"\bmanages?\b", r"\bleads?\b", r"\bsupervises?\b", r"\bdirects?\b"],
        "WORKS_WITH": [r"\bworks?\s+with\b", r"\bcollaborates?\s+with\b", r"\bpartners?\s+with\b"],
        "REPORTS_TO": [r"\breports?\s+to\b", r"\banswers?\s+to\b"],
        "CREATES": [
            r"\bcreates?\b",
            r"\bmakes?\b",
            r"\bbuilds?\b",
            r"\bdevelops?\b",
            r"\bwrites?\b",
        ],
        "MODIFIES": [r"\bmodifies?\b", r"\bupdates?\b", r"\bchanges?\b", r"\bedits?\b"],
        "OWNS": [r"\bowns?\b", r"\bhas\b", r"\bpossesses?\b"],
        "DEPENDS_ON": [r"\bdepends?\s+on\b", r"\brequires?\b", r"\bneeds?\b", r"\brelies?\s+on\b"],
        "BLOCKS": [r"\bblocks?\b", r"\bprevents?\b", r"\bstops?\b"],
        "RELATES_TO": [r"\brelates?\s+to\b", r"\bconnects?\s+to\b", r"\blinks?\s+to\b"],
        "MENTIONS": [r"\bmentions?\b", r"\brefers?\s+to\b", r"\bcites?\b"],
        "LOCATED_IN": [r"\blocated\s+in\b", r"\bin\b", r"\bat\b"],
        "PART_OF": [r"\bpart\s+of\b", r"\bbelongs?\s+to\b", r"\bmember\s+of\b"],
        "USES": [r"\buses?\b", r"\butilizes?\b", r"\bemploys?\b"],
    }

    def __init__(self, confidence_threshold: float = 0.5):
        """Initialize heuristic extractor.

        Args:
            confidence_threshold: Minimum confidence to include relationships.
        """
        self.confidence_threshold = confidence_threshold
        # Compile patterns
        self._compiled_patterns: dict[str, list[re.Pattern[str]]] = {
            rel_type: [re.compile(p, re.IGNORECASE) for p in patterns]
            for rel_type, patterns in self.RELATIONSHIP_PATTERNS.items()
        }

    def extract(
        self,
        text: str,
        entities: list[dict[str, Any]] | None = None,
        relationship_types: list[str] | None = None,
    ) -> RelationshipExtractionResult:
        """Extract relationships using heuristics.

        Args:
            text: Text to extract relationships from.
            entities: Optional pre-extracted entities.
            relationship_types: Optional relationship types to extract.

        Returns:
            RelationshipExtractionResult with extracted relationships.
        """
        if not text or len(text.strip()) < 10:
            return RelationshipExtractionResult(
                relationships=[],
                method="heuristic",
            )

        # Get entity names for matching
        entity_names: set[str] = set()
        if entities:
            for e in entities:
                name = e.get("text") or e.get("name")
                if name:
                    entity_names.add(name.lower())

        relationships = []
        rel_types = relationship_types or list(self.RELATIONSHIP_PATTERNS.keys())

        # Split into sentences
        sentences = re.split(r"[.!?]+", text)

        for sentence in sentences:
            sentence = sentence.strip()
            if len(sentence) < 5:
                continue

            # Look for relationship patterns
            for rel_type in rel_types:
                if rel_type not in self._compiled_patterns:
                    continue

                for pattern in self._compiled_patterns[rel_type]:
                    match = pattern.search(sentence)
                    if match:
                        # Try to extract subject and object around the verb
                        subject, obj = self._extract_entities_around_verb(
                            sentence, match.start(), match.end(), entity_names
                        )

                        if subject and obj:
                            relationships.append(
                                ExtractedRelationship(
                                    subject=subject,
                                    predicate=rel_type,
                                    object=obj,
                                    confidence=0.6,  # Lower confidence for heuristic
                                    source_text=sentence[:200],
                                )
                            )
                            break  # Only one relationship per sentence per type

        # Filter by confidence
        filtered = [r for r in relationships if r.confidence >= self.confidence_threshold]

        return RelationshipExtractionResult(
            relationships=filtered,
            method="heuristic",
        )

    async def extract_async(
        self,
        text: str,
        entities: list[dict[str, Any]] | None = None,
        relationship_types: list[str] | None = None,
    ) -> RelationshipExtractionResult:
        """Extract relationships (async version - just calls sync)."""
        return self.extract(text, entities, relationship_types)

    def _extract_entities_around_verb(
        self,
        sentence: str,
        verb_start: int,
        verb_end: int,
        known_entities: set[str],
    ) -> tuple[str | None, str | None]:
        """Extract subject and object entities around a verb.

        Args:
            sentence: The sentence to analyze.
            verb_start: Start position of the verb.
            verb_end: End position of the verb.
            known_entities: Set of known entity names (lowercase).

        Returns:
            Tuple of (subject, object) or (None, None) if not found.
        """
        before = sentence[:verb_start].strip()
        after = sentence[verb_end:].strip()

        # Simple heuristic: last capitalized word(s) before verb = subject
        # First capitalized word(s) after verb = object
        subject = self._find_entity_in_text(before, known_entities, from_end=True)
        obj = self._find_entity_in_text(after, known_entities, from_end=False)

        return subject, obj

    def _find_entity_in_text(
        self,
        text: str,
        known_entities: set[str],
        from_end: bool = False,
    ) -> str | None:
        """Find an entity in text.

        Args:
            text: Text to search.
            known_entities: Known entity names.
            from_end: Search from end of text.

        Returns:
            Entity name or None.
        """
        # First check for known entities
        for entity in known_entities:
            if entity in text.lower():
                # Find the actual cased version
                idx = text.lower().find(entity)
                return text[idx : idx + len(entity)]

        # Fall back to proper noun patterns
        proper_nouns: list[str] = re.findall(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b", text)
        if proper_nouns:
            return str(proper_nouns[-1] if from_end else proper_nouns[0])

        # Fall back to quoted strings
        quoted: list[str] = re.findall(r'"([^"]+)"', text)
        if quoted:
            return str(quoted[-1] if from_end else quoted[0])

        return None


# Convenience functions

_default_extractor: LLMRelationshipExtractor | None = None


def get_extractor(
    llm_provider: Any = None,
    model: str | None = None,
) -> RelationshipExtractor:
    """Get a relationship extractor.

    Args:
        llm_provider: Optional LLM provider. If provided, returns LLM extractor.
        model: Optional model name for OpenRouter.

    Returns:
        RelationshipExtractor instance.
    """
    if llm_provider is not None:
        return LLMRelationshipExtractor(llm_provider, model)

    global _default_extractor
    if _default_extractor is None:
        _default_extractor = LLMRelationshipExtractor(model=model)
    return _default_extractor


def extract_relationships(
    text: str,
    entities: list[dict[str, Any]] | None = None,
    relationship_types: list[str] | None = None,
    llm_provider: Any = None,
) -> list[ExtractedRelationship]:
    """Extract relationships from text.

    Args:
        text: Text to extract relationships from.
        entities: Optional pre-extracted entities.
        relationship_types: Optional relationship types to extract.
        llm_provider: Optional LLM provider.

    Returns:
        List of extracted relationships.
    """
    extractor = get_extractor(llm_provider)
    result = extractor.extract(text, entities, relationship_types)
    return result.relationships


def extract_relationships_as_dicts(
    text: str,
    entities: list[dict[str, Any]] | None = None,
    relationship_types: list[str] | None = None,
    llm_provider: Any = None,
) -> list[dict[str, Any]]:
    """Extract relationships and return as list of dictionaries.

    Args:
        text: Text to extract relationships from.
        entities: Optional pre-extracted entities.
        relationship_types: Optional relationship types to extract.
        llm_provider: Optional LLM provider.

    Returns:
        List of relationship dictionaries.
    """
    relationships = extract_relationships(text, entities, relationship_types, llm_provider)
    return [r.to_dict() for r in relationships]


async def extract_relationships_async(
    text: str,
    entities: list[dict[str, Any]] | None = None,
    relationship_types: list[str] | None = None,
    llm_provider: Any = None,
) -> list[ExtractedRelationship]:
    """Extract relationships from text (async).

    Args:
        text: Text to extract relationships from.
        entities: Optional pre-extracted entities.
        relationship_types: Optional relationship types to extract.
        llm_provider: Optional LLM provider.

    Returns:
        List of extracted relationships.
    """
    extractor = get_extractor(llm_provider)
    result = await extractor.extract_async(text, entities, relationship_types)
    return result.relationships
