"""Coreference resolution for memory ingestion (Issue #1027).

Implements write-time disambiguation inspired by SimpleMem to replace
pronouns with entity names, making memories context-independent.

Uses LLM-based resolution as the primary approach (most accurate),
with a simple heuristic fallback when no LLM is available.

References:
- SimpleMem Paper: https://arxiv.org/abs/2601.02553
- LREC-COLING 2024: Few-shot + CoT prompting best practices

Example:
    "He went to the store" -> "John Smith went to the store"
    "She called him yesterday" -> "Alice called John yesterday"
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CorefResult:
    """Result of coreference resolution."""

    resolved_text: str
    original_text: str
    replacements: list[dict[str, Any]] = field(default_factory=list)
    method: str = "none"  # "llm", "heuristic", "none"


class CorefResolver(ABC):
    """Abstract base class for coreference resolvers."""

    @abstractmethod
    def resolve(
        self,
        text: str,
        context: str | None = None,
        entity_hints: dict[str, str] | None = None,
    ) -> CorefResult:
        """Resolve coreferences in text.

        Args:
            text: Text to resolve pronouns in.
            context: Optional prior conversation context (recommended: 2-3 preceding turns).
            entity_hints: Optional dict mapping roles to names,
                         e.g., {"speaker": "Alice", "other": "Bob"}

        Returns:
            CorefResult with resolved text and replacement details.
        """
        pass


class LLMCorefResolver(CorefResolver):
    """LLM-based coreference resolver (recommended approach).

    Uses a language model with few-shot + Chain of Thought prompting
    for accurate pronoun resolution. This matches SimpleMem's approach.

    Examples:
        >>> resolver = LLMCorefResolver(llm_provider)
        >>> result = resolver.resolve(
        ...     "He went to the store.",
        ...     context="John Smith was hungry. He looked in the fridge."
        ... )
        >>> print(result.resolved_text)
        "John Smith went to the store."
    """

    # Few-shot + CoT prompt based on research best practices
    RESOLUTION_PROMPT = """You are a coreference resolution system. Your task is to replace all pronouns with the specific entity names they refer to, making the text self-contained and unambiguous.

## Instructions
1. Identify all pronouns (he, she, him, her, his, hers, they, them, their, it, its, etc.)
2. For each pronoun, determine which entity it refers to from the context
3. Replace the pronoun with the entity name
4. If a pronoun cannot be resolved confidently, leave it unchanged
5. Preserve the original meaning and grammar

## Examples

### Example 1
Context: "John Smith met with Sarah at the cafe."
Text: "He ordered coffee and she had tea."
Reasoning: "He" refers to John Smith (male subject from context). "She" refers to Sarah (female subject from context).
Resolved: "John Smith ordered coffee and Sarah had tea."

### Example 2
Context: "Alice is the project manager. Bob is the developer."
Text: "She reviewed his code and approved the changes."
Reasoning: "She" refers to Alice (female, project manager role fits reviewing). "His" refers to Bob (male, developer role fits having code reviewed).
Resolved: "Alice reviewed Bob's code and approved the changes."

### Example 3
Context: "The team discussed the new feature."
Text: "They decided to implement it next sprint."
Reasoning: "They" refers to "the team" (plural subject). "It" refers to "the new feature" (thing being discussed).
Resolved: "The team decided to implement the new feature next sprint."

## Your Task

Context: {context}

Text to resolve: {text}

First, identify each pronoun and reason about what it refers to.
Then provide ONLY the resolved text with pronouns replaced.

Reasoning:"""

    # Simpler prompt for when context is minimal
    SIMPLE_PROMPT = """Replace all pronouns in the text with the entity names they refer to.

Known entities: {entities}

Text: {text}

Return ONLY the resolved text with pronouns replaced by names. If a pronoun cannot be resolved, leave it unchanged.

