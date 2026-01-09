"""Tests for coreference resolution (Issue #1027)."""

import pytest

from nexus.core.coref_resolver import (
    CorefResult,
    HeuristicCorefResolver,
    LLMCorefResolver,
    get_resolver,
    resolve_coreferences,
)


class TestCorefResult:
    """Tests for CorefResult dataclass."""

    def test_coref_result_basic(self):
        """Test basic CorefResult creation."""
        result = CorefResult(
            resolved_text="John went to the store.",
            original_text="He went to the store.",
            replacements=[{"pronoun": "He", "resolved": True}],
            method="llm",
        )
        assert result.resolved_text == "John went to the store."
        assert result.original_text == "He went to the store."
        assert result.method == "llm"
        assert len(result.replacements) == 1

    def test_coref_result_defaults(self):
        """Test CorefResult with default values."""
        result = CorefResult(
            resolved_text="Test",
            original_text="Test",
        )
        assert result.replacements == []
        assert result.method == "none"


class TestHeuristicCorefResolver:
    """Tests for the heuristic-based coreference resolver."""

    @pytest.fixture
    def resolver(self):
        """Create HeuristicCorefResolver instance."""
        return HeuristicCorefResolver()

    def test_no_pronouns(self, resolver):
        """Test text without pronouns returns unchanged."""
        text = "John went to the store."
        result = resolver.resolve(text)
        assert result.resolved_text == text
        assert result.method == "heuristic"
        assert len(result.replacements) == 0

    def test_male_pronoun_replacement(self, resolver):
        """Test male pronoun replacement with entity hints."""
        text = "He went to the store."
        result = resolver.resolve(text, entity_hints={"male": "John Smith"})
        assert result.resolved_text == "John Smith went to the store."
        assert result.method == "heuristic"
        assert len(result.replacements) == 1
        assert result.replacements[0]["pronoun"] == "He"
        assert result.replacements[0]["referent"] == "John Smith"

    def test_female_pronoun_replacement(self, resolver):
        """Test female pronoun replacement with entity hints."""
        text = "She called the office."
        result = resolver.resolve(text, entity_hints={"female": "Alice"})
        assert result.resolved_text == "Alice called the office."
        assert result.method == "heuristic"

    def test_possessive_pronoun(self, resolver):
        """Test possessive pronoun replacement."""
        text = "His car is blue."
        result = resolver.resolve(text, entity_hints={"male": "Bob"})
        assert result.resolved_text == "Bob's car is blue."

    def test_extract_entities_from_context(self, resolver):
        """Test entity extraction from context text."""
        text = "He went to the store."
        context = "John was hungry."
        result = resolver.resolve(text, context=context)
        # "John" should be extracted as male from context
        assert result.resolved_text == "John went to the store."

    def test_no_entities_available(self, resolver):
        """Test behavior when no entities can be determined."""
        text = "He went to the store."
        result = resolver.resolve(text)  # No context or hints
        # Should return unchanged when no entity can be found
        assert result.resolved_text == text
        assert len(result.replacements) == 0

    def test_preserve_capitalization(self, resolver):
        """Test that capitalization is preserved in replacements."""
        text = "he went to the store."  # lowercase 'he'
        result = resolver.resolve(text, entity_hints={"male": "John"})
        assert result.resolved_text == "john went to the store."

    def test_multiple_pronouns(self, resolver):
        """Test multiple pronoun replacement."""
        text = "He called her about his project."
        result = resolver.resolve(
            text, entity_hints={"male": "Bob", "female": "Alice"}
        )
        assert "Bob" in result.resolved_text
        assert "Alice" in result.resolved_text

    def test_neutral_pronouns(self, resolver):
        """Test neutral pronouns (they/them)."""
        text = "They decided to leave."
        result = resolver.resolve(text, entity_hints={"neutral": "The team"})
        assert result.resolved_text == "The team decided to leave."


