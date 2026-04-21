"""SSRF-safe URL validation for outbound HTTP requests (Issues #1596, #3792).

Validates URLs before fetching to prevent Server-Side Request Forgery attacks.
Blocks RFC 1918 private ranges, loopback, link-local, and cloud metadata IPs.

Returns a ``ValidatedURL`` NamedTuple so callers can pin the resolved IPs
at connect time (see ``nexus.lib.security.ssrf_transport``), preventing
DNS rebinding TOCTOU.

Usage::

    from nexus.lib.security import validate_outbound_url, SSRFBlocked

    try:
        validated = validate_outbound_url("https://example.com/webhook")
    except SSRFBlocked as exc:
        log.warning("SSRF blocked: %s (ip=%s cidr=%s)", exc.reason, exc.ip, exc.cidr)
        raise
"""

import ipaddress
import logging
import socket
from collections.abc import Sequence
from typing import NamedTuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class SSRFBlocked(ValueError):
    """Raised when an outbound URL targets a blocked network or metadata IP.

    Subclass of ``ValueError`` so existing ``except ValueError:`` call sites
    continue to catch blocks. New call sites catch ``SSRFBlocked`` directly
    to access the structured fields for auditing.
    """

    def __init__(
        self,
        url: str,
        *,
        reason: str,
        ip: str | None = None,
        cidr: str | None = None,
    ) -> None:
        self.url = url
        self.reason = reason
        self.ip = ip
        self.cidr = cidr
        super().__init__(f"SSRF blocked: {reason} (url={url}, ip={ip})")


class ValidatedURL(NamedTuple):
    """A URL that has passed SSRF validation, with resolved IPs pinned.

    Callers should use attribute access (``result.url``,
    ``result.resolved_ips``, ``result.hostname``) or 3-tuple unpacking.
    Existing callers that previously unpacked the old ``(url, ips)``
    two-tuple return were updated to either discard the return (calls
    made purely for their raising side effect) or use attribute access.
    """

    url: str
    resolved_ips: tuple[str, ...]
    hostname: str


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

# Explicit cloud metadata IPs. Most are already covered by link-local
# (169.254.0.0/16), but listing them here serves as self-documentation and
# catches Alibaba Cloud (100.100.100.200) which is outside link-local.
CLOUD_METADATA_IPS: frozenset[ipaddress.IPv4Address | ipaddress.IPv6Address] = frozenset(
    {
        # AWS IMDS (also GCP, Azure, OCI, DigitalOcean)
        ipaddress.IPv4Address("169.254.169.254"),
        # AWS IPv6 IMDS
        ipaddress.IPv6Address("fd00:ec2::254"),
        # Alibaba Cloud metadata
        ipaddress.IPv4Address("100.100.100.200"),
    }
)


def validate_outbound_url(
    url: str,
    *,
    allow_private: bool = False,
    extra_deny_cidrs: Sequence[str] = (),
) -> ValidatedURL:
    """Validate that a URL is safe for outbound HTTP requests.

    Resolves DNS once and checks the resolved IP against blocked ranges.
    Callers should pass the returned ``resolved_ips`` to
    ``PinnedResolverTransport`` so the actual connect uses the validated
    IPs (prevents DNS rebinding TOCTOU).

    Args:
        url: The URL to validate.
        allow_private: If True, skip RFC1918 / ULA private range checks.
            Metadata and loopback are always blocked regardless. (Wired
            in a later task — currently accepted but unused.)
        extra_deny_cidrs: Additional CIDRs to reject (e.g. internal
            service mesh). Each entry must parse as an ip_network. (Wired
            in a later task — currently accepted but unused.)

    Returns:
        ValidatedURL(url, resolved_ips, hostname).

    Raises:
        SSRFBlocked: URL targets a blocked network or uses a disallowed scheme.
        ValueError: URL is malformed or DNS resolution fails (transient,
            not a security signal).
    """
    # Silence unused-for-now parameters (wired by later tasks)
    _ = (allow_private, extra_deny_cidrs)

    parsed = urlparse(url)

    if parsed.scheme not in ALLOWED_SCHEMES:
        raise SSRFBlocked(url, reason="scheme_not_allowed")

    if parsed.username or parsed.password:
        raise SSRFBlocked(url, reason="userinfo_not_allowed")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL has no hostname")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addr_infos = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValueError(f"Cannot resolve hostname: {hostname!r}") from exc

    if not addr_infos:
        raise ValueError(f"No DNS records for hostname: {hostname!r}")

    resolved_ips: list[str] = []
    for _family, _type, _proto, _canonname, sockaddr in addr_infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if ip in CLOUD_METADATA_IPS:
            logger.warning("SSRF blocked: %r resolved to metadata IP %s", url, ip)
            raise SSRFBlocked(url, reason="cloud_metadata", ip=str(ip))
        for network in BLOCKED_NETWORKS:
            if ip in network:
                logger.warning(
                    "SSRF blocked: URL %r resolved to %s (in %s)",
                    url,
                    ip,
                    network,
                )
                raise SSRFBlocked(
                    url,
                    reason="blocked_network",
                    ip=str(ip),
                    cidr=str(network),
                )
        resolved_ips.append(str(ip))

    return ValidatedURL(
        url=url,
        resolved_ips=tuple(resolved_ips),
        hostname=hostname,
    )
