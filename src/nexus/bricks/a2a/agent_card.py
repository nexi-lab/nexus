"""A2A Agent Card builder.

Builds an ``AgentCard`` from the Nexus server configuration and the
registered skills.  The card is built once and cached as pre-serialized
JSON bytes for zero-copy responses.
"""

import json
import logging
from typing import Any
from urllib.parse import urlparse

from nexus.bricks.a2a.models import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentProvider,
    AgentSkill,
    AuthScheme,
)
from nexus.contracts.constants import DEFAULT_NEXUS_URL

logger = logging.getLogger(__name__)


class AgentCardCache:
    """Write-once cache for a single Agent Card.

    Each ``AgentCardCache`` instance is independent — no shared globals.
    The cache is **write-once**: the first call to ``get_card_bytes()``
    builds and caches the card; all subsequent calls return the cached
    value.  To rebuild, create a new ``AgentCardCache`` instance.

    Thread-safety: the write-once contract means no lock is required.
    After the first ``get_card_bytes()`` call completes, the cached
    values are effectively immutable.  Concurrent initial calls may
    redundantly build the card, but the result is identical and harmless.
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
        grpc_port: int | None = None,
    ) -> bytes:
        """Return the Agent Card as pre-serialised JSON bytes.

        Builds on first call and caches the result.  The cache is
        write-once — to rebuild, create a new ``AgentCardCache``.
        """
        if self._card_bytes is not None:
            return self._card_bytes

        card = build_agent_card(
            config=config,
            skills=skills,
            base_url=base_url,
            auth_provider=auth_provider,
            grpc_port=grpc_port,
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


def build_agent_card(
    *,
    config: Any = None,
    skills: list[Any] | None = None,
    base_url: str = DEFAULT_NEXUS_URL,
    auth_provider: Any = None,
    grpc_port: int | None = None,
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

    # Build supported interfaces (v1.0)
    interfaces = [
        AgentInterface(
            url=f"{base_url}/a2a",
            protocol_binding="JSONRPC",
            protocol_version="1.0",
        ),
    ]
    if grpc_port:
        host = urlparse(base_url).hostname or "localhost"
        interfaces.append(
            AgentInterface(
                url=f"{host}:{grpc_port}",
                protocol_binding="GRPC",
                protocol_version="1.0",
            ),
        )

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
        supportedInterfaces=interfaces,
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
