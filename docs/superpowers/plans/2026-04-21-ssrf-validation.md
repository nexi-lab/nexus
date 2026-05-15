# SSRF Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden the existing outbound URL validator and wire it (with DNS pinning) into the MCP HTTP/SSE transport, closing the DNS rebinding TOCTOU and adding typed exceptions, explicit cloud metadata coverage, and audit events.

**Architecture:** Extend `nexus.lib.security.url_validator` in place — add `SSRFBlocked(ValueError)`, `ValidatedURL` NamedTuple, explicit cloud metadata IP set, IPv4-mapped IPv6 normalization, mixed-resolution rejection, userinfo rejection, and `allow_private` / `extra_deny_cidrs` kwargs. Add a new `nexus.lib.security.ssrf_transport` module exposing `PinnedResolverTransport` (httpx subclass) and a `make_pinned_client_factory()` helper compatible with `mcp.shared._httpx_utils.McpHttpClientFactory`. Wire the factory into `mount.py`'s SSE client creation. Add a `SSRFConfig` pydantic model read from `NexusConfig.security.ssrf`. Emit `security.ssrf_blocked` audit events at the MCP call site.

**Tech Stack:** Python 3.12, httpx, pydantic v2, pytest, `mcp` client library (`sse_client`, `streamablehttp_client`), existing nexus event bus.

**Spec:** `docs/superpowers/specs/2026-04-21-ssrf-validation-design.md`

---

## File Structure

### Files to modify
- `src/nexus/lib/security/url_validator.py` — add exception, NamedTuple, metadata set, new kwargs, new checks
- `src/nexus/lib/security/__init__.py` — re-export `SSRFBlocked`, `ValidatedURL`
- `src/nexus/config.py` — add `SSRFConfig`, `SecurityConfig`, wire onto `NexusConfig`
- `src/nexus/bricks/mcp/mount.py` — validate URLs, use pinned httpx factory in SSE client calls
- `tests/unit/security/test_url_validator.py` — extend with new vectors

### Files to create
- `src/nexus/lib/security/ssrf_transport.py` — `PinnedResolverTransport`, `make_pinned_client_factory`
- `tests/unit/security/test_ssrf_transport.py` — transport unit tests
- `tests/integration/mcp/test_ssrf_wiring.py` — MCP wiring integration tests
- `tests/integration/mcp/__init__.py` — package marker (if missing)

### Why these boundaries
- **Validator stays in `nexus.lib.security.url_validator`** — existing consumers keep working; subclassing `ValueError` preserves `except ValueError:` call sites.
- **Transport helper in its own module** (`ssrf_transport.py`) — httpx dependency is isolated from the pure validator; the validator remains usable from any tier without a heavy httpx dep path.
- **Config split:** `SSRFConfig` under `SecurityConfig` under `NexusConfig` mirrors the yaml shape (`security.ssrf.*`) and keeps related knobs clustered for future security settings.
- **MCP wiring in `mount.py`** — the file already owns `_create_sse_client`; validation happens where URLs are consumed.

---

## Task 1: Add `SSRFBlocked` exception and `ValidatedURL` NamedTuple

**Files:**
- Modify: `src/nexus/lib/security/url_validator.py:1-100`
- Modify: `src/nexus/lib/security/__init__.py`
- Test: `tests/unit/security/test_url_validator.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/security/test_url_validator.py`:

```python
from nexus.lib.security.url_validator import (
    SSRFBlocked,
    ValidatedURL,
    validate_outbound_url,
)


class TestSSRFBlockedException:
    """SSRFBlocked is a ValueError subclass with structured fields."""

    def test_ssrf_blocked_is_value_error_subclass(self) -> None:
        exc = SSRFBlocked("http://10.0.0.1/", reason="private_ip", ip="10.0.0.1", cidr="10.0.0.0/8")
        assert isinstance(exc, ValueError)

    def test_ssrf_blocked_exposes_fields(self) -> None:
        exc = SSRFBlocked(
            "http://10.0.0.1/", reason="private_ip", ip="10.0.0.1", cidr="10.0.0.0/8"
        )
        assert exc.url == "http://10.0.0.1/"
        assert exc.reason == "private_ip"
        assert exc.ip == "10.0.0.1"
        assert exc.cidr == "10.0.0.0/8"

    def test_ssrf_blocked_message_includes_url_and_ip(self) -> None:
        exc = SSRFBlocked("http://10.0.0.1/", reason="private_ip", ip="10.0.0.1")
        assert "10.0.0.1" in str(exc)
        assert "http://10.0.0.1/" in str(exc)


class TestValidatedURL:
    """ValidatedURL NamedTuple with backward-compatible 2-tuple unpacking."""

    def test_two_tuple_unpack_backward_compat(self) -> None:
        # Existing callers do: _url, _ips = validate_outbound_url(x)
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(2, 1, 6, "", ("93.184.216.34", 443))]
            result = validate_outbound_url("https://example.com/")
            url, ips = result  # two-tuple unpack must still work
            assert url == "https://example.com/"
            assert "93.184.216.34" in ips

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
            assert result.resolved_ips == ["93.184.216.34"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/security/test_url_validator.py::TestSSRFBlockedException tests/unit/security/test_url_validator.py::TestValidatedURL -v`
Expected: FAIL — `SSRFBlocked` and `ValidatedURL` not importable.

- [ ] **Step 3: Implement exception and NamedTuple**

Edit `src/nexus/lib/security/url_validator.py`:

Replace the top of the file (imports + existing content) with:

```python
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

    Field order preserves backward compatibility with callers that did
    ``url, ips = validate_outbound_url(x)`` before Issue #3792.
    """

    url: str
    resolved_ips: list[str]
    hostname: str
```

Then keep `BLOCKED_NETWORKS` and `ALLOWED_SCHEMES` as-is (next task extends them).

Update `validate_outbound_url` signature and return type. Replace the existing function body with:

```python
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
            Metadata and loopback are always blocked regardless.
        extra_deny_cidrs: Additional CIDRs to reject (e.g. internal
            service mesh). Each entry must parse as an ip_network.

    Returns:
        ValidatedURL(url, resolved_ips, hostname).

    Raises:
        SSRFBlocked: URL targets a blocked network or metadata IP.
        ValueError: URL is malformed, scheme is not http(s), or DNS
            resolution fails (transient — not a security signal).
    """
    parsed = urlparse(url)

    if parsed.scheme not in ALLOWED_SCHEMES:
        raise SSRFBlocked(url, reason="scheme_not_allowed")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL has no hostname")

    # (Subsequent tasks add: userinfo rejection, IP literal path,
    #  cloud metadata set, v4-mapped-v6 normalization, mixed-resolution
    #  rejection, extra_deny_cidrs, allow_private.)

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

    return ValidatedURL(url=url, resolved_ips=resolved_ips, hostname=hostname)
```

