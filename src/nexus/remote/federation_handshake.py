"""Federation handshake: authenticate to hub and discover zone grants (issue #3786)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from nexus.contracts.exceptions import HandshakeAuthError, HandshakeConnectionError, NexusError
from nexus.remote.rpc_transport import RPCTransport

logger = logging.getLogger(__name__)

DEFAULT_HANDSHAKE_TIMEOUT_SECONDS = 5.0
DEFAULT_HANDSHAKE_CONNECT_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class HubZoneGrant:
    zone_id: str
    permission: str  # "r" or "rw"


@dataclass
class HubSession:
    transport: RPCTransport
    zones: list[HubZoneGrant]


class FederationHandshake:
    """Authenticates to a Nexus hub and discovers the caller's zone grants."""

    def __init__(
        self,
        hub_url: str,
        token: str,
        *,
        timeout: float = DEFAULT_HANDSHAKE_TIMEOUT_SECONDS,
        connect_timeout: float = DEFAULT_HANDSHAKE_CONNECT_TIMEOUT_SECONDS,
    ) -> None:
        self._hub_url = hub_url
        self._token = token
        self._timeout = timeout
        self._connect_timeout = connect_timeout

    def run(self) -> HubSession:
        """Connect to hub, call federation_client_whoami, return HubSession.

        Raises:
            HandshakeAuthError: Hub returned 401 (bad or expired token).
            HandshakeConnectionError: Hub is unreachable.
        """
        try:
            transport = RPCTransport(
                self._hub_url,
                auth_token=self._token,
                timeout=self._timeout,
                connect_timeout=self._connect_timeout,
            )
        except ValueError as exc:
            raise HandshakeConnectionError(str(exc)) from exc
        try:
            result = transport.call_rpc(
                "federation_client_whoami",
                read_timeout=self._timeout,
            )
        except NexusError as exc:
            if getattr(exc, "status_code", None) == 401:
                raise HandshakeAuthError(str(exc)) from exc
            raise HandshakeConnectionError(str(exc)) from exc
        except OSError as exc:
            raise HandshakeConnectionError(str(exc)) from exc

        zones = [
            HubZoneGrant(zone_id=z["zone_id"], permission=z["permission"])
            for z in result.get("zones", [])
        ]
        logger.debug(
            "FederationHandshake: connected to %s, got %d zone grant(s)",
            self._hub_url,
            len(zones),
        )
        return HubSession(transport=transport, zones=zones)
