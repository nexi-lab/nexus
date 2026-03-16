"""SSRF-safe URL validation for outbound HTTP requests (Issue #1596).

Validates URLs before fetching to prevent Server-Side Request Forgery attacks.
Blocks RFC 1918 private ranges, loopback, link-local, and cloud metadata IPs.

Usage::

    from nexus.lib.security import validate_outbound_url

    validate_outbound_url("https://example.com/webhook")   # OK
    validate_outbound_url("http://169.254.169.254/meta")   # raises ValueError
    validate_outbound_url("http://10.0.0.1/internal")      # raises ValueError
"""

import ipaddress
import logging
import socket
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# IP networks that MUST NOT be reachable from outbound requests.
# Covers: RFC 1918 private, loopback, link-local, cloud metadata, IPv6 equivalents.
BLOCKED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    # IPv4
    ipaddress.IPv4Network("0.0.0.0/8"),  # "This" network
    ipaddress.IPv4Network("10.0.0.0/8"),  # RFC 1918 Class A
    ipaddress.IPv4Network("100.64.0.0/10"),  # Carrier-grade NAT (RFC 6598)
    ipaddress.IPv4Network("127.0.0.0/8"),  # Loopback
    ipaddress.IPv4Network("169.254.0.0/16"),  # Link-local (includes cloud metadata)
    ipaddress.IPv4Network("172.16.0.0/12"),  # RFC 1918 Class B
    ipaddress.IPv4Network("192.0.0.0/24"),  # IETF protocol assignments
    ipaddress.IPv4Network("192.168.0.0/16"),  # RFC 1918 Class C
    ipaddress.IPv4Network("198.18.0.0/15"),  # Benchmarking (RFC 2544)
    # IPv6
    ipaddress.IPv6Network("::1/128"),  # Loopback
    ipaddress.IPv6Network("fc00::/7"),  # Unique local address (ULA)
    ipaddress.IPv6Network("fe80::/10"),  # Link-local
    ipaddress.IPv6Network("fd00:ec2::254/128"),  # AWS IPv6 metadata
)

ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})


def validate_outbound_url(url: str) -> tuple[str, list[str]]:
    """Validate that a URL is safe for outbound HTTP requests.

    Resolves DNS once and checks the resolved IP against blocked ranges.
    Returns the validated URL and the resolved IPs so callers can pin the
    connection to the validated addresses (prevents DNS rebinding TOCTOU).

    Args:
        url: The URL to validate.

    Returns:
        Tuple of (validated_url, resolved_ips).  Callers should configure
        their HTTP client to connect only to the returned IPs.

    Raises:
        ValueError: If the URL targets a blocked network, has an invalid scheme,
            or cannot be resolved.
    """
    parsed = urlparse(url)

    # 1. Scheme check
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise ValueError(f"URL scheme not allowed: {parsed.scheme!r}")

    # 2. Hostname extraction
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL has no hostname")

    # 3. Resolve DNS once and pin resolved IPs
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addr_infos = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValueError(f"Cannot resolve hostname: {hostname!r}") from exc

    if not addr_infos:
        raise ValueError(f"No DNS records for hostname: {hostname!r}")

    # 4. Check every resolved IP against blocked networks
    resolved_ips: list[str] = []
    for _family, _type, _proto, _canonname, sockaddr in addr_infos:
        ip = ipaddress.ip_address(sockaddr[0])
        for network in BLOCKED_NETWORKS:
            if ip in network:
                logger.warning(
                    "SSRF blocked: URL %r resolved to %s (in %s)",
                    url,
                    ip,
                    network,
                )
                raise ValueError(f"URL resolves to blocked IP range: {ip} in {network}")
        resolved_ips.append(str(ip))

    return url, resolved_ips
