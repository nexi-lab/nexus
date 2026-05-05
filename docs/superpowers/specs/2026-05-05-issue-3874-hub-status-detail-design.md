# Issue #3874 - Hub Status Detail Design

**Date**: 2026-05-05
**Issue**: [#3874](https://github.com/nexi-lab/nexus/issues/3874) - feat: richer nexus hub status output
**Follows**: [#3784](https://github.com/nexi-lab/nexus/issues/3784) - hub mode MVP

## Context

Issue #3784 shipped `nexus hub status` as a local admin diagnostic for hub hosts. The command currently reports endpoint, profile, Postgres health, Redis health, token counts, active clients, and 5-minute QPS. It reads Postgres directly via `get_session_factory()` and reads lightweight Redis counters written by `middleware_audit._record_metrics`.

Issue #3874 asks for richer operator detail without changing the default output:

- Zoekt index size and last-indexed timestamp per zone.
- txtai embed queue depth.
- Per-zone breakdown of active clients and QPS.
- Per-token last-seen, already stored as `APIKeyModel.last_used_at`.
- Rate-limit hit counts per tier.

## Decision

Add `nexus hub status --detail` and keep the existing `nexus hub status` output unchanged.

The detailed view stays local-first: it reads Postgres, Redis, and local filesystem/index metadata from the hub host. It does not depend on the MCP or API HTTP endpoint being healthy. This preserves `hub status` as a diagnostic command instead of turning it into a client of the service it is diagnosing.

## Non-goals

1. Remote admin/status queries over HTTP.
2. Prometheus metrics or a new `/metrics` endpoint.
3. New database tables for historical status data.
4. Exact persistent socket counting for MCP HTTP. Existing active-client metrics remain behavioral: clients seen in recent request counters.
5. A full search daemon control plane. Search fields are best-effort status facts surfaced through the hub CLI.

## CLI

Existing command:

```bash
nexus hub status [--json]
```

New flag:

```bash
nexus hub status --detail [--json]
```

Default, non-detail text output remains unchanged:

```text
endpoint:    http://0.0.0.0:8081/mcp
profile:     full
postgres:    ok
redis:       ok
tokens:      12 active, 3 revoked
connections: 7
qps (5m):    4.2
```

Detailed text output appends sections after the existing lines:

```text

zones:
zone        clients  qps_5m
eng         5        2.10
ops         2        0.80

tokens:
key_id        name      zones    admin  last_seen
6a1f...       alice     eng,ops  no     2026-05-05T14:20:10

rate limits:
tier           hits_5m
anonymous      3
authenticated  12
premium        0

search:
zone        zoekt_size  zoekt_last_indexed  txtai_queue_depth  last_indexed
eng         184.2 MiB   2026-05-05T14:18:00 n/a                2026-05-05T14:18:00
ops         n/a         n/a                 n/a                n/a
```

The exact table widths follow the existing `format_table` helper.

## JSON Shape

Without `--detail`, the JSON payload is unchanged:

```json
{
  "endpoint": "http://0.0.0.0:8081/mcp",
  "profile": "full",
  "postgres": "ok",
  "redis": "ok",
  "tokens": {"active": 12, "revoked": 3},
  "connections": 7,
  "qps_5m": 4.2
}
```

With `--detail`, the payload gains additive fields:

```json
{
  "detail": true,
  "zones": [
    {"zone_id": "eng", "clients": 5, "qps_5m": 2.1},
    {"zone_id": "ops", "clients": 2, "qps_5m": 0.8}
  ],
  "tokens_detail": [
    {
      "key_id": "6a1f...",
      "name": "alice",
      "zones": ["eng", "ops"],
      "admin": false,
      "created": "2026-05-05T13:00:00",
      "last_seen": "2026-05-05T14:20:10",
      "revoked": false,
      "revoked_at": null
    }
  ],
  "rate_limits": {
    "window_seconds": 300,
    "hits_by_tier": {"anonymous": 3, "authenticated": 12, "premium": 0}
  },
  "search": {
    "zones": [
      {
        "zone_id": "eng",
        "zoekt_index_size_bytes": 193147208,
        "zoekt_last_indexed": "2026-05-05T14:18:00",
        "txtai_queue_depth": null,
        "last_indexed": "2026-05-05T14:18:00"
      }
    ]
  }
}
```

All detail fields are best-effort. Missing Redis/search data is represented as `null` in JSON and `n/a` in text.

## Data Sources

### Postgres

`hub status --detail` reuses the same session opened by the base command and reads:

- token counts from `APIKeyModel.revoked`
- token detail rows from `APIKeyModel`
- token zones from `APIKeyZoneModel`
- active zones from `ZoneModel`

Token rows include `last_seen` sourced directly from `APIKeyModel.last_used_at`.

Postgres remains the only hard health dependency. If the DB is unavailable, the command still emits a parseable payload with `postgres: err` and exits 2, matching the current behavior.

### Redis

Existing aggregate counters remain:

- `nexus:hub:qps:<epoch-minute>`
- `nexus:hub:active:<epoch-minute>`

Add per-zone counters to `middleware_audit._record_metrics`:

- `nexus:hub:qps:zone:<zone_id>:<epoch-minute>`
- `nexus:hub:active:zone:<zone_id>:<epoch-minute>`

Each key gets the same 10-minute TTL as the existing counters. The zone identifier comes from `record["zone_id"]` when present; otherwise the middleware skips only the per-zone counters and still records aggregate counters.

Add rate-limit breach counters to `middleware_ratelimit._MCPRateLimitMiddleware` when a request is rejected:

- `nexus:hub:rate_limit:<tier>:<epoch-minute>`

These counters use the same Redis/Dragonfly URL as the rate limiter and expire after 10 minutes. If the limiter uses memory storage or Redis is unavailable, counters are best-effort and may be `n/a` in status output.

### Search/Index Metadata

Search detail is intentionally local and best-effort.

Zoekt:

- Use `NEXUS_ZOEKT_INDEX_DIR` when set; otherwise use the local default already used by `ZoektIndexManager` (`/app/data/.zoekt-index`).
- For each active zone, look for a zone-specific subdirectory or file prefix when present.
- If no per-zone path exists, report aggregate index metadata under a synthetic `all` zone only when it can be computed without guessing.
- `zoekt_index_size_bytes` is the recursive file size.
- `zoekt_last_indexed` is the newest mtime in the relevant index path.

txtai:

- The current repo does not expose a durable, CLI-readable txtai/embed queue depth.
- Initial implementation reports the field explicitly as `null`/`n/a`.
- Do not add a new database table or call the HTTP API to satisfy this field. A future search-daemon metric can populate the same JSON field without changing the CLI contract.

Last indexed:

- Prefer an existing zone-specific index timestamp if one exists.
- Otherwise use the best available local index mtime.
- Otherwise report `null`/`n/a`.

## Error Handling

- Postgres unavailable: same as today, output includes `postgres: err`, exit code 2.
- Redis unavailable: output includes `redis: n/a`; detail Redis fields are `null`/`n/a`; exit code remains 0 if Postgres is healthy.
- Search metadata unavailable: search fields are `null`/`n/a`; exit code remains 0 if Postgres is healthy.
- Malformed Redis values: ignore bad values for that key and continue.
- Permission errors while walking index directories: mark the affected zone as unavailable and continue.

## Implementation Boundaries

Most code stays in `src/nexus/cli/commands/hub.py`:

- Add `--detail` option to `hub_status`.
- Split payload construction into helpers so base and detail payloads can be tested without duplicating command logic.
- Add helper to fetch token rows and token-zone mappings in batched queries.
- Add helper to read Redis aggregate, per-zone, and rate-limit counters.
- Add helper to compute local index directory size and latest mtime.

Middleware changes are small and metric-only:

- `src/nexus/bricks/mcp/middleware_audit.py`: write per-zone QPS/active-client counters.
- `src/nexus/bricks/mcp/middleware_ratelimit.py`: write per-tier 429 counters on rejection.

No migration is required.

## Testing

Unit tests:

1. Existing `hub status --json` output remains unchanged without `--detail`.
2. `hub status --detail --json` includes `zones`, `tokens_detail`, `rate_limits`, and `search`.
3. Per-token `last_seen` reflects `APIKeyModel.last_used_at`.
4. Per-zone Redis counters are aggregated over the same 5-minute window as aggregate QPS.
5. Rate-limit counters are incremented by tier when the middleware returns 429.
6. Search metadata helper reports size and latest mtime for a temporary index directory.
7. Detail mode degrades to `null`/`n/a` when Redis or search metadata is missing.
8. Postgres error behavior and exit code 2 remain unchanged.

Target commands:

```bash
pytest tests/unit/cli/test_hub.py -v -k status
pytest tests/unit/bricks/mcp/test_middleware_audit_metrics.py -v
pytest tests/unit/bricks/mcp/test_middleware_ratelimit.py -v
```

## Acceptance Mapping

- `nexus hub status --detail` shows requested richer data: implemented through additive detail sections and JSON fields.
- Default `nexus hub status` output is unchanged: protected by an explicit unit test.
- Per-token last-seen: sourced from `APIKeyModel.last_used_at`.
- Per-zone active clients and QPS: sourced from new per-zone Redis counters.
- Rate-limit hit counts per tier: sourced from new best-effort Redis counters on 429.
- Zoekt/txtai metadata: surfaced where local sources exist, otherwise explicit `n/a` instead of silently omitting the fields.