Update `src/nexus/lib/security/__init__.py` exports:

```python
"""Security utilities for the Nexus platform (Issues #1596, #1756, #3792).

Tier-neutral security package — usable from any Nexus layer (core, services,
server) without creating cross-tier dependency violations.
"""

from nexus.lib.security.output_validator import validate_llm_output
from nexus.lib.security.policy import InjectionAction, InjectionPolicyConfig
from nexus.lib.security.prompt_sanitizer import (
    detect_injection_patterns,
    enforce_injection_policy,
    sanitize_for_prompt,
    wrap_untrusted_data,
)
from nexus.lib.security.url_validator import (
    SSRFBlocked,
    ValidatedURL,
    validate_outbound_url,
)

__all__ = [
    "InjectionAction",
    "InjectionPolicyConfig",
    "SSRFBlocked",
    "ValidatedURL",
    "detect_injection_patterns",
    "enforce_injection_policy",
    "sanitize_for_prompt",
    "validate_llm_output",
    "validate_outbound_url",
    "wrap_untrusted_data",
]
```

Also update the existing `TestBlockedIPRanges` tests to use `SSRFBlocked` — they currently match against `ValueError, match="blocked IP range"`. Replace both parametrize blocks' `pytest.raises` to use `SSRFBlocked`:

```python
with pytest.raises(SSRFBlocked):
    validate_outbound_url(url)
```

Update the `TestSchemeValidation` test similarly:

```python
with pytest.raises(SSRFBlocked):
    validate_outbound_url(url)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/security/test_url_validator.py -v`
Expected: PASS — all existing + new tests.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/lib/security/url_validator.py src/nexus/lib/security/__init__.py tests/unit/security/test_url_validator.py
git commit -m "feat(security): typed SSRFBlocked exception and ValidatedURL tuple (#3792)

Replaces generic ValueError with SSRFBlocked(ValueError) for policy
blocks so call sites can audit the structured fields (url, reason, ip,
cidr). Returns ValidatedURL NamedTuple; existing two-tuple unpacking
at call sites continues to work."
```

---

## Task 2: Add explicit cloud metadata IPs and userinfo rejection

**Files:**
- Modify: `src/nexus/lib/security/url_validator.py`
- Test: `tests/unit/security/test_url_validator.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/security/test_url_validator.py`:

```python
class TestCloudMetadataExplicit:
    """Cloud metadata IPs are blocked via the explicit metadata set,
    catching targets like Alibaba that aren't in RFC1918/link-local."""

    @pytest.mark.parametrize(
        "ip,description",
        [
            ("169.254.169.254", "AWS/GCP/Azure/OCI/DO IMDS"),
            ("100.100.100.200", "Alibaba Cloud metadata"),
            ("fd00:ec2::254", "AWS IPv6 metadata"),
        ],
    )
    def test_cloud_metadata_blocked(self, ip: str, description: str) -> None:
        url = "http://metadata-target.example.com/"
        family = 10 if ":" in ip else 2
        sockaddr = (ip, 80, 0, 0) if ":" in ip else (ip, 80)
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(family, 1, 6, "", sockaddr)]
            with pytest.raises(SSRFBlocked) as excinfo:
                validate_outbound_url(url)
            assert excinfo.value.reason in {"cloud_metadata", "blocked_network"}
            assert excinfo.value.ip == ip


class TestUserinfoRejection:
    """URLs with userinfo are rejected to defend against parser divergence."""

    @pytest.mark.parametrize(
        "url",
        [
            "http://user@example.com/",
            "http://user:pass@example.com/",
            "http://public.com@10.0.0.1/",
        ],
    )
    def test_userinfo_rejected(self, url: str) -> None:
        with pytest.raises(SSRFBlocked) as excinfo:
            validate_outbound_url(url)
        assert excinfo.value.reason == "userinfo_not_allowed"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/security/test_url_validator.py::TestCloudMetadataExplicit tests/unit/security/test_url_validator.py::TestUserinfoRejection -v`
Expected: FAIL — userinfo currently accepted; Alibaba IP not in any block.

- [ ] **Step 3: Implement explicit metadata set and userinfo check**

Edit `src/nexus/lib/security/url_validator.py`:

Add after `ALLOWED_SCHEMES`:

```python
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
```

In `validate_outbound_url`, after the scheme check, insert userinfo rejection:

```python
    if parsed.username or parsed.password:
        raise SSRFBlocked(url, reason="userinfo_not_allowed")
```

Then in the IP loop, check `CLOUD_METADATA_IPS` *before* the CIDR loop:

```python
    for _family, _type, _proto, _canonname, sockaddr in addr_infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if ip in CLOUD_METADATA_IPS:
            logger.warning("SSRF blocked: %r resolved to metadata IP %s", url, ip)
            raise SSRFBlocked(url, reason="cloud_metadata", ip=str(ip))
        for network in BLOCKED_NETWORKS:
            if ip in network:
                logger.warning(
                    "SSRF blocked: URL %r resolved to %s (in %s)", url, ip, network,
                )
                raise SSRFBlocked(
                    url,
                    reason="blocked_network",
                    ip=str(ip),
                    cidr=str(network),
                )
        resolved_ips.append(str(ip))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/security/test_url_validator.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/lib/security/url_validator.py tests/unit/security/test_url_validator.py
git commit -m "feat(security): explicit cloud metadata set and userinfo rejection (#3792)

Adds an explicit CLOUD_METADATA_IPS frozenset covering AWS, GCP, Azure,
OCI, DigitalOcean (all 169.254.169.254) and Alibaba (100.100.100.200).
Alibaba was not covered by RFC1918/link-local. Rejects URLs containing
userinfo to block parser-divergence SSRF tricks (http://public@internal)."
```

---

## Task 3: IP literal hostnames, IPv4-mapped IPv6 normalization, mixed-resolution rejection

**Files:**
- Modify: `src/nexus/lib/security/url_validator.py`
- Test: `tests/unit/security/test_url_validator.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/security/test_url_validator.py`:

```python
class TestIPLiteralHostnames:
    """IP literal hostnames skip DNS and are checked directly."""

    @pytest.mark.parametrize(
        "url",
        [
            "http://10.0.0.1/",
            "http://169.254.169.254/",
            "http://[::1]/",
            "http://[fd00:ec2::254]/",
        ],
    )
    def test_ip_literal_blocked(self, url: str) -> None:
        # No mock: must not call DNS for IP literals
        with pytest.raises(SSRFBlocked):
            validate_outbound_url(url)


class TestIPv4MappedIPv6:
    """::ffff:a.b.c.d is normalized to a.b.c.d before category check."""

    @pytest.mark.parametrize(
        "ip",
        [
            "::ffff:127.0.0.1",
            "::ffff:10.0.0.1",
            "::ffff:169.254.169.254",
        ],
    )
    def test_v4_mapped_v6_blocked(self, ip: str) -> None:
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(10, 1, 6, "", (ip, 80, 0, 0))]
            with pytest.raises(SSRFBlocked):
                validate_outbound_url("http://dual-stack.example.com/")


