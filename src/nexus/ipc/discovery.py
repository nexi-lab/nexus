"""Agent discovery via filesystem.

Agents discover each other by listing the filesystem:
- ``ls /agents/`` = who exists
- ``read /agents/{id}/AGENT.json`` = capabilities and status

Bridges internal discovery (filesystem) with external discovery
(A2A Agent Card at ``/.well-known/agent.json``).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from nexus.ipc.conventions import AGENTS_ROOT, agent_card_path
from nexus.ipc.protocols import VFSOperations

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DiscoveredAgent:
    """Information about a discovered agent.

    Attributes:
        agent_id: Unique agent identifier.
        name: Human-readable name.
        skills: List of skill/capability identifiers.
        status: Current agent status (connected, idle, etc.).
        inbox: Path to the agent's inbox directory.
        metadata: Additional agent metadata.
    """

    agent_id: str
    name: str
    skills: list[str]
    status: str
    inbox: str
    metadata: dict[str, Any]


class AgentDiscovery:
    """Discovers agents by reading the filesystem.

    Args:
        vfs: VFS operations for listing and reading.
        zone_id: Zone ID for multi-tenant isolation.
    """

    def __init__(
        self,
        vfs: VFSOperations,
        zone_id: str = "default",
        cache_ttl_seconds: float = 10.0,
    ) -> None:
        self._vfs = vfs
        self._zone_id = zone_id
        self._cache_ttl = cache_ttl_seconds
        self._cache: list[DiscoveredAgent] | None = None
        self._cache_expires_at: float = 0.0

    async def list_agents(self) -> list[str]:
        """List all registered agent IDs.

        Returns:
            List of agent directory names under ``/agents/``.
        """
        try:
            entries = await self._vfs.list_dir(AGENTS_ROOT, self._zone_id)
            return sorted(entries)
        except Exception:
            logger.warning(
                "Failed to list agents at %s",
                AGENTS_ROOT,
                exc_info=True,
            )
            return []

    async def get_agent_card(self, agent_id: str) -> DiscoveredAgent | None:
        """Read an agent's card from the filesystem.

        Args:
            agent_id: The agent to look up.

        Returns:
            DiscoveredAgent if the card exists and is valid, None otherwise.
        """
        card_path = agent_card_path(agent_id)
        try:
            data = await self._vfs.read(card_path, self._zone_id)
            card_dict = json.loads(data)
        except Exception:
            logger.debug(
                "No valid AGENT.json for agent %s at %s",
                agent_id,
                card_path,
            )
            return None

        return DiscoveredAgent(
            agent_id=agent_id,
            name=card_dict.get("name", agent_id),
            skills=card_dict.get("skills", []),
            status=card_dict.get("status", "unknown"),
            inbox=card_dict.get("inbox", f"/agents/{agent_id}/inbox"),
            metadata={
                k: v for k, v in card_dict.items() if k not in ("name", "skills", "status", "inbox")
            },
        )

    async def discover_all(self, *, bypass_cache: bool = False) -> list[DiscoveredAgent]:
        """Discover all agents with valid agent cards.

        Uses a TTL-based cache to avoid re-scanning the filesystem on
        every call. Cache can be bypassed for one-off fresh lookups.

        Args:
            bypass_cache: If True, skip the cache and re-scan.

        Returns:
            List of DiscoveredAgent instances for all agents that have
            a valid AGENT.json file.
        """
        now = time.monotonic()
        if not bypass_cache and self._cache is not None and now < self._cache_expires_at:
            return list(self._cache)  # Return copy to prevent mutation

        agent_ids = await self.list_agents()
        discovered: list[DiscoveredAgent] = []
        for agent_id in agent_ids:
            agent = await self.get_agent_card(agent_id)
            if agent is not None:
                discovered.append(agent)

        self._cache = discovered
        self._cache_expires_at = now + self._cache_ttl
        return discovered

    async def find_by_skill(self, skill: str) -> list[DiscoveredAgent]:
        """Find agents that have a specific skill.

        Args:
            skill: The skill identifier to search for.

        Returns:
            List of agents whose skills list contains the given skill.
        """
        all_agents = await self.discover_all()
        return [a for a in all_agents if skill in a.skills]
