"""Network-specific constants for the Nexus network subsystem.

Centralizes network defaults that are specific to the network layer.
General server constants (DEFAULT_NEXUS_PORT, DEFAULT_GRPC_BIND_ADDR)
remain in ``nexus.contracts.constants`` — they predate this package.
"""

# =============================================================================
# WireGuard Mesh Defaults
# =============================================================================

WG_SUBNET = "10.99.0"
"""WireGuard mesh subnet.  Avoids common LAN ranges (192.168.x, 10.0.x, 172.16.x)."""

WG_DEFAULT_PORT = 51820
"""Default WireGuard listen port (upstream default)."""

WG_INTERFACE = "wg0"
"""Default WireGuard interface name."""

# =============================================================================
# Future: Discovery, Headscale, etc.
# =============================================================================