class TestMixedResolution:
    """Hostnames resolving to a mix of public and private IPs are rejected."""

    def test_mixed_public_private_rejected(self) -> None:
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [
                (2, 1, 6, "", ("93.184.216.34", 80)),
                (2, 1, 6, "", ("10.0.0.1", 80)),
            ]
            with pytest.raises(SSRFBlocked):
                validate_outbound_url("http://split-horizon.example.com/")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/security/test_url_validator.py::TestIPLiteralHostnames tests/unit/security/test_url_validator.py::TestIPv4MappedIPv6 tests/unit/security/test_url_validator.py::TestMixedResolution -v`
Expected: FAIL — IP literal path currently calls DNS; v4-mapped-v6 and mixed resolution not yet handled.

- [ ] **Step 3: Implement normalization and IP-literal path**

Edit `src/nexus/lib/security/url_validator.py`. Replace the body of `validate_outbound_url` from hostname extraction through the return statement:

```python
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL has no hostname")

    # IP literal hostname: skip DNS; check the literal directly.
    # urlparse strips brackets from IPv6 literals, so ipaddress.ip_address
    # accepts them as-is.
    try:
        literal_ip = ipaddress.ip_address(hostname)
    except ValueError:
        literal_ip = None

    if literal_ip is not None:
        _check_ip(literal_ip, url, allow_private=allow_private)
        # extra_deny check uses pre-parsed networks
        _check_extra_deny(literal_ip, url, extra_deny_cidrs)
        return ValidatedURL(url=url, resolved_ips=[str(literal_ip)], hostname=hostname)

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addr_infos = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValueError(f"Cannot resolve hostname: {hostname!r}") from exc

    if not addr_infos:
        raise ValueError(f"No DNS records for hostname: {hostname!r}")

    resolved_ips: list[str] = []
    had_public = False
    had_blocked = False
    for _family, _type, _proto, _canonname, sockaddr in addr_infos:
        ip = ipaddress.ip_address(sockaddr[0])
        # Normalize IPv4-mapped IPv6 (::ffff:a.b.c.d) to its IPv4 form so
        # category checks catch it regardless of the address family.
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped

        try:
            _check_ip(ip, url, allow_private=allow_private)
            _check_extra_deny(ip, url, extra_deny_cidrs)
        except SSRFBlocked:
            had_blocked = True
            # Re-raise only after considering mixed resolution below.
            # In practice, raising immediately is fine — the mixed-
            # resolution rule is strictly more restrictive than single-
            # IP blocking.
            raise
        else:
            had_public = True
        resolved_ips.append(str(ip))

    # (Unreachable when had_blocked — _check_ip raises. The had_public
    # flag is retained for future logging; kept simple per YAGNI.)
    _ = (had_public, had_blocked)

    return ValidatedURL(url=url, resolved_ips=resolved_ips, hostname=hostname)
```

Add the helper functions above `validate_outbound_url`:

```python
def _check_ip(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
    url: str,
    *,
    allow_private: bool,
) -> None:
    """Raise SSRFBlocked if ip is a metadata IP or in a blocked network.

    When allow_private=True, RFC1918 / ULA ranges are allowed but
    metadata, loopback, and link-local remain blocked.
    """
    if ip in CLOUD_METADATA_IPS:
        logger.warning("SSRF blocked: %r resolved to metadata IP %s", url, ip)
        raise SSRFBlocked(url, reason="cloud_metadata", ip=str(ip))

    for network in BLOCKED_NETWORKS:
        if ip in network:
            if allow_private and _is_private_range(network):
                continue
            logger.warning(
                "SSRF blocked: URL %r resolved to %s (in %s)", url, ip, network
            )
            raise SSRFBlocked(
                url,
                reason="blocked_network",
                ip=str(ip),
                cidr=str(network),
            )


def _check_extra_deny(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
    url: str,
    extra_deny_cidrs: Sequence[str],
) -> None:
    for cidr in extra_deny_cidrs:
        network = ipaddress.ip_network(cidr, strict=False)
        if ip.version != network.version:
            continue
        if ip in network:
            logger.warning(
                "SSRF blocked: URL %r resolved to %s (in extra_deny %s)",
                url,
                ip,
                network,
            )
            raise SSRFBlocked(
                url,
                reason="extra_deny_cidr",
                ip=str(ip),
                cidr=str(network),
            )


_PRIVATE_RANGES: frozenset[str] = frozenset(
    {
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "fc00::/7",
    }
)


def _is_private_range(
    network: ipaddress.IPv4Network | ipaddress.IPv6Network,
) -> bool:
    return str(network) in _PRIVATE_RANGES
```

The mixed-resolution test case (public + private) actually already passes because `_check_ip` raises on the first private IP encountered. We'll tighten by adding an explicit pre-scan for mixed resolution so the rejection reason is clear and order-independent. Replace the IP loop with:

```python
    resolved: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for _family, _type, _proto, _canonname, sockaddr in addr_infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        resolved.append(ip)

    # Mixed-resolution defense: if any IP is blocked, reject all —
    # split-horizon DNS must not permit a "first public, then private"
    # rebind.
    for ip in resolved:
        _check_ip(ip, url, allow_private=allow_private)
        _check_extra_deny(ip, url, extra_deny_cidrs)

    resolved_ips = [str(ip) for ip in resolved]
    return ValidatedURL(url=url, resolved_ips=resolved_ips, hostname=hostname)
```

(Remove the `had_public` / `had_blocked` scaffolding from the earlier code — not needed since `_check_ip` raises directly.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/security/test_url_validator.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/lib/security/url_validator.py tests/unit/security/test_url_validator.py
git commit -m "feat(security): IP literal, v4-mapped-v6, mixed resolution checks (#3792)

IP literal hostnames skip DNS and are checked directly. IPv4-mapped
IPv6 addresses (::ffff:a.b.c.d) are normalized to IPv4 before category
check. Mixed-resolution scenarios (public + private) are rejected to
prevent split-horizon DNS bypass."
```

---

## Task 4: `allow_private` and `extra_deny_cidrs` kwargs

