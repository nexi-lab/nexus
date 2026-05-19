# FULL deployment profile

Nexus's `full` profile is the all-feature shared hub for a team:
PostgreSQL + Dragonfly + Zoekt, the complete brick set, and local
inference. Use it for a shared node that exposes the full CLI/RPC
surface; use `sandbox` for per-agent clients that connect to it.

## Three things called "profile" (read this first)

| Term | Where | What it controls |
|---|---|---|
| Docker Compose profile (`core`, `cache`) | `nexus up` / `docker-compose.yml` | Which containers start |
| CLI connection profile | `nexus profile use <name>` (`~/.nexus/config.yaml`) | Which hub the CLI talks to |
| Deployment profile (`full`) | `nexusd --profile full` / `NEXUS_PROFILE` | Which bricks/drivers are enabled |

`nexus up` runs the FULL deployment profile because
`docker-compose.yml` sets `NEXUS_PROFILE=full`. No `nexus init` preset
is literally named `full`; the `shared` and `demo` presets both run
FULL.

## What you get

| Surface | FULL |
|---|---|
| Storage | PostgreSQL |
| Cache | Dragonfly / Redis |
| Keyword search | BM25S + Zoekt |
| Bricks | LITE + search, pay, llm, mcp, workspace, snapshot, versioning, identity, delegation, share_link, portability, task_manager, observability, … (see contract test) |
| Federation | OFF (that is the `cloud` profile) |
| Auth | static (`NEXUS_API_KEY`) or database (`DatabaseAPIKeyAuth`) |
| Remote clients | `profile=remote` SDK; requires gRPC, not just HTTP |

## Running

### Via the stack (recommended)

```bash
nexus init --preset shared
nexus up
eval $(nexus env)
nexus status
```

### Via the daemon directly

```bash
nexusd --profile full --host 0.0.0.0 --port 2026 \
  --data-dir ./nexus-data --auth-type static --api-key "$NEXUS_API_KEY"
```

`nexusd --profile remote` is rejected: a daemon cannot be a thin
client of another daemon.

## Auth

- **static**: `--api-key` / `NEXUS_API_KEY` / `NEXUS_API_KEY_FILE`.
  Request without a key → 401; with key → 200.
- **database**: `--auth-type database` + `--database-url` (or
  `POSTGRES_URL`) → `DatabaseAPIKeyAuth`. Use for multi-user key
  issuance/revocation.

## Remote client

```python
from nexus.sdk import connect

nx = connect(config={"profile": "remote",
                     "url": "http://hub:2026",
                     "api_key": "..."})
```

Set `NEXUS_GRPC_PORT` if the server's gRPC port is non-default. The
HTTP URL alone is not sufficient.

## Correctness check you can run

The FULL contract is locked by
`tests/unit/core/test_full_profile.py`. Run:

```bash
pytest tests/unit/core/test_full_profile.py -v
```

You can also verify a *running* hub's resolved contract directly:

```bash
nexus profile contract
```

It prints the live `deployment_profile`, enabled `bricks`, `drivers`,
`grpc_required`, and `auth_mode` as JSON (sourced from the hub's
`/api/v2/features`).

## Benchmark guidance

Boot time and idle RSS are setup-path metrics, not CI gates; the FULL
stack (PostgreSQL + Dragonfly + Zoekt) targets multi-GB RSS and a
15–60 s boot. `health` / `features` / `Ping` are control-plane calls
with sub-100 ms expectations on a warm hub. There is no steady-state
data-plane hot path in the startup story.

## Troubleshooting

- Remote SDK hangs / connection refused: gRPC port unreachable — set
  `NEXUS_GRPC_PORT`, confirm `nexus status` shows gRPC healthy.
- 401 from every call: static auth with no `NEXUS_API_KEY`, or
  database auth with no issued key.
