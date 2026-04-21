# MCP HTTP Transport Hardening — Design

- **Issue**: [#3779](https://github.com/nexi-lab/nexus/issues/3779) (Phase 1 of Epic [#3777](https://github.com/nexi-lab/nexus/issues/3777))
- **Date**: 2026-04-19
- **Author**: windoliver (via Claude Code)
- **Status**: Design approved, pending implementation plan

## Context

The MCP server (`src/nexus/bricks/mcp/server.py`) currently runs reliably for single-client, stdio usage. Hub mode requires the HTTP transport (`MCP_TRANSPORT=http`) to handle many concurrent agent connections reliably: different auth tokens, different zone scopes, simultaneous search queries, isolated failure domains.

An audit of the current code identified:

| Area | Current state |
|------|---------------|
| `/health` endpoint | Already implemented (`server.py:2262`) |
| Per-request auth | Works via `contextvars` + `auth_bridge.resolve_mcp_operation_context`, fail-closed |
| Rate limiting | **Missing** |
| Per-token audit logging | **Missing** (basic Python logging only) |
| Auth caching | **None** — every MCP tool call does a ≤10s auth round-trip |
| BM25S search lock | Per-instance `RLock` in `bricks/search/bm25s_search.py:338` (not global) — contention unmeasured |
| Concurrency / zone-isolation tests | **Missing** |
| Graceful sandbox disconnect | Starlette surfaces `ClientDisconnect` natively; not tested |

Existing HTTP API rate limiter (`src/nexus/server/rate_limiting.py`, Issue [#780](https://github.com/nexi-lab/nexus/issues/780)) uses SlowAPI with a Redis/Dragonfly backend. That pattern is reused here.

## Goals

Satisfy the acceptance criteria in #3779:

1. 10 concurrent MCP clients with different tokens get correct zone-scoped results.
2. No cross-tenant data leakage under concurrent load.
3. `/health` endpoint responds (already met; retained).
4. Per-token rate limiting, configurable via environment variables.
5. Per-token request logging with structured fields, delivered to both stdout and the event bus.

Additional goals derived from the audit:

6. Auth resolution caching to cut 10s round-trip from the hot path.
7. Measure BM25S lock contention under realistic load (do not preemptively refactor).
8. Verify graceful client disconnect via explicit test coverage.

## Non-goals

- Rewriting FastMCP auth flow or moving auth resolution to middleware (rejected in Variant A).
- Read/write lock separation for BM25S — gated on measurement (Q5).
- Multi-tenant quota accounting beyond per-token rate limits.
- Cache invalidation API for rotated keys (60s TTL tolerated; documented).
- Sandbox↔connection tracking map (rejected in Q6).

## Architecture

HTTP request flow through the Starlette app that FastMCP exposes:

```
HTTP request (Starlette)
  │
  ├─ [NEW] MCPRateLimitMiddleware (SlowAPI, Redis backend)
  │      ├─ extracts token from Authorization: Bearer sk-...
  │      ├─ looks up tier via AuthIdentityCache
  │      └─ rejects 429 if over limit
  │
  ├─ [NEW] MCPAuditLogMiddleware
  │      ├─ structured JSON log → stdout
  │      ├─ event bus publish (fire-and-forget) → "mcp.request"
  │      └─ fields: ts, token_hash, zone_id, subject_id, rpc_method,
  │                 tool_name, status_code, latency_ms, user_agent
  │
  ├─ FastMCP JSON-RPC dispatch → ToolNamespaceMiddleware (existing)
  │
  └─ Tool handler → _resolve_mcp_operation_context()
         └─ [NEW] AuthIdentityCache.get_or_resolve(api_key_hash)
                 ├─ hit: return cached (subject_id, zone_id, is_admin) — 60s TTL
                 └─ miss: call authenticate_api_key() → cache → return
```

**Variant choice**: Variant B (hybrid) — middleware handles rate-limit and audit log; auth resolution stays in `auth_bridge.py` with a cache layered underneath. Variant A (auth in middleware) was rejected because FastMCP routes auth via JSON-RPC body, not HTTP headers alone; refactoring that path has a larger blast radius.

## Components

### 1. `src/nexus/bricks/mcp/auth_cache.py` (new, ~80 LoC)

- `AuthIdentityCache` wraps `cachetools.TTLCache(maxsize=1024, ttl=60)`.
- Key: `sha256(api_key)[:16]` (never store raw keys).
- Value: `ResolvedIdentity(subject_id, zone_id, is_admin, tier)`.
- `threading.RLock` for thread safety.
- Methods: `get(key_hash)`, `put(key_hash, identity)`, `invalidate(key_hash)`.
- Module-level singleton `_auth_identity_cache` — one per process.
- Only positive results cached; failed auths retry immediately (no negative caching).

### 2. `src/nexus/bricks/mcp/middleware_ratelimit.py` (new, ~120 LoC)

- Builds a SlowAPI `Limiter` with `storage_uri` from `NEXUS_REDIS_URL` (or `DRAGONFLY_URL`).
- Key func reuses header parsing logic from `server/rate_limiting.py:37`; shared logic lifted to `server/token_utils.py` if not already shared.
- Tier lookup via `AuthIdentityCache`. Admin tokens → `premium`; authenticated non-admin → `authenticated`; unauthenticated → `anonymous` (IP-keyed).
- Environment overrides:
  - `MCP_RATE_LIMIT_ENABLED` (default `false` to preserve current behavior; CI sets `true`)
  - `NEXUS_MCP_RATE_LIMIT_ANONYMOUS` (default `60/minute`)
  - `NEXUS_MCP_RATE_LIMIT_AUTHENTICATED` (default `300/minute`)
  - `NEXUS_MCP_RATE_LIMIT_PREMIUM` (default `1000/minute`)
- Returns `429` with `Retry-After` header and JSON `{"error":"Rate limit exceeded","retry_after":N}`.

### 3. `src/nexus/bricks/mcp/middleware_audit.py` (new, ~100 LoC)

- `AuditLogMiddleware(BaseHTTPMiddleware)` — Starlette middleware.
- On request: capture start time, extract token hash, peek JSON-RPC body for `method` / `params.name`, restore body via closure around `request.scope["receive"]`.
- On response: structured JSON log to stdout; `asyncio.create_task` publishes event `mcp.request` via existing event bus client (`services/event_bus/redis.py`) — fire-and-forget.
- Event / log schema:
  ```json
  {
    "ts": "ISO-8601",
    "event": "mcp.request",
    "token_hash": "sha256[:16]",
    "zone_id": "...",
    "subject_id": "...",
    "rpc_method": "tools/call",
    "tool_name": "nexus_grep",
    "status_code": 200,
    "latency_ms": 47,
    "user_agent": "..."
  }
  ```
- Failures publishing to event bus are logged at WARN and increment `mcp_audit_publish_errors_total`; they never block the response.

### 4. `src/nexus/bricks/mcp/auth_bridge.py` (modified, ~20 LoC delta)

- `authenticate_api_key()` consults `AuthIdentityCache` before calling `auth_provider.authenticate()`; populates cache on success.
- No behavior change on cache miss or auth failure — existing fail-closed semantics preserved.

### 5. `src/nexus/bricks/mcp/server.py` (modified, ~15 LoC delta)

- After `create_mcp_server()`, obtain Starlette app via FastMCP's installed streamable-HTTP surface (exact accessor verified at implementation time — framework-dependent).
- Install middleware in order: `MCPRateLimitMiddleware` → `MCPAuditLogMiddleware`.
- Middleware activation gated on `MCP_TRANSPORT=http` (no-op for stdio/sse).

### Reused infrastructure

- `server/rate_limiting.py` token-key extraction (to be lifted into a shared util).
- Redis/Dragonfly connection pool from `cache/dragonfly.py`.
- Event bus client from `services/event_bus/redis.py`.
- OTEL instrumentation pattern from `server/logging_processors.py` for exposing counters.

## Data flow

Authenticated MCP `tools/call` on `nexus_grep`:

1. `POST /mcp`  `Authorization: Bearer sk-zone_abc-user_xyz-...`
2. `MCPRateLimitMiddleware`: key `"user:zone_abc:user_xyz"`, cache lookup → tier `"authenticated"`, Redis counter check → PASS (else 429 short-circuit).
3. `MCPAuditLogMiddleware` (enter): record `start`, peek body → `rpc_method="tools/call"`, `tool_name="nexus_grep"`, restore receive.
4. FastMCP dispatches JSON-RPC.
5. `ToolNamespaceMiddleware` (existing) filters visible tools.
6. `nexus_grep` handler calls `_resolve_mcp_operation_context()` → `authenticate_api_key()` → `AuthIdentityCache.get(hash)` hits → returns identity. `apply_rebac_filter(results, zone_abc)` scopes output.
7. `MCPAuditLogMiddleware` (exit): compute `latency_ms`, emit stdout JSON, schedule event publish, return response.

Cache staleness window: up to 60s after key rotation. Documented; no explicit invalidation API in v1.

## Error handling

**Rate-limit middleware**

- Redis unreachable at init: log error, SlowAPI falls back to in-memory storage. Service stays up.
- Redis unreachable at runtime: fail-open for that request; log WARN; increment `mcp_rate_limit_storage_errors_total`. Infra issues must not block legitimate traffic.
- Malformed `Authorization` header: treat as anonymous, limit by IP.
- Limit breach: `429` with `Retry-After`.

**Audit middleware**

- Body-peek exception (truncated / non-JSON): emit log with `tool_name=null, rpc_method=null`; do not block.
- Event bus publish failure: caught in fire-and-forget wrapper; WARN; increment `mcp_audit_publish_errors_total`.
- stdout log failure: Python logging resilience handles it.

**Auth cache**

- Cache miss + `auth_provider.authenticate()` raises: propagate, do not cache (fail-closed).
- Process restart: empty cache; cold-start requests warm it quickly.

**Client disconnect**

- Starlette raises `ClientDisconnect` from `request.body()` or downstream awaits.
- Audit middleware catches it, emits log with `status_code=499` (Nginx convention for client-closed), latency recorded, no exception re-raised.
- No server-side state to clean per connection (stateless HTTP).

**Sandbox death**

- Sandbox process exits → TCP close → Starlette surfaces `ClientDisconnect` → handled as above. No tracking map needed.

## Testing

### Unit tests — `tests/unit/bricks/mcp/`

1. `test_auth_cache.py`
   - cache hit bypasses `auth_provider`
   - cache miss calls `auth_provider` and stores the result
   - TTL expiry triggers re-fetch
   - thread-safe under concurrent threads
   - failed auth not cached

2. `test_middleware_ratelimit.py`
   - anonymous key → IP-based limit
   - authenticated token → user-scoped limit
   - admin token → premium tier
   - 429 response shape and headers
   - Redis unreachable → fail-open path
   - Redis backed by `fakeredis`

3. `test_middleware_audit.py`
   - body-peek preserves downstream handler read
   - JSON log structure matches schema
   - event bus publish called with correct payload
   - `ClientDisconnect` → `status_code=499`
   - event bus failure does not block response

### Integration tests — `tests/e2e/self_contained/mcp/`

4. `test_mcp_http_concurrent.py` *(acceptance criteria 1, 2, and measurement for criterion 7)*
   - Boot MCP server with `MCP_TRANSPORT=http` + real Redis.
   - `asyncio.gather` 10 tasks, each with a distinct API key / zone.
   - Each task calls `nexus_grep` for a term that exists only in its zone.
   - Assert: no task observes another zone's results.
   - Assert: wall time ≪ serialized expectation (proves BM25S lock is not the bottleneck). Captured as a log/metric for the measurement record.

5. `test_mcp_http_rate_limit.py` *(acceptance criterion 4)*
   - Burst 400 requests from one token within 60s → ≥100 responses return 429.
   - Different tokens do not interfere with each other.

6. `test_mcp_http_audit.py` *(acceptance criterion 5)*
   - Capture stdout; subscribe to `mcp.request` event bus topic.
   - Assert each request emits both artifacts with matching fields.

7. `test_mcp_http_disconnect.py`
   - Client closes connection mid-request.
   - Assert audit log emits `status_code=499`, latency recorded, no leaked tasks.

### Coverage targets

- Unit ≥ 90% on new modules.
- Integration validates all five acceptance criteria.
- Metrics exposed via existing OTEL / Prometheus scrape surface.

## Rollout

- `MCP_RATE_LIMIT_ENABLED` defaults to `false`. CI and hub-mode deployments set it to `true`.
- Audit middleware is unconditional when `MCP_TRANSPORT=http`; logs scale with request volume and publish failures degrade gracefully.
- Auth cache is unconditional; cold start is indistinguishable from today's behavior.

## Open questions

None at design time. Exact FastMCP API for exposing the Starlette app (e.g., `streamable_http_app()`) will be verified during implementation and, if the installed version does not expose it, the design will document the alternative and flag for review.

## References

- Issue [#3779](https://github.com/nexi-lab/nexus/issues/3779) — this work
- Epic [#3777](https://github.com/nexi-lab/nexus/issues/3777) — parent
- Issue [#780](https://github.com/nexi-lab/nexus/issues/780) — HTTP API rate limiter (pattern reused)
- `src/nexus/bricks/mcp/server.py`, `auth_bridge.py`, `middleware.py`
- `src/nexus/server/rate_limiting.py`, `cache/dragonfly.py`, `services/event_bus/redis.py`
