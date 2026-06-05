# CLI Design: `nexus` / `nexusd` Split

## Motivation

Nexus has two fundamentally different runtime modes:

1. **In-process (invocation-style)** — `nexus`: A NexusFS instance embedded
   in the invoker's process. Lifecycle tied to the invoker — exits when the
   invoker exits. Can operate as a REMOTE-profile RPC client (proxying
   to a nexusd) OR as a full embedded instance (e.g. CLUSTER profile
   with local storage). The invoker decides.
2. **Daemon (persistent)** — `nexusd`: A long-running daemon process on a
   node, exposing gRPC/HTTP. Manages local storage, serves RPC, participates
   in federation. Self-managed lifecycle (SIGTERM to stop).

This design introduces a clean two-binary split inspired by Unix conventions
(`docker`/`dockerd`, `consul`/`consul agent`) and the Nexus OS metaphor.

## The Two Binaries

### `nexus` — In-process, Invocation-style

Starts a NexusFS instance **in the invoker's process**. The instance's
lifecycle is tied to the invoker — when the invoker exits, NexusFS exits.

Two modes of operation depending on the command:

**RPC client commands** (`ls`, `cat`, `write`, `grep`, ...):
Start a REMOTE-profile NexusFS that proxies all syscalls to a running
`nexusd` via gRPC. Functionally a thin client — no local storage, no
bricks. Requires a `nexusd` to be running.

```
nexus <command> [args] [flags]
```

Connection target for RPC commands (highest priority first):
1. `--remote-url` / `--remote-api-key` flags
2. `NEXUS_URL` / `NEXUS_API_KEY` environment variables
3. Active connection in `~/.nexus/config.yaml`

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

### `nexusd-cluster` — Node Daemon (Rust binary)

A self-contained ~5 MB Rust binary (`rust/profiles/cluster/`) that runs the
Nexus node. Manages local storage via redb, serves VFS gRPC, and participates
in multi-zone Raft federation. Like `sshd`, `dockerd`, `systemd` — the `d`
suffix is the Unix daemon convention.

```
nexusd-cluster [flags]
nexusd-cluster share <path> --zone-id <id> [--mount-at <path>]
nexusd-cluster join <peer> <zone-id> <local-path>
```

Examples:
```bash
# Start daemon (static bootstrap — env-driven cluster formation)
nexusd-cluster --bootstrap-mode static --data-dir /var/lib/nexus

# Start with explicit peers
nexusd-cluster --bootstrap-mode static --peers 2@nexus-2:2126,3@nexus-3:2126

# Restart from persisted state
nexusd-cluster --bootstrap-mode restart

# Dynamic mode — rootless, operator drives create_zone via runtime API
nexusd-cluster --bootstrap-mode dynamic

# Detach a subtree into a new federation zone
nexusd-cluster share /data/shared --zone-id shared-zone --mount-at /data/shared

# Mount a remote zone locally
nexusd-cluster join 2@nexus-2:2126 shared-zone /mnt/shared
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
| Start daemon | `nexusd-cluster` | Starts the node process |
| `share` (federation) | `nexusd-cluster share` | Detach subtree into new zone |
| `join` (federation) | `nexusd-cluster join` | Mount remote zone locally |

## Entry Points

| Binary | Source | Language |
|--------|--------|----------|
| `nexus` | `src/nexus/cli` (`pyproject.toml` script) | Python |
| `nexusd-cluster` | `rust/profiles/cluster/src/main.rs` (`cargo build -p nexus-cluster`) | Rust |

The Python `nexus` CLI spawns or connects to `nexusd-cluster` via gRPC
(`RPCTransport`). The two binaries live in separate repos: `nexus` in the
monorepo, `nexusd-cluster` in `nexus-vfs`.

## `nexusd-cluster` Startup Sequence

1. Parse CLI flags + env vars (clap)
2. Install tracing subscriber (non-blocking stdout writer)
3. Build tokio multi-thread runtime (worker count = available parallelism)
4. Register `ObjectStoreProvider` (backends crate)
5. Create `Kernel` + mount host-fs at `/` via `PathLocalBackend`
6. Build VFS gRPC service (tonic Routes, co-hosted on raft port)
7. Open `ZoneManager` (TLS bootstrap, peer address book, node identity)
8. Bootstrap root zone (static / restart / dynamic mode)
9. Bootstrap static federation topology from `NEXUS_FEDERATION_ZONES` / `_MOUNTS`
10. Install `RaftDistributedCoordinator` (DT_MOUNT apply-cb, blob-fetcher, self-address)
11. Install outbound `PeerBlobClient` (cross-node content fetch)
12. Start topology convergence loop (10s tick)
13. `wait_for_shutdown()` (SIGTERM / Ctrl+C)
14. Drain: abort topology loop → `ZoneManager.shutdown()` → drop kernel

Source of truth: `rust/profiles/cluster/src/main.rs::run_daemon()`.

## Environment Variables

`nexusd-cluster` reads configuration from CLI flags and environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `NEXUS_HOSTNAME` | OS hostname | Node hostname for peer addressing |
| `NEXUS_BIND_ADDR` | `0.0.0.0:2126` | gRPC bind address (raft + VFS) |
| `NEXUS_DATA_DIR` | `./nexus-cluster-data` | Persistent data directory (TLS + redb) |
| `NEXUS_PEERS` | (empty) | Comma-separated raft peers (`id@host:port`) |
| `NEXUS_ROOT_FS` | `<data_dir>/root` | Host-fs directory mounted at `/` |
| `NEXUS_BOOTSTRAP_MODE` | (required) | `static`, `dynamic`, or `restart` |
| `NEXUS_NO_TLS` | `false` | Disable TLS (plaintext gRPC for local testing) |
| `NEXUS_FEDERATION_ZONES` | (empty) | Static federation zone definitions |
| `NEXUS_FEDERATION_MOUNTS` | (empty) | Static federation mount topology |
| `RUST_LOG` / `NEXUS_LOG_LEVEL` | `info` | Logging verbosity (tracing EnvFilter) |

## Docker Integration

```dockerfile
ENTRYPOINT ["nexusd-cluster"]
CMD ["--bootstrap-mode", "static"]
```

```bash
# docker-entrypoint.sh
exec nexusd-cluster \
  --bind-addr "0.0.0.0:${NEXUS_PORT:-2126}" \
  --data-dir "${NEXUS_DATA_DIR:-/var/lib/nexus}" \
  --bootstrap-mode "${NEXUS_BOOTSTRAP_MODE:-static}"
```
