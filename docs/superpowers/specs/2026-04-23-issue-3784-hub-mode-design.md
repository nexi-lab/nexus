# Issue #3784 — Hub Mode Design

**Date**: 2026-04-23
**Issue**: [#3784](https://github.com/nexi-lab/nexus/issues/3784) — feat(P3-1): hub mode — shared nexus serving multiple agents via MCP
**Epic**: [#3777](https://github.com/nexi-lab/nexus/issues/3777) — Nexus as Context Layer for Secure Agent Runtimes
**Depends on**: [#3779](https://github.com/nexi-lab/nexus/pull/3779) — MCP multi-client hardening (merged)

## Context

Issue #3784 proposes a `NEXUS_MODE=hub` for multi-agent MCP serving. Exploration of the current codebase showed that the bulk of hub-mode runtime capability already shipped with #3779 (MCP HTTP multi-client hardening):

- Bearer-token auth via `APIKeyExtractionMiddleware`
- Token store `SQLAlchemyAPIKeyStore` (`APIKeyModel` in Postgres)
- Per-request zone scoping via `resolve_mcp_operation_context`
- Audit pipeline (stdout JSON + Redis `nexus:audit:mcp`)
- Rate-limit middleware (`slowapi`-based, per-tier)
- Concurrent HTTP client support (Starlette async, FastMCP)
- Existing `docker-compose.yml` with `mcp-server` service on port 8081

The delta for #3784 is therefore a **UX + docs layer** on top of #3779: a `nexus hub` CLI command group, a reference `docker-compose.hub.yml`, and an admin guide. The only runtime code change is a small metrics hook in `middleware_audit.py` (Redis `INCR` / `SADD` per request) to back `nexus hub status`. No new components, no schema changes.

## Non-goals

Deferred to follow-up issues, filed before closing #3784:

1. **Multi-zone per token** — current `APIKeyModel.zone_id` is single-valued; issue examples show `--zones eng,ops`. Full multi-zone requires schema change, updates to `auth_bridge.resolve_mcp_operation_context`, downstream search/read zone-set filtering, RLS policy updates, audit schema. Own brainstorm.
2. **Remote admin CLI** — `--remote <url> --admin-token <t>` flag for calling `hub token create` from off-host.
3. **Prometheus `/metrics` endpoint** — stats currently scraped locally only.
4. **Richer status output** — Zoekt/txtai queue depth, per-zone breakdown.
5. **Kubernetes/Helm deploy** — only docker-compose in MVP.
6. **Separate `NEXUS_MODE=hub` flag** — existing `NEXUS_PROFILE=full` is equivalent; adding a second mode flag would duplicate config surface.

## Architecture

Hub mode is a deployment pattern, not a new runtime. The stack reuses existing components:

| Concern | Component | Location |
|---------|-----------|----------|
| MCP HTTP server | FastMCP + Starlette | `src/nexus/bricks/mcp/server.py` |
| Bearer auth | `APIKeyExtractionMiddleware` | `src/nexus/cli/commands/mcp.py:50` |
| Token store | `SQLAlchemyAPIKeyStore` | `src/nexus/storage/auth_stores/sqlalchemy_api_key_store.py` |
| Zone scoping | `resolve_mcp_operation_context` | `src/nexus/bricks/mcp/auth_bridge.py:132` |
| Audit log | `middleware_audit.py` | `src/nexus/bricks/mcp/middleware_audit.py` |
| Rate limit | `middleware_ratelimit.py` | `src/nexus/bricks/mcp/middleware_ratelimit.py` |
| Profile config | `NEXUS_PROFILE=full` | `src/nexus/config.py:183` |
| Auth cache | `AuthIdentityCache` (60s TTL) | `src/nexus/bricks/mcp/auth_cache.py:31` |

New surface:

- `src/nexus/cli/commands/hub.py` — thin CLI wrapper group
- `src/nexus/cli/commands/_hub_common.py` — shared helpers (DB session, table formatting)
- ~20-line metrics hook inside `src/nexus/bricks/mcp/middleware_audit.py` (Redis `INCR nexus:hub:qps:<epoch-min>` + `SADD nexus:hub:active:<epoch-min> <key_id>`, each with 10-min `EXPIRE`; wrapped in the same `try/except` used by pub/sub and gated on `NEXUS_REDIS_URL`)
- `docker-compose.hub.yml` — reference deployment at repo root
- `docs/hub-deploy.md` — admin guide

## CLI: `nexus hub`

New command group in `src/nexus/cli/commands/hub.py`, wired into the `nexus` entry point alongside existing `admin`, `zone`, `mcp` groups. Keep the module under ~300 LOC by delegating to existing code paths.

### `nexus hub token create`

```
nexus hub token create --name <name> --zone <zone_id> [--admin] [--expires <duration>]
```

- Delegates to the existing `admin apikey create` path (same `SQLAlchemyAPIKeyStore.create` call).
- Prints the generated token (format defined by `api_key_ops.create_api_key`: `sk-<zone_prefix>_<subject_prefix>_<key_id_part>_<random_suffix>`) exactly once to stdout. Never retrievable after.
- `--admin` sets `is_admin=true` on the row.
- `--expires 90d` accepts Go-style durations; sets `expires_at`.
- Validates `--zone` against the existing zone list; exits 2 on unknown zone.
- CLI-level check: exits 1 if a non-revoked token with the same `name` already exists. `name` has no DB-level uniqueness constraint (`APIKeyModel.name` is `nullable=False` only), so this is enforced in the CLI, not the schema.

### `nexus hub token list`

```
nexus hub token list [--show-revoked] [--json]
```

- Tabular columns: `key_id | name | zone | admin | created | last_used | revoked_at`.
- Truncates `key_id` display to `nxs_xxxx…`; revocations shown only with `--show-revoked`.
- `--json` emits `{tokens: [...]}` for machine consumption.
- Reads `SQLAlchemyAPIKeyStore.list()`.

### `nexus hub token revoke`

```
nexus hub token revoke <key_id | name>
```

- Soft-delete: sets `revoked=1` and `revoked_at=now()` (model has both the flag and the timestamp). Row persists for audit.
- Resolves argument as `key_id` prefix first, falls back to `name` (errors on ambiguous match).
- Prints: `revoked <name> (<key_id>). effective within 60s (auth cache TTL).`
- Exits 1 if not found, 2 if ambiguous.

### `nexus hub zone list`

```
nexus hub zone list [--json]
```

- Alias calling the existing `nexus zone ls` implementation. No new code path for listing.

### `nexus hub status`

```
nexus hub status [--json]
```

Local-direct command (reads DB + Redis + Postgres stats). No RPC call. Output:

```
endpoint:    http://0.0.0.0:8081/mcp
profile:     full
postgres:    ok
redis:       ok
tokens:      12 active, 3 revoked
connections: 7
qps (5m):    4.2
```

Data sources:

| Field | Source |
|-------|--------|
| `endpoint` | `NEXUS_MCP_HOST`, `NEXUS_MCP_PORT` config |
| `profile` | `NEXUS_PROFILE` env |
| `postgres` | `SELECT 1` via `get_filesystem()` session; 2s timeout |
| `redis` | `PING` against `NEXUS_REDIS_URL`; 2s timeout; shows `unreachable` on failure |
| `tokens` | `SELECT COUNT(*) FILTER (...)` on `api_keys` |
| `connections` | Count of distinct `key_id` values INCR'd into Redis set `nexus:hub:active:<epoch-minute>` (written by the same middleware addition; SADD + 10-min EXPIRE). Represents distinct clients seen in the last minute. HTTP MCP is request-response (no persistent socket to count), so "active clients" is measured behaviorally rather than at the socket layer. Falls back to `n/a` if Redis unreachable. |
| `qps (5m)` | Sum of Redis keys `nexus:hub:qps:<epoch-minute>` for the last 5 minutes ÷ 300. Requires a small addition to `middleware_audit.py`: on each audited request, `INCR nexus:hub:qps:<epoch-minute>` with a 10-minute `EXPIRE`. Falls back to `n/a` if Redis unreachable. (The existing audit pub/sub is fire-and-forget and does not persist entries, so qps cannot be derived from replaying it.) |

`--json` emits the same fields as a JSON object.

## Token / admin model

**Reuse `APIKeyModel` unchanged.** Fields relied on: `key_id`, `name`, `zone_id`, `is_admin`, `expires_at`, `revoked`, `revoked_at`, `last_used_at`, `key_hash`.

**Token format** (already in use by existing auth, per `src/nexus/storage/api_key_ops.py`):
- Wire format: `sk-<zone_prefix>_<subject_prefix>_<key_id_part>_<random_suffix>` (prefix `sk-` from `API_KEY_PREFIX`)
- Storage: HMAC-SHA256 of the raw key in `key_hash` column (unique + indexed)
- Display-once: the raw key is returned by `create_api_key` at create time; only the hash is stored

**Bootstrap**. No separate `init` step and no env-seed. On a fresh database, the operator (who has shell access to the hub host) runs:

```
docker compose -f docker-compose.hub.yml exec nexus \
  nexus hub token create --name root --admin --zone root
```

The DB insert succeeds because the command runs locally via `get_filesystem()`. Subsequent admin tokens are created by any existing `is_admin=true` token holder using the same CLI.

**Revocation propagation**. Revocation is a soft delete. `AuthIdentityCache` has a 60s TTL, so a revoked token may remain usable for up to 60 seconds after revocation. Documented explicitly in `docs/hub-deploy.md`.

**Zone assignment**. `--zone` is required at create-time, single value. Validated against the existing zone list. Multi-zone per token is deferred (see Non-goals §1).

## Deploy artifact: `docker-compose.hub.yml`

Reference deployment at repo root. Separate file (not a compose profile) so it's discoverable via `ls docker-compose*` and safe to copy without mutating the existing dev stack.

```yaml
services:
  nexus:
    image: ghcr.io/nexi-lab/nexus:latest
    environment:
      NEXUS_PROFILE: full
      NEXUS_DATABASE_URL: postgresql://nexus:${POSTGRES_PASSWORD}@postgres:5432/nexus
      NEXUS_MCP_HOST: 0.0.0.0
      NEXUS_MCP_PORT: 8081
      NEXUS_REDIS_URL: redis://redis:6379
    ports:
      - "8081:8081"
    volumes:
      - nexus-data:/data
    depends_on:
      postgres: {condition: service_healthy}
      redis: {condition: service_started}
    command: ["nexus", "mcp", "serve", "--transport", "http"]
  postgres:
    image: pgvector/pgvector:pg17
    environment:
      POSTGRES_DB: nexus
      POSTGRES_USER: nexus
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    volumes:
      - postgres-data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U nexus"]
  redis:
    image: redis:7-alpine
    volumes:
      - redis-data:/data
volumes:
  nexus-data:
  postgres-data:
  redis-data:
```

Notes:
- Only the MCP port (8081) is exposed; the internal RPC port (2026) is not published.
- `POSTGRES_PASSWORD` is expected from a `.env` file or orchestrator secret; the file itself ships without a default.
- TLS is expected to be terminated at a reverse proxy (Caddy or nginx); a sample snippet is included in `docs/hub-deploy.md`.

## Docs: `docs/hub-deploy.md`

Audience: operators deploying a Nexus hub for an agent fleet. Sections:

1. **Quickstart** — clone, set `POSTGRES_PASSWORD`, `docker compose -f docker-compose.hub.yml up -d`, create the first admin token.
2. **Token lifecycle** — create, distribute (display-once), revoke, 60s cache TTL, expiry.
3. **Zone model** — one zone per token in MVP; link to multi-zone follow-up.
4. **Agent client config** — `X-Nexus-API-Key: <token>` header or `Authorization: Bearer <token>` (token begins with `sk-`), `mcp_endpoint_url` examples for Claude Code, Codex, Goose.
5. **Operations** — `nexus hub status`, where audit logs land (stdout + Redis channel), how rate-limit tiers work, how to inspect with `docker compose logs`.
6. **TLS** — recommended Caddy block and nginx block; no built-in TLS.
7. **Backup/restore** — `pg_dump`/`pg_restore` against the `postgres` service; volume backup pattern.
8. **Troubleshooting** — common failure modes: Postgres unhealthy, Redis down (audit lost but serving continues), token rejected after revoke (cache TTL), rate-limit 429 signatures.

## Testing

**Unit** — `tests/unit/cli/test_hub.py`:
- argparse surface for every subcommand
- Human vs `--json` output shapes
- Error exit codes (unknown zone → 2, duplicate name → 1, ambiguous revoke → 2)
- Mock `SQLAlchemyAPIKeyStore` and `get_filesystem()` session

**Integration** — `tests/e2e/self_contained/cli/test_hub_flow.py`:
Real Postgres and Redis from the self-contained harness. End-to-end:

1. `hub token create --admin --zone root` → token string captured
2. `hub token list` shows the new row
3. HTTP POST to MCP endpoint with `X-Nexus-API-Key` → 200
4. `hub token revoke` → row has `revoked_at`; after cache reset, MCP request → 401
5. `hub status --json` → fields present, `postgres=ok`, `tokens.active=1`

**Load (no new test)** — AC "serves 50+ concurrent clients" is already covered by `tests/e2e/self_contained/mcp/test_mcp_http_concurrent.py` (merged with #3779). Cite in the PR description rather than duplicate.

## Acceptance criteria (issue #3784)

After this work, with follow-up issues filed:

- [x] Hub mode serves MCP to 50+ concurrent clients — delivered by #3779, cited in PR.
- [x] Token-based auth with per-token zone scoping — single-zone in MVP; multi-zone tracked in follow-up.
- [x] Management CLI for tokens and zones — `nexus hub token`, `nexus hub zone`.
- [x] Monitoring: active connections, queries/sec — `nexus hub status`.
- [x] Docker Compose deployment guide — `docker-compose.hub.yml` + `docs/hub-deploy.md`.

## Rollout

Single PR targeting `develop`. Commits:

1. `feat(#3784): add nexus hub CLI group` — `hub.py`, `_hub_common.py`, unit tests.
2. `feat(#3784): metrics hook for hub status` — INCR/SADD addition in `middleware_audit.py`, unit tests.
3. `feat(#3784): add hub status command` — CLI wiring reading the Redis counters, unit tests.
4. `test(#3784): hub e2e token flow` — integration test.
5. `docs(#3784): hub deployment guide` — `docker-compose.hub.yml`, `docs/hub-deploy.md`.
6. `chore(#3784): file follow-up issues` — links in PR description.

No migrations. No behavior changes to existing endpoints. Backward-compatible.
