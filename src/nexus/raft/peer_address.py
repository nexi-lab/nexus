"""Hostname-based peer address parsing for Raft clusters.

Derives deterministic node IDs from hostnames using SHA-256, eliminating
the need for manually assigned NEXUS_NODE_ID environment variables.

The SSOT algorithm lives in Rust (``nexus_kernel.hostname_to_node_id``,
``rust/raft/src/transport/mod.rs``); this module re-exports it so
Python callers keep their existing import path.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass

from nexus_kernel import hostname_to_node_id as _rust_hostname_to_node_id


def hostname_to_node_id(hostname: str) -> int:
    """Derive a deterministic node ID from a hostname (SHA-256 → u64).

    Delegates to the native Rust helper so node IDs are identical
    across Python ``ZoneManager`` callers and the Rust raft layer.
    """
    value: int = _rust_hostname_to_node_id(hostname)
    return value


@dataclass(frozen=True)
class PeerAddress:
    """Parsed peer address with hostname-derived node ID.

    Attributes:
        hostname: The peer's hostname (e.g., "nexus-1").
        port: The peer's port number (e.g., 2126).
        node_id: Deterministic ID derived from hostname via SHA-256.
    """

    hostname: str
    port: int
    node_id: int

    @classmethod
    def parse(cls, s: str) -> PeerAddress:
        """Parse a "host:port" string into a PeerAddress.

        Raises ``ValueError`` if the string is not in ``host:port`` form.
        """
        s = s.strip()
        if ":" not in s:
            raise ValueError(f"Expected 'host:port', got '{s}'")
        host, port_str = s.rsplit(":", 1)
        try:
            port = int(port_str)
        except ValueError:
            raise ValueError(f"Invalid port in '{s}'") from None
        return cls(hostname=host, port=port, node_id=hostname_to_node_id(host))

    @classmethod
    def parse_peer_list(cls, peers_str: str) -> list[PeerAddress]:
        """Parse a comma-separated list of ``host:port`` peers."""
        if not peers_str or not peers_str.strip():
            return []
        return [cls.parse(p) for p in peers_str.split(",") if p.strip()]

    @classmethod
    def from_local(cls, port: int = 2126) -> PeerAddress:
        """Create a PeerAddress for the local machine via ``socket.gethostname()``."""
        hostname = socket.gethostname()
        return cls(hostname=hostname, port=port, node_id=hostname_to_node_id(hostname))

    @property
    def grpc_target(self) -> str:
        """Return ``host:port`` for gRPC connection target."""
        return f"{self.hostname}:{self.port}"

    def to_raft_peer_str(self) -> str:
        """Return ``id@host:port`` for Raft peer configuration."""
        return f"{self.node_id}@{self.hostname}:{self.port}"
