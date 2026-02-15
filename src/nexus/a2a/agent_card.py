"""A2A Agent Card builder.

Builds an ``AgentCard`` from the Nexus server configuration and the
registered skills.  The card is built once and cached as pre-serialized
JSON bytes for zero-copy responses.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from nexus.a2a.models import (
    AgentCapabilities,
    AgentCard,
    AgentProvider,
    AgentSkill,
    AuthScheme,
)
from nexus.constants import DEFAULT_NEXUS_URL

logger = logging.getLogger(__name__)


class AgentCardCache:
    """Encapsulates the mutable cache state for a single Agent Card.

    Each ``AgentCardCache`` instance is independent — no shared globals.
    The class is intentionally simple: GIL + write-once semantics make
    a ``threading.Lock`` unnecessary.
    """

    __slots__ = ("_card_bytes", "_card")

    def __init__(self) -> None:
        self._card_bytes: bytes | None = None
        self._card: AgentCard | None = None

    def get_card_bytes(
        self,
        *,
        config: Any = None,
        skills: list[Any] | None = None,
        base_url: str = DEFAULT_NEXUS_URL,
        auth_provider: Any = None,
        force_rebuild: bool = False,
    ) -> bytes:
        """Return the Agent Card as pre-serialised JSON bytes.

        Builds on first call and caches the result.  Pass
        ``force_rebuild=True`` to invalidate and rebuild.
        """
        if self._card_bytes is not None and not force_rebuild:
            return self._card_bytes

        card = build_agent_card(
            config=config,
            skills=skills,
            base_url=base_url,
            auth_provider=auth_provider,
        )

        self._card = card
        self._card_bytes = json.dumps(
            card.model_dump(mode="json", exclude_none=True),
            indent=2,
        ).encode("utf-8")

        logger.info(
            "A2A Agent Card built: %s (%d skills)",
            card.name,
            len(card.skills),
        )
        return self._card_bytes

    def get_card(self) -> AgentCard | None:
        """Return the cached ``AgentCard`` instance, or *None* if not yet built."""
        return self._card

    def invalidate(self) -> None:
        """Clear the cache so the card will be rebuilt on next access."""
        self._card_bytes = None
        self._card = None


# Module-level default instance (backward-compatible with existing callers)
_default_cache = AgentCardCache()


def get_cached_card_bytes(
    *,
    config: Any = None,
    skills: list[Any] | None = None,
    base_url: str = DEFAULT_NEXUS_URL,
    auth_provider: Any = None,
    force_rebuild: bool = False,
) -> bytes:
    """Get the Agent Card as pre-serialized JSON bytes.

    Thin wrapper around the default ``AgentCardCache`` instance.
    """
    return _default_cache.get_card_bytes(
        config=config,
        skills=skills,
        base_url=base_url,
        auth_provider=auth_provider,
        force_rebuild=force_rebuild,
    )


def get_cached_card() -> AgentCard | None:
    """Return the cached ``AgentCard`` instance, or *None* if not yet built."""
    return _default_cache.get_card()


def invalidate_cache() -> None:
    """Clear the cached Agent Card so it will be rebuilt on next access."""
    _default_cache.invalidate()


def build_agent_card(
    *,
    config: Any = None,
    skills: list[Any] | None = None,
    base_url: str = DEFAULT_NEXUS_URL,
    auth_provider: Any = None,
) -> AgentCard:
    """Build an Agent Card from server configuration and skills.

    Parameters
    ----------
    config:
        ``NexusConfig`` instance.  If *None*, sensible defaults are used.
    skills:
        List of ``SkillMetadata`` objects from the skills registry.
    base_url:
        The public URL of this Nexus instance.
    auth_provider:
        The active authentication provider, used to determine which
        auth schemes to advertise.

    Returns
    -------
    AgentCard
        A fully populated Agent Card ready for serialization.
    """
    name = "Nexus Agent"
    description = "AI-native distributed filesystem agent"
    version = "0.7.1"

    if config is not None:
        name = getattr(config, "a2a_agent_name", None) or name
        description = getattr(config, "a2a_agent_description", None) or description

    # Map skills
    agent_skills = _map_skills(skills or [])

    # Detect auth schemes
    auth_schemes = _detect_auth_schemes(auth_provider)

    card = AgentCard(
        name=name,
        description=description,
        url=f"{base_url}/a2a",
        version=version,
        provider=AgentProvider(
            organization="Nexus",
            url=base_url,
        ),
        capabilities=AgentCapabilities(
            streaming=True,
            pushNotifications=False,
        ),
        authentication=auth_schemes,
        defaultInputModes=["text/plain", "application/json"],
        defaultOutputModes=["text/plain", "application/json"],
        skills=agent_skills,
    )

    return card


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


def _map_skills(skills: list[Any]) -> list[AgentSkill]:
    """Map Nexus SkillMetadata objects to A2A AgentSkill models."""
    result: list[AgentSkill] = []

    for skill in skills:
        name = getattr(skill, "name", None)
        description = getattr(skill, "description", None)

        if not name or not description:
            continue

        tags = getattr(skill, "tags", []) or []

        result.append(
            AgentSkill(
                id=name,
                name=name,
                description=description,
                tags=list(tags),
            )
        )

    return result


def _detect_auth_schemes(auth_provider: Any) -> list[AuthScheme]:
    """Detect authentication schemes from the active auth provider."""
    if auth_provider is None:
        return []

    schemes: list[AuthScheme] = []
    provider_type = type(auth_provider).__name__

    if "APIKey" in provider_type or "StaticKey" in provider_type:
        schemes.append(AuthScheme(type="apiKey"))
    elif "OAuth" in provider_type or "OIDC" in provider_type:
        schemes.append(AuthScheme(type="oauth2"))
        schemes.append(AuthScheme(type="openIdConnect"))
    elif "DatabaseLocal" in provider_type:
        # JWT-based auth via Bearer token
        schemes.append(AuthScheme(type="httpBearer"))
    elif "Discriminating" in provider_type:
        # Supports multiple auth methods
        schemes.append(AuthScheme(type="apiKey"))
        schemes.append(AuthScheme(type="httpBearer"))

    # If we couldn't detect, default to Bearer
    if not schemes and auth_provider is not None:
        schemes.append(AuthScheme(type="httpBearer"))

    return schemes
