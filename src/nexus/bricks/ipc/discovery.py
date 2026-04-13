"""Agent discovery via IPC storage.

Agents discover each other by querying the storage driver:
- ``list_dir /agents/`` = who exists
- ``read /agents/{id}/AGENT.json`` = capabilities and status

Bridges internal discovery (IPC storage) with external discovery.
"""

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from nexus.bricks.ipc.conventions import AGENTS_ROOT, agent_card_path
from nexus.contracts.constants import ROOT_ZONE_ID

if TYPE_CHECKING:
    from nexus.contracts.cache_store import CacheStoreABC

logger = logging.getLogger(__name__)

_DISCOVERY_CACHE_KEY = "nexus:ipc:discovery:agents"


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
    """Discovers agents by reading IPC storage.

    Uses CacheStoreABC for TTL-based caching per KERNEL-ARCHITECTURE.md §2
    (CacheStore pillar: ephemeral KV with TTL).

    Args:
        vfs: NexusFS instance for IPC listing and reading.
        zone_id: Zone ID for multi-tenant isolation.
        cache_store: CacheStoreABC for ephemeral caching (optional, degrades gracefully).
        cache_ttl_seconds: TTL for the agent discovery cache.
    """

    def __init__(
        self,
        vfs: Any,
        zone_id: str = ROOT_ZONE_ID,
        cache_ttl_seconds: float = 10.0,
        cache_store: "CacheStoreABC | None" = None,
    ) -> None:
        self._vfs = vfs
        self._zone_id = zone_id
        self._cache_ttl = cache_ttl_seconds
        self._cache_store = cache_store

    def _ctx(self) -> Any:
        from nexus.contracts.types import OperationContext

        return OperationContext(user_id="system", groups=[], zone_id=self._zone_id, is_system=True)

    def _cache_key(self) -> str:
        """Zone-scoped cache key for agent discovery."""
        return f"{_DISCOVERY_CACHE_KEY}:{self._zone_id}"

    async def list_agents(self) -> list[str]:
        """List all registered agent IDs.

        Returns:
            List of agent directory names under ``/agents/``.
        """
        try:
            entries = self._vfs.sys_readdir(AGENTS_ROOT, recursive=False, context=self._ctx())
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
            data = self._vfs.sys_read(card_path, context=self._ctx())
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

        Uses CacheStoreABC for TTL-based caching when available. Falls back
        to direct scan when no cache store is configured.

        Args:
            bypass_cache: If True, skip the cache and re-scan.

        Returns:
            List of DiscoveredAgent instances for all agents that have
            a valid AGENT.json file.
        """
        key = self._cache_key()

        # Try cache first
        if not bypass_cache and self._cache_store is not None:
            cached_bytes = await self._cache_store.get(key)
            if cached_bytes is not None:
                return _deserialize_agents(cached_bytes)

        # Cache miss or bypass — scan filesystem
        agent_ids = await self.list_agents()
        discovered: list[DiscoveredAgent] = []
        for agent_id in agent_ids:
            agent = await self.get_agent_card(agent_id)
            if agent is not None:
                discovered.append(agent)

        # Write to cache
        if self._cache_store is not None:
            serialized = _serialize_agents(discovered)
            await self._cache_store.set(key, serialized, ttl=int(self._cache_ttl))

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


def _serialize_agents(agents: list[DiscoveredAgent]) -> bytes:
    """Serialize a list of DiscoveredAgent to JSON bytes for CacheStoreABC."""
    payload = [
        {
            "agent_id": a.agent_id,
            "name": a.name,
            "skills": a.skills,
            "status": a.status,
            "inbox": a.inbox,
            "metadata": a.metadata,
        }
        for a in agents
    ]
    return json.dumps(payload).encode()


def _deserialize_agents(data: bytes) -> list[DiscoveredAgent]:
    """Deserialize JSON bytes back to a list of DiscoveredAgent."""
    payload = json.loads(data)
    return [
        DiscoveredAgent(
            agent_id=d["agent_id"],
            name=d["name"],
            skills=d["skills"],
            status=d["status"],
            inbox=d["inbox"],
            metadata=d["metadata"],
        )
        for d in payload
    ]
