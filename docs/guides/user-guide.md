# User Guide

This guide is for someone using Nexus from the terminal for the first time.
It starts with the easiest path, then adds remote servers, search, permissions,
agents, workspaces, workflows, sandboxing, MCP, and federation.

Nexus does not ship a full-screen TUI in this repository today. The supported
terminal UX is:

- the `nexus` CLI
- the `nexusd` daemon
- JSON output for scripting
- the Python SDK for building your own UI later

One important note before you start: some older examples in the repo still say
`nexus serve`. The current daemon entrypoint is `nexusd`.

Another important distinction: local, shared, remote, and federation are not
the same thing.

- Local embedded: no daemon. `nexus init` and `nexus.connect(...)` use a local
  data directory directly.
- Shared single-node: one `nexusd` process serves multiple terminals, users,
  agents, or SDK clients.
- Remote thin client: the CLI or SDK uses `profile="remote"` to talk to an
  existing daemon. `remote` is client-only and is never a valid `nexusd`
  profile.
- Federation: multiple `nexusd` nodes are joined with TLS, networking, and
  zone-sharing.
- Database auth is orthogonal to all of the above. It changes how a
  server-backed deployment authenticates users and stores auth/search metadata;
  it is not what makes a client "remote".

Today there is not a first-party `nexus up` or `nexus demo init` command in
the public CLI. Those would likely be a cleaner operator UX later, but this
guide documents the commands that actually ship today: `nexus init` for local
state and `nexusd` for node startup.

## Before You Start

Use this guide in order the first time:

1. sections 1 through 4 to get a working local or shared server
2. sections 5 through 10 to turn on the features most people actually use
3. sections 11 and 12 only after the basics already work

What you need depends on how far you go:

| You want to try... | What you need |
| --- | --- |
| basic local CLI and SDK | Python and a virtualenv |
| shared server for multiple users or agents | `nexusd` plus an API key |
| database auth and richer multi-user setups | Postgres-compatible database |
| parsed document search | parser API keys such as `UNSTRUCTURED_API_KEY` or `LLAMA_CLOUD_API_KEY` |
| Zoekt-backed code search | a separately running Zoekt service |
| sandbox execution | Docker or E2B, depending on provider |
| federation mesh networking | TLS material and usually WireGuard |

## 1. Install Nexus

Pick one path and stick to it for your first run.

### Option A: Install from PyPI

Use this if you want to try Nexus as a user.

```bash
python -m venv .venv
source .venv/bin/activate
pip install nexus-ai-fs
```

### Option B: Install from a source checkout

Use this if you are working from this repository and want the docs, examples,
and code to line up.

```bash
uv python install 3.14
uv venv --python 3.14
source .venv/bin/activate
uv pip install -e .
```

If you want the optional Rust acceleration module in a source checkout, build
it in the same uv-managed environment:

```bash
source .venv/bin/activate
uv pip install -e . maturin
maturin develop --release -m rust/nexus_kernel/Cargo.toml
python -c "import nexus_kernel; print('nexus_kernel available')"
```

You do not need this for a normal first run. Nexus falls back to Python
implementations when `nexus_kernel` is not installed. Add it when you want faster
grep/glob, hashing, lock/semaphore primitives, Bloom-filter paths, and some
permission/search fast paths.

If you already have Conda, pyenv, or another Python activated, do not build the
Rust module there by accident. Activate `.venv` first, then run `maturin
develop`.

### Optional extras

The base package already includes the main CLI, server, remote client, LLM,
MCP, and most storage/search plumbing. Add extras only when you need them.

- Semantic search with remote embedding providers: `pip install "nexus-ai-fs[semantic-search-remote]"`
- E2B sandbox provider: `pip install "nexus-ai-fs[e2b]"`
- Docker sandbox provider: `pip install "nexus-ai-fs[docker]"`
- FUSE support: `pip install "nexus-ai-fs[fuse]"`

### Verify the install

You should have both console scripts:

```bash
nexus --help
nexusd --help
```

If you are running from source and the console scripts are not on your `PATH`,
the safe fallbacks are:

```bash
python -m nexus.cli.main --help
python -m nexus.daemon.main --help
```

## 2. Pick The Right Mode

If you are unsure, use `profile=full`.

| If you want to... | Use this | Typical profile |
| --- | --- | --- |
| Try Nexus alone on one machine | local CLI or SDK, no daemon | `full` |
| Run a shared Nexus node for CLI and SDK clients | `nexusd` | `full` |
| Turn on permissions, agent registry, IPC, scheduler | server-backed flow | `lite` or `full` |
| Use search, workspaces, workflows, sandbox, MCP, LLM | full feature set | `full` |
| Run multi-zone federation | multiple `nexusd` nodes plus TLS/networking | `cloud` |
| Connect Python or the CLI to an existing node | remote thin client | `remote` |

