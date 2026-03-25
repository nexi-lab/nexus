"""Hostname-based peer address parsing for Raft clusters.

Derives deterministic node IDs from hostnames using SHA-256, eliminating
the need for manually assigned NEXUS_NODE_ID environment variables.

The algorithm matches the Rust implementation in transport/mod.rs:
    SHA-256(hostname) -> first 8 bytes as little-endian u64 -> 0 mapped to 1
"""

from __future__ import annotations

import hashlib
import socket
from dataclasses import dataclass


def hostname_to_node_id(hostname: str) -> int:
    """Derive a deterministic node ID from a hostname.

    Uses SHA-256 of the hostname, takes the first 8 bytes as a
    little-endian unsigned 64-bit integer. Maps 0 to 1 (raft-rs
    reserves 0 as "no node").

    Args:
        hostname: The hostname string (e.g., "nexus-1", "witness").

    Returns:
        A non-zero u64 node ID.
    """
    digest = hashlib.sha256(hostname.encode("utf-8")).digest()
    value = int.from_bytes(digest[:8], byteorder="little", signed=False)
    return value if value != 0 else 1


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

        Args:
            s: Peer address string like "nexus-1:2126".

        Returns:
            PeerAddress with hostname-derived node_id.

        Raises:
            ValueError: If the string is not in "host:port" format.
        """
        s = s.strip()
        if ":" not in s:
            raise ValueError(f"Expected 'host:port', got '{s}'")
        host, port_str = s.rsplit(":", 1)
        try:
            port = int(port_str)
        except ValueError:
            raise ValueError(f"Invalid port in '{s}'") from None
        nid = hostname_to_node_id(host)
        return cls(hostname=host, port=port, node_id=nid)

    @classmethod
    def parse_peer_list(cls, peers_str: str) -> list[PeerAddress]:
        """Parse a comma-separated list of "host:port" peers.

        Args:
            peers_str: Comma-separated peer addresses (e.g., "nexus-1:2126,nexus-2:2126").

        Returns:
            List of PeerAddress instances.
        """
        if not peers_str or not peers_str.strip():
            return []
        return [cls.parse(p) for p in peers_str.split(",") if p.strip()]

    @classmethod
    def from_local(cls, port: int = 2126) -> PeerAddress:
        """Create a PeerAddress for the local machine.

        Args:
            port: Local port number (default: 2126).

        Returns:
            PeerAddress using socket.gethostname() as hostname.
        """
        hostname = socket.gethostname()
        nid = hostname_to_node_id(hostname)
        return cls(hostname=hostname, port=port, node_id=nid)

    @property
    def grpc_target(self) -> str:
        """Return "host:port" for gRPC connection target."""
        return f"{self.hostname}:{self.port}"

    def to_raft_peer_str(self) -> str:
        """Return "id@host:port" for Raft peer configuration."""
        return f"{self.node_id}@{self.hostname}:{self.port}"
