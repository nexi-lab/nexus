"""Unit tests for query expansion integration.

Tests cover:
- DaemonConfig expansion defaults
- create_query_expander factory (openrouter, openai, local, invalid)
- LocalQueryExpander._parse_response output parsing
"""

from __future__ import annotations

import pytest

from nexus.bricks.search.daemon import DaemonConfig
from nexus.bricks.search.query_expansion import (
    ExpansionType,
    LocalQueryExpander,
    OpenAIQueryExpander,
    OpenRouterQueryExpander,
    QueryExpansionConfig,
    create_query_expander,
)

# =============================================================================
# DaemonConfig expansion defaults
# =============================================================================


class TestExpansionConfigDefaults:
    """Test DaemonConfig expansion default values."""

    def test_expansion_config_defaults(self) -> None:
        """DaemonConfig expansion defaults are all False/sensible."""
        config = DaemonConfig()

        assert config.query_expansion_enabled is False
        assert config.expansion_provider == "openrouter"
        assert config.expansion_model == "deepseek/deepseek-chat"


# =============================================================================
# create_query_expander factory
# =============================================================================


class TestCreateQueryExpander:
    """Tests for create_query_expander factory function."""

    def test_create_query_expander_openrouter(self) -> None:
        """Factory returns OpenRouterQueryExpander for 'openrouter' provider."""
        expander = create_query_expander(provider="openrouter")
        assert isinstance(expander, OpenRouterQueryExpander)
        # OpenAIQueryExpander is a subclass, so check it is NOT the subclass
        assert type(expander) is OpenRouterQueryExpander

    def test_create_query_expander_openai(self) -> None:
        """Factory returns OpenAIQueryExpander for 'openai' provider."""
        expander = create_query_expander(provider="openai")
        assert isinstance(expander, OpenAIQueryExpander)
        assert type(expander) is OpenAIQueryExpander

    def test_create_query_expander_local(self) -> None:
        """Factory returns LocalQueryExpander for 'local' provider."""
        expander = create_query_expander(provider="local")
        assert isinstance(expander, LocalQueryExpander)

    def test_create_query_expander_invalid(self) -> None:
        """Invalid provider raises ValueError."""
        with pytest.raises(ValueError, match="Unsupported provider"):
            create_query_expander(provider="anthropic_fake")

    def test_create_query_expander_with_custom_model(self) -> None:
        """Factory passes custom model to the expander config."""
        expander = create_query_expander(provider="local", model="my-custom-model.gguf")
        assert isinstance(expander, LocalQueryExpander)
        assert expander.config.model == "my-custom-model.gguf"


# =============================================================================
# LocalQueryExpander._parse_response
# =============================================================================


class TestLocalExpanderParseResponse:
    """Tests for LocalQueryExpander._parse_response output parsing."""

    def test_local_expander_parse_response(self) -> None:
        """lex:/vec:/hyde: output lines are correctly parsed into QueryExpansion objects."""
        config = QueryExpansionConfig(
            max_lex_variants=2,
            max_vec_variants=2,
            max_hyde_passages=2,
        )
        expander = LocalQueryExpander(config=config)

        response_text = (
            "lex: authentication login security\n"
            "lex: auth SSO SAML\n"
            "vec: How does the authentication system handle SSO logins?\n"
            "vec: What security protocols are used for user login?\n"
            "hyde: The authentication module uses SAML 2.0 for single sign-on.\n"
            "hyde: Users authenticate via OAuth2 tokens stored in secure cookies.\n"
        )

        expansions = expander._parse_response(response_text)

        assert len(expansions) == 6

        # Check types
        lex_items = [e for e in expansions if e.expansion_type == ExpansionType.LEX]
        vec_items = [e for e in expansions if e.expansion_type == ExpansionType.VEC]
        hyde_items = [e for e in expansions if e.expansion_type == ExpansionType.HYDE]

        assert len(lex_items) == 2
        assert len(vec_items) == 2
        assert len(hyde_items) == 2

        # Check parsed text (should strip the prefix)
        assert lex_items[0].text == "authentication login security"
        assert lex_items[1].text == "auth SSO SAML"
        assert "SSO" in vec_items[0].text
        assert "SAML 2.0" in hyde_items[0].text

        # All should have default weight 1.0
        for e in expansions:
            assert e.weight == 1.0

    def test_local_expander_respects_limits(self) -> None:
        """Parser respects max_lex_variants/max_vec_variants limits."""
        config = QueryExpansionConfig(
            max_lex_variants=1,
            max_vec_variants=1,
            max_hyde_passages=0,
        )
        expander = LocalQueryExpander(config=config)

        response_text = (
            "lex: first keyword\n"
            "lex: second keyword (should be ignored)\n"
            "vec: first question\n"
            "vec: second question (should be ignored)\n"
            "hyde: some passage (should be ignored)\n"
        )

        expansions = expander._parse_response(response_text)

        assert len(expansions) == 2
        assert expansions[0].expansion_type == ExpansionType.LEX
        assert expansions[0].text == "first keyword"
        assert expansions[1].expansion_type == ExpansionType.VEC
        assert expansions[1].text == "first question"

    def test_local_expander_parse_empty_response(self) -> None:
        """Empty response returns no expansions."""
        expander = LocalQueryExpander()
        expansions = expander._parse_response("")
        assert expansions == []

    def test_local_expander_parse_malformed_lines(self) -> None:
        """Lines without valid prefixes are ignored."""
        expander = LocalQueryExpander()
        response_text = (
            "Some random text\nlex: valid keyword\nnot a valid line\n  \nvec: valid question\n"
        )

        expansions = expander._parse_response(response_text)
        assert len(expansions) == 2
        assert expansions[0].expansion_type == ExpansionType.LEX
        assert expansions[1].expansion_type == ExpansionType.VEC