**Files:**
- Modify: `src/nexus/lib/security/url_validator.py` (already has the kwargs in signature; tests exercise them)
- Test: `tests/unit/security/test_url_validator.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/security/test_url_validator.py`:

```python
class TestAllowPrivateKwarg:
    """allow_private=True lets RFC1918/ULA through, but not metadata."""

    def test_private_allowed_when_flag_set(self) -> None:
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(2, 1, 6, "", ("10.0.0.1", 80))]
            result = validate_outbound_url(
                "http://dev-internal/", allow_private=True
            )
            assert "10.0.0.1" in result.resolved_ips

    def test_loopback_still_blocked_when_flag_set(self) -> None:
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(2, 1, 6, "", ("127.0.0.1", 80))]
            with pytest.raises(SSRFBlocked):
                validate_outbound_url("http://local/", allow_private=True)

    def test_metadata_still_blocked_when_flag_set(self) -> None:
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(2, 1, 6, "", ("169.254.169.254", 80))]
            with pytest.raises(SSRFBlocked) as excinfo:
                validate_outbound_url("http://meta/", allow_private=True)
            # Link-local is not "private", so still blocked
            assert excinfo.value.reason in {"cloud_metadata", "blocked_network"}


class TestExtraDenyCidrs:
    """extra_deny_cidrs adds operator-specific blocked ranges."""

    def test_custom_cidr_blocked(self) -> None:
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(2, 1, 6, "", ("203.0.113.50", 80))]
            with pytest.raises(SSRFBlocked) as excinfo:
                validate_outbound_url(
                    "http://svcmesh.example.com/",
                    extra_deny_cidrs=["203.0.113.0/24"],
                )
            assert excinfo.value.reason == "extra_deny_cidr"
            assert excinfo.value.cidr == "203.0.113.0/24"

    def test_no_match_passes(self) -> None:
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(2, 1, 6, "", ("93.184.216.34", 443))]
            result = validate_outbound_url(
                "https://example.com/", extra_deny_cidrs=["203.0.113.0/24"]
            )
            assert result.url == "https://example.com/"
```

- [ ] **Step 2: Run tests to verify they pass (or fail)**

Run: `pytest tests/unit/security/test_url_validator.py::TestAllowPrivateKwarg tests/unit/security/test_url_validator.py::TestExtraDenyCidrs -v`
Expected: PASS — the kwargs and helpers were already added in Task 3. This task codifies behavior via tests.

- [ ] **Step 3: If any test fails, fix in `url_validator.py`**

If `TestAllowPrivateKwarg::test_metadata_still_blocked_when_flag_set` fails because `allow_private` skips link-local, re-check `_is_private_range` — only the four canonical RFC1918/ULA ranges should be skippable. Expected: already correct because link-local is not in `_PRIVATE_RANGES`.

If `TestExtraDenyCidrs::test_custom_cidr_blocked` fails because `_check_extra_deny` isn't called for non-literal hostnames, inspect the DNS path in `validate_outbound_url` — confirm `_check_extra_deny` is invoked on each resolved IP.

- [ ] **Step 4: Run full validator test file**

Run: `pytest tests/unit/security/test_url_validator.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/unit/security/test_url_validator.py
git commit -m "test(security): allow_private and extra_deny_cidrs coverage (#3792)

Codifies that allow_private=True lets RFC1918/ULA through but leaves
loopback and metadata blocked, and that extra_deny_cidrs adds operator-
specific denies that fire with reason='extra_deny_cidr'."
```

---

## Task 5: `PinnedResolverTransport` and pinned httpx client factory

**Files:**
- Create: `src/nexus/lib/security/ssrf_transport.py`
- Test: `tests/unit/security/test_ssrf_transport.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/security/test_ssrf_transport.py`:

```python
"""Tests for PinnedResolverTransport and make_pinned_client_factory (Issue #3792).

The transport pins TCP connect to IPs captured at validation time. These
tests cover the observable surface:
  - factory signature and configured client options (timeout, follow_redirects)
  - transport rejects empty pinned IP lists
  - transport uses a pinned httpcore network backend that refuses fresh DNS

The actual pin enforcement is tested by patching the underlying
``httpcore.AsyncNetworkBackend`` connect method and asserting the host
it was called with.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from nexus.lib.security.ssrf_transport import (
    PinnedResolverTransport,
    make_pinned_client_factory,
)
from nexus.lib.security.url_validator import ValidatedURL


@pytest.fixture
def validated_url() -> ValidatedURL:
    return ValidatedURL(
        url="https://example.com/",
        resolved_ips=["93.184.216.34"],
        hostname="example.com",
    )


class TestPinnedResolverTransportConstruction:
    def test_requires_pinned_ips(self) -> None:
        empty = ValidatedURL(url="https://x/", resolved_ips=[], hostname="x")
        with pytest.raises(ValueError, match="at least one pinned IP"):
            PinnedResolverTransport(empty)

    def test_stores_validated(self, validated_url: ValidatedURL) -> None:
        t = PinnedResolverTransport(validated_url)
        assert t.pinned_ips == ["93.184.216.34"]
        assert t.server_hostname == "example.com"


class TestPinnedResolverBackend:
    """The network backend substitutes the pinned IP for the request host."""

    @pytest.mark.asyncio
    async def test_connect_uses_pinned_ip(self, validated_url: ValidatedURL) -> None:
        captured: dict[str, object] = {}

        async def fake_connect_tcp(
            host: str,
            port: int,
            timeout: float | None = None,
            local_address: str | None = None,
            socket_options=None,
            **kw,
        ):
            captured["host"] = host
            captured["port"] = port
            raise httpx.ConnectError("stop here")

        transport = PinnedResolverTransport(validated_url)

        # Patch the backend's connect_tcp — behavior is observable regardless
        # of whether the impl uses a custom AsyncNetworkBackend or a wrapped
        # original one, because make_pinned_client_factory wires the transport
        # through a backend whose connect_tcp we can patch via the exposed
        # `network_backend` attribute.
        with patch.object(
            transport.network_backend, "connect_tcp", side_effect=fake_connect_tcp
        ):
            async with httpx.AsyncClient(transport=transport) as client:
                with pytest.raises(httpx.ConnectError):
                    await client.get("https://example.com/")

        assert captured["host"] == "93.184.216.34"

    @pytest.mark.asyncio
    async def test_multi_ip_failover_without_fresh_dns(self) -> None:
        validated = ValidatedURL(
            url="https://example.com/",
            resolved_ips=["93.184.216.34", "93.184.216.35"],
            hostname="example.com",
        )
        transport = PinnedResolverTransport(validated)
        attempts: list[str] = []

        async def fake_connect_tcp(host: str, port: int, **kw):
            attempts.append(host)
            raise httpx.ConnectError("simulated")

        with patch.object(
            transport.network_backend, "connect_tcp", side_effect=fake_connect_tcp
        ):
            async with httpx.AsyncClient(transport=transport) as client:
                with pytest.raises(httpx.ConnectError):
                    await client.get("https://example.com/")

        # All attempts used pinned IPs; no fresh DNS.
        assert all(a in {"93.184.216.34", "93.184.216.35"} for a in attempts)
        assert len(attempts) >= 1


class TestMakePinnedClientFactory:
    """Factory matches McpHttpClientFactory signature and returns an
    AsyncClient wired to PinnedResolverTransport with redirects off."""

    @pytest.mark.asyncio
    async def test_factory_signature_is_mcp_compatible(
        self, validated_url: ValidatedURL
    ) -> None:
        factory = make_pinned_client_factory(validated_url)
        client = factory(headers={"X-Test": "1"}, timeout=None, auth=None)
        try:
            assert isinstance(client, httpx.AsyncClient)
            assert client.headers["X-Test"] == "1"
            assert client.follow_redirects is False
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_factory_accepts_timeout_and_auth(
        self, validated_url: ValidatedURL
    ) -> None:
        factory = make_pinned_client_factory(validated_url)
        client = factory(
            headers=None,
            timeout=httpx.Timeout(5.0),
            auth=httpx.BasicAuth("u", "p"),
        )
        try:
            assert client.timeout.connect == 5.0
        finally:
            await client.aclose()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/security/test_ssrf_transport.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement `PinnedResolverTransport` and factory**

Create `src/nexus/lib/security/ssrf_transport.py`. The implementation
uses a custom ``httpcore.AsyncNetworkBackend`` whose ``connect_tcp`` is
overridden to substitute the pinned IP for the hostname. SNI is
preserved by the default TLS wrapping because ``httpcore`` passes the
original URL host through to the TLS context.

```python
"""Pinned-DNS httpx transport for SSRF hardening (Issue #3792).

Wraps ``httpx.AsyncHTTPTransport`` so that the TCP connect uses IPs that
were captured and validated by ``validate_outbound_url``. This closes
the DNS rebinding TOCTOU: the OS resolver is never consulted again after
validation, so an attacker cannot flip DNS between the validator call
and the actual connect.

TLS SNI and the outgoing ``Host`` header continue to use the original
hostname, so certificate verification works normally.

Implementation: a custom ``httpcore.AsyncNetworkBackend`` wraps the
default backend and rewrites the destination host to the next pinned IP
on every ``connect_tcp`` call. The TLS handshake (performed by httpcore
upstream of connect_tcp) still uses the request URL's hostname for SNI.
"""

