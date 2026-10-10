"""Zone routes and capability snapshots reported by their Search hosts."""

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ZoneSearchCapabilities:
    """Search modes and embedding identity reported by a zone's host."""

    zone_id: str
    search_modes: tuple[str, ...]
    device_tier: str = "unknown"
    embedding_model: str | None = None
    embedding_dimensions: int = 0
    has_graph: bool = False

    @property
    def supports_semantic(self) -> bool:
        return "semantic" in self.search_modes or "hybrid" in self.search_modes

    @property
    def supports_keyword(self) -> bool:
        return "keyword" in self.search_modes or "hybrid" in self.search_modes


class ZoneSearchRegistry:
    """Route federated searches and retain discovered capabilities in memory."""

    def __init__(self, default_daemon: Any | None = None) -> None:
        """Initialize registry with optional default daemon.

        Args:
            default_daemon: Fallback daemon used for zones without
                a dedicated daemon.
        """
        self._default_daemon = default_daemon
        self._daemons: dict[str, Any] = {}
        self._capabilities: dict[str, ZoneSearchCapabilities] = {}
        self._transports: dict[str, Any] = {}

    def register(
        self,
        zone_id: str,
        daemon: Any,
        capabilities: ZoneSearchCapabilities | None = None,
    ) -> None:
        """Register a daemon for a zone.

        Args:
            zone_id: Zone identifier.
            daemon: SearchDaemon instance for this zone.
            capabilities: Capabilities reported by the host, when known.
        """
        self._daemons[zone_id] = daemon
        self._transports.pop(zone_id, None)
        if capabilities is not None:
            self._capabilities[zone_id] = capabilities
        else:
            self._capabilities.pop(zone_id, None)
        logger.info("[ZONE-REGISTRY] Registered local zone %s", zone_id)

    def register_remote(
        self,
        zone_id: str,
        transport: Any,
        capabilities: ZoneSearchCapabilities | None = None,
    ) -> None:
        """Register a remote zone with its gRPC transport.

        The transport supports peer capability discovery. Query fan-out
        uses the owning host's configured remote routes.

        Args:
            zone_id: Remote zone identifier.
            transport: RPCTransport instance connected to the remote node.
            capabilities: Zone capabilities (discovered via GetSearchCapabilities).
        """
        self._transports[zone_id] = transport
        self._daemons.pop(zone_id, None)
        if capabilities is not None:
            self._capabilities[zone_id] = capabilities
        else:
            self._capabilities.pop(zone_id, None)
        logger.info("[ZONE-REGISTRY] Registered remote zone %s", zone_id)

    def get_transport(self, zone_id: str) -> Any | None:
        """Get RPCTransport for a remote zone, or None if local."""
        return self._transports.get(zone_id)

    def is_remote(self, zone_id: str) -> bool:
        """Check if a zone is served by a remote transport."""
        return zone_id in self._transports

    def unregister(self, zone_id: str) -> None:
        """Remove a zone's daemon from the registry."""
        self._daemons.pop(zone_id, None)
        self._capabilities.pop(zone_id, None)
        self._transports.pop(zone_id, None)

    def get_daemon(self, zone_id: str) -> Any | None:
        """Get the daemon for a zone, falling back to default.

        Returns None only if no daemon is registered AND no default exists.
        """
        return self._daemons.get(zone_id, self._default_daemon)

    def get_capabilities(self, zone_id: str) -> ZoneSearchCapabilities | None:
        """Get capabilities for a zone, or None if unknown."""
        return self._capabilities.get(zone_id)

    def list_zones(self) -> list[str]:
        """List all zone IDs with registered daemons."""
        return list(self._daemons.keys())

    def has_zone(self, zone_id: str) -> bool:
        """Check if a zone has a registered daemon (not counting default)."""
        return zone_id in self._daemons

    async def discover_remote_capabilities(
        self,
        zone_id: str,
        raft_client: Any,
    ) -> ZoneSearchCapabilities:
        """Read host-reported capabilities. Failed discovery leaves them unknown."""
        try:
            raw = await raft_client.get_search_capabilities(zone_id=zone_id)
            if raw["zone_id"] != zone_id:
                raise ValueError("Search capabilities belong to a different zone")
            caps = ZoneSearchCapabilities(
                zone_id=zone_id,
                device_tier=raw["device_tier"],
                search_modes=tuple(raw["search_modes"]),
                embedding_model=raw["embedding_model"] or None,
                embedding_dimensions=raw["embedding_dimensions"],
                has_graph=raw["has_graph"],
            )
        except Exception:
            self._capabilities.pop(zone_id, None)
            raise
        self._capabilities[zone_id] = caps
        return caps

    @property
    def default_daemon(self) -> Any | None:
        return self._default_daemon

    @default_daemon.setter
    def default_daemon(self, daemon: Any) -> None:
        self._default_daemon = daemon
