# FULL deployment profile

Nexus's `full` profile is the all-feature shared hub for a team. The
`shared`/`demo` preset stack provisions **PostgreSQL + Dragonfly** (plus
the Nexus server), the complete brick set, and local inference. Keyword
search uses **BM25S**; Zoekt is an *optional, separately-run* code-search
backend the preset does **not** start (see the user guide, "What about
Zoekt?"). Use this profile for a shared node that exposes the full
CLI/RPC surface; use `sandbox` for per-agent clients that connect to it.

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
| Keyword search | BM25S (Zoekt optional, not started by the preset) |
| Bricks | LITE + search, pay, llm, mcp, workspace, snapshot, versioning, identity, delegation, share_link, portability, task_manager, observability, … (see contract test) |
| Federation | OFF (that is the `cloud` profile) |
| Auth | static (`NEXUS_API_KEY`) or database (`DatabaseAPIKeyAuth`) |
| Remote clients | `profile=remote` SDK; requires gRPC, not just HTTP |

## Running

### Via the daemon directly (supported)

```bash
nexusd --profile full --host 0.0.0.0 --port 2026 \
  --data-dir ./nexus-data --auth-type static --api-key "$NEXUS_API_KEY"
```

`nexusd --profile remote` is rejected: a daemon cannot be a thin
client of another daemon.

### Via the managed stack (known issue — see below)

```bash
nexus init --preset shared
nexus up                 # ⚠ currently exits rc=1 (see note)
eval $(nexus env)
nexus status
```

> **Known issue (Bug B, tracked):** `nexus up --preset shared`
> currently returns a non-zero exit code because the `nexus up` health
> gate waits on a `zoekt` service that the `shared` preset does not
> start. **The hub itself boots and serves correctly** (`/health`,
> `/api/v2/features`, gRPC all work) — only the `nexus up` wrapper's
> aggregate exit status is wrong. This is a pre-existing `nexus up`
> health-gate defect, out of this docs/test issue's scope, tracked in
> the #4132 design spec ("Bug B"). Until it is fixed, prefer the
> **direct daemon path above**; if you use the stack, the containers
> are healthy despite the rc=1 (verify with `nexus status` / a direct
> `curl $URL/health`).

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

It prints JSON with a `_sources` map marking each field's provenance:

- **hub-authoritative** (from the hub's `/api/v2/features`):
  `deployment_profile`, `bricks`, `disabled_bricks`, `mode`, `version`.
- **client-inferred** (NOT hub-authoritative — derived from the hub's
  profile name via this CLI's `DeploymentProfile`; may differ under
  CLI/server version skew): `client_inferred_drivers`.
- **local/contextual**: `auth_mode` reflects the local `nexus.yaml`
  only for the locally-managed stack; for an explicit remote target
  (`--url` / `NEXUS_URL` / global `--profile`) it is `"unknown"`.
- **invariant**: `grpc_required` is always `true` (the remote SDK path
  requires gRPC, not just HTTP).

`nexus profile contract --url <hub> --api-key <key>` targets a remote
hub; `nexus --profile <name> profile contract` uses a saved connection
profile.

## Benchmark guidance

Boot time and idle RSS are setup-path metrics, not CI gates; the FULL
stack (PostgreSQL + Dragonfly + the Nexus server) targets multi-GB RSS and a
15–60 s boot. `health` / `features` / `Ping` are control-plane calls
with sub-100 ms expectations on a warm hub. There is no steady-state
data-plane hot path in the startup story.

## Troubleshooting

- Remote SDK hangs / connection refused: gRPC port unreachable — set
  `NEXUS_GRPC_PORT`, confirm `nexus status` shows gRPC healthy.
- 401 from every call: static auth with no `NEXUS_API_KEY`, or
  database auth with no issued key.