from __future__ import annotations

import itertools
import logging
from typing import Any

import httpcore
import httpx
from httpx import AsyncHTTPTransport

from nexus.lib.security.url_validator import ValidatedURL

logger = logging.getLogger(__name__)


class _PinnedBackend(httpcore.AsyncNetworkBackend):
    """Rewrites TCP connect host to the next pinned IP; round-robins on failure."""

    def __init__(
        self,
        inner: httpcore.AsyncNetworkBackend,
        pinned_ips: list[str],
        server_hostname: str,
    ) -> None:
        if not pinned_ips:
            raise ValueError("pinned_ips must be non-empty")
        self._inner = inner
        self._pinned_ips = pinned_ips
        self._server_hostname = server_hostname
        self._cycle = itertools.cycle(pinned_ips)

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        # Ignore the caller-supplied host — we pin to the validated IPs.
        # httpx/httpcore performs TLS SNI later using the URL host it
        # tracks separately, so cert verification still works against
        # the original hostname.
        last_exc: BaseException | None = None
        for _ in range(len(self._pinned_ips)):
            pinned = next(self._cycle)
            try:
                return await self._inner.connect_tcp(
                    host=pinned,
                    port=port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, OSError) as exc:
                last_exc = exc
                continue
        assert last_exc is not None
        raise httpcore.ConnectError(str(last_exc)) from last_exc

    async def connect_unix_socket(self, *args: Any, **kwargs: Any) -> Any:
        # Pass through unchanged — unix sockets are not SSRF-relevant.
        return await self._inner.connect_unix_socket(*args, **kwargs)

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class PinnedResolverTransport(AsyncHTTPTransport):
    """httpx transport that pins TCP connects to pre-validated IPs.

    Attributes:
        pinned_ips: Copy of ``validated.resolved_ips`` that TCP connects
            are restricted to.
        server_hostname: The original hostname from ``validated`` — used
            for logging and exposed for tests.
        network_backend: The ``_PinnedBackend`` wrapping the default
            backend; exposed so tests can patch ``connect_tcp``.
    """

    def __init__(self, validated: ValidatedURL, **kwargs: Any) -> None:
        if not validated.resolved_ips:
            raise ValueError("PinnedResolverTransport requires at least one pinned IP")
        super().__init__(**kwargs)
        self.pinned_ips: list[str] = list(validated.resolved_ips)
        self.server_hostname: str = validated.hostname

        # httpx's AsyncHTTPTransport constructs an internal httpcore pool.
        # Replace its network backend with our pinned wrapper.
        pool = self._pool  # type: ignore[attr-defined]
        default_backend = pool._network_backend  # type: ignore[attr-defined]
        self.network_backend = _PinnedBackend(
            inner=default_backend,
            pinned_ips=self.pinned_ips,
            server_hostname=self.server_hostname,
        )
        pool._network_backend = self.network_backend  # type: ignore[attr-defined]


