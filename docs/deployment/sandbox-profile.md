# SANDBOX deployment profile

Nexus's `sandbox` profile is the lightweight runtime for running one Nexus
inside each AI-agent sandbox. It boots with **zero external services**
(SQLite + in-process LRU + BM25S; no PostgreSQL, Dragonfly, or Zoekt).

Target: ~300-400 MB RSS, <5 s warm boot.

## When to use

- You want per-agent isolation: one Nexus instance per sandbox, with its
  own storage and policy boundary.
- The sandbox's outer orchestrator (e.g.
  [agentenv](https://github.com/windoliver/agentenv)) provisions the
  sandbox and injects `NEXUS_URL` / `NEXUS_API_KEY` so the sandbox can
  federate to a peer Nexus or hub.
- You don't want to operate PostgreSQL + Dragonfly inside every sandbox.

Use the `full` profile for a shared Nexus hub; use `sandbox` for the
per-sandbox clients that talk to it.

## What you get

| Surface | SANDBOX | FULL |
|---|---|---|
| Storage (metastore + records) | SQLite | PostgreSQL |
| Cache | In-process LRU | Dragonfly / Redis |
| Keyword search | BM25S mmap | BM25S + Zoekt |
| Semantic search | Federated to peers; BM25S fallback | Local txtai + federation |
| HTTP surface | `/health`, `/api/v2/features` | Full `/api/v2/*` |
| MCP | Yes | Yes |
| Target RSS | <400 MB | Multi-GB |
| Boot time | <5 s (warm) | 15-60 s |

## Running

### From pip

```bash
pip install 'nexus-ai-fs[sandbox]'
NEXUS_PROFILE=sandbox nexus serve
```

### From Docker

```bash
docker run --rm \
  -e NEXUS_PROFILE=sandbox \
  -e NEXUS_DATA_DIR=/data \
  -v sandbox-data:/data \
  -p 8000:8000 \
  ghcr.io/nexi-lab/nexus:sandbox
```

### Config file

```yaml
profile: sandbox
# SANDBOX defaults fill these in automatically; override only if needed:
#   backend: path_local
#   data_dir: ~/.nexus/sandbox
#   db_path: ~/.nexus/sandbox/nexus.db
#   cache_size_mb: 64
#   enable_vector_search: false

features:
  # Everything off by default except SANDBOX's required set.
  # Re-enable specific bricks:
  # workflows: true
```

## Federation

SANDBOX delegates semantic search to configured peer zones. Point it at
a hub zone via the federation config:

```yaml
federation:
  peers:
    - zone_id: main-hub
      url: https://nexus.example.com
      token: ${NEXUS_HUB_TOKEN}
```

When all peers are unreachable, search returns BM25S keyword results
stamped with `semantic_degraded=true` on each result. The MCP client
can surface this to the agent so it knows the results are keyword-only
for that request.

## What's off by default in SANDBOX

The following bricks are NOT enabled in SANDBOX. Re-enable individually
with `features.<brick>: true`:

`pay`, `llm`, `workflows`, `sandbox` (the sandbox-provisioning brick,
distinct from this profile), `observability`, `uploads`, `resiliency`,
`access_manifest`, `catalog`, `delegation`, `identity`, `share_link`,
`versioning`, `workspace`, `portability`, `snapshot`, `task_manager`,
`acp`, `discovery`, `memory`, `skills`.

Enabled in SANDBOX (10 bricks = LITE + SEARCH + MCP + PARSERS):
`eventlog`, `namespace`, `permissions`, `cache`, `ipc`, `scheduler`,
`agent_runtime`, `search`, `mcp`, `parsers`.

Note: federation is auto-detected from ZoneManager / peer config; it
does not require a brick flag.

## Troubleshooting

- **Boot fails with `ModuleNotFoundError: bm25s`**: install the extras
  with `pip install 'nexus-ai-fs[sandbox]'`.
- **Boot tries to connect to Postgres/Redis**: you have a leftover
  `NEXUS_DATABASE_URL` or `NEXUS_DRAGONFLY_URL` in your env. Unset them
  or explicitly set `NEXUS_CACHE_BACKEND=inmem`.
- **Semantic search returns `semantic_degraded=true`**: no peer is
  reachable. Check `federation.peers` in your config + network access.
- **Boot slower than 5 s**: Python interpreter cold-start on first run.
  Subsequent boots (warm) should hit target.
