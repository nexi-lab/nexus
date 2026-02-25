"""Nexus Network Subsystem — transport infrastructure for federation.

Linux analogy: ``net/`` — the networking protocol stack.

This package owns everything ABOUT the network (connectivity, tunnels,
transport, discovery), not things that merely USE the network (Raft client,
REMOTE profile, WebSocket manager — those stay in their domain packages).

Submodules:
    constants     Network defaults (ports, subnets, timeouts)
    wireguard     WireGuard tunnel management (``net/wireguard/``)
    transport/    RPC transport utilities (``net/core/sock.c``)
    discovery/    Peer discovery — mDNS, broadcast (``net/dns_resolver/``)

Architecture context (KERNEL-ARCHITECTURE.md §6 Communication):
    This package provides Layer 3 (IP) connectivity that the System tier
    (gRPC, HTTP) runs on top of.  WireGuard creates encrypted tunnels
    between nodes; RPC transport provides connection pooling and retry.
"""
