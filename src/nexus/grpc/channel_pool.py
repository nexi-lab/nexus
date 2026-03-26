"""PeerChannelPool — shared gRPC channel pool for peer-to-peer IPC.

One persistent channel per peer address. gRPC channels multiplex HTTP/2
internally, so a single channel handles many concurrent RPCs efficiently.

Replaces per-call build_peer_channel() + close() in the data path.
Control-plane operations (e.g., try_delete) can still use one-shot channels.

Issue #1576: DT_PIPE/DT_STREAM federation fast-path.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

import grpc

from nexus.grpc.channel_factory import build_peer_channel

if TYPE_CHECKING:
    from nexus.security.tls.config import ZoneTlsConfig

logger = logging.getLogger(__name__)


class PeerChannelPool:
    """Shared gRPC channel pool for peer-to-peer IPC. One channel per address.

    Thread-safe: channels may be requested from RPC worker threads and the
    asyncio event loop concurrently.

    TLS config is set via :meth:`set_tls_config` (deferred — not available at
    NexusFS init time, only after federation bootstrap).
    """

    def __init__(self, tls_config: "ZoneTlsConfig | None" = None) -> None:
        self._channels: dict[str, grpc.Channel] = {}
        self._tls_config: ZoneTlsConfig | None = tls_config
        self._lock = threading.Lock()

    def get(self, address: str) -> grpc.Channel:
        """Get or create a persistent channel to *address*."""
        channel = self._channels.get(address)
        if channel is not None:
            return channel
        with self._lock:
            # Double-check after acquiring lock
            channel = self._channels.get(address)
            if channel is not None:
                return channel
            channel = build_peer_channel(address, self._tls_config)
            self._channels[address] = channel
            logger.debug("PeerChannelPool: created channel to %s", address)
            return channel

    def set_tls_config(self, config: "ZoneTlsConfig") -> None:
        """Set or update TLS config. Existing channels are NOT replaced."""
        self._tls_config = config

    def close_all(self) -> None:
        """Close all pooled channels. Called on shutdown."""
        with self._lock:
            for addr, ch in self._channels.items():
                try:
                    ch.close()
                except Exception:  # noqa: BLE001
                    logger.debug("PeerChannelPool: error closing channel to %s", addr)
            self._channels.clear()
        logger.debug("PeerChannelPool: all channels closed")