def make_pinned_client_factory(validated: ValidatedURL):
    """Return a callable compatible with ``mcp.shared._httpx_utils.McpHttpClientFactory``.

    The MCP SSE/streamable-HTTP clients accept an
    ``httpx_client_factory(headers, timeout, auth) -> httpx.AsyncClient``
    parameter. We return a factory that builds an AsyncClient wired to
    ``PinnedResolverTransport`` with redirects disabled.
    """

    def factory(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        transport = PinnedResolverTransport(validated)
        return httpx.AsyncClient(
            transport=transport,
            headers=headers,
            timeout=timeout if timeout is not None else httpx.Timeout(30.0),
            auth=auth,
            follow_redirects=False,
        )

    return factory
```

**Impl note on httpcore attributes:** the `_pool._network_backend` path
is an internal attribute in httpcore. If the installed httpcore version
exposes a public `network_backend=` parameter on `AsyncConnectionPool`,
prefer that. Either way, keep the public surface
(`PinnedResolverTransport(validated)` + `make_pinned_client_factory`) stable.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/security/test_ssrf_transport.py -v`
Expected: PASS. If the httpx version in this repo exposes `_open_stream` differently, adjust the test patch path to match the real connect method on `AsyncHTTPTransport`.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/lib/security/ssrf_transport.py tests/unit/security/test_ssrf_transport.py
git commit -m "feat(security): pinned-DNS httpx transport for SSRF (#3792)

Adds PinnedResolverTransport and make_pinned_client_factory. Transport
pins TCP connect to IPs captured by validate_outbound_url; SNI and Host
header preserve the original hostname so cert verification still works.
Factory matches mcp.McpHttpClientFactory signature so it drops into
sse_client and streamablehttp_client without further wiring. Redirects
are disabled at the client level."
```

---

## Task 6: `SSRFConfig` pydantic model wired onto `NexusConfig`

**Files:**
- Modify: `src/nexus/config.py` — add `SSRFConfig`, `SecurityConfig`, field on `NexusConfig`
- Test: `tests/unit/test_config.py` (create `tests/unit/test_ssrf_config.py` if config tests split elsewhere)

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_ssrf_config.py`:

```python
"""Tests for SSRFConfig / SecurityConfig (Issue #3792)."""

import pytest
from pydantic import ValidationError

from nexus.config import NexusConfig, SecurityConfig, SSRFConfig


class TestSSRFConfigDefaults:
    def test_defaults_safe(self) -> None:
        cfg = SSRFConfig()
        assert cfg.allow_private is False
        assert cfg.extra_deny_cidrs == ()

    def test_nexus_config_has_security_ssrf(self) -> None:
        cfg = NexusConfig()
        assert isinstance(cfg.security, SecurityConfig)
        assert isinstance(cfg.security.ssrf, SSRFConfig)
        assert cfg.security.ssrf.allow_private is False


class TestSSRFConfigValidation:
    def test_valid_extra_deny_cidrs(self) -> None:
        cfg = SSRFConfig(extra_deny_cidrs=["10.100.0.0/16", "203.0.113.0/24"])
        assert len(cfg.extra_deny_cidrs) == 2

    def test_invalid_cidr_rejected_at_load(self) -> None:
        with pytest.raises(ValidationError):
            SSRFConfig(extra_deny_cidrs=["not-a-cidr"])

    def test_ipv6_cidr_accepted(self) -> None:
        cfg = SSRFConfig(extra_deny_cidrs=["fc00::/7"])
        assert "fc00::/7" in cfg.extra_deny_cidrs
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_ssrf_config.py -v`
Expected: FAIL — `SSRFConfig` and `SecurityConfig` not importable.

- [ ] **Step 3: Add config models**

Edit `src/nexus/config.py`. After the `FeaturesConfig` class (around line 110), add:

```python
class SSRFConfig(BaseModel):
    """SSRF validator configuration (Issue #3792).

    Knobs here are minimal by design — metadata and loopback are always
    blocked. ``allow_private`` is the opt-in for dev / self-hosted hub
    deployments that reach internal services by design.
    """

    allow_private: bool = Field(
        default=False,
        description=(
            "Allow RFC1918 / ULA private ranges through the SSRF validator. "
            "Metadata and loopback remain blocked. Set True for dev or "
            "self-hosted hub deployments that must reach internal services."
        ),
    )
    extra_deny_cidrs: tuple[str, ...] = Field(
        default=(),
        description=(
            "Operator-supplied CIDRs to block in addition to the built-in "
            "ranges (e.g. internal service-mesh subnets)."
        ),
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("extra_deny_cidrs", mode="before")
    @classmethod
    def _coerce_sequence(cls, v: Any) -> tuple[str, ...]:
        if v is None:
            return ()
        if isinstance(v, str):
            return (v,)
        return tuple(v)

    @field_validator("extra_deny_cidrs")
    @classmethod
    def _validate_cidrs(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        import ipaddress

        for cidr in v:
            try:
                ipaddress.ip_network(cidr, strict=False)
            except ValueError as exc:
                raise ValueError(f"Invalid CIDR in extra_deny_cidrs: {cidr!r}") from exc
        return v


class SecurityConfig(BaseModel):
    """Top-level security settings.

    Grows as additional security subsections land (e.g. rate limiting,
    content policy). SSRF is the first subsection (Issue #3792).
    """

    ssrf: SSRFConfig = Field(default_factory=SSRFConfig)

    model_config = ConfigDict(extra="forbid")
```

Then in `class NexusConfig(BaseModel):`, add the field (place near the existing feature flag / section fields — a spot near `features: FeaturesConfig = ...` is ideal):

```python
    security: SecurityConfig = Field(
        default_factory=SecurityConfig,
        description="Security settings (SSRF, etc. — Issue #3792)",
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_ssrf_config.py -v`
Expected: PASS.

Also run the wider config test to make sure no existing test broke:

Run: `pytest tests/unit -k config -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/config.py tests/unit/test_ssrf_config.py
git commit -m "feat(config): SecurityConfig.ssrf section with allow_private/extra_deny_cidrs (#3792)

Adds SSRFConfig and SecurityConfig pydantic models under NexusConfig.
CIDR entries in extra_deny_cidrs are validated at config load so
startup fails fast on typos. Defaults are safe (allow_private=False,
empty deny list). Sits under security.ssrf.* in yaml."
```

---

## Task 7: Wire validator + pinned factory into MCP SSE client

**Files:**
- Modify: `src/nexus/bricks/mcp/mount.py` — `_create_sse_client` and the second `sse_client` call around line 599
- Test: `tests/integration/mcp/test_ssrf_wiring.py` (new)
- Create: `tests/integration/mcp/__init__.py` if missing

- [ ] **Step 1: Check integration test directory exists**

Run: `ls tests/integration/mcp/ 2>/dev/null || mkdir -p tests/integration/mcp && touch tests/integration/mcp/__init__.py`

- [ ] **Step 2: Write the failing test**

Create `tests/integration/mcp/test_ssrf_wiring.py`:

```python
"""Integration test: MCP mount refuses internal / metadata URLs (Issue #3792)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from nexus.bricks.mcp.models import MCPMount
from nexus.lib.security.url_validator import SSRFBlocked


@pytest.fixture
def mount_manager():
    from nexus.bricks.mcp.mount import MCPMountManager

    # MCPMountManager accepts an optional nexus connection; for validation
    # paths we do not need a real one. Pass None; tests exercise only the
    # SSE-client creation which does not call through the nexus object.
    return MCPMountManager(nx=None)


@pytest.mark.asyncio
async def test_sse_mount_rejects_metadata_url(mount_manager) -> None:
    mount = MCPMount(
        name="evil",
        description="metadata target",
        transport="sse",
        url="http://169.254.169.254/mcp",
    )

    with pytest.raises(SSRFBlocked) as excinfo:
        await mount_manager._create_sse_client(mount)

    assert excinfo.value.reason in {"cloud_metadata", "blocked_network"}


@pytest.mark.asyncio
async def test_sse_mount_rejects_rfc1918_url(mount_manager) -> None:
    mount = MCPMount(
        name="intranet",
        description="RFC1918 target",
        transport="sse",
        url="http://10.0.0.1/mcp",
    )

    with pytest.raises(SSRFBlocked):
        await mount_manager._create_sse_client(mount)


@pytest.mark.asyncio
async def test_sse_mount_uses_pinned_factory_for_public_url(mount_manager) -> None:
    """For a public URL, sse_client must be called with our pinned factory."""
    mount = MCPMount(
        name="okay",
        description="public endpoint",
        transport="sse",
        url="https://example.com/mcp",
    )

    recorded: dict[str, object] = {}

    async def fake_sse_client(url, headers=None, httpx_client_factory=None, **kw):
        recorded["url"] = url
        recorded["factory"] = httpx_client_factory
        raise RuntimeError("short-circuit test")

    with (
        patch("socket.getaddrinfo") as mock_dns,
        patch("mcp.client.sse.sse_client", fake_sse_client),
    ):
        mock_dns.return_value = [(2, 1, 6, "", ("93.184.216.34", 443))]
        with pytest.raises(RuntimeError, match="short-circuit"):
            await mount_manager._create_sse_client(mount)

    assert recorded["url"] == "https://example.com/mcp"
    assert recorded["factory"] is not None
```

If `MCPMountManager.__init__` requires a non-None `nx` argument, stub it with a `unittest.mock.MagicMock()` — the validation path in `_create_sse_client` never uses it.

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/integration/mcp/test_ssrf_wiring.py -v`
Expected: FAIL — validation not yet wired into `_create_sse_client`.

- [ ] **Step 4: Wire validation and pinned factory into `mount.py`**

Edit `src/nexus/bricks/mcp/mount.py`. Find `_create_sse_client` (around line 361). Replace the method body with:

```python
    async def _create_sse_client(self, mount_config: MCPMount) -> Any:
        """Create an SSE/HTTP MCP client with SSRF validation + DNS pinning.

        The URL is validated with ``validate_outbound_url`` and the
        resulting pinned IP set is injected into the MCP client's
        ``httpx_client_factory`` so the TCP connect cannot target a
        different host than the one that was validated.
        """
        try:
            from mcp import ClientSession
            from mcp.client.sse import sse_client
        except ImportError as e:
            raise MCPMountError(
                "MCP client library not installed. Install with: pip install mcp"
            ) from e

        if not mount_config.url:
            raise MCPMountError("URL is required for SSE/HTTP transport")

        # SSRF validation + DNS pinning (Issue #3792).
        from nexus.lib.security.ssrf_transport import make_pinned_client_factory
        from nexus.lib.security.url_validator import (
            SSRFBlocked,
            validate_outbound_url,
        )

        ssrf_cfg = self._ssrf_config()
        try:
            validated = validate_outbound_url(
                mount_config.url,
                allow_private=ssrf_cfg.allow_private,
                extra_deny_cidrs=ssrf_cfg.extra_deny_cidrs,
            )
        except SSRFBlocked as exc:
            self._emit_ssrf_audit(mount_config, exc)
            logger.warning(
                "SSRF blocked for MCP mount %r: %s", mount_config.name, exc
            )
            raise

        headers: dict[str, str] = dict(mount_config.headers) if mount_config.headers else {}
        if mount_config.auth_type == "api_key" and mount_config.auth_config:
            api_key = mount_config.auth_config.get("api_key")
            header_name = mount_config.auth_config.get("header_name", "Authorization")
            if api_key:
                headers[header_name] = f"Bearer {api_key}"

        pinned_factory = make_pinned_client_factory(validated)

        async with (
            sse_client(
                mount_config.url,
                headers=headers,
                httpx_client_factory=pinned_factory,
            ) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            return session
```

Add the helpers on `MCPMountManager` (near the bottom of the class). Find a clean insertion point just before `async def unmount`:

```python
    def _ssrf_config(self) -> Any:
        """Fetch SSRF config; fall back to safe defaults if config missing."""
        try:
            from nexus.config import SSRFConfig

            cfg = getattr(getattr(self, "config", None), "security", None)
            if cfg is not None and getattr(cfg, "ssrf", None) is not None:
                return cfg.ssrf
            return SSRFConfig()
        except Exception:
            from nexus.config import SSRFConfig

            return SSRFConfig()

    def _emit_ssrf_audit(self, mount_config: Any, exc: Any) -> None:
        """Emit a security.ssrf_blocked audit event; never raise."""
        try:
            from nexus.lib.events import emit_audit_event  # best-effort; see Task 8
        except ImportError:
            return
        try:
            emit_audit_event(
                "security.ssrf_blocked",
                {
                    "url": exc.url,
                    "reason": exc.reason,
                    "ip": exc.ip,
                    "cidr": exc.cidr,
                    "integration": "mcp",
                    "mount_name": getattr(mount_config, "name", None),
                },
            )
        except Exception as audit_err:  # pragma: no cover — audit must never raise
            logger.warning("Failed to emit SSRF audit event: %s", audit_err)
```

Also patch the second `sse_client` call around line 599 in `mount.py` (used in a different code path — likely tool listing). Wrap it with the same validation + factory:

```python
        ssrf_cfg = self._ssrf_config()
        try:
            validated = validate_outbound_url(
                mount.url,
                allow_private=ssrf_cfg.allow_private,
                extra_deny_cidrs=ssrf_cfg.extra_deny_cidrs,
            )
        except SSRFBlocked as exc:
            self._emit_ssrf_audit(mount, exc)
            raise

        pinned_factory = make_pinned_client_factory(validated)

        async with (
            sse_client(
                mount.url,
                headers=headers,
                httpx_client_factory=pinned_factory,
            ) as (read, write),
            ClientSession(read, write) as session,
        ):
            ...
```

(Import `validate_outbound_url`, `SSRFBlocked`, `make_pinned_client_factory` at the top of the function as in `_create_sse_client`. Alternatively, hoist to module-level imports if no circular-import issues surface during test run.)

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/integration/mcp/test_ssrf_wiring.py -v`
Expected: PASS.

Also run the existing MCP test suite to make sure we didn't regress:

Run: `pytest tests/unit/bricks/mcp tests/integration/mcp -v`
Expected: PASS. If pre-existing MCP tests depend on `_create_sse_client` calling `sse_client` with only `url`/`headers`, update those tests to accept the new `httpx_client_factory` kwarg.

- [ ] **Step 6: Commit**

```bash
git add src/nexus/bricks/mcp/mount.py tests/integration/mcp/test_ssrf_wiring.py tests/integration/mcp/__init__.py
git commit -m "feat(mcp): validate SSRF and pin DNS for SSE mounts (#3792)

_create_sse_client and the tool-listing SSE path now route every
outbound MCP URL through validate_outbound_url and build the underlying
httpx.AsyncClient with a PinnedResolverTransport via
make_pinned_client_factory. Blocked mounts emit a security.ssrf_blocked
audit event before raising."
```

---

## Task 8: Audit event helper (`emit_audit_event`)

If `nexus.lib.events.emit_audit_event` already exists, this task is a no-op — verify and skip to Task 9.

**Files (if helper does not exist):**
- Create: `src/nexus/lib/events.py` (or extend existing audit pathway)
- Test: `tests/unit/lib/test_events.py`

- [ ] **Step 1: Check whether helper exists**

Run:
```bash
rg -n "def emit_audit_event|emit_audit_event" src/nexus/
```

If a helper is present under a different name (e.g. `emit_security_event`, `log_audit`), prefer using it instead and update `mount.py` in Task 7 accordingly. Only add a new helper if none exists.

- [ ] **Step 2: Write the failing test (only if helper must be created)**

Create `tests/unit/lib/test_events.py`:

```python
"""Tests for the audit event helper (Issue #3792)."""

from nexus.lib.events import emit_audit_event, register_audit_sink


def test_emit_audit_event_delivers_to_sink() -> None:
    captured: list[tuple[str, dict]] = []

    def sink(name: str, payload: dict) -> None:
        captured.append((name, payload))

    handle = register_audit_sink(sink)
    try:
        emit_audit_event(
            "security.ssrf_blocked",
            {"url": "http://10.0.0.1/", "reason": "blocked_network"},
        )
    finally:
        handle.remove()

    assert captured == [
        ("security.ssrf_blocked", {"url": "http://10.0.0.1/", "reason": "blocked_network"}),
    ]


def test_emit_audit_event_no_sinks_does_not_raise() -> None:
    # Must be safe to call even when nothing is listening.
    emit_audit_event("security.ssrf_blocked", {"url": "x"})
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/unit/lib/test_events.py -v`
Expected: FAIL.

- [ ] **Step 4: Implement minimal event helper**

Create `src/nexus/lib/events.py`:

```python
"""Minimal in-process audit event bus (Issue #3792).

The existing nexus event-log service handles durable delivery; this
module is a tiny synchronous sink registry for security-signal events
that need to fan out to whatever the current deployment uses for
audit (log exporter, activity feed, etc.).

Audit emits must never raise. Failures are logged and swallowed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_Sink = Callable[[str, dict[str, Any]], None]
_sinks: list[_Sink] = []


@dataclass
class SinkHandle:
    sink: _Sink

    def remove(self) -> None:
        try:
            _sinks.remove(self.sink)
        except ValueError:
            pass


def register_audit_sink(sink: _Sink) -> SinkHandle:
    """Register a sink that receives (name, payload) for each event."""
    _sinks.append(sink)
    return SinkHandle(sink)


def emit_audit_event(name: str, payload: dict[str, Any]) -> None:
    """Emit an audit event to all registered sinks.

    Never raises — sink failures are logged at WARNING.
    """
    for sink in list(_sinks):
        try:
            sink(name, payload)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("Audit sink %r raised on event %r: %s", sink, name, exc)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/lib/test_events.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/nexus/lib/events.py tests/unit/lib/test_events.py
git commit -m "feat(events): in-process audit sink registry (#3792)

Minimal synchronous fan-out used by security signals (SSRF blocks).
Existing durable event log handles long-term storage; sinks here are
for near-real-time audit surfaces. emit_audit_event never raises."
```

---

## Task 9: Full-suite verification

- [ ] **Step 1: Run all affected tests**

Run:
```bash
pytest tests/unit/security tests/unit/test_ssrf_config.py tests/unit/lib/test_events.py tests/integration/mcp -v
```
Expected: PASS.

- [ ] **Step 2: Run linters**

Run:
```bash
ruff check src/nexus/lib/security src/nexus/bricks/mcp/mount.py src/nexus/config.py
ruff format --check src/nexus/lib/security src/nexus/bricks/mcp/mount.py src/nexus/config.py
mypy src/nexus/lib/security src/nexus/config.py
```
Expected: clean.

- [ ] **Step 3: Run the broader test selection likely impacted**

Run:
```bash
pytest tests/unit/security tests/unit/bricks/mcp tests/unit -k "config or security" -v
```
Expected: PASS. Investigate any pre-existing failure before concluding — do not paper over them.

- [ ] **Step 4: If anything is red, fix and re-run before commit**

This is the verification-before-completion gate. Do NOT claim the branch is green unless `pytest` exits 0 on the commands above.

- [ ] **Step 5: No commit needed (verification only). Push branch for PR.**

```bash
git push -u origin feat/3792-ssrf-validation
```

---

## Acceptance Criteria checklist (maps to spec)

- [x] `SSRFBlocked(ValueError)` with `url`, `reason`, `ip`, `cidr` — Task 1
- [x] `validate_outbound_url` returns `ValidatedURL` NamedTuple; backward-compat two-tuple unpack — Task 1
- [x] Explicit cloud metadata set (AWS, GCP, Azure, OCI, Alibaba, DO) — Task 2
- [x] Userinfo rejection — Task 2
- [x] IP literal hostname path — Task 3
- [x] IPv4-mapped IPv6 normalization — Task 3
- [x] Mixed public/private resolution rejected — Task 3
- [x] `allow_private`, `extra_deny_cidrs` kwargs — Task 4 (behavior) + Task 3 (impl)
- [x] `PinnedResolverTransport` + `make_pinned_client_factory` — Task 5
- [x] Redirects disabled — Task 5 (factory)
- [x] MCP SSE mounts validate + pin — Task 7
- [x] `security.ssrf.*` config surface with CIDR validation at load — Task 6
- [x] `security.ssrf_blocked` audit events — Task 7 + Task 8
- [x] Unit + integration tests — Tasks 1-7

## Out-of-scope (deferred; tracked under #3792)

- Federation hub URL validation at mount / reconnect
- Blueprint fetch validation with redirect re-validation helper
- Config extensions: `max_redirects`, `dns_resolver`
- Audit dashboard / alerting on `security.ssrf_blocked` volume
