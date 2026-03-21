# Nexus CLI Reference

## Quick Start

```bash
# Initialize a project (one-time)
nexus init --preset shared

# Start the stack (Docker Compose)
nexus up

# Load connection env vars into your shell
eval $(nexus env)

# Run a command with env vars injected
nexus run python my_agent.py
```

## Commands

### `nexus init`

Initialize a new Nexus project. Creates `nexus.yaml`, data directories, and optionally TLS certificates.

```bash
nexus init                              # local embedded (no Docker)
nexus init --preset shared              # one shared node (Postgres, Dragonfly, Zoekt)
nexus init --preset demo                # shared + demo seed data
nexus init --preset shared --tls        # with self-signed TLS certs
nexus init --preset shared --with nats  # add NATS event bus
nexus init --preset shared --channel edge --accelerator cuda
```

**Presets:**
| Preset | Services | Auth | Docker |
|--------|----------|------|--------|
| `local` | None (embedded) | none | No |
| `shared` | nexus, postgres, dragonfly, zoekt | static | Yes |
| `demo` | nexus, postgres, dragonfly, zoekt | database | Yes |

### `nexus up`

Start the Docker Compose stack. Resolves port conflicts, pulls (or reuses) images, health-checks all services.

```bash
nexus up                        # start from nexus.yaml
nexus up --build                # build from local Dockerfile
nexus up --pull                 # discard local build, pull from remote
nexus up --with nats            # add NATS event bus
nexus up --port-strategy prompt # ask on port conflicts (default: auto)
nexus up --timeout 300          # health check timeout in seconds
```

**Image resolution:**
- Default: pulls from `ghcr.io/nexi-lab/nexus:{channel}`
- `--build`: builds from local Dockerfile, tags as `nexus:local-{hash}`
- After `--build`, subsequent `nexus up` reuses the local image (no pull)
- `--pull`: clears local build mode, pulls from remote

**Runtime state** is written to `{data_dir}/.state.json` (not `nexus.yaml`), so the declarative config stays clean across git worktrees.

### `nexus down`

Stop and remove containers (volumes persist).

```bash
nexus down                # stop services
nexus down --volumes      # stop and remove volumes
```

### `nexus stop` / `nexus start`

Lightweight pause/resume. No port checks, no image pulls, no health polling.

```bash
nexus stop                # pause containers (fast)
nexus start               # resume containers (fast)
```

### `nexus env`

Print connection environment variables from `nexus.yaml` + `.state.json`.

```bash
eval $(nexus env)              # load into current shell
nexus env --dotenv > .env      # write .env file
nexus env --json               # machine-readable JSON
nexus env --shell fish | source
```

**Variables emitted:**
| Variable | Example |
|----------|---------|
| `NEXUS_URL` | `http://localhost:2026` |
| `NEXUS_API_KEY` | `sk-...` |
| `NEXUS_GRPC_HOST` | `localhost:2028` |
| `NEXUS_GRPC_PORT` | `2028` |
| `DATABASE_URL` | `postgresql://postgres:nexus@localhost:5432/nexus` |
| `NEXUS_TLS_CERT` | `/path/to/cert` (if TLS) |
| `NEXUS_TLS_KEY` | `/path/to/key` (if TLS) |
| `NEXUS_TLS_CA` | `/path/to/ca` (if TLS) |

### `nexus run <cmd>`

Run a command with Nexus env vars injected. Interactive (stdin/stdout pass-through).

```bash
nexus run python my_agent.py
nexus run pytest tests/
nexus run bash                  # interactive shell with env vars
```

### `nexus status`

Display service health. Reads ports from `nexus.yaml`/`.state.json` (not hardcoded).

```bash
nexus status               # Rich table
nexus status --json        # machine-readable
nexus status --watch       # auto-refresh every 2s
```

### `nexus logs`

```bash
nexus logs                 # all services
nexus logs nexus           # single service
nexus logs --tail 50       # last 50 lines
```

### `nexus restart` / `nexus upgrade`

```bash
nexus restart              # down + up
nexus restart --build      # rebuild and restart
nexus upgrade              # pull latest image for channel
nexus upgrade --channel edge
```

## Architecture

### Config vs State

```
nexus.yaml                 ← declarative config (checked into git)
  preset, data_dir, ports (desired), image_ref, api_key, tls, services

{data_dir}/.state.json     ← runtime state (gitignored, written by nexus up)
  ports (actual), api_key (active), image_used, build_mode, tls paths
```

`nexus.yaml` is never mutated by `nexus up`. Only `nexus init` and `nexus upgrade` write to it.

### Concurrent Worktrees

Each worktree gets isolated state via:
- `COMPOSE_PROJECT_NAME = nexus-{md5(data_dir)[:8]}` — unique Docker project
- Docker volumes/networks scoped by project name — no collision
- `{data_dir}/.state.json` — per-worktree runtime state
- Auto port resolution — conflicts detected and resolved without mutating `nexus.yaml`

```bash
# Worktree A
cd ~/nexus/worktrees/feature-a && nexus init --preset shared && nexus up --build

# Worktree B (concurrent)
cd ~/nexus/worktrees/feature-b && nexus init --preset shared && nexus up --build
# → auto-resolves to different ports, different Docker project, different local image tag
```

### Port Mapping

Docker Compose maps `host_port:container_port`. Container ports are fixed (2026 HTTP, 2028 gRPC). Host ports vary with auto-resolution.

### Default Ports

| Service | Default Port |
|---------|-------------|
| HTTP | 2026 |
| gRPC | 2028 |
| PostgreSQL | 5432 |
| DragonflyDB | 6379 |
| Zoekt | 6070 |
