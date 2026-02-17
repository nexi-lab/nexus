"""Unit tests for A2A Agent Card builder and cache.

Tests cover:
- ``AgentCardCache`` write-once caching behaviour
- ``build_agent_card()`` with various inputs
- ``_map_skills()`` edge cases
- ``_detect_auth_schemes()`` all provider branches
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from nexus.a2a.agent_card import (
    AgentCardCache,
    _detect_auth_schemes,
    _map_skills,
    build_agent_card,
)
from nexus.a2a.models import AgentCard

# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


def _make_skill(
    name: str = "search",
    description: str = "Search files",
    tags: list[str] | None = None,
) -> SimpleNamespace:
    """Create a fake SkillMetadata-like object."""
    return SimpleNamespace(name=name, description=description, tags=tags or [])


def _make_config(
    a2a_agent_name: str | None = None,
    a2a_agent_description: str | None = None,
) -> SimpleNamespace:
    """Create a fake NexusConfig-like object."""
    return SimpleNamespace(
        a2a_agent_name=a2a_agent_name,
        a2a_agent_description=a2a_agent_description,
    )


def _make_auth_provider(class_name: str) -> object:
    """Create a fake auth provider with a specific class name."""
    cls = type(class_name, (), {})
    return cls()


# ------------------------------------------------------------------
# AgentCardCache
# ------------------------------------------------------------------


class TestAgentCardCache:
    """Tests for the write-once ``AgentCardCache``."""

    def test_get_card_returns_none_before_build(self) -> None:
        cache = AgentCardCache()
        assert cache.get_card() is None

    def test_get_card_bytes_builds_and_caches(self) -> None:
        cache = AgentCardCache()
        result = cache.get_card_bytes()

        assert isinstance(result, bytes)
        parsed = json.loads(result)
        assert parsed["name"] == "Nexus Agent"

    def test_get_card_bytes_returns_same_object_on_second_call(self) -> None:
        cache = AgentCardCache()
        first = cache.get_card_bytes()
        second = cache.get_card_bytes()

        # Same object identity — not just equal, but the *same* bytes.
        assert first is second

    def test_get_card_returns_card_after_build(self) -> None:
        cache = AgentCardCache()
        cache.get_card_bytes()

        card = cache.get_card()
        assert card is not None
        assert isinstance(card, AgentCard)
        assert card.name == "Nexus Agent"

    def test_write_once_ignores_different_config_on_second_call(self) -> None:
        """After the first build, new params are ignored (write-once)."""
        cache = AgentCardCache()
        first = cache.get_card_bytes(config=_make_config(a2a_agent_name="First"))
        second = cache.get_card_bytes(config=_make_config(a2a_agent_name="Second"))

        assert first is second
        assert json.loads(first)["name"] == "First"

    def test_new_instance_allows_rebuild(self) -> None:
        """To rebuild, create a new instance (the write-once contract)."""
        cache1 = AgentCardCache()
        bytes1 = cache1.get_card_bytes(config=_make_config(a2a_agent_name="Alpha"))

        cache2 = AgentCardCache()
        bytes2 = cache2.get_card_bytes(config=_make_config(a2a_agent_name="Beta"))

        assert json.loads(bytes1)["name"] == "Alpha"
        assert json.loads(bytes2)["name"] == "Beta"

    def test_get_card_bytes_with_skills(self) -> None:
        cache = AgentCardCache()
        skills = [_make_skill("search", "Search files", ["fs"])]
        result = cache.get_card_bytes(skills=skills)

        parsed = json.loads(result)
        assert len(parsed["skills"]) == 1
        assert parsed["skills"][0]["name"] == "search"

    def test_get_card_bytes_custom_base_url(self) -> None:
        cache = AgentCardCache()
        result = cache.get_card_bytes(base_url="https://example.com")

        parsed = json.loads(result)
        assert parsed["url"] == "https://example.com/a2a"


# ------------------------------------------------------------------
# build_agent_card()
# ------------------------------------------------------------------


class TestBuildAgentCard:
    """Tests for the pure ``build_agent_card()`` function."""

    def test_defaults(self) -> None:
        card = build_agent_card()

        assert card.name == "Nexus Agent"
        assert card.description == "AI-native distributed filesystem agent"
        assert card.version == "0.7.1"
        assert card.url == "http://localhost:2026/a2a"
        assert card.capabilities.streaming is True
        assert card.capabilities.pushNotifications is False
        assert card.skills == []
        assert card.authentication == []

    def test_custom_config(self) -> None:
        config = _make_config(
            a2a_agent_name="Custom Agent",
            a2a_agent_description="A custom agent",
        )
        card = build_agent_card(config=config)

        assert card.name == "Custom Agent"
        assert card.description == "A custom agent"

    def test_config_none_values_use_defaults(self) -> None:
        config = _make_config(a2a_agent_name=None, a2a_agent_description=None)
        card = build_agent_card(config=config)

        assert card.name == "Nexus Agent"
        assert card.description == "AI-native distributed filesystem agent"

    def test_config_empty_strings_use_defaults(self) -> None:
        config = _make_config(a2a_agent_name="", a2a_agent_description="")
        card = build_agent_card(config=config)

        assert card.name == "Nexus Agent"
        assert card.description == "AI-native distributed filesystem agent"

    def test_custom_base_url(self) -> None:
        card = build_agent_card(base_url="https://nexus.example.com")
        assert card.url == "https://nexus.example.com/a2a"

    def test_provider(self) -> None:
        card = build_agent_card()
        assert card.provider is not None
        assert card.provider.organization == "Nexus"

    def test_default_io_modes(self) -> None:
        card = build_agent_card()
        assert "text/plain" in card.defaultInputModes
        assert "application/json" in card.defaultInputModes
        assert "text/plain" in card.defaultOutputModes
        assert "application/json" in card.defaultOutputModes

    def test_with_multiple_skills(self) -> None:
        skills = [
            _make_skill("search", "Search files"),
            _make_skill("write", "Write files"),
            _make_skill("read", "Read files"),
        ]
        card = build_agent_card(skills=skills)
        assert len(card.skills) == 3
        assert [s.name for s in card.skills] == ["search", "write", "read"]


# ------------------------------------------------------------------
# _map_skills()
# ------------------------------------------------------------------


class TestMapSkills:
    """Tests for the ``_map_skills()`` helper."""

    def test_empty_list(self) -> None:
        assert _map_skills([]) == []

    def test_valid_skill(self) -> None:
        skills = _map_skills([_make_skill("search", "Search files", ["fs"])])

        assert len(skills) == 1
        assert skills[0].id == "search"
        assert skills[0].name == "search"
        assert skills[0].description == "Search files"
        assert skills[0].tags == ["fs"]

    def test_skill_missing_name_is_skipped(self) -> None:
        skill = SimpleNamespace(name=None, description="Has description", tags=[])
        assert _map_skills([skill]) == []

    def test_skill_missing_description_is_skipped(self) -> None:
        skill = SimpleNamespace(name="has_name", description=None, tags=[])
        assert _map_skills([skill]) == []

    def test_skill_empty_name_is_skipped(self) -> None:
        skill = SimpleNamespace(name="", description="Has description", tags=[])
        assert _map_skills([skill]) == []

    def test_skill_empty_description_is_skipped(self) -> None:
        skill = SimpleNamespace(name="has_name", description="", tags=[])
        assert _map_skills([skill]) == []

    def test_skill_none_tags_default_to_empty(self) -> None:
        skill = SimpleNamespace(name="s", description="d", tags=None)
        result = _map_skills([skill])

        assert len(result) == 1
        assert result[0].tags == []

    def test_skill_missing_tags_attribute(self) -> None:
        """Object without a tags attribute should get empty tags."""
        skill = SimpleNamespace(name="s", description="d")
        result = _map_skills([skill])

        assert len(result) == 1
        assert result[0].tags == []

    def test_mixed_valid_and_invalid(self) -> None:
        skills = [
            _make_skill("good", "Valid skill"),
            SimpleNamespace(name=None, description="No name", tags=[]),
            _make_skill("also_good", "Another valid skill"),
        ]
        result = _map_skills(skills)

        assert len(result) == 2
        assert result[0].name == "good"
        assert result[1].name == "also_good"


# ------------------------------------------------------------------
# _detect_auth_schemes()
# ------------------------------------------------------------------


class TestDetectAuthSchemes:
    """Tests for the ``_detect_auth_schemes()`` helper."""

    def test_none_provider(self) -> None:
        assert _detect_auth_schemes(None) == []

    def test_api_key_provider(self) -> None:
        provider = _make_auth_provider("APIKeyAuthProvider")
        schemes = _detect_auth_schemes(provider)

        assert len(schemes) == 1
        assert schemes[0].type == "apiKey"

    def test_static_key_provider(self) -> None:
        provider = _make_auth_provider("StaticKeyProvider")
        schemes = _detect_auth_schemes(provider)

        assert len(schemes) == 1
        assert schemes[0].type == "apiKey"

    def test_oauth_provider(self) -> None:
        provider = _make_auth_provider("OAuthProvider")
        schemes = _detect_auth_schemes(provider)

        assert len(schemes) == 2
        types = {s.type for s in schemes}
        assert types == {"oauth2", "openIdConnect"}

    def test_oidc_provider(self) -> None:
        provider = _make_auth_provider("OIDCProvider")
        schemes = _detect_auth_schemes(provider)

        assert len(schemes) == 2
        types = {s.type for s in schemes}
        assert types == {"oauth2", "openIdConnect"}

    def test_database_local_provider(self) -> None:
        provider = _make_auth_provider("DatabaseLocalAuthProvider")
        schemes = _detect_auth_schemes(provider)

        assert len(schemes) == 1
        assert schemes[0].type == "httpBearer"

    def test_discriminating_provider(self) -> None:
        provider = _make_auth_provider("DiscriminatingAuthProvider")
        schemes = _detect_auth_schemes(provider)

        assert len(schemes) == 2
        types = [s.type for s in schemes]
        assert types == ["apiKey", "httpBearer"]

    def test_unknown_provider_defaults_to_bearer(self) -> None:
        provider = _make_auth_provider("SomethingUnknown")
        schemes = _detect_auth_schemes(provider)

        assert len(schemes) == 1
        assert schemes[0].type == "httpBearer"


# ------------------------------------------------------------------
# supportedInterfaces (gRPC transport binding, #1726)
# ------------------------------------------------------------------


class TestSupportedInterfaces:
    """Tests for the ``supportedInterfaces`` field on AgentCard."""

    def test_default_includes_jsonrpc_interface(self) -> None:
        card = build_agent_card()

        assert len(card.supportedInterfaces) == 1
        iface = card.supportedInterfaces[0]
        assert iface.protocol_binding == "JSONRPC"
        assert iface.protocol_version == "1.0"
        assert iface.url == "http://localhost:2026/a2a"

    def test_grpc_port_adds_grpc_interface(self) -> None:
        card = build_agent_card(grpc_port=2027)

        assert len(card.supportedInterfaces) == 2
        bindings = {i.protocol_binding for i in card.supportedInterfaces}
        assert bindings == {"JSONRPC", "GRPC"}

    def test_grpc_interface_url_format(self) -> None:
        card = build_agent_card(
            base_url="https://nexus.example.com:2026",
            grpc_port=2027,
        )

        grpc_iface = next(i for i in card.supportedInterfaces if i.protocol_binding == "GRPC")
        assert grpc_iface.url == "nexus.example.com:2027"
        assert grpc_iface.protocol_version == "1.0"

    def test_no_grpc_interface_when_port_zero(self) -> None:
        card = build_agent_card(grpc_port=0)

        assert len(card.supportedInterfaces) == 1
        assert card.supportedInterfaces[0].protocol_binding == "JSONRPC"

    def test_no_grpc_interface_when_port_none(self) -> None:
        card = build_agent_card(grpc_port=None)

        assert len(card.supportedInterfaces) == 1

    def test_cache_passes_grpc_port(self) -> None:
        import json

        cache = AgentCardCache()
        result = cache.get_card_bytes(grpc_port=2027)
        parsed = json.loads(result)

        interfaces = parsed.get("supportedInterfaces", [])
        assert len(interfaces) == 2
        bindings = {i["protocol_binding"] for i in interfaces}
        assert "GRPC" in bindings
