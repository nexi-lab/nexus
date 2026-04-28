# Sudowork Integration: `nexusd-cluster`

This guide is for sudowork (and any other downstream that wants to
embed Nexus's Raft + IPC + federation capabilities without taking on
the full Python runtime). The deliverable is a single ~5 MiB Rust
binary — `nexusd-cluster` — that runs as a long-lived process and
exposes everything via the existing federation gRPC + IPC-over-VFS
surfaces.

## What you get

- **Raft** (multi-zone, mTLS): every node hosts an in-process raft
  state machine for the root zone plus any federation zones declared
  via env vars.
- **IPC-over-VFS**: agent-to-agent message passing implemented as VFS
  paths under `/__ipc__/`. See `nexus_runtime::ipc` (PR #3896).
- **Federation**: Day-1 cluster formation, dynamic share/join, mTLS
  Cert-of-First-Use trust store. All convergent — every node tries
  to apply the same topology and retries on per-zone leader changes.

## What you don't get

- No Python runtime. No PostgreSQL. No PyInstaller bundle.
- No HTTP REST surface (the binary speaks raft / federation gRPC
  only). If you need REST, run a separate `nexusd` (Python) process
  and point it at this node's gRPC port.
- No bricks (search, llm, pay, …). Those are Python-only and live in
  the Python runtime.

## Getting the binary

Pre-built artifacts are attached to every Nexus GitHub release:

- `nexusd-cluster-linux-x86_64`
- `nexusd-cluster-macos-arm64`
- `nexusd-cluster-windows-x86_64.exe`

CI workflow: `.github/workflows/cluster-binary.yml`. Each PR also
uploads platform artifacts for testing (14-day retention).

To build from source:

```bash
git clone https://github.com/nexi-lab/nexus
cd nexus
cargo build --release -p nexus-cluster --bin nexusd-cluster
# Result: target/release/nexusd-cluster
```

The build needs only the Rust toolchain — no Python, no protoc
(vendored via `protoc-bin-vendored`).

## Running

The minimal single-node invocation:

```bash
NEXUS_HOSTNAME=node-1 \
NEXUS_DATA_DIR=/var/lib/nexus-cluster \
nexusd-cluster
```

A 3-node federation:

```bash
# All three nodes — same env on each, just hostname differs.
NEXUS_HOSTNAME=node-1 \
NEXUS_DATA_DIR=/var/lib/nexus-cluster \
NEXUS_PEERS="1@node-1:2126,2@node-2:2126,3@node-3:2126" \
nexusd-cluster
```

On first start, `nexusd-cluster` provisions a new mTLS bundle under
`<data-dir>/tls/`:

- `ca.pem` / `ca-key.pem` — cluster root CA (10-year validity)
- `node.pem` / `node-key.pem` — this node's mTLS cert (1-year)
- `join-token` — K3s-style operator token. Hand this to additional
  nodes that join later via `nexusd-cluster join`.
- `join-token-hash` — server-side hash for verifying inbound
  `JoinCluster` RPCs.

The CA private key never leaves the originating node; joining nodes
get their certs signed via `JoinCluster` on the leader.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `NEXUS_HOSTNAME` | OS hostname | Unique per node; must equal this node's entry in `NEXUS_PEERS`. |
| `NEXUS_BIND_ADDR` | `0.0.0.0:2126` | gRPC bind address. |
| `NEXUS_DATA_DIR` | `./nexus-cluster-data` | TLS bundle + per-zone redb files. |
| `NEXUS_PEERS` | *(empty)* | Comma-separated `id@host:port` for raft membership. All cluster nodes must use identical lists. |
| `NEXUS_FEDERATION_ZONES` | *(empty)* | Comma-separated non-root zone ids to create at startup (e.g. `corp,corp-eng,family`). |
| `NEXUS_FEDERATION_MOUNTS` | *(empty)* | Static mount topology as `path=zone` pairs (e.g. `/corp=corp,/corp/eng=corp-eng`). |
| `NEXUS_NO_TLS` | `false` | Plaintext gRPC. **Local testing only.** |

`NEXUS_FEDERATION_ZONES` and `NEXUS_FEDERATION_MOUNTS` together drive
the Day-1 static topology: every node reads the same env, calls
`bootstrap_static`, and a background ticker drives `apply_topology`
to convergence (handles per-zone leader splits without operator
intervention).

## CLI subcommands

```bash
nexusd-cluster                # daemon (default)
nexusd-cluster share <path> --zone-id <new-zone>
                              # detach a subtree into a new zone
nexusd-cluster join <peer> <remote-zone-id> <local-mount-path>
                              # join a remote zone, mount locally
```

`share` and `join` open the data directory directly via
`ZoneManager`, so they must run while the daemon is stopped (redb
holds an exclusive file lock). The primary deployment path for
Sudowork is the static topology env vars consumed at daemon startup;
share/join are operator escape hatches for incremental federation
changes outside that flow.

## Talking to the daemon

The daemon exposes the standard Nexus federation gRPC services:

- `nexus.raft.transport.RaftService` — raft consensus (internal)
- `nexus.raft.transport.ZoneApiService` — zone CRUD, JoinZone
- `nexus.grpc.vfs.NexusVFSService` — full VFS surface (sys_*, IPC paths)

Sudowork can either:

1. **Embed the gRPC client** — generate Python / Rust stubs from
   `proto/nexus/raft/transport.proto` and `proto/nexus/grpc/vfs/vfs.proto`,
   talk directly to `127.0.0.1:2126` (or wherever `NEXUS_BIND_ADDR`
   points).
2. **Use the existing Python `rpc_call`** — if you have Python in
   another process, `nexus.contracts.rpc.rpc_call(url, ..., method,
   **kwargs)` against the `federation_*` / VFS RPCs works the same
   way it does for Python `nexusd`.

For IPC-over-VFS specifically, every message becomes a VFS write at
`/__ipc__/<recipient>/<message-id>` and a read on the recipient
side. See `rust/kernel/src/ipc.rs` for the in-process API and the
contract proto for the wire format.

## Operational notes

- **Disk layout**: per-zone redb files live under `<data-dir>/<zone-id>/`.
  Snapshots and TLS are at the root.
- **Logging**: `tracing-subscriber` reads `RUST_LOG` (default
  `nexusd_cluster=info,nexus_raft=info`). Use `RUST_LOG=debug` for
  troubleshooting raft membership / mount topology issues.
- **Graceful shutdown**: SIGTERM / Ctrl+C trigger an ordered shutdown.
  The topology ticker is aborted first, then the gRPC server, then
  per-zone raft groups flush their state.
- **Health probe**: there's no HTTP `/healthz` endpoint — use the
  `cluster_status` gRPC method on `ZoneApiService` (returns
  `is_leader`, `leader_id`, `term`, etc.) or just check the process
  is alive and `applied_index` is monotonic.

## Migration from conda-pack

The `conda-pack.yml` artifact path that Sudowork used to consume
ships the full Python runtime (~150 MiB) with `nexusd` Python plus
all bricks. The `nexusd-cluster` binary replaces only the
**federation/raft/IPC** subset — the Python runtime is gone.

Migration steps:
1. Drop `nexusd` Python from your service unit; it's not needed
   anymore unless you also need the REST API or full bricks.
2. Replace the conda-pack tarball with the platform-appropriate
   `nexusd-cluster` binary from the GitHub Release.
3. Translate your existing config:
   - `NEXUS_PEERS` carries over unchanged.
   - `NEXUS_FEDERATION_ZONES` / `NEXUS_FEDERATION_MOUNTS` were
     previously parsed by `nexus.raft.federation.bootstrap` (deleted);
     they're now parsed by Rust and used identically.
   - TLS auto-generation is on by default; if you were pre-provisioning
     certs, drop them in `<data-dir>/tls/` and the binary picks them
     up instead of regenerating.
4. Verify membership: `cluster_status` should report the same
   voter_count and leader_id as the old Python daemon.

The conda-pack workflow is intentionally not deleted in this PR —
it still serves Sudowork's old code path until the migration is
complete. Removing it is a follow-up after Sudowork confirms the
binary path is in place.