The same `nexusd` binary starts both a simple shared node and a future
federation-capable node. What changes is:

- deployment profile (`full`, `lite`, `cloud`)
- auth backend (`--api-key` for static auth or `--auth-type database`)
- storage/search wiring (`--data-dir`, `--database-url`, `NEXUS_SEARCH_DAEMON`)
- whether other clients connect to it remotely
- whether other nodes join it for federation

What does not change:

- `profile=remote` is still client-only
- `--auth-type database` does not make Nexus "remote"
- federation is more than database auth; it adds multi-node trust and join flows

Profile summary:

| Profile | What it is good for |
| --- | --- |
| `minimal` | storage only |
| `embedded` | tiny local deployments |
| `lite` | permissions, agent registry, IPC, scheduler, cache |
| `full` | the easiest all-features starting point |
| `cloud` | federation on top of full |
| `remote` | client only, never for `nexusd` |

If you want to force features on or off, create a `nexus.yaml` file and start
Nexus with it:

```yaml
profile: full
features:
  agent_registry: true
  permissions: true
  search: true
  sandbox: true
  workflows: true
  mcp: true
  federation: false
```

Then start the daemon with:

```bash
nexusd --config ./nexus.yaml --port 2026
```

For anything beyond a one-off shell demo, prefer a config file such as
`nexus.yaml` for stable settings and keep environment variables for secrets or
machine-specific overrides. This is less error-prone than re-exporting a long
set of values in every terminal.

## 2.1 Capability checklist for a serious demo

If you are evaluating Nexus as more than a toy filesystem, the first real
single-node walkthrough should cover:

- file CRUD
- version history and rollback
- permissions enabled
- agent registry
- agent-to-agent coordination, either through files, IPC, or both
- audit and operation logs
- grep and semantic search

This guide covers those capabilities in these sections:

- local file CRUD: sections 3 and 4
- search and semantic retrieval: section 5
- permissions and policy: section 6
- agent registry, IPC, identity, and delegation: section 7
- versions, snapshots, and operations: section 10
- audit examples and advanced operator flows: section 12

For federation, repeat the same checklist after join/share succeeds. A
federation guide is only convincing when the same user stories work across
zones, not just on one node.

## 3. First Local Run

This is the smallest working path and the best place to start.

### Step 1: Create a local workspace

```bash
mkdir -p ~/nexus-demo
cd ~/nexus-demo
nexus init .
export NEXUS_DATA_DIR="$PWD/nexus-data"
export NEXUS_PROFILE=full
```

`nexus init .` creates a local data directory for the workspace. Exporting
`NEXUS_DATA_DIR` makes the CLI keep using that workspace instead of the global
default under `~/.nexus`.

### Step 2: Write, read, and list files

```bash
nexus write /workspace/hello.txt "hello from nexus"
nexus cat /workspace/hello.txt
nexus ls /workspace -l
```

### Step 3: Try the Python SDK against the same data

```bash
python - <<'PY'
import nexus

nx = nexus.connect(config={
    "profile": "full",
    "data_dir": "./nexus-data",
})

nx.sys_write("/workspace/sdk.txt", b"written from sdk")
print(nx.sys_read("/workspace/sdk.txt").decode())
nx.close()
PY
```

### Step 4: Run a quick environment check

```bash
nexus doctor
```

Packages behind this:

- Kernel and syscalls: `nexus.core`, `nexus.contracts`
- Persistence: `nexus.storage`
- Object backends and connectors: `nexus.backends`
- Programmatic entrypoint: `nexus.sdk` and top-level `nexus.connect()`

## 4. Start A Shared Server

Use `nexusd` when you want multiple terminals, users, or SDK clients to hit
the same Nexus node.

This is the first place where "shared" and "remote" start to matter:

- the server process is still `nexusd`
- local and remote clients can both talk to it
- the remote SDK path uses `profile="remote"` on the client side, never on the daemon side
- auth mode is a separate choice from transport mode

### Step 1: Start a simple dev server with one API key

Open terminal A:

```bash
mkdir -p ~/nexus-server
cd ~/nexus-server
export NEXUS_DATA_DIR="$PWD/data"
export NEXUS_API_KEY="dev-key-123"
export NEXUS_GRPC_PORT=2028
nexusd --profile full --host 0.0.0.0 --port 2026 --data-dir "$NEXUS_DATA_DIR" --api-key "$NEXUS_API_KEY"
```

