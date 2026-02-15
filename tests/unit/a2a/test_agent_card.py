"""Unit tests for A2A Agent Card builder."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from nexus.a2a.agent_card import (
    AgentCardCache,
    _detect_auth_schemes,
    _map_skills,
    build_agent_card,
    get_cached_card,
    get_cached_card_bytes,
    invalidate_cache,
)
from nexus.a2a.models import AgentCard

# ======================================================================
# Helpers — mock skill metadata
# ======================================================================


@dataclass
class MockSkillMetadata:
    """Minimal mock of nexus.skills.models.SkillMetadata."""

    name: str
    description: str
    tags: list[str] = field(default_factory=list)


# ======================================================================
# build_agent_card
# ======================================================================


class TestBuildAgentCard:
    def test_returns_agent_card(self) -> None:
        card = build_agent_card(base_url="https://example.com")
        assert isinstance(card, AgentCard)

    def test_default_name(self) -> None:
        card = build_agent_card()
        assert card.name == "Nexus Agent"

    def test_default_description(self) -> None:
        card = build_agent_card()
        assert "filesystem" in card.description.lower()

    def test_url_points_to_a2a_endpoint(self) -> None:
        card = build_agent_card(base_url="https://nexus.example.com")
        assert card.url == "https://nexus.example.com/a2a"

    def test_version(self) -> None:
        card = build_agent_card()
        assert card.version  # non-empty

    def test_streaming_enabled(self) -> None:
        card = build_agent_card()
        assert card.capabilities.streaming is True

    def test_push_notifications_disabled(self) -> None:
        card = build_agent_card()
        assert card.capabilities.pushNotifications is False

    def test_default_input_modes(self) -> None:
        card = build_agent_card()
        assert "text/plain" in card.defaultInputModes
        assert "application/json" in card.defaultInputModes

    def test_default_output_modes(self) -> None:
        card = build_agent_card()
        assert "text/plain" in card.defaultOutputModes

    def test_provider_info(self) -> None:
        card = build_agent_card(base_url="http://localhost:2026")
        assert card.provider is not None
        assert card.provider.organization == "Nexus"

    def test_with_skills(self) -> None:
        skills = [
            MockSkillMetadata(name="search", description="Search files", tags=["search"]),
            MockSkillMetadata(name="write", description="Write files"),
        ]
        card = build_agent_card(skills=skills)
        assert len(card.skills) == 2
        assert card.skills[0].id == "search"
        assert card.skills[0].name == "search"

    def test_empty_skills(self) -> None:
        card = build_agent_card(skills=[])
        assert card.skills == []


# ======================================================================
# Skill mapping
# ======================================================================


class TestMapSkills:
    def test_maps_name_and_description(self) -> None:
        skills = [MockSkillMetadata(name="test", description="A test skill")]
        mapped = _map_skills(skills)
        assert len(mapped) == 1
        assert mapped[0].id == "test"
        assert mapped[0].name == "test"
        assert mapped[0].description == "A test skill"

    def test_maps_tags(self) -> None:
        skills = [MockSkillMetadata(name="s", description="d", tags=["t1", "t2"])]
        mapped = _map_skills(skills)
        assert mapped[0].tags == ["t1", "t2"]

    def test_skips_skills_without_name(self) -> None:
        skills = [MockSkillMetadata(name="", description="no name")]
        mapped = _map_skills(skills)
        assert len(mapped) == 0

    def test_skips_skills_without_description(self) -> None:
        skills = [MockSkillMetadata(name="test", description="")]
        mapped = _map_skills(skills)
        assert len(mapped) == 0

    def test_handles_non_skill_objects(self) -> None:
        """Gracefully handles objects without expected attributes."""
        skills = [object()]  # no name/description
        mapped = _map_skills(skills)
        assert len(mapped) == 0


# ======================================================================
# Auth scheme detection
# ======================================================================


class TestDetectAuthSchemes:
    def test_no_provider(self) -> None:
        schemes = _detect_auth_schemes(None)
        assert schemes == []

    def test_api_key_provider(self) -> None:
        class FakeAPIKeyAuth:
            pass

        schemes = _detect_auth_schemes(FakeAPIKeyAuth())
        assert any(s.type == "apiKey" for s in schemes)

    def test_oauth_provider(self) -> None:
        class FakeOAuthProvider:
            pass

        schemes = _detect_auth_schemes(FakeOAuthProvider())
        assert any(s.type == "oauth2" for s in schemes)

    def test_database_local_provider(self) -> None:
        class FakeDatabaseLocalAuth:
            pass

        schemes = _detect_auth_schemes(FakeDatabaseLocalAuth())
        assert any(s.type == "httpBearer" for s in schemes)

    def test_discriminating_provider(self) -> None:
        class FakeDiscriminatingAuthProvider:
            pass

        schemes = _detect_auth_schemes(FakeDiscriminatingAuthProvider())
        assert any(s.type == "apiKey" for s in schemes)
        assert any(s.type == "httpBearer" for s in schemes)

    def test_unknown_provider_defaults_to_bearer(self) -> None:
        class FakeUnknownAuth:
            pass

        schemes = _detect_auth_schemes(FakeUnknownAuth())
        assert len(schemes) == 1
        assert schemes[0].type == "httpBearer"


# ======================================================================
# Caching
# ======================================================================


class TestCaching:
    def setup_method(self) -> None:
        invalidate_cache()

    def test_get_cached_card_bytes_returns_bytes(self) -> None:
        card_bytes = get_cached_card_bytes()
        assert isinstance(card_bytes, bytes)

    def test_cached_bytes_is_valid_json(self) -> None:
        card_bytes = get_cached_card_bytes()
        card_dict = json.loads(card_bytes)
        assert "name" in card_dict
        assert "url" in card_dict

    def test_cached_card_matches(self) -> None:
        get_cached_card_bytes()
        card = get_cached_card()
        assert card is not None
        assert isinstance(card, AgentCard)

    def test_cache_returns_same_bytes(self) -> None:
        b1 = get_cached_card_bytes()
        b2 = get_cached_card_bytes()
        assert b1 is b2  # Same object (cached)

    def test_force_rebuild(self) -> None:
        b1 = get_cached_card_bytes()
        b2 = get_cached_card_bytes(force_rebuild=True)
        # Different objects but same content
        assert b1 is not b2
        assert b1 == b2

    def test_invalidate_cache(self) -> None:
        get_cached_card_bytes()
        assert get_cached_card() is not None
        invalidate_cache()
        assert get_cached_card() is None

    def test_card_json_has_required_spec_fields(self) -> None:
        """Verify the Agent Card JSON contains fields required by the A2A spec."""
        card_bytes = get_cached_card_bytes(base_url="https://example.com")
        card_dict = json.loads(card_bytes)
        assert "name" in card_dict
        assert "description" in card_dict
        assert "url" in card_dict
        assert "version" in card_dict
        assert "capabilities" in card_dict
        assert "skills" in card_dict


# ======================================================================
# AgentCardCache class
# ======================================================================


class TestAgentCardCache:
    def test_cache_instances_independent(self) -> None:
        """Two AgentCardCache instances don't share state."""
        cache1 = AgentCardCache()
        cache2 = AgentCardCache()

        cache1.get_card_bytes(base_url="https://one.example.com")
        assert cache2.get_card() is None  # cache2 was never populated

    def test_cache_returns_same_bytes(self) -> None:
        """Repeated calls return identical bytes (same object)."""
        cache = AgentCardCache()
        b1 = cache.get_card_bytes()
        b2 = cache.get_card_bytes()
        assert b1 is b2

    def test_invalidate_clears_cache(self) -> None:
        """After invalidate, next call rebuilds."""
        cache = AgentCardCache()
        cache.get_card_bytes()
        assert cache.get_card() is not None

        cache.invalidate()
        assert cache.get_card() is None

    def test_force_rebuild(self) -> None:
        """force_rebuild=True produces fresh bytes object."""
        cache = AgentCardCache()
        b1 = cache.get_card_bytes()
        b2 = cache.get_card_bytes(force_rebuild=True)
        assert b1 is not b2
        assert b1 == b2  # Same content, different object
