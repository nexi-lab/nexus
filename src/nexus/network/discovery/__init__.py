"""Peer discovery for Nexus federation.

Linux analogy: ``net/dns_resolver/`` — finding peers on the network.

Future home for:
    - mDNS-based LAN discovery (auto-find Nexus nodes on same network)
    - Headscale coordination (NAT traversal for cross-network federation)
    - Static peer registry (manual endpoint configuration)

Currently, peer endpoints are configured manually via ``nexus network add-peer``.
"""