### Step 2: Connect from another terminal

Open terminal B:

```bash
export NEXUS_URL="http://localhost:2026"
export NEXUS_API_KEY="dev-key-123"
export NEXUS_GRPC_PORT=2028

nexus status
curl http://localhost:2026/health
nexus ls /
```

### Step 3: Save a reusable CLI profile

```bash
nexus connect http://localhost:2026 --name local-dev -k "$NEXUS_API_KEY"
nexus profile list
nexus --profile local-dev ls /
```

### Step 4: Connect with the remote Python client

The remote SDK path uses `profile="remote"`. `NEXUS_URL` is the HTTP address,
but filesystem operations still use gRPC on `NEXUS_GRPC_PORT`.

If you start `nexusd` without `NEXUS_GRPC_PORT`, the HTTP server can still come
up while remote `nexus ls`, `nexus cat`, and SDK filesystem calls fail because
there is no gRPC listener.

```bash
python - <<'PY'
import nexus

nx = nexus.connect(config={
    "profile": "remote",
    "url": "http://localhost:2026",
    "api_key": "dev-key-123",
})

print(nx.sys_readdir("/"))
nx.close()
PY
```

### When should you use database auth?

Use `--auth-type database` and `--database-url ...` when you need:

- per-user API keys
- admin/user provisioning
- more realistic multi-user permissions
- server-side search backed by a real record store

What database auth does not mean:

- it does not replace `nexusd`; it is still the same daemon entrypoint
- it does not change the client into `profile="remote"` by itself
- it does not imply federation

In practice, think about the combinations like this:

- local embedded: no daemon, no remote client
- shared static-auth daemon: `nexusd --api-key ...`
- shared database-auth daemon: `nexusd --auth-type database --database-url ...`
- remote client: CLI or SDK pointed at either of the daemon shapes above
- federation: one or more database-backed or durable nodes joined with TLS/networking

Typical single-node database-auth daemon startup:

```bash
export NEXUS_DATA_DIR="$PWD/data"
export NEXUS_DATABASE_URL="postgresql://$USER@localhost/nexus"
export NEXUS_SEARCH_DAEMON=true

nexusd \
  --profile full \
  --host 0.0.0.0 \
  --port 2026 \
  --data-dir "$NEXUS_DATA_DIR" \
  --auth-type database \
  --database-url "$NEXUS_DATABASE_URL"
```

Packages behind this:

- Daemon entrypoint: `nexus.daemon`
- HTTP and app lifecycle: `nexus.server`
- Remote client transport: `nexus.remote`, `nexus.grpc`
- Auth, policy, and server-side feature wiring: `nexus.bricks.*`, `nexus.system_services.*`

## 5. Search, Parsing, And Indexing

Think about search in three layers:

1. file discovery: `glob`, `grep`
2. parsed text extraction: PDFs, docs, and other formats
3. semantic and hybrid retrieval: `nexus search ...`

### 5.1 Find files and text first

```bash
nexus glob "**/*.py" /workspace
nexus grep "TODO" /workspace
nexus grep "revenue" /workspace -f "**/*.pdf" --search-mode parsed
```

Parser providers are auto-discovered from environment variables:

- `UNSTRUCTURED_API_KEY`
- `LLAMA_CLOUD_API_KEY`
- local pdf-inspector fallback

### 5.2 Initialize semantic search

Use keyword-only mode first if you just want index-backed retrieval without
embedding keys:

```bash
nexus search init
```

For semantic or hybrid search, initialize with an embedding provider:

```bash
nexus search init --provider openai --api-key "$OPENAI_API_KEY"
```

Voyage is also supported:

```bash
nexus search init --provider voyage --api-key "$VOYAGE_API_KEY"
```

### 5.3 Build the index

```bash
nexus search index /workspace
nexus search stats
```

### 5.4 Query the index

```bash
nexus search query "How does authentication work?" --path /workspace
nexus search query "database migration" --mode hybrid --limit 5
```

### 5.5 Start the server-side search daemon

The server-side search API is what you want for a long-running shared node.
It is enabled when:

- `NEXUS_SEARCH_DAEMON=true`, or
- the daemon has a database URL and search is not explicitly disabled

Start the daemon like this:

```bash
export NEXUS_SEARCH_DAEMON=true
export NEXUS_DATABASE_URL="postgresql://$USER@localhost/nexus"

nexusd \
  --profile full \
  --port 2026 \
  --data-dir "$PWD/data" \
  --auth-type database \
  --database-url "$NEXUS_DATABASE_URL"
```

