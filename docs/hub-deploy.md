# Nexus Hub Deployment Guide

This guide shows how to run a **Nexus hub** — a shared MCP server that many
agents connect to with individual bearer tokens. Hub mode is the deployment
pattern introduced by issue #3784; the underlying runtime is unchanged from
the standard `NEXUS_PROFILE=full` stack.

## 0. Architecture

The reference compose ships **two** Nexus services:

- `nexus` — `nexusd` RPC server on port 2026, backed by postgres (auth) +
  redis (metrics). This is the source of truth for auth, zones, and files.
- `mcp-frontend` — `nexus mcp serve --transport http --url http://nexus:2026`
  on port 8081. It extracts the bearer from each request and opens a
  per-request `NexusFS` remote connection to the RPC server with that
  token as the api key, so the RPC server's `DatabaseAPIKeyAuth` enforces
  per-token identity/zone on every tool call. Missing/invalid/expired/
  revoked tokens are rejected with 401 at the RPC layer.

`mcp-frontend` deliberately does *not* set `NEXUS_API_KEY`; unauthenticated
requests therefore fail at the RPC layer rather than falling through to an
ambient frontend identity.

## 1. Quickstart

```bash
# 1. Get the repo and the reference compose file.
git clone https://github.com/nexi-lab/nexus.git
cd nexus

# 2. Set the Postgres password (use a secret manager in production).
export POSTGRES_PASSWORD="$(openssl rand -base64 32)"

# 3. Start the stack.
docker compose -f docker-compose.hub.yml up -d

# 4. Create the first admin token (bootstrap).
#    Run inside the `nexus` (RPC) container — it has direct Postgres access.
docker compose -f docker-compose.hub.yml exec nexus \
  nexus hub token create --name root --admin --zone root
# → prints the raw token once. Save it immediately; it cannot be retrieved.
```

The MCP endpoint is now at `http://<host>:8081/mcp`. Terminate TLS at a
reverse proxy before exposing it to the internet (see §6).

## 2. Token lifecycle

- **Create** — `nexus hub token create --name <name> --zone <zone> [--admin] [--expires 90d]`.
  The raw token (`sk-…`) is printed once.
- **List** — `nexus hub token list [--show-revoked] [--json]`.
- **Revoke** — `nexus hub token revoke <name|key_id>` (soft-delete).

The auth identity cache has a 60-second TTL, so a revoked token may remain
usable for up to 60 seconds. Plan rotations around this window.

## 3. Zone model (MVP)

Each token is scoped to **one** zone (`--zone`). Requests made with the
token only see files and indexes in that zone. Multi-zone-per-token is
tracked as a follow-up to #3784.

List zones: `nexus hub zone list`.

## 4. Agent client config

Clients authenticate with either header:

```
X-Nexus-API-Key: sk-…
# or
Authorization: Bearer sk-…
```

Client-side configuration depends on the agent framework — consult the
MCP client docs for Claude Code, Codex, or Goose and supply the endpoint
URL + one of the headers above.

## 5. Operations

- **`nexus hub status`** — endpoint, profile, postgres/redis health, token
  counts, connections, and 5-minute QPS. JSON output via `--json`.
- **Audit logs** — every MCP request emits a JSON line to stdout
  (`docker compose logs -f nexus`) and publishes to Redis channel
  `nexus:audit:mcp` for external consumers.
- **Rate limits** — configured per tier in the existing rate-limit
  middleware. See `src/nexus/bricks/mcp/middleware_ratelimit.py`.

## 6. TLS

Hub mode does not ship with built-in TLS. Run a reverse proxy.

Caddy (minimal):

```caddy
nexus.example.com {
    reverse_proxy localhost:8081
}
```

Nginx (excerpt):

```nginx
server {
    listen 443 ssl http2;
    server_name nexus.example.com;
    ssl_certificate     /etc/letsencrypt/live/nexus.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/nexus.example.com/privkey.pem;
    location /mcp {
        proxy_pass http://127.0.0.1:8081/mcp;
        proxy_set_header Host $host;
        proxy_http_version 1.1;
    }
}
```

## 7. Backup & restore

Postgres is the system of record (tokens, zones, index metadata). Run
`pg_dump` on a schedule:

```bash
docker compose -f docker-compose.hub.yml exec postgres \
  pg_dump -U nexus nexus > backup.sql
```

The `nexus-data` volume holds local indexes and transient state; back it up
with any standard volume backup tool.

## 8. Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| `nexus hub status` shows `postgres: err` | `NEXUS_DATABASE_URL` mis-set or Postgres unhealthy. Check `docker compose ps`. |
| `redis: n/a` in status | Redis unreachable. Metrics degrade (qps/connections become `n/a`); serving continues. |
| Client gets 401 just after revoke | Auth cache TTL — wait 60 s or restart the MCP server. |
| 429 from client | Rate limit tier; see `middleware_ratelimit.py`. |
| CLI exits `RuntimeError: NEXUS_DATABASE_URL not set` | Run `nexus hub …` inside the container (`docker compose exec nexus nexus hub …`) or export the env var manually. |

## 9. What's next

Tracked as follow-up issues to #3784:

- Multi-zone tokens
- Remote admin CLI (admin over MCP with a bootstrap token)
- Prometheus `/metrics` endpoint
- Richer `hub status` (Zoekt/txtai queue depth, per-zone breakdown)
- Kubernetes/Helm deploy
