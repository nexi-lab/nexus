# CLI Design: `nexus` / `nexusd` Split

## Motivation

Nexus has two fundamentally different runtime modes:

1. **In-process (invocation-style)**: Agent imports `nexus` as a library,
   calls syscalls directly. No daemon, no RPC. This is the local-first default.
2. **Daemon (persistent)**: A long-running `nexusd` process on a node, exposing
   gRPC/HTTP. Other processes (CLI, agents, peers) connect via RPC.

Previously, both client CLI commands and server startup lived in a single
`nexus` binary, making it unclear whether a command was remote (RPC) or local
(in-process). After PR #2842 deleted the `serve`/`start` commands, there is
no way to start the daemon at all.

This design introduces a clean two-binary split inspired by Unix conventions
(`docker`/`dockerd`, `consul`/`consul agent`) and the Nexus OS metaphor.

## The Two Binaries

### `nexus` — Client CLI (remote, via RPC)

Pure client. Every command connects to a running `nexusd` via gRPC/HTTP.
Never starts a server, never touches local storage directly.

```
nexus <command> [args] [flags]
```

Connection target resolved by (highest priority first):
1. `--remote-url` / `--remote-api-key` flags
2. `NEXUS_URL` / `NEXUS_API_KEY` environment variables
3. Active profile in `~/.nexus/config.yaml`

Examples:
```bash
export NEXUS_URL=http://localhost:2026

nexus ls /workspace --json
nexus cat /workspace/main.py
nexus write /test.txt "hello"
nexus glob "**/*.py"
nexus grep "import nexus" -n
nexus admin create-user alice
nexus rebac check user:alice read /file.txt
nexus status --json
nexus doctor --json
nexus profile use production
```

### `nexusd` — Node Daemon (local, long-running process)

Starts and runs the Nexus node. Manages local storage, serves RPC,
participates in federation. Like `sshd`, `dockerd`, `systemd` — the `d`
suffix is the Unix daemon convention.

```
nexusd [flags]
```

Examples:
```bash
# Start with defaults (port 2026, auto-detect profile)
nexusd

# Explicit configuration
nexusd --port 2026 --host 0.0.0.0 --data-dir /var/lib/nexus

# With config file
nexusd --config /etc/nexus/config.yaml

# Join federation on startup
nexusd --join peer1.example.com:2026 --zone us-west

# Foreground with debug logging
nexusd --log-level debug
```

## Why Not "server"?

Nexus is local-first. In federation, every node is a **peer**, not a
"server" serving "clients". The word "daemon" is neutral — it describes
a long-running background process without implying centralized architecture.

| Term   | Implication                | Fit for Nexus |
|--------|----------------------------|---------------|
| server | Central, serves clients    | No — peers    |
| daemon | Background process         | Yes — neutral |
| node   | Participant in a network   | Yes — federation |
| agent  | Autonomous actor           | Conflicts with AI agents |

## Command Ownership

| Command | Binary | Why |
|---------|--------|-----|
| `ls`, `cat`, `write`, `cp`, `rm` | `nexus` | File operations via RPC |
| `glob`, `grep` | `nexus` | Search via RPC |
| `admin`, `rebac`, `versions` | `nexus` | Management via RPC |
| `status`, `doctor` | `nexus` | Health checks via RPC |
| `profile`, `connect`, `config` | `nexus` | Local CLI config (no RPC) |
| Start daemon | `nexusd` | Starts the node process |
| `join` (federation) | `nexusd` | Node-local operation |
| FUSE `mount` / `unmount` | `nexusd` | Node-local, needs local NexusFS |
| `mcp serve` | `nexusd` | Starts MCP adapter process |

## Removed Commands

| Command | Removed in | Reason |
|---------|------------|--------|
| `nexus serve` | PR #2842 | Replaced by `nexusd` |
| `nexus start` | PR #2842 | Replaced by `nexusd` |
| `nexus up/down/logs` | This PR | Thin docker-compose wrapper, no added value |

Developers who used `docker compose` can continue doing so. The
`docker-entrypoint.sh` is updated to call `nexusd` instead of `nexus serve`.

## Entry Points (pyproject.toml)

```toml
[project.scripts]
nexus  = "nexus.cli:main"
nexusd = "nexus.daemon:main"
```

## `nexusd` Startup Sequence

1. Parse CLI flags + load config (YAML file + env vars)
2. Initialize storage pillars (Metastore, RecordStore, ObjectStore)
3. Create NexusFS via factory orchestrator
4. Create FastAPI app (`create_app()`)
5. Run uvicorn server (blocking)
   - Lifespan context handles 11 async startup phases
   - Graceful shutdown on SIGTERM in reverse order

## Environment Variables

`nexusd` reads all configuration from environment variables (same as before):

| Variable | Default | Description |
|----------|---------|-------------|
| `NEXUS_HOST` | `0.0.0.0` | Bind address |
| `NEXUS_PORT` | `2026` | HTTP/gRPC port |
| `NEXUS_DATA_DIR` | `~/.nexus/data` | Local data directory |
| `NEXUS_PROFILE` | `auto` | Deployment profile |
| `NEXUS_DATABASE_URL` | — | PostgreSQL connection string |
| `NEXUS_API_KEY` | — | Admin API key |
| `NEXUS_GRPC_PORT` | `2028` | Separate gRPC port (if needed) |
| `NEXUS_LOG_LEVEL` | `info` | Logging verbosity |

## Docker Integration

```dockerfile
# Dockerfile — entrypoint calls nexusd directly
ENTRYPOINT ["nexusd"]
CMD ["--port", "2026"]
```

```bash
# docker-entrypoint.sh
exec nexusd --port "${NEXUS_PORT:-2026}" --host "${NEXUS_HOST:-0.0.0.0}"
```

## Migration Guide

| Before (pre-PR #2842) | After |
|------------------------|-------|
| `nexus serve --port 2026` | `nexusd --port 2026` |
| `nexus start` | `nexusd` |
| `nexus mount /mnt` | `nexusd mount /mnt` |
| `nexus up --profile server` | `docker compose --profile server up` |
| `nexus down` | `docker compose down` |
| `nexus logs` | `docker compose logs` |