Verify it:

```bash
curl "$NEXUS_URL/api/v2/search/health"
curl -H "Authorization: Bearer $NEXUS_API_KEY" "$NEXUS_URL/api/v2/search/stats"
```

### 5.6 What about Zoekt?

Zoekt is an optional fast trigram/code-search backend behind Nexus search.
There is not a separate `nexus zoekt ...` command today. You run Zoekt
separately, then point Nexus at it.

Step by step:

1. start your Zoekt service outside Nexus
2. point Nexus at that service with the Zoekt environment variables
3. start `nexusd`
4. keep using `nexus grep` and `nexus search ...` from the client side

Typical Nexus-side setup:

```bash
export ZOEKT_ENABLED=true
export ZOEKT_URL="http://localhost:6070"
export ZOEKT_INDEX_DIR="$PWD/.zoekt-index"
export ZOEKT_DATA_DIR="$PWD"
export ZOEKT_INDEX_BINARY="zoekt-index"
export NEXUS_SEARCH_DAEMON=true

nexusd --profile full --port 2026 --data-dir "$PWD/data"
```

What this means in practice:

- Nexus still exposes normal `grep` and `search` flows
- the search brick uses Zoekt when it is available
- Zoekt is especially useful for large code trees

If you only want a beginner path, start with `nexus search init/index/query`
and add Zoekt later.

Packages behind this:

- Search daemon and retrieval: `nexus.bricks.search`
- Document parsing: `nexus.bricks.parsers`
- Search HTTP API: `nexus.server.api.v2.routers.search`
- Search daemon startup: `nexus.server.lifespan.search`

## 6. Turn On Permissions And Policy

If you want permissions, agent registry, and IPC, use `profile=full` unless you
have a reason to squeeze into `lite`.

### Step 1: Make sure permissions are actually enforced

The important settings are:

- use `lite`, `full`, or `cloud`
- keep `NEXUS_ENFORCE_PERMISSIONS=true` (this is already the default)
- authenticate to the server with `NEXUS_API_KEY`

For an explicit config:

```yaml
profile: full
enforce_permissions: true
features:
  permissions: true
  agent_registry: true
```

### Step 2: Create a file to protect

```bash
nexus write /workspace/secret.txt "top secret"
```

### Step 3: Create ReBAC relationships

```bash
nexus rebac create agent alice direct_owner file /workspace/secret.txt
nexus rebac check agent alice write file /workspace/secret.txt
nexus rebac explain agent alice write file /workspace/secret.txt --verbose
```

### Step 4: Use zones when you need tenant isolation

```bash
nexus rebac create agent alice direct_owner file /workspace/secret.txt --zone-id org_acme
nexus rebac check agent alice write file /workspace/secret.txt --zone-id org_acme
```

### Step 5: Create and test access manifests

Access manifests let you say which tools and data surfaces an agent may use.

```bash
nexus manifest create agent_alice --name "dev tools" --entry "read_*:allow"
nexus manifest list
nexus manifest evaluate <manifest-id> --tool-name read_file
```

### Step 6: If you are running database auth, create real user keys

This is the operator path once a database-auth deployment is in place:

```bash
nexus admin create-user alice --name "Alice Laptop" --expires-days 90
nexus admin create-user bot1 --name "Bot Agent" --subject-type agent
```

Packages behind this:

- Auth: `nexus.bricks.auth`
- ReBAC and policy graph: `nexus.bricks.rebac`
- Access manifests: `nexus.bricks.access_manifest`
- Identity and delegation: `nexus.bricks.identity`, `nexus.bricks.delegation`

## 7. Agent Registry, IPC, Identity, And Delegation

This is the part of Nexus that turns a filesystem into an agent platform.

### Step 1: Register agents

```bash
nexus agent register alice_bot "Alice Research Bot"
nexus agent register bob_bot "Bob Worker"
nexus agent list
nexus agent info alice_bot
```

By default, registered agents do not get their own API keys. They use the
owner's auth plus the `X-Agent-ID` model, which is the recommended path.

If you really need an agent-specific key:

```bash
nexus agent register legacy_bot "Legacy Bot" --with-api-key
```

### Step 2: Send messages between agents

```bash
nexus ipc send bob_bot "hello from alice" --from alice_bot
nexus ipc inbox bob_bot
nexus ipc count bob_bot
```

### Step 3: Inspect identity

```bash
nexus identity show alice_bot
nexus identity credentials alice_bot
nexus identity passport alice_bot
```

### Step 4: Delegate work