Resolved text:"""

    def __init__(self, llm_provider: Any = None):
        """Initialize LLM resolver.

        Args:
            llm_provider: LLM provider instance with complete() or acomplete() method.
                         If None, will attempt to use default provider.
        """
        self.llm_provider = llm_provider

    def resolve(
        self,
        text: str,
        context: str | None = None,
        entity_hints: dict[str, str] | None = None,
    ) -> CorefResult:
        """Resolve coreferences using LLM.

        Args:
            text: Text to resolve.
            context: Prior conversation context (2-3 turns recommended).
            entity_hints: Entity hints like {"speaker": "Alice", "other": "Bob"}.

        Returns:
            CorefResult with resolved text.
        """
        from nexus.core.sync_bridge import run_sync

        return run_sync(self.resolve_async(text, context, entity_hints))

    async def resolve_async(
        self,
        text: str,
        context: str | None = None,
        entity_hints: dict[str, str] | None = None,
    ) -> CorefResult:
        """Resolve coreferences using LLM (async version).

        Args:
            text: Text to resolve.
            context: Prior conversation context.
            entity_hints: Entity hints to add to context.

        Returns:
            CorefResult with resolved text.
        """
        # Check if text has pronouns that need resolution
        if not self._has_pronouns(text):
            return CorefResult(
                resolved_text=text,
                original_text=text,
                replacements=[],
                method="none",
            )

        # Get or create LLM provider
        provider = self.llm_provider
        if provider is None:
            provider = self._get_default_provider()

        if provider is None:
            # Fall back to heuristic if no LLM available
            fallback = HeuristicCorefResolver()
            result = fallback.resolve(text, context, entity_hints)
            result.method = "heuristic"
            return result

        # Build context
        full_context = self._build_context(context, entity_hints)

        # Choose prompt based on context richness
        if full_context and len(full_context) > 50:
            prompt = self.RESOLUTION_PROMPT.format(
                context=full_context,
                text=text,
            )
        else:
            entities = self._format_entities(entity_hints) if entity_hints else "Unknown"
            prompt = self.SIMPLE_PROMPT.format(
                entities=entities,
                text=text,
            )

        try:
            # Call LLM - handle different provider interfaces
            # Some providers accept raw string prompts, others require Message objects
            if hasattr(provider, "complete_async"):
                # LiteLLMProvider from nexus.llm - requires Message objects
                from nexus.llm import Message, MessageRole

                messages = [Message(role=MessageRole.USER, content=prompt)]
                response = await provider.complete_async(messages)
            elif hasattr(provider, "acomplete"):
                # Generic async provider - try with raw prompt
                response = await provider.acomplete(prompt)
            elif hasattr(provider, "complete"):
                # Check if it's a LiteLLMProvider
                if hasattr(provider, "config"):
                    from nexus.llm import Message, MessageRole

                    messages = [Message(role=MessageRole.USER, content=prompt)]
                    response = provider.complete(messages)
                else:
                    response = provider.complete(prompt)
            else:
                raise ValueError("LLM provider must have complete() or complete_async() method")

            # Extract resolved text from response
            resolved_text = self._extract_resolved_text(response, text)

            # Detect what was replaced
            replacements = self._detect_replacements(text, resolved_text)

            return CorefResult(
                resolved_text=resolved_text,
                original_text=text,
                replacements=replacements,
                method="llm",
            )

        except Exception as e:
            # Log error and fall back to heuristic
            import logging

            logging.getLogger(__name__).warning(
                f"LLM coreference resolution failed: {e}, falling back to heuristic"
            )
            fallback = HeuristicCorefResolver()
            result = fallback.resolve(text, context, entity_hints)
            result.method = "heuristic"
            return result

    def _has_pronouns(self, text: str) -> bool:
        """Check if text contains pronouns that might need resolution."""
        pronouns = {
            "he",
            "him",
            "his",
            "himself",
            "she",
            "her",
            "hers",
            "herself",
            "they",
            "them",
            "their",
            "theirs",
            "themselves",
            "it",
            "its",
            "itself",
        }
        words = set(re.findall(r"\b\w+\b", text.lower()))
        return bool(words & pronouns)

    def _build_context(self, context: str | None, entity_hints: dict[str, str] | None) -> str:
        """Build full context string from context and hints."""
        parts = []

        if entity_hints:
            hint_strs = [f"{role}: {name}" for role, name in entity_hints.items()]
            parts.append("Known entities - " + ", ".join(hint_strs))

        if context:
            parts.append(context)

        return " ".join(parts) if parts else ""

    def _format_entities(self, entity_hints: dict[str, str]) -> str:
        """Format entity hints for prompt."""
        return ", ".join(f"{name} ({role})" for role, name in entity_hints.items())

    def _extract_resolved_text(self, response: Any, original: str) -> str:
        """Extract resolved text from LLM response."""
        # Handle different response types
        text: str
        if hasattr(response, "content"):
            text = str(response.content)
        elif hasattr(response, "text"):
            text = str(response.text)
        elif isinstance(response, str):
            text = response
        else:
            text = str(response)

        text = text.strip()

        # If using the detailed prompt, extract after "Resolved:" or similar markers
        markers = ["Resolved:", "Resolved text:", "Output:", "Result:"]
        for marker in markers:
            if marker.lower() in text.lower():
                idx = text.lower().rfind(marker.lower())
                text = text[idx + len(marker) :].strip()
                break

        # Clean up any quotes
        if text.startswith('"') and text.endswith('"'):
            text = text[1:-1]
        if text.startswith("'") and text.endswith("'"):
            text = text[1:-1]

        # If the response seems malformed, return original
        if len(text) < len(original) * 0.5 or len(text) > len(original) * 3:
            return original

        return text

    def _detect_replacements(self, original: str, resolved: str) -> list[dict[str, Any]]:
        """Detect what pronouns were replaced."""
        replacements = []

        pronouns = re.findall(
            r"\b(he|him|his|himself|she|her|hers|herself|they|them|their|theirs|themselves|it|its|itself)\b",
            original,
            re.IGNORECASE,
        )

        for pronoun in pronouns:
            # Check if this pronoun is still in resolved text at same relative position
            if pronoun.lower() not in resolved.lower():
                replacements.append(
                    {
                        "pronoun": pronoun,
                        "resolved": True,
                    }
                )

        return replacements

    def _get_default_provider(self) -> Any:
        """Try to get a default LLM provider."""
        import os

        try:
            from pydantic import SecretStr

            from nexus.llm import LiteLLMProvider, LLMConfig

            # Check for common API keys in environment
            if os.environ.get("ANTHROPIC_API_KEY"):
                config = LLMConfig(
                    model="claude-sonnet-4-20250514",
                    api_key=SecretStr(os.environ["ANTHROPIC_API_KEY"]),
                    max_output_tokens=1024,
                )
                return LiteLLMProvider(config)
            elif os.environ.get("OPENAI_API_KEY"):
                config = LLMConfig(
                    model="gpt-4o-mini",
                    api_key=SecretStr(os.environ["OPENAI_API_KEY"]),
                    max_output_tokens=1024,
                )
                return LiteLLMProvider(config)
        except Exception:
            pass

        return None


class HeuristicCorefResolver(CorefResolver):
    """Simple heuristic-based coreference resolver (fallback only).

    Uses basic rules to replace pronouns when no LLM is available.
    Less accurate than LLM-based resolution but works offline.

    Note: This should only be used as a fallback. SimpleMem research shows
    removing proper coreference resolution drops F1 by 56.7%.
    """

    MALE_PRONOUNS = {"he", "him", "his", "himself"}
    FEMALE_PRONOUNS = {"she", "her", "hers", "herself"}
    NEUTRAL_PRONOUNS = {"they", "them", "their", "theirs", "themselves"}

    PRONOUN_REPLACEMENTS = {
        "he": "{name}",
        "him": "{name}",
        "his": "{name}'s",
        "himself": "{name}",
        "she": "{name}",
        "her": "{name}",
        "hers": "{name}'s",
        "herself": "{name}",
        "they": "{name}",
        "them": "{name}",
        "their": "{name}'s",
        "theirs": "{name}'s",
        "themselves": "{name}",
    }

    def resolve(
        self,
        text: str,
        context: str | None = None,
        entity_hints: dict[str, str] | None = None,
    ) -> CorefResult:
        """Resolve coreferences using simple heuristics.

        Args:
            text: Text to resolve.
            context: Prior context (used to extract names).
            entity_hints: Hints like {"male": "John", "female": "Alice"}.

        Returns:
            CorefResult with resolved text.
        """
        # Extract entities from context and hints
        entities = self._extract_entities(context, entity_hints)

        if not entities:
            return CorefResult(
                resolved_text=text,
                original_text=text,
                replacements=[],
                method="heuristic",
            )

        resolved_text = text
        replacements = []
        offset = 0

        # Find and replace pronouns
        pattern = re.compile(
            r"\b(" + "|".join(self.PRONOUN_REPLACEMENTS.keys()) + r")\b",
            re.IGNORECASE,
        )

        for match in pattern.finditer(text):
            pronoun = match.group(1).lower()
            original = match.group(1)

            # Determine which entity to use
            entity = self._find_matching_entity(pronoun, entities)
            if not entity:
                continue

            # Get replacement pattern
            replacement_pattern = self.PRONOUN_REPLACEMENTS.get(pronoun, "{name}")
            replacement = replacement_pattern.format(name=entity)

            # Preserve capitalization for start of sentence
            # Note: Proper nouns remain capitalized regardless of original pronoun case
            if original[0].isupper():
                replacement = replacement[0].upper() + replacement[1:]

            # Apply replacement
            start = match.start() + offset
            end = match.end() + offset
            resolved_text = resolved_text[:start] + replacement + resolved_text[end:]
            offset += len(replacement) - len(original)

            replacements.append(
                {
                    "pronoun": original,
                    "referent": entity,
                    "position": match.start(),
                }
            )

        return CorefResult(
            resolved_text=resolved_text,
            original_text=text,
            replacements=replacements,
            method="heuristic",
        )

    def _extract_entities(
        self, context: str | None, hints: dict[str, str] | None
    ) -> dict[str, str]:
        """Extract entities organized by gender/type."""
        entities: dict[str, str] = {}

        # Add hints directly
        if hints:
            for key, name in hints.items():
                if key in ("male", "female", "neutral", "speaker", "other"):
                    entities[key] = name

        # Extract names from context
        if context:
            # Find proper nouns (capitalized word sequences)
            names = re.findall(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b", context)
            for name in names:
                gender = self._guess_gender(name)
                if gender and gender not in entities:
                    entities[gender] = name

        return entities

    def _guess_gender(self, name: str) -> str | None:
        """Guess gender from first name (very basic)."""
        first = name.split()[0].lower()

        male_names = {
            "john",
            "james",
            "robert",
            "michael",
            "david",
            "william",
            "richard",
            "joseph",
            "thomas",
            "charles",
            "daniel",
            "matthew",
            "bob",
            "tom",
            "jim",
            "mike",
            "dave",
            "bill",
            "joe",
            "dan",
        }
        female_names = {
            "mary",
            "patricia",
            "jennifer",
            "linda",
            "elizabeth",
            "barbara",
            "susan",
            "jessica",
            "sarah",
            "karen",
            "lisa",
            "nancy",
            "betty",
            "alice",
            "jane",
            "kate",
            "emma",
            "olivia",
            "sophia",
            "anna",
        }

        if first in male_names:
            return "male"
        if first in female_names:
            return "female"
        return None

    def _find_matching_entity(self, pronoun: str, entities: dict[str, str]) -> str | None:
        """Find entity matching the pronoun's gender."""
        if pronoun in self.MALE_PRONOUNS:
            return entities.get("male") or entities.get("speaker")
        if pronoun in self.FEMALE_PRONOUNS:
            return entities.get("female") or entities.get("other")
        if pronoun in self.NEUTRAL_PRONOUNS:
            # For they/them, use any available entity
            return (
                entities.get("neutral")
                or entities.get("speaker")
                or entities.get("other")
                or next(iter(entities.values()), None)
            )
        return None


# Convenience functions

_default_resolver: CorefResolver | None = None


def get_resolver(llm_provider: Any = None) -> CorefResolver:
    """Get a coreference resolver.

    Args:
        llm_provider: Optional LLM provider. If provided, returns LLM resolver.
                     If None, returns cached default resolver.

    Returns:
        CorefResolver instance.
    """
    if llm_provider is not None:
        return LLMCorefResolver(llm_provider)

    global _default_resolver
    if _default_resolver is None:
        # Try to create LLM resolver with default provider
        _default_resolver = LLMCorefResolver()
    return _default_resolver


def resolve_coreferences(
    text: str,
    context: str | None = None,
    entity_hints: dict[str, str] | None = None,
    llm_provider: Any = None,
) -> str:
    """Resolve coreferences in text.

    Args:
        text: Text with pronouns to resolve.
        context: Optional prior context (2-3 turns recommended).
        entity_hints: Optional entity hints.
        llm_provider: Optional LLM provider.

    Returns:
        Text with pronouns replaced by entity names.
    """
    resolver = get_resolver(llm_provider)
    result = resolver.resolve(text, context, entity_hints)
    return result.resolved_text