class TestLLMCorefResolver:
    """Tests for the LLM-based coreference resolver."""

    @pytest.fixture
    def resolver(self):
        """Create LLMCorefResolver without LLM provider."""
        return LLMCorefResolver(llm_provider=None)

    def test_no_pronouns_skips_llm(self, resolver):
        """Test that text without pronouns skips LLM call."""
        text = "John went to the store."
        result = resolver.resolve(text)
        assert result.resolved_text == text
        assert result.method == "none"  # No LLM call needed

    def test_has_pronouns_detection(self, resolver):
        """Test pronoun detection."""
        assert resolver._has_pronouns("He went there.")
        assert resolver._has_pronouns("She called him.")
        assert resolver._has_pronouns("They are ready.")
        assert resolver._has_pronouns("It was interesting.")
        assert not resolver._has_pronouns("John went to the store.")
        assert not resolver._has_pronouns("The cat sat on the mat.")

    def test_build_context(self, resolver):
        """Test context building from hints and context text."""
        context = resolver._build_context(
            context="John met Sarah.",
            entity_hints={"speaker": "John", "other": "Sarah"},
        )
        assert "speaker: John" in context
        assert "other: Sarah" in context
        assert "John met Sarah." in context

    def test_format_entities(self, resolver):
        """Test entity formatting for prompt."""
        formatted = resolver._format_entities(
            {"male": "John", "female": "Alice"}
        )
        assert "John (male)" in formatted
        assert "Alice (female)" in formatted

    def test_fallback_to_heuristic(self, resolver):
        """Test fallback to heuristic when no LLM available."""
        text = "He went to the store."
        result = resolver.resolve(
            text,
            entity_hints={"male": "John"},
        )
        # Should fall back to heuristic since no LLM provider
        assert result.method == "heuristic"
        assert result.resolved_text == "John went to the store."

    def test_detect_replacements(self, resolver):
        """Test replacement detection."""
        original = "He called her."
        resolved = "John called Alice."
        replacements = resolver._detect_replacements(original, resolved)
        assert len(replacements) == 2
        pronoun_list = [r["pronoun"] for r in replacements]
        assert "He" in pronoun_list
        assert "her" in pronoun_list

    def test_extract_resolved_text_markers(self, resolver):
        """Test extraction of resolved text with various markers."""
        # Test "Resolved:" marker
        response = "Reasoning: He refers to John.\nResolved: John went to the store."
        result = resolver._extract_resolved_text(response, "He went to the store.")
        assert result == "John went to the store."

        # Test quoted text
        response = '"John went to the store."'
        result = resolver._extract_resolved_text(response, "He went to the store.")
        assert result == "John went to the store."

    def test_extract_resolved_text_malformed(self, resolver):
        """Test that malformed responses return original."""
        # Response too short
        response = "Hi"
        original = "He went to the store and bought groceries."
        result = resolver._extract_resolved_text(response, original)
        assert result == original

        # Response too long
        response = original * 5
        result = resolver._extract_resolved_text(response, original)
        assert result == original


class TestConvenienceFunctions:
    """Tests for convenience functions."""

    def test_get_resolver_default(self):
        """Test getting default resolver."""
        resolver = get_resolver()
        assert isinstance(resolver, LLMCorefResolver)

    def test_get_resolver_with_provider(self):
        """Test getting resolver with custom provider."""
        mock_provider = object()
        resolver = get_resolver(llm_provider=mock_provider)
        assert isinstance(resolver, LLMCorefResolver)
        assert resolver.llm_provider is mock_provider

    def test_resolve_coreferences_function(self):
        """Test the convenience resolve_coreferences function."""
        # With entity hints, should use heuristic fallback
        result = resolve_coreferences(
            text="He went to the store.",
            entity_hints={"male": "John"},
        )
        assert result == "John went to the store."

    def test_resolve_coreferences_no_pronouns(self):
        """Test resolve_coreferences with no pronouns."""
        result = resolve_coreferences(text="John went to the store.")
        assert result == "John went to the store."


class TestGenderGuessing:
    """Tests for the heuristic gender guessing."""

    @pytest.fixture
    def resolver(self):
        """Create HeuristicCorefResolver instance."""
        return HeuristicCorefResolver()

    def test_guess_male_names(self, resolver):
        """Test guessing male names."""
        assert resolver._guess_gender("John") == "male"
        assert resolver._guess_gender("Michael") == "male"
        assert resolver._guess_gender("Bob") == "male"
        assert resolver._guess_gender("Tom Smith") == "male"

    def test_guess_female_names(self, resolver):
        """Test guessing female names."""
        assert resolver._guess_gender("Alice") == "female"
        assert resolver._guess_gender("Sarah") == "female"
        assert resolver._guess_gender("Jennifer") == "female"
        assert resolver._guess_gender("Mary Johnson") == "female"

    def test_guess_unknown_names(self, resolver):
        """Test unknown names return None."""
        assert resolver._guess_gender("Xyzzy") is None
        assert resolver._guess_gender("Aakash") is None