```bash
nexus delegation create alice_bot bob_bot --mode CLEAN --scope "/workspace/project/*" --ttl 3600
nexus delegation list
```

Packages behind this:

- Agent registry and lifecycle: `nexus.system_services.agents`
- IPC: `nexus.bricks.ipc`
- Identity: `nexus.bricks.identity`
- Delegation: `nexus.bricks.delegation`
- A2A support: `nexus.bricks.a2a`

## 8. Workspaces And Context Branching

This is where Nexus starts feeling like long-lived agent infrastructure instead
of a normal filesystem.

### 8.1 Register a workspace and snapshot it

```bash
nexus mkdir /workspace/project
nexus workspace register /workspace/project --name project --description "Main project workspace"
nexus workspace snapshot /workspace/project --description "Before refactor"
nexus workspace log /workspace/project
```

### 8.2 Create context branches

```bash
nexus context branch /workspace/project --name try-new-approach
nexus context checkout /workspace/project --target try-new-approach
nexus context commit /workspace/project --message "Experiment 1"
nexus context merge /workspace/project --source try-new-approach
```

Packages behind this:

- Workspace and branching: `nexus.bricks.workspace`, `nexus.bricks.context_manifest`
- Registry and lifecycle plumbing: `nexus.system_services.workspace`

## 9. Workflows, Sandbox, LLM, And MCP

These are the main "do useful work with agents" feature families.

### 9.1 Load a workflow

Create a file:

```bash
mkdir -p .nexus/workflows
cat > .nexus/workflows/tag-incoming.yaml <<'YAML'
name: tag-incoming
version: "1.0"
description: Mark incoming files as processed
triggers:
  - type: file_write
    pattern: /workspace/inbox/*
actions:
  - name: mark-processed
    type: metadata
    metadata:
      workflow_status: processed
YAML
```

Load and test it:

```bash
nexus workflows load .nexus/workflows/tag-incoming.yaml
nexus workflows list
nexus workflows test tag-incoming --file /workspace/inbox/demo.txt
nexus workflows enable tag-incoming
```

`nexus workflows discover .nexus/workflows --load` is the quickest way to load
a whole workflow directory.

### 9.2 Create a sandbox

Docker provider:

```bash
pip install "nexus-ai-fs[docker]"
nexus sandbox create demo-box --provider docker
nexus sandbox list
nexus sandbox run <sandbox-id> -c "print('hello from sandbox')"
nexus sandbox stop <sandbox-id>
```

E2B provider:

```bash
pip install "nexus-ai-fs[e2b]"
export E2B_API_KEY="..."
nexus sandbox create demo-box --provider e2b
```

### 9.3 Ask an LLM to read your files

```bash
export OPENROUTER_API_KEY="..."
nexus llm read /workspace/hello.txt "Summarize this file"
```

