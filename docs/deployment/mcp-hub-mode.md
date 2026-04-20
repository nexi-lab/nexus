# MCP HTTP Hub-Mode Configuration

The MCP server (`nexus.bricks.mcp.server`) supports three transports — `stdio`, `http`, and `sse`. Hub mode runs the server over `http` to serve many concurrent agents behind shared auth and rate limiting. This document covers hub-mode configuration added in Issue #3779.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `MCP_TRANSPORT` | `stdio` | Set to `http` to enable hub mode |
| `MCP_HOST` | `0.0.0.0` | Bind address |
| `MCP_PORT` | `8081` | Bind port |
| `MCP_RATE_LIMIT_ENABLED` | `false` | Enable per-token rate limits (SlowAPI) |
| `NEXUS_MCP_RATE_LIMIT_ANONYMOUS` | `60/minute` | IP-keyed quota for unauthenticated requests |
| `NEXUS_MCP_RATE_LIMIT_AUTHENTICATED` | `300/minute` | Token-keyed quota for regular users |
| `NEXUS_MCP_RATE_LIMIT_PREMIUM` | `1000/minute` | Quota for admin / premium tokens |
| `NEXUS_REDIS_URL` | unset | Redis/Dragonfly URL for rate limiter + audit publish |
| `DRAGONFLY_URL` | unset | Alternative to `NEXUS_REDIS_URL` (both accepted) |

Rate limiting falls back to in-process `memory://` storage when the Redis URL is unset or unreachable. Rate-limit checks fail open on runtime Redis errors so infrastructure outages don't block legitimate traffic.

## Audit log

Every HTTP request emits one JSON record to stdout and publishes the same payload to the Redis Pub/Sub channel `nexus:audit:mcp`:

```json
{
  "ts": "2026-04-19T22:14:03.128492+00:00",
  "event": "mcp.request",
  "token_hash": "3d29e8f7b4a1c0d2",
  "zone_id": "zone-01",
  "subject_id": "user-42",
  "rpc_method": "tools/call",
  "tool_name": "nexus_grep",
  "status_code": 200,
  "latency_ms": 47,
  "user_agent": "mcp-client/1.0"
}
```

- `token_hash` — first 16 hex chars of sha256(api_key). Raw keys are never logged.
- `rpc_method` / `tool_name` — extracted from the JSON-RPC body; null on non-JSON or malformed payloads.
- `status_code=499` — client disconnected before the response was sent (Nginx convention).
- Publish is fire-and-forget: failures increment `mcp_audit_publish_errors_total` and are logged at WARN, never blocking the response.

Subscribers can consume the channel with any Redis client:

```python
import redis.asyncio as redis
client = redis.from_url("redis://dragonfly:6379")
pubsub = client.pubsub()
await pubsub.subscribe("nexus:audit:mcp")
async for msg in pubsub.listen():
    ...
```

## Auth identity cache

Per-request API keys are resolved once, then cached in-process for 60 seconds (keyed by sha256[:16] of the key). Token rotation takes up to 60s to propagate through warm caches. Only positive authentication results are cached — failed auths retry immediately.

## Health endpoint

`GET /health` returns `{"status": "healthy", "service": "nexus-mcp"}` — unchanged from pre-hub behavior, suitable for liveness checks.

## Tier assignment

| Token | Tier |
|---|---|
| Missing / invalid | `anonymous` (IP-keyed) |
| Authenticated non-admin | `authenticated` |
| Admin (`is_admin=true`) | `premium` |

## Integration tests

Four integration tests in `tests/e2e/self_contained/mcp/` validate the full stack under concurrent load:

- `test_mcp_http_concurrent.py` — 10 clients × distinct zones, no cross-zone leakage.
- `test_mcp_http_rate_limit.py` — burst-429 enforcement + token isolation.
- `test_mcp_http_audit.py` — Redis Pub/Sub delivery + schema.
- `test_mcp_http_disconnect.py` — graceful client-disconnect handling.

All four tests currently skip when `MCP_HTTP_SEEDED_ZONES` is unset. A seeding fixture that provisions 10 zones + per-zone API keys + marker files is a prerequisite for running them; that fixture lands in a follow-up PR.

To run manually once the fixture exists:

```bash
MCP_TRANSPORT=http MCP_RATE_LIMIT_ENABLED=true \
  NEXUS_REDIS_URL=redis://localhost:6379 \
  MCP_HTTP_SEEDED_ZONES=true \
  uv run pytest tests/e2e/self_contained/mcp/ -v -k http
```
