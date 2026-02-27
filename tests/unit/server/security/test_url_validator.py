"""Comprehensive tests for SSRF URL validator (Issue #1596).

Tests cover:
- RFC 1918 private ranges (10.x, 172.16-31.x, 192.168.x)
- Loopback (127.x, ::1)
- Link-local / cloud metadata (169.254.x)
- IPv6 equivalents (fc00::/7, fe80::/10)
- Carrier-grade NAT (100.64.x)
- Allowed external URLs
- Scheme validation
- Missing hostname / unresolvable hosts
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from nexus.server.security.url_validator import validate_outbound_url


class TestBlockedIPRanges:
    """URLs resolving to private/internal IPs MUST be blocked."""

    @pytest.mark.parametrize(
        "url,description",
        [
            ("http://127.0.0.1/", "IPv4 loopback"),
            ("http://127.0.0.255/", "IPv4 loopback range"),
            ("http://10.0.0.1/", "RFC 1918 Class A"),
            ("http://10.255.255.255/", "RFC 1918 Class A end"),
            ("http://172.16.0.1/", "RFC 1918 Class B start"),
            ("http://172.31.255.255/", "RFC 1918 Class B end"),
            ("http://192.168.0.1/", "RFC 1918 Class C"),
            ("http://192.168.255.255/", "RFC 1918 Class C end"),
            ("http://169.254.169.254/latest/meta-data/", "AWS metadata"),
            ("http://169.254.0.1/", "Link-local"),
            ("http://0.0.0.1/", "'This' network"),
            ("http://100.64.0.1/", "Carrier-grade NAT"),
            ("http://198.18.0.1/", "Benchmarking RFC 2544"),
        ],
    )
    def test_blocked_ipv4_ranges(self, url: str, description: str) -> None:
        """Each RFC 1918/special IPv4 range is blocked."""
        # Mock DNS to return the IP directly (avoid real DNS)
        ip = url.split("//")[1].split("/")[0].split(":")[0]
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [
                (2, 1, 6, "", (ip, 80)),  # AF_INET, SOCK_STREAM, TCP
            ]
            with pytest.raises(ValueError, match="blocked IP range"):
                validate_outbound_url(url)

    @pytest.mark.parametrize(
        "url,ip,description",
        [
            ("http://[::1]/", "::1", "IPv6 loopback"),
            ("http://ipv6host/", "fc00::1", "IPv6 ULA"),
            ("http://ipv6host/", "fe80::1", "IPv6 link-local"),
            ("http://ipv6host/", "fd00:ec2::254", "AWS IPv6 metadata"),
        ],
    )
    def test_blocked_ipv6_ranges(self, url: str, ip: str, description: str) -> None:
        """IPv6 private/reserved ranges are blocked."""
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [
                (10, 1, 6, "", (ip, 80, 0, 0)),  # AF_INET6
            ]
            with pytest.raises(ValueError, match="blocked IP range"):
                validate_outbound_url(url)


class TestAllowedURLs:
    """Valid external URLs MUST be allowed."""

    @pytest.mark.parametrize(
        "url,ip",
        [
            ("https://example.com/webhook", "93.184.216.34"),
            ("http://api.stripe.com/v1/webhooks", "104.18.6.126"),
            ("https://hooks.slack.com/services/T00/B00/xxx", "34.237.47.128"),
        ],
    )
    def test_allowed_external_urls(self, url: str, ip: str) -> None:
        """External public IPs are allowed."""
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [
                (2, 1, 6, "", (ip, 443)),
            ]
            result = validate_outbound_url(url)
            assert result == url


class TestSchemeValidation:
    """Only http:// and https:// schemes are allowed."""

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "ftp://internal.server/data",
            "gopher://evil.com/",
            "data:text/html,<script>alert(1)</script>",
            "javascript:alert(1)",
        ],
    )
    def test_blocked_schemes(self, url: str) -> None:
        with pytest.raises(ValueError, match="scheme not allowed"):
            validate_outbound_url(url)


class TestEdgeCases:
    """Edge cases and error handling."""

    def test_empty_url(self) -> None:
        with pytest.raises(ValueError):
            validate_outbound_url("")

    def test_no_hostname(self) -> None:
        with pytest.raises(ValueError, match="no hostname"):
            validate_outbound_url("http:///path")

    def test_unresolvable_hostname(self) -> None:
        with (
            patch("socket.getaddrinfo", side_effect=__import__("socket").gaierror("No DNS")),
            pytest.raises(ValueError, match="Cannot resolve"),
        ):
            validate_outbound_url("http://does-not-exist.invalid/")

    def test_no_dns_records(self) -> None:
        with (
            patch("socket.getaddrinfo", return_value=[]),
            pytest.raises(ValueError, match="No DNS records"),
        ):
            validate_outbound_url("http://empty-dns.example.com/")