You can also set `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or provider-specific
model flags depending on your environment.

### 9.4 Start the MCP server

For local desktop tools:

```bash
nexus mcp serve --transport stdio
```

For networked clients:

```bash
nexus mcp serve --transport http --port 8081
curl http://localhost:8081/health
```

If the MCP server should talk to a remote Nexus node, export:

```bash
export NEXUS_URL="http://localhost:2026"
export NEXUS_API_KEY="..."
```

Packages behind this:

- Workflow engine: `nexus.bricks.workflows`
- Sandbox providers: `nexus.bricks.sandbox`
- LLM reading: `nexus.bricks.llm`
- MCP and tool serving: `nexus.bricks.mcp`, `nexus.bricks.discovery`
- Validation and guardrails: `nexus.validation`

## 10. Versions, Snapshots, Uploads, Events, And Operations

These commands are about durability, rollback, and operator visibility.

### File version history

```bash
nexus versions history /workspace/hello.txt
nexus versions get /workspace/hello.txt --version 1
nexus versions rollback /workspace/hello.txt --version 1
```

### Transactional snapshots

```bash
nexus snapshot create --description "Before migration"
nexus snapshot list
nexus snapshot restore <txn_id>
```

### Event replay and live subscriptions

```bash
nexus events replay --since 1h
nexus events subscribe "file_write"
```

### Scheduler visibility

```bash
nexus scheduler status
nexus scheduler queue
```

### Upload visibility

The upload command group is mainly for inspecting or cancelling resumable
uploads that already exist on the server:

```bash
nexus upload status <upload-id>
nexus upload cancel <upload-id>
```

### Operation logs and undo

```bash
nexus ops log
nexus undo
```

Packages behind this:

- Versioning: `nexus.bricks.versioning`
- Snapshots: `nexus.bricks.snapshot`
- Uploads: `nexus.bricks.upload`
- Event subsystem and scheduler: `nexus.system_services.event_subsystem`, `nexus.system_services.scheduler`
- Storage and audit trails: `nexus.storage`

## 11. Federation, Networking, And Cluster Mode

This is the most advanced part of Nexus. Start here only after local and
single-node remote mode already work.

Federation should prove the same user stories that you already validated on one
node:

- file CRUD
- version history
- permissions
- agent registry and delegation
- file-mediated or IPC-based collaboration
- audit trails
- grep and semantic search

The difference is that these flows now cross node and zone boundaries instead
of staying inside one local daemon.

Nexus supports two bootstrap modes:

- **Static bootstrap** — all peers known upfront via `NEXUS_PEERS` env var.
  Best for fixed-topology clusters and the recommended starting point.
- **Dynamic bootstrap** — new nodes join at runtime using a K3s-style join
  token (`{data_dir}/tls/join-token`). Best for elastic scaling after a
  cluster is already running.

The guide below uses **static bootstrap** (simpler, fewer moving parts).

### Step 1: Use the `cluster` profile

The `cluster` profile enables Raft consensus + federation with kernel-native
storage (redb). No external PostgreSQL required.

```bash
# Environment variable approach (recommended for cross-machine setups):
export NEXUS_PROFILE=cluster
export NEXUS_DATA_DIR="$PWD/data"
nexusd --port 2026
```

Or pass the profile flag directly:

```bash
nexusd --profile cluster --port 2026 --data-dir "$PWD/data"
```

### Step 2: Configure the Raft cluster (static bootstrap)

Each node needs to know all peers. Raft gRPC runs on port **2126** (separate
from the HTTP API on 2026).

Set these environment variables on **every node** before starting `nexusd`:

```bash
# -- Required --
export NEXUS_PROFILE=cluster
export NEXUS_PEERS="<node1-ip>:2126,<node2-ip>:2126"    # all peers
export NEXUS_BIND_ADDR="0.0.0.0:2126"                    # Raft gRPC listen
export NEXUS_ADVERTISE_ADDR="<this-node-ip>:2126"        # reachable from peers

# -- TLS (disable for initial testing over VPN / trusted LAN) --
export NEXUS_RAFT_TLS=false

# -- Optional: pre-configure zones and mounts at startup --
export NEXUS_FEDERATION_ZONES="shared"
export NEXUS_FEDERATION_MOUNTS="/shared=shared"
```

Then start the daemon on each node:

```bash
nexusd --port 2026 --data-dir "$PWD/data"
```

All nodes bootstrap the same root zone automatically. Static zones and mounts
declared via `NEXUS_FEDERATION_ZONES` / `NEXUS_FEDERATION_MOUNTS` are created
idempotently on every startup.

### Step 3 (optional): Set up the WireGuard mesh

If nodes are on different networks (e.g., macOS + Windows over the internet),
use WireGuard to create an encrypted tunnel first.

```bash
nexus network init --node-id 1
nexus network add-peer --node-id 2 --public-key "<peer-public-key>" --endpoint "<peer-ip>:51820"
nexus network config
nexus network status
```

`nexus network up` usually needs sudo or admin privileges because it brings up
the WireGuard interface. IP scheme: `10.99.0.{node_id}/24`.

### Step 4: Verify federation

```bash
nexus federation status          # overview: zone count, link count
nexus federation zones           # list all Raft zones
nexus federation info <zone-id>  # cluster info for a specific zone
```

### Step 5: Manage mounts

```bash
# Create a cross-zone mount point
nexus federation mount --parent-zone root --path /shared --target-zone team-shared

# Remove a mount point
nexus federation unmount --parent-zone root --path /shared
```

> **Note:** `share` and `join` are daemon-level operations (triggered via
> `NexusFederation.share()` / `NexusFederation.join()` API or pre-configured
> via `NEXUS_FEDERATION_ZONES` / `NEXUS_FEDERATION_MOUNTS` env vars). They are
> not separate CLI commands.

### Step 6 (optional): Enable TLS later

Once the cluster works over plaintext, enable mTLS:

```bash
# On the first node — generate CA + node certs:
nexus tls init --data-dir "$PWD/data" --zone-id root
nexus tls show

