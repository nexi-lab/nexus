# SSRF Validation for Outbound Fetch and MCP Tool Calls

**Issue:** [#3792](https://github.com/nexi-lab/nexus/issues/3792)
**Depends on:** #3779 (MCP HTTP transport hardening)
**Epic:** #3777 (Nexus as Context Layer for Secure Agent Runtimes), Phase 1 — Foundation
**Date:** 2026-04-21

## Problem

Nexus accepts URLs and hostnames in several places where a malicious or
confused agent could weaponize them:

- MCP tool calls that fetch a URL (agent-controlled server URL, future
  `http_fetch` / `web_search` / `fetch_page` tools)
- Federation mount pointing at a hub URL
- Blueprint / policy references to remote YAML
- Webhook targets for approval callbacks

An existing validator from Issue #1596
(`nexus.lib.security.url_validator.validate_outbound_url`) blocks RFC1918,
loopback, link-local, and several cloud metadata ranges. It is wired into
the workflow webhook action, the proxy transport, and subscription models.
However, it has material gaps against the threat model in #3792:

1. **DNS rebinding not enforced.** The validator returns resolved IPs, but
   no consumer pins them; httpx re-resolves DNS at connect time. An
   attacker controlling a DNS server can answer with a public IP during
   validation and an internal IP during the subsequent connect.
2. **No typed exception.** Validator raises generic `ValueError`, so call
   sites cannot distinguish SSRF blocks from other URL errors for audit.
3. **No redirect policy.** httpx follows redirects by default; a public
   URL can redirect into an internal one with no re-validation.
4. **Not wired to MCP HTTP transport.** The highest-risk surface (agent-
   controlled URLs) is unprotected.
5. **No audit event.** Blocked attempts are logged but not emitted to the
   activity/event bus, so operators cannot see patterns.
6. **Cloud metadata coverage gap.** Alibaba's metadata IP
   (`100.100.100.200`) is outside any private range and not in the
   current blocklist.

This must close before #3784 (hub mode) ships, because a shared hub
magnifies the blast radius.

## Scope of this PR

- Harden the existing validator (`nexus.lib.security.url_validator`) in
  place — no new module.
- Add an httpx transport that pins DNS to the validator's resolved IPs.
- Wire validator + pinned transport into the MCP HTTP transport.
- Emit audit events at call sites that catch `SSRFBlocked`.
- Add minimal config surface: `allow_private` and `extra_deny_cidrs`.

**Explicitly out of scope (follow-up PRs tracked under #3792):**

- Federation hub URL validation (operator-supplied; lower risk pre-#3784)
- Blueprint fetch validation (operator-supplied)
- Redirect re-validation chain (redirects disabled for MCP this PR)
- Custom DNS resolver configuration (`security.ssrf.dns_resolver`)

## Architecture

```
┌───────────────────────────────────────────────┐
│ nexus.lib.security.url_validator (extended)   │
│                                               │
│  SSRFBlocked(ValueError)                      │
│  CLOUD_METADATA_IPS: frozenset[IP*]           │
│  BLOCKED_NETWORKS: (existing + documented)    │
│                                               │
│  validate_outbound_url(                       │
│      url,                                     │
│      *,                                       │
│      allow_private: bool = False,             │
│      extra_deny_cidrs: Sequence[str] = (),    │
│  ) -> ValidatedURL                            │
│                                               │
│  ValidatedURL: NamedTuple(url, resolved_ips, hostname) │
└────────────────────┬──────────────────────────┘
                     │
      ┌──────────────┼──────────────┐
      ▼              ▼              ▼
┌───────────┐  ┌───────────┐  ┌───────────┐
│  proxy/   │  │ workflow  │  │  mcp/     │
│ transport │  │ webhook   │  │ http_tx   │
│ (exists)  │  │ (exists)  │  │ (NEW)     │
└───────────┘  └───────────┘  └─────┬─────┘
                                    │ uses
                                    ▼
                    ┌─────────────────────────────┐
                    │ nexus.lib.security.         │
                    │   ssrf_transport            │
                    │                             │
                    │ PinnedResolverTransport     │
                    │  (httpx AsyncHTTPTransport) │
                    │  — resolver pinned to       │
                    │    ValidatedURL.resolved_ips│
                    │  — SNI uses original host   │
                    │  — follow_redirects=False   │
                    └─────────────────────────────┘
```

Tier placement: validator stays in `nexus.lib.security` (tier-neutral).
New httpx transport helper lives alongside in
`nexus.lib.security.ssrf_transport`. MCP client in `src/nexus/bricks/mcp/`
imports both. Module naming keeps `nexus.lib.security.url_validator`
rather than adopting the spec's `nexus.security.ssrf` — existing module
is already the tier-neutral home and consumers are wired.

## Components

### 1. `SSRFBlocked` exception

```python
class SSRFBlocked(ValueError):
    def __init__(
        self,
        url: str,
        reason: str,
        *,
        ip: str | None = None,
        cidr: str | None = None,
    ) -> None:
        self.url = url
        self.reason = reason
        self.ip = ip
        self.cidr = cidr
        super().__init__(f"SSRF blocked: {reason} (url={url}, ip={ip})")
```

Subclassing `ValueError` preserves backward compat: existing
`except ValueError:` paths at call sites still catch.

### 2. Extended `validate_outbound_url`

Signature adds keyword-only arguments with defaults preserving today's
behavior:

```python
class ValidatedURL(NamedTuple):
    url: str
    resolved_ips: list[str]
    hostname: str


def validate_outbound_url(
    url: str,
    *,
    allow_private: bool = False,
    extra_deny_cidrs: Sequence[str] = (),
) -> ValidatedURL: ...
```

Field order `(url, resolved_ips, hostname)` preserves existing two-tuple
unpacking at call sites (`_url, _resolved_ips = validate_outbound_url(url)`
in `bricks/workflows/actions.py`). `hostname` is appended at position 3
and available via attribute access on the NamedTuple.

Other changes vs current:

- Raises `SSRFBlocked` (subclass of `ValueError`) instead of plain
  `ValueError` for policy-based blocks. DNS failures still raise
  `ValueError` (distinguishable).

- Adds `CLOUD_METADATA_IPS` explicit set, checked before CIDR loop:
  - AWS IMDSv2: `169.254.169.254`, `fd00:ec2::254`
  - GCP: `169.254.169.254` (metadata.google.internal)
  - Azure IMDS: `169.254.169.254`
  - OCI: `169.254.169.254`
  - Alibaba: `100.100.100.200` — **not covered by current CIDR list**
  - DigitalOcean: `169.254.169.254`

- Normalizes IPv4-mapped IPv6 (`::ffff:a.b.c.d`) to v4 before category
  check, so `::ffff:10.0.0.1` is caught by RFC1918 rule.

- Rejects URLs containing userinfo (`http://user@host/`) to block
  parser-divergence tricks where some libraries read host from `user`.

- Rejects mixed-resolution hostnames (some public IPs, some private)
  rather than allowing first-public; conservative default prevents
  split-horizon bypass.

- Treats IP literal hostnames as their direct IP (no DNS lookup, but
  still subject to blocklist).

- New kwargs:
  - `allow_private=True` opts into RFC1918 + ULA ranges; metadata and
    loopback remain blocked.
  - `extra_deny_cidrs=["10.100.0.0/16"]` adds operator-specific denies
    (internal service mesh).

### 3. `PinnedResolverTransport`

New file `src/nexus/lib/security/ssrf_transport.py`:

```python
class PinnedResolverTransport(httpx.AsyncHTTPTransport):
    """httpx transport that pins DNS resolution to pre-validated IPs.

    Prevents DNS rebinding TOCTOU: the URL host is validated once, IPs
    are captured, and all subsequent connect calls use those IPs only.
    TLS SNI continues to use the original hostname so certificate
    verification works.
    """

    def __init__(self, validated: ValidatedURL, **kwargs: Any) -> None: ...

    async def handle_async_request(
        self, request: httpx.Request
    ) -> httpx.Response: ...
```

Implementation approach: httpx exposes a lower-level `httpcore` pool.
The pinned transport installs a resolver that returns only
`validated.resolved_ips`; the Host header and SNI hostname are preserved
by passing the original `request.url.host` as the SNI value. If the
first pinned IP fails to connect, subsequent pinned IPs are tried with
no fresh DNS.

The exact injection point (custom `httpcore` transport vs. httpx
transport subclass) is validated during the implementation spike. If
upstream hooks are insufficient, we fall back to a small custom socket
opener that accepts a pre-resolved IP list.

### 4. MCP HTTP client wiring

Changes in `src/nexus/bricks/mcp/` (connection manager / transport):

- At MCP server URL registration: call
  `validate_outbound_url(url, allow_private=cfg.ssrf.allow_private,
  extra_deny_cidrs=cfg.ssrf.extra_deny_cidrs)`
- Build `httpx.AsyncClient` with
  `transport=PinnedResolverTransport(validated)` and
  `follow_redirects=False`.
- Catch `SSRFBlocked` → log warning, emit audit event, re-raise.
- Convert `SSRFBlocked` at the MCP boundary into a generic MCP error
  response so the agent sees "outbound URL blocked by security policy"
  without IP/CIDR details.

### 5. Audit event

Event type: `security.ssrf_blocked`. Fired via the existing activity/
event bus (same one used by other security signals in the server).
Fields:

- `url` — the rejected URL
- `resolved_ip` — IP that matched the block, if known
- `matched_cidr` — CIDR that matched, if known
- `reason` — "metadata_ip" / "private_ip" / "loopback" / "scheme" / etc.
- `context` — caller-supplied dict with `integration`, `tool_name`,
  `agent_id` — validator itself stays pure (no event bus coupling).

### 6. Config

New section in nexus config (`configs/nexus.yaml` + pydantic model):

```yaml
security:
  ssrf:
    allow_private: false          # set true for dev/self-hosted
    extra_deny_cidrs:
      - "10.100.0.0/16"           # example internal service mesh
```

`max_redirects` and `dns_resolver` deferred (see Scope). Pydantic
validates `extra_deny_cidrs` entries as parseable CIDRs at config load
— invalid entries raise on startup, not per request.

## Data Flow

MCP tool registration (agent invokes a tool whose MCP server URL is
agent-supplied):

```
1. MCP server URL arrives at connection_manager
2. validate_outbound_url(url,
                         allow_private=cfg.ssrf.allow_private,
                         extra_deny_cidrs=cfg.ssrf.extra_deny_cidrs)
   ├─ parse URL; reject userinfo; reject non-http(s) scheme
   ├─ if hostname is IP literal → skip DNS, use literal
   ├─ else socket.getaddrinfo(hostname) → ips
   ├─ for ip in ips:
   │    normalize v6-mapped-v4
   │    if ip ∈ CLOUD_METADATA_IPS: raise SSRFBlocked(metadata_ip)
   │    if ip ∈ BLOCKED_NETWORKS (allow_private skips private):
   │        raise SSRFBlocked(<category>)
   │    if ip ∈ extra_deny_cidrs: raise SSRFBlocked(extra_deny)
   ├─ if any public and any private: raise SSRFBlocked(mixed_resolution)
   └─ return ValidatedURL(url=url, resolved_ips=ips, hostname=...)

3. httpx.AsyncClient(
       transport=PinnedResolverTransport(validated),
       follow_redirects=False,
   )
4. Client used for every subsequent call to that MCP server.

Per request:
5. client.post(path, json=...)
6. PinnedResolverTransport intercepts connect:
   - resolver returns ONLY validated.resolved_ips (no fresh DNS)
   - TLS SNI uses original hostname → cert verify works
   - connects to pinned IP
7. If SSRFBlocked raised at step 2:
   - log.warning(...)
   - emit_audit_event("security.ssrf_blocked", {
         url, reason, ip, cidr,
         context={integration: "mcp", tool_name, agent_id},
     })
   - re-raise; MCP boundary converts to generic error response
```

**DNS rebinding defense:** steps 2 and 6 use the same IP set. Step 6
never re-resolves, so attacker-controlled DNS TTL flip after step 2 has
no effect.

**Redirect defense:** step 3 disables redirects. A 3xx response surfaces
to the caller rather than being followed. A future follow-up PR
integrating blueprint fetch can add a `validated_redirect_chain()`
helper that re-validates each `Location` and caps hop count.

## Error Handling

**`SSRFBlocked`** — policy block. Caller catches, emits audit event,
converts to generic error at the trust boundary (agent sees no
internal topology).

**DNS resolution failure (`socket.gaierror`)** — existing behavior,
keep raising plain `ValueError("Cannot resolve hostname: ...")`. Not a
security signal; no audit event.

**Pinned transport connect failure (`httpx.ConnectError`)** — validated
IP unreachable after validation (firewall, host down). Normal retry /
timeout logic applies. No audit event.

**Invalid CIDR in `extra_deny_cidrs`** — pydantic validation catches at
config load; startup fails fast rather than per-request failures.

**Edge cases explicitly handled:**

- Hostname is IP literal → skip DNS, check literal.
- Mixed public/private resolution → reject.
- IPv6-mapped IPv4 → normalize before category check.
- URL with userinfo → reject.
- Caller attempts to override `Host` header → transport ignores override
  (uses validated hostname).

## Testing

### Unit tests (`tests/unit/security/test_url_validator.py`, extend)

Existing coverage audited and kept:

- Loopback, RFC1918, link-local blocks
- AWS IPv6 metadata
- Scheme allowlist

Added (curated ~20 high-signal cases):

- `SSRFBlocked` caught as `ValueError` (backward compat)
- `SSRFBlocked` exposes `url`, `ip`, `cidr`, `reason`
- `allow_private=True` → RFC1918 passes; metadata + loopback still block
- `extra_deny_cidrs=["10.100.0.0/16"]` blocks custom CIDR
- Alibaba `100.100.100.200` blocked via explicit metadata set
- `::ffff:127.0.0.1` and `::ffff:169.254.169.254` blocked (v4-mapped)
- `fd00:ec2::254` blocked
- `http://user@10.0.0.1/` rejected (userinfo)
- IP literal hostnames directly blocked (no DNS)
- Mixed resolution `[1.2.3.4, 10.0.0.1]` → blocked
- Octal/hex/decimal encodings rejected or correctly normalized
- `file://`, `gopher://`, `dict://`, `ftp://` scheme blocked
- Empty hostname, missing scheme rejected
- Return is `ValidatedURL` NamedTuple — both tuple unpack and attribute
  access work

### Unit tests (`tests/unit/security/test_ssrf_transport.py`, new)

- `PinnedResolverTransport` uses pinned IPs — mock DNS returns a
  different IP; transport ignores DNS and connects to pinned.
- SNI preserved: TLS handshake uses original hostname (mock TLS ctx;
  assert `server_hostname`).
- Redirects disabled: 3xx response not followed.
- Multi-IP pinned set: on connect failure, next pinned IP tried without
  fresh DNS.

### Integration test (`tests/integration/mcp/test_ssrf_wiring.py`, new)

- MCP client configured with `http://10.0.0.1/` → `SSRFBlocked` raised
  at registration.
- Audit event `security.ssrf_blocked` observed with `tool_name`,
  `agent_id`, `url`, `ip`, `cidr`, `reason`.
- DNS rebinding scenario: mock resolver flips IPs between validate and
  connect; client connects to originally validated IP.

No end-to-end test — external calls are flaky; unit + integration
covers behavior.

## Acceptance Criteria

- [ ] `SSRFBlocked(ValueError)` exception with `url`, `reason`, `ip`,
      `cidr`.
- [ ] `validate_outbound_url` extended with `allow_private` and
      `extra_deny_cidrs` kwargs; returns `ValidatedURL` NamedTuple.
- [ ] Cloud metadata IPs covered explicitly across AWS, GCP, Azure,
      OCI, Alibaba, DigitalOcean.
- [ ] IPv4-mapped IPv6 normalized before category check.
- [ ] Mixed public/private resolution rejected.
- [ ] Userinfo in URL rejected.
- [ ] `PinnedResolverTransport` pins DNS to validated IPs; preserves
      SNI; redirects disabled.
- [ ] MCP HTTP transport validates URL at registration and uses pinned
      transport.
- [ ] `security.ssrf_blocked` audit event emitted at MCP call sites.
- [ ] Config surface: `security.ssrf.allow_private` and
      `security.ssrf.extra_deny_cidrs` with startup-time CIDR validation.
- [ ] Unit + integration tests passing.

## Follow-up issues (tracked under #3792)

1. Federation hub URL validation at mount and reconnect.
2. Blueprint fetch validation with redirect re-validation helper.
3. Config surface extension: `max_redirects`, `dns_resolver`.
4. Audit dashboard / alerting on `security.ssrf_blocked` rate.
