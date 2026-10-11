# SANDBOX deployment profile

Nexus's `sandbox` profile is the lightweight runtime for running one Nexus
inside each AI-agent sandbox. It boots with **zero external services**
(SQLite + in-process LRU). Indexed search requires a Rust Search host
configured as described in the [Search deployment contract](search-plugin.md).

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
| Keyword search | Tantivy in the configured Rust Search host | Tantivy in the configured Rust Search host |
| Semantic search | HNSW in the Search host, with a configured embedder | HNSW in the Search host, with a configured embedder |
| HTTP surface | `/health`, `/api/v2/features` | Full `/api/v2/*` |
| MCP | Yes | Yes |
| Target RSS | <400 MB | Multi-GB |
| Boot time | <5 s (warm) | 15-60 s |

## Running

### From pip

```bash
pip install 'nexus-ai-fs[sandbox]'
nexusd --profile sandbox --data-dir ~/.nexus/sandbox --port 8000 --host 127.0.0.1
```

> The long-running server entrypoint is `nexusd`. The workspace `nexus` CLI
> does not expose a `serve` subcommand. `NEXUS_PROFILE=sandbox` works as an
> equivalent env-var override if you prefer.

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

features:
  # Everything off by default except SANDBOX's required set.
  # Re-enable specific bricks:
  # workflows: true
```

## Indexed search

Load the Rust Search plugin in the Kernel that owns the sandbox's workspace
mounts. Configure a local or API embedder on that host to enable semantic
and hybrid search. The [Search deployment contract](search-plugin.md) covers
the host process, embedding model and index configuration.

The Python server connects to that host through `NEXUS_SEARCH_PLUGIN_TARGET`.
`/api/v2/search/health` reports `disabled` if no host is connected. The host's
runtime capabilities report which query modes are configured; they do not
persist temporary model or index readiness.

## Federation

Federated queries use accessible zones and each peer's current Search
capabilities. Credentials and file permissions remain scoped to the owning
host. Configure peer routes on the cluster that owns the workspace and verify
zone permissions before enabling fanout. Backend failures are reported for
the affected zones; they do not establish that those zones contain no matches.

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

- **Boot fails with `'PyKernel' object has no attribute 'agent_registry'`**:
  the loaded Rust extension predates the `agent_registry` getter. Rebuild
  with `maturin develop -m rust/nexus-cdylib/Cargo.toml --features full`
  (running from a fresh `git pull` on `main` requires this when the kernel
  ABI moves).
- **Boot tries to connect to Postgres/Redis**: you have a leftover
  `NEXUS_DATABASE_URL` or `NEXUS_DRAGONFLY_URL` in your env. Unset them
  or explicitly set `NEXUS_CACHE_BACKEND=inmem`.
- **Search is disabled**: check `NEXUS_SEARCH_PLUGIN_TARGET`, the host's
  credentials and whether its Search plugin is loaded.
- **Semantic search returns `semantic_degraded=true`**: check the host's
  embedding configuration and the query's backend error.
- **Boot slower than 5 s**: Python interpreter cold-start on first run.
  Subsequent boots (warm) should hit target.