# Remove NEXUS_RAFT_TLS=false (default is TLS enabled) and restart all nodes.
# For dynamic join with TLS, place a join token file at {data_dir}/tls/join-token
# on the joining node — see the dynamic bootstrap section in federation-memo.md.
```

### Environment variable reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `NEXUS_PROFILE` | Yes | `full` | `cluster` for federation |
| `NEXUS_PEERS` | Yes (federation) | — | Comma-separated `host:port` (Raft gRPC) |
| `NEXUS_BIND_ADDR` | No | `0.0.0.0:2126` | Raft gRPC listen address |
| `NEXUS_ADVERTISE_ADDR` | Recommended | — | Address peers use to reach this node |
| `NEXUS_RAFT_TLS` | No | `true` | Set `false` to disable mTLS |
| `NEXUS_FEDERATION_ZONES` | No | — | Comma-separated zone IDs to pre-create |
| `NEXUS_FEDERATION_MOUNTS` | No | — | `path=zone_id,...` mount mappings |
| `NEXUS_HOSTNAME` | No | OS hostname | Used to derive deterministic node ID |

### Packages behind this

- Federation and consensus: `nexus.raft`
- Trust and TLS: `nexus.security`
- Network mesh: `nexus.network`
- Federation APIs and runtime: `nexus.server`, `nexus.cli.commands.federation`

## 12. Connectors, OAuth, Plugins, And Other Advanced Areas

Once the basics work, these are the next user-facing areas to explore.

### 12.1 Connectors and external data sources

```bash
nexus connectors list
nexus connectors info gcs_connector
```

If your build exposes `nexus mounts`, that is the persistent mount management
group for attaching external backends under virtual paths.

### 12.2 OAuth-backed integrations

```bash
nexus oauth list
nexus oauth setup-gdrive
nexus oauth test google alice@example.com
nexus oauth revoke google alice@example.com
```

### 12.3 Plugins

```bash
nexus plugins list
nexus plugins init my-plugin
nexus plugins install some-plugin
nexus plugins info some-plugin
nexus plugins uninstall some-plugin
```

### 12.4 Knowledge graph

Use this when Nexus has already extracted or stored graph entities and you want
to inspect relationships:

```bash
nexus graph search "alice"
nexus graph entity ent_123
nexus graph neighbors ent_123 --hops 2
```

### 12.5 Governance and fraud signals

These commands are mainly for operator or marketplace deployments, not for a
single-user laptop setup:

```bash
nexus governance status
nexus governance alerts --severity high
nexus governance rings --json
```

### 12.6 Exchange, payments, reputation, and audit

These features fit together as a marketplace flow:

1. publish something through the exchange
2. pay for it
3. leave reputation feedback
4. inspect the audit trail

Typical first commands:

```bash
nexus exchange list
nexus exchange create /workspace/report.csv --price 25 --description "Weekly report"
nexus pay balance
nexus pay transfer bob_bot 10.00 --memo "For data access"
nexus reputation show bob_bot
nexus reputation feedback exch_123 --rater alice_bot --rated bob_bot --outcome positive
nexus audit list --since 1h
```

The current exchange CLI is present, but the backend is still marked as under
development. Use it as an advanced deployment feature, not as the first thing
you try.

### 12.7 Conflicts, locks, cache, migrate, secrets audit, and RLM

These commands are operational tools. You usually need them after a deployment
already exists.

Conflict handling:

```bash
nexus conflicts list
nexus conflicts show <conflict-id>
nexus conflicts resolve <conflict-id> --outcome nexus_wins
```

Distributed lock inspection:

```bash
nexus lock list
nexus lock info /workspace/project/db.sqlite
nexus lock release /workspace/project/db.sqlite --force
```

Cache warmup for hot paths:

```bash
nexus cache stats
nexus cache warmup /workspace/project --include-content
nexus cache hot
```

Migration and bulk import:

```bash
nexus migrate status
nexus migrate plan --from 0.9.0 --to 0.10.0
nexus migrate import-fs --source ./docs --target /workspace/docs/ --dry-run
```

Secret access audit:

```bash
nexus secrets-audit list --since 1h
nexus secrets-audit export --format csv
nexus secrets-audit verify <record-id>
```

Recursive language-model inference:

```bash
nexus rlm infer /workspace/report.pdf --prompt "Summarize the key findings"
```

### 12.8 Other command families you will eventually see

These are real user-facing areas, but they are more specialized than the core
guide above:

- `graph`: knowledge graph queries
- `governance`: anti-fraud and collusion analysis
- `reputation`: reputation and disputes
- `exchange`: agent exchange marketplace
- `pay`: credits and payment flows
- `audit`: exchange transaction audit
- `conflicts`: optimistic concurrency conflict resolution
- `lock`: distributed lock visibility
- `cache`: cache warming and cache stats
- `migrate`: upgrade, rollback, backup, restore, and import flows
- `secrets-audit`: secret access auditing
- `rlm`: recursive language-model inference

## 13. Package Map By Use Case

If you want to read the code after using the product, this is the shortest
useful map.

### Kernel and storage

| Package group | What it gives you as a user |
| --- | --- |
| `nexus.core` | the kernel facade, VFS, syscalls, routing, locks |
| `nexus_kernel` (Rust) | Rust kernel binary — DT_PIPE / DT_STREAM registries, mount router, blocking IPC waits, and the syscall fast paths used by background consumers |
| `nexus.contracts` | stable protocol and type boundaries |
| `nexus.storage` | metadata, record store, history, audit, snapshots |
| `nexus.backends` | local/cloud/object backends and connector adapters |

### Server and remote access

| Package group | What it gives you as a user |
| --- | --- |
| `nexus.daemon` | `nexusd` startup |
| `nexus.server` | FastAPI app, health, auth, RPC, search/workflow routes |
| `nexus.grpc` | gRPC transport for remote clients |
| `nexus.remote` | thin client proxies used by `profile=remote` |

### System services

| Package group | What it gives you as a user |
| --- | --- |
| `nexus.system_services.agents` | agent registry, lifecycle, warmup |
| `nexus.system_services.workspace` | workspace registration and snapshots |
| `nexus.system_services.scheduler` | queue visibility and scheduling |
| `nexus.system_services.event_subsystem` | replay, subscriptions, exporters |
| `nexus.system_services.sync` | sync and write-back plumbing |
| `nexus.system_services.agent_runtime` | embedded agent process runtime |

### Bricks for user features

| Package group | What it gives you as a user |
| --- | --- |
| `nexus.bricks.auth`, `rebac`, `identity`, `delegation`, `access_manifest` | auth, permissions, identity, delegation, tool scoping |
| `nexus.bricks.ipc`, `a2a` | agent messaging and agent-to-agent protocols |
| `nexus.bricks.search`, `parsers`, `llm`, `mcp`, `discovery` | retrieval, parsing, LLM reading, MCP serving, tool discovery |
| `nexus.bricks.workspace`, `context_manifest` | workspace management and context packaging |
| `nexus.bricks.workflows`, `sandbox` | automation and isolated execution |
| `nexus.bricks.snapshot`, `versioning`, `upload`, `mount` | durability, transfer, rollback, external mount flows |
| `nexus.bricks.governance`, `reputation`, `pay`, `exchange` | marketplace and governance features |

### Supporting packages

| Package group | What it gives you as a user |
| --- | --- |
| `nexus.network`, `nexus.security`, `nexus.raft` | federation networking, trust, consensus |
| `nexus.tools` | agent-framework-facing tool wrappers |
| `nexus.validation` | validation pipelines before execution or sandbox use |
| `nexus.plugins` | extension points and plugin discovery |

## 14. Troubleshooting

### The first commands to run

```bash
nexus doctor
nexus status
curl "$NEXUS_URL/health"
curl "$NEXUS_URL/api/v2/bricks/health"
```

### If `nexus search` or another command group is missing

Nexus CLI command registration is import-based. If a command group does not
appear in `nexus --help`, the module likely failed to load in your environment.
The usual fix is:

1. reinstall from a clean checkout or fresh virtualenv
2. rerun `nexus --help`
3. verify the feature dependencies you need are installed

Also make sure your shell is using the repo virtualenv, not an older global or
Conda install:

```bash
source .venv/bin/activate
which nexus
nexus --help
python -m nexus.cli.main --help
```

For this checkout, `which nexus` should point at `.venv/bin/nexus`. If `nexus
status` is missing but `python -m nexus.cli.main status` works, your shell is
resolving the wrong executable.

### If remote SDK calls fail

Check:

- `NEXUS_URL`
- `NEXUS_API_KEY`
- `NEXUS_GRPC_PORT`
- the server was started with the same `NEXUS_GRPC_PORT` value, because gRPC is disabled if the server never exported it

### If permissions seem ignored

Check:

- you are not accidentally using `minimal` or `embedded`
- `NEXUS_ENFORCE_PERMISSIONS` is still true
- you are authenticating as the subject you think you are
- your `rebac` tuples were created in the correct zone

### If search feels half-enabled

Check:

- `nexus search init` has been run for semantic search
- `NEXUS_SEARCH_DAEMON=true` for long-running server-side search
- parser keys such as `UNSTRUCTURED_API_KEY` or `LLAMA_CLOUD_API_KEY` if you expect parsed search
- `ZOEKT_ENABLED=true` only after a Zoekt server is actually running

### If older docs say `nexus serve`

Use `nexusd`.
