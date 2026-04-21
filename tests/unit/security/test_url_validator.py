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

from unittest.mock import patch

import pytest

from nexus.lib.security.url_validator import (
    SSRFBlocked,
    validate_outbound_url,
)


class TestSSRFBlockedException:
    """SSRFBlocked is a ValueError subclass with structured fields."""

    def test_ssrf_blocked_is_value_error_subclass(self) -> None:
        exc = SSRFBlocked("http://10.0.0.1/", reason="private_ip", ip="10.0.0.1", cidr="10.0.0.0/8")
        assert isinstance(exc, ValueError)

    def test_ssrf_blocked_exposes_fields(self) -> None:
        exc = SSRFBlocked("http://10.0.0.1/", reason="private_ip", ip="10.0.0.1", cidr="10.0.0.0/8")
        assert exc.url == "http://10.0.0.1/"
        assert exc.reason == "private_ip"
        assert exc.ip == "10.0.0.1"
        assert exc.cidr == "10.0.0.0/8"

    def test_ssrf_blocked_message_includes_url_and_ip(self) -> None:
        exc = SSRFBlocked("http://10.0.0.1/", reason="private_ip", ip="10.0.0.1")
        assert "10.0.0.1" in str(exc)
        assert "http://10.0.0.1/" in str(exc)


class TestValidatedURL:
    """ValidatedURL NamedTuple exposing url, resolved_ips, and hostname."""

    def test_three_tuple_unpack_with_hostname(self) -> None:
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(2, 1, 6, "", ("93.184.216.34", 443))]
            result = validate_outbound_url("https://example.com/")
            url, ips, hostname = result
            assert hostname == "example.com"

    def test_attribute_access(self) -> None:
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(2, 1, 6, "", ("93.184.216.34", 443))]
            result = validate_outbound_url("https://example.com/")
            assert result.url == "https://example.com/"
            assert result.hostname == "example.com"
            assert result.resolved_ips == ("93.184.216.34",)


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
        ip = url.split("//")[1].split("/")[0].split(":")[0]
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [
                (2, 1, 6, "", (ip, 80)),
            ]
            with pytest.raises(SSRFBlocked):
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
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [
                (10, 1, 6, "", (ip, 80, 0, 0)),
            ]
            with pytest.raises(SSRFBlocked):
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
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [
                (2, 1, 6, "", (ip, 443)),
            ]
            result = validate_outbound_url(url)
            assert result.url == url
            assert ip in result.resolved_ips


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
        with pytest.raises(SSRFBlocked):
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
