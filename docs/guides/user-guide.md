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
maturin develop --release -m rust/nexus_runtime/Cargo.toml
python -c "import nexus_runtime; print('nexus_runtime available')"
```

You do not need this for a normal first run. Nexus falls back to Python
implementations when `nexus_runtime` is not installed. Add it when you want faster
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

### Sandbox profile (per-agent runtime)

**Goal:** start a lightweight, self-contained Nexus for a single agent
sandbox with one command, and know exactly what it runs locally.

**Why this profile:** `sandbox` runs with **no PostgreSQL, no
Dragonfly/Redis, no Zoekt** — SQLite + in-process cache + BM25S keyword
search. It is the per-agent runtime target: low RSS, fast boot, optional
hub federation. Full reference: [Sandbox deployment
profile](../deployment/sandbox-profile.md).

> **Not to be confused with the sandbox-provisioning brick.** The
> `sandbox` *deployment profile* is *how Nexus runs* (a lightweight
> runtime). `BRICK_SANDBOX` is a *feature* — provisioning code-execution
> sandboxes (E2B/Docker). They are orthogonal: the `sandbox` profile has
> `BRICK_SANDBOX` **disabled** by default. A `full`/`cloud` profile can
> provision sandboxes; a `sandbox`-profile runtime cannot.

**Start it (from a source checkout or a package install):**

```bash
# One command — `nexus up` shells out to nexusd directly (no Docker).
# --host/--port/--data-dir are passed through to nexusd and the
# resolved connection is persisted so the follow-up workflow can find it:
nexus up --profile sandbox --workspace ~/app \
  --host 127.0.0.1 --port 2026 --data-dir ~/.nexus/sandbox

# Equivalent direct daemon invocation:
nexusd --profile sandbox --workspace ~/app --host 127.0.0.1 --port 2026
```

**Discover it afterwards (#4144 / #4126 — works on non-default host/port):**

The sandbox daemon **always runs on an isolated data dir** — your explicit
`--data-dir` if given, else `~/.nexus/sandbox`. It is never silently
pointed at an existing project's `data_dir`, and it never modifies a
project's `nexus.yaml` or clobbers/mixes its `.state.json`. How you
discover the running sandbox depends on whether a project `nexus.yaml`
exists in the current directory:

- **No project `nexus.yaml`:** `nexus up --profile sandbox` writes a
  minimal `nexus.yaml` *and* the runtime-state record
  (`<isolated-data-dir>/.state.json`) so `nexus env`/`nexus status`
  (no `--url`) discover the sandbox directly:

  ```bash
  # Connection vars resolved from persisted state (NEXUS_URL,
  # NEXUS_GRPC_HOST, NEXUS_GRPC_PORT, NEXUS_PROFILE=sandbox,
  # NEXUS_WORKSPACE) — the hub token is NEVER persisted:
  eval "$(nexus env)"

  # Health/status against the persisted sandbox endpoint:
  nexus status
  ```

- **A project `nexus.yaml` already exists:** it stays authoritative for
  your main stack — the sandbox does **not** touch it or its `.state.json`.
  Discover the running sandbox via the purpose-built `nexus ready` command
  and the daemon readiness file instead (it reports
  `ready: true`, `profile: sandbox`, and the endpoint):

  ```bash
  # Default readiness file is ~/.nexus/nexusd.ready
  nexus ready --json
  ```

**Verify what it started (RPC surface — HTTP only):**

```bash
# Wait for / check the daemon is up (exit 0 when ready):
nexus ready --timeout 60
# Machine-readable:
nexus ready --json

# HTTP health:
curl -s http://127.0.0.1:2026/health

# Profile + enabled/disabled bricks (public, no auth):
curl -s http://127.0.0.1:2026/api/v2/features
```

**Expected behavior:**

- **Success:** `/health` returns `200`; `/api/v2/features` reports
  `"profile": "sandbox"` with `enabled_bricks` ⊇ `{search, mcp, parsers,
  eventlog, namespace, permissions}` and `llm`/`pay`/`observability`/
  `federation` **absent**.
- **Denied (usage error, exit 64):** `--workspace`, `--hub-url`, or
  `--hub-token` without `--profile sandbox`; `--hub-url` without
  `--hub-token`.
- **Unavailable by architecture:** the typed VFS gRPC `Ping`
  (`NexusVfsService`) is bound **only by the cluster profile** (single
  server spawn call site, `rust/profiles/cluster/src/main.rs`). The sandbox
  profile is **HTTP-only for the VFS surface by architecture** — it never
  binds the typed VFS gRPC server (verified: connection-refused on
  `http_port + 2`). The sandbox profile also does **not** start Raft
  federation: the boot path sets `NEXUS_FEDERATION_DISABLED` so the kernel
  keeps its no-op distributed coordinator — no `ZoneManager`, no Raft gRPC
  listener on `:2126`, no "federation bootstrap" ([#4126](https://github.com/nexi-lab/nexus/issues/4126);
  `--hub-url` hub federation is a separate `SandboxBootstrapper` path,
  unaffected). [#4148](https://github.com/nexi-lab/nexus/issues/4148)
  (the issue that reported an UNAUTHENTICATED `Ping`) does not reproduce in
  sandbox because no VFS gRPC server exists there; it is the **triage
  issue** for this surface (close-recommended / reclassify as a cluster-only
  feature request). Sandbox-provisioning RPCs/CLI are absent
  (`BRICK_SANDBOX` disabled).

**Correctness assertion you can run:** with the daemon up,
`curl -s http://127.0.0.1:2026/api/v2/features | jq -r .profile` prints
`sandbox`, and the boot succeeds with no Postgres/Redis/Zoekt process
running. Proven in CI by `tests/integration/test_sandbox_boot_smoke.py`
(real-subprocess boot, HTTP surface, no external services) and
`tests/unit/cli/test_stack_sandbox.py` (flag-gating).

**Performance:** boot is a **setup path** and the features endpoint is
**control plane** — not performance-sensitive hot paths, so they are not
regression-gated, only loosely bounded. Observed in the smoke test under
cold CI conditions (cold Rust-kernel init + parallel test load — not a
tuned product target): cold boot ≈ 43 s, warm boot ≈ 70 s, RSS ≈ 192 MB.
(The "warm" figure exceeds the "cold" one here purely because of test-ordering and parallel xdist load — boot time is not a tuned target, so do not read this as a warm-vs-cold performance relationship.)
The `docs/deployment/sandbox-profile.md` design target is the reference
envelope; these are characterization numbers, not guarantees.

**Story surface coverage** (this story; aggregated into the shared
matrix, [#4139](https://github.com/nexi-lab/nexus/issues/4139)):

| Surface | Type | Sandbox status | Test | Benchmark class |
|---|---|---|---|---|
| `nexus up --profile sandbox` | CLI | supported | `tests/unit/cli/test_stack_sandbox.py`, `tests/integration/test_sandbox_boot_smoke.py` | setup path |
| `--workspace` / `--hub-url` / `--hub-token` | CLI | supported (gated) | `tests/unit/cli/test_stack_sandbox.py`, `tests/integration/test_sandbox_boot_smoke.py` | setup path |
| `nexusd --profile sandbox` | CLI | supported | `tests/integration/test_sandbox_boot_smoke.py` | setup path |
| `nexus ready` | CLI | supported | `tests/unit/cli/test_ready_cmd.py`, `tests/integration/test_sandbox_boot_smoke.py` | control plane |
| HTTP `/health` | HTTP | supported | `tests/integration/test_sandbox_boot_smoke.py` | control plane |
| HTTP `/api/v2/features` | HTTP | supported | `tests/integration/test_sandbox_boot_smoke.py` | control plane |
| gRPC `Ping` | typed gRPC | unavailable — cluster-profile-only (`NexusVfsService`); not bound in sandbox by architecture — see #4148 | `tests/integration/test_sandbox_boot_smoke.py` (`test_sandbox_does_not_bind_typed_vfs_grpc`) | n/a |
| `nexus status` | CLI | supported (reads persisted sandbox state, #4144) | `tests/unit/cli/test_stack_sandbox.py`, `tests/integration/test_sandbox_boot_smoke.py` | control plane |
| `nexus env` / `nexus run` | CLI | supported (reads persisted sandbox state, #4144) | `tests/unit/cli/test_stack_sandbox.py`, `tests/integration/test_sandbox_boot_smoke.py` | control plane |

**Missing-surface gate verdict:** all core boot-story surfaces exist, so
this story is **not blocked**. The typed VFS gRPC `Ping` is unavailable in
sandbox **by architecture** (cluster-profile-only — `NexusVfsService` is
bound only by the cluster profile, never the sandbox path); #4148 is the
triage issue for that surface (close-recommended / reclassify as
cluster-only). The readiness/discovery gap — a sandbox started
on a non-default host/port could not be found by the follow-up
`nexus env` / `nexus status` / `nexus run` workflow — is **genuinely
closed in this PR** ([#4144](https://github.com/nexi-lab/nexus/issues/4144)):
`nexus up --profile sandbox` now passes `--host`/`--port`/`--data-dir`
through to `nexusd` and persists a runtime-state record (resolved HTTP
and gRPC ports, profile, workspace, bind host) that `nexus env`,
`nexus run`, and `nexus status` consume. The hub token is never written
to persistent state. `nexus ready` remains a complementary readiness
probe (waits for `~/.nexus/nexusd.ready`, polls `/health` +
`/api/v2/features`, exits `0` when ready). No build issue is required.

### Sandbox local file workflow (agent-local edits)

**Goal:** let an agent inspect and edit the operator's local project through
the sandbox runtime without starting Postgres, Redis/Dragonfly, Zoekt, or the
full shared stack.

When the daemon is started with `--profile sandbox --workspace ~/app`, the
workspace is mounted inside Nexus at `/zone/local`. That mount is the
workspace-local path for kernel callers and embedded agents:

```bash
nexus up --profile sandbox --workspace ~/app \
  --host 127.0.0.1 --port 2026 --data-dir ~/.nexus/sandbox

nexus ready --timeout 60
curl -s http://127.0.0.1:2026/health | jq .
```

The equivalent SDK / kernel-call shape for an embedded agent running inside
that sandbox node is:

```python
def edit_workspace(nx):
    # In a sandbox daemon booted with --workspace, SandboxBootstrapper mounts
    # the operator workspace at /zone/local before serving traffic.
    nx.write("/zone/local/notes/todo.txt", b"first task")
    assert nx.read("/zone/local/notes/todo.txt") == b"first task"
    assert nx.stat("/zone/local/notes/todo.txt")["size"] == 10
    nx.sys_rename("/zone/local/notes/todo.txt", "/zone/local/notes/done.txt")
    nx.sys_unlink("/zone/local/notes/done.txt")
```

CLI file commands have the same command shapes as the full/local file
surface and are covered for parity:

```bash
nexus mkdir /workspace/notes
nexus write /workspace/notes/todo.txt "first task"
nexus stat /workspace/notes/todo.txt --json
nexus cat /workspace/notes/todo.txt
nexus ls /workspace/notes --json
nexus rename-batch /workspace/notes/todo.txt:/workspace/notes/done.txt --json
nexus rm-batch /workspace/notes/done.txt --json
nexus rmdir /workspace/notes
```

For the sandbox daemon specifically, remote file access over HTTP or typed
VFS gRPC is **unavailable by architecture**: sandbox HTTP is allowlisted to
`/health` and `/api/v2/features`, and `NexusVFSService` is not bound. Use the
embedded SDK/kernel path for `/zone/local` file work. A remote CLI pointed at
the sandbox daemon should treat file commands as unavailable rather than
silently falling back to another server.

**Success:** reads and writes under `/zone/local/...` affect the local
workspace directory, and `/health` includes `workspace_index_status`
(`indexing` during the initial walk, then `ready`).

**Denied or failed:** missing paths raise the normal file-not-found error;
invalid paths are rejected by the VFS validator; OS-level workspace
permission problems are logged by `BootIndexer`, and the health state still
transitions to `ready` because a partial index must not block the daemon.

**Correctness assertion you can run:** write bytes through Nexus, stat the
same path, then read it back. The returned bytes must match exactly and the
`stat.size` must equal the byte length. The daemon-local disk assertion is
covered by
`tests/e2e/self_contained/cli/test_sandbox_federation_e2e.py::TestSandboxZonePermissions::test_write_to_local_zone_stays_on_disk`;
CLI/RPC parity is covered by `tests/unit/cli/test_fs_parity.py`.

**Performance classification:** read, write, list, stat, and batch read/write
are hot local edit paths. Guidance benchmarks live in
`tests/benchmarks/bench_read_write_overhead.py`:
`TestTypedVsGenericRead`, `TestWriteNewFile`, `TestListLocalDirectory`,
`TestReadBulkOverhead`, `TestWriteBatchThroughput`, and
`TestSandboxBootIndexerInitialWalk`. Rename/delete/mkdir/rmdir are tested
behaviorally and treated as non-hot local edit mutations.

### Sandbox search workflow (local context and degraded semantics)

**Goal:** let an agent find local workspace context quickly in the sandbox
profile and tell whether a semantic-looking answer came from local vectors,
federated peers, or a keyword-only fallback.

**Why this profile:** sandbox search is intentionally local-first. `glob` and
`grep` run over the mounted workspace. Semantic search tries the local
sqlite-vec vector lane when it is wired, fuses it with BM25S keyword results
in hybrid mode, and reports `semantic_degraded=true` when the answer degraded
to keyword-only BM25S because there were no peers or no usable vector lane.

CLI examples:

```bash
nexus glob "**/*.py" /workspace --plain
nexus grep "TODO" /workspace --search-mode raw --json
nexus search init
nexus search index /workspace
nexus search stats
nexus search query "auth flow" --mode hybrid --json
```

The equivalent SDK/RPC shape is the `SearchService` RPC surface. A direct
semantic call is `nx.service("search").semantic_search(...)`:

```python
async def find_context(nx):
    search = nx.service("search")

    py_files = search.glob("**/*.py", "/workspace")
    grouped = search.glob_batch(["**/*.py", "**/*.md"], "/workspace")
    todos = await search.grep("TODO", path="/workspace", search_mode="raw")

    await search.initialize_semantic_search(embedding_provider=None)
    await search.semantic_search_index(path="/workspace", recursive=True)
    stats = await search.semantic_search_stats()
    hits = await search.semantic_search(
        query="auth flow",
        path="/workspace",
        search_mode="hybrid",
        limit=5,
    )
    return py_files, grouped, todos, stats, hits
```

MCP agents call the same behavior through tool envelopes:

```text
nexus_glob(pattern="**/*.py", path="/workspace")
nexus_grep(pattern="TODO", path="/workspace")
nexus_semantic_search(query="auth flow", path="/workspace", search_mode="hybrid")
```

**Success:** `glob` returns matching paths, `grep` returns file/line/content
matches, `search stats` reports indexed chunks, and `semantic_search` returns
ranked chunks. Real hybrid results include source score labels such as
`keyword_score` and `vector_score` so callers can tell which lane contributed.

**Degraded or unavailable:** when sqlite-vec is disabled, empty, or errors,
or when the sandbox has no reachable semantic peers, semantic results are
still allowed to fall back to BM25S keyword search. Those results carry
`semantic_degraded=true` on each item and MCP also surfaces an envelope-level
`semantic_degraded`. If the search brick is not loaded, MCP returns an
`unavailable` tool error instead of pretending semantic search succeeded.

**Denied:** file, grep, and semantic results are filtered by the caller's
operation context and ReBAC path permissions. If the permission filter strips
all vector-lane hits and only keyword hits remain, the surviving semantic
response is marked degraded because the user did not receive a real semantic
match.

**Correctness assertion you can run:** compare CLI JSON with the SDK/RPC
calls above for the same workspace. The path sets from `nexus glob` and
`search.glob(...)` should match; `nexus grep --json` and `search.grep(...)`
should agree on file/line/content tuples; degraded sandbox MCP semantic
search should include `semantic_degraded=true`. Covered by
`tests/e2e/self_contained/test_cli_output_e2e.py::TestGlobE2E`,
`tests/e2e/self_contained/test_cli_output_e2e.py::TestGrepE2E`,
`tests/integration/services/test_search_service.py::TestGlobBatch`,
`tests/unit/bricks/search/test_sandbox_hybrid_rrf.py`, and
`tests/e2e/self_contained/test_sandbox_mcp.py::test_sandbox_mcp_semantic_search_includes_degraded_flag`.

**Performance classification:** `glob`, `grep`, semantic query latency,
sqlite-vec insert/query, BM25S fallback, and indexing throughput are hot or
setup paths. Benchmarks live in
`tests/benchmarks/test_search_benchmarks.py`,
`tests/benchmarks/test_indexing_benchmarks.py`,
`tests/benchmarks/test_search_protocol_benchmark.py`, and
`docs/benchmarks/2026-04-18-sandbox-vs-gbrain.md`. The April benchmark
reported sandbox hybrid retrieval tied gbrain baseline P@1 at 0.947 on the
gbrain corpus and passed the HERB QA gate at 8/8 top-5 hits.

**Missing-surface gate verdict:** no additional RPC is required for this
story: `glob`, `glob_batch`, `grep`, `semantic_search`,
`semantic_search_index`, `semantic_search_stats`, and
`initialize_semantic_search` exist. The required CLI display path for degraded
and source evidence is JSON output (`--json`), where `semantic_degraded`,
`keyword_score`, and `vector_score` are visible when returned by the service.
Human-mode source/degraded formatting is a UX enhancement, not a blocker for
the documented agent workflow.

### Sandbox local + company hub federation workflow

**Goal:** give an agent one searchable context plane that includes the local
checkout plus hub-served company knowledge, while keeping writes scoped to the
local workspace or to hub zones whose token grants `rw`.

Start the sandbox with a local workspace and a hub token:

```bash
export NEXUS_HUB_TOKEN="nk_live_agent_scoped"
nexus up --profile sandbox --workspace ~/app --hub-url grpc://hub.example.com:2028 --hub-token "$NEXUS_HUB_TOKEN"
nexus ready --timeout 60
```

Hub and zone status are operator surfaces, not sandbox data-path calls. From a
machine that can administer the hub, use either the local hub CLI or the remote
MCP admin tool:

```bash
nexus hub status --detail --json
nexus hub status --remote https://hub.example.com/mcp --admin-token "$NEXUS_HUB_ADMIN_TOKEN" --json
nexus hub zone list --json
nexus federation status
nexus federation zones
nexus federation info <zone-id>
```

Equivalent RPC / SDK shape:

```python
from nexus.contracts.exceptions import ZoneReadOnlyError


async def use_local_and_hub_context(nx):
    # The sandbox startup handshake calls this RPC with the hub token.
    grants = nx.call_rpc("federation_client_whoami", {})
    assert {"zone_id": "company", "permission": "r"} in grants["zones"]
    assert {"zone_id": "shared", "permission": "rw"} in grants["zones"]

    nx.write("/zone/local/notes/plan.md", b"local draft")
    assert nx.read("/zone/local/notes/plan.md") == b"local draft"

    company_policy = nx.read("/zone/company/policies/rate-limit.md")

    try:
        nx.write("/zone/company/policies/rate-limit.md", b"blocked")
    except ZoneReadOnlyError:
        pass

    nx.write("/zone/shared/runbooks/new-runbook.md", b"shared edit")

    search = nx.service("search")
    hits = await search.semantic_search(
        query="rate limit",
        path="/",
        search_mode="hybrid",
        limit=5,
    )
    assert all(hit.get("zone_id") or hit.get("zone_qualified_path") for hit in hits)
    return company_policy, hits
```

**Expected behavior:**

- **Success:** the handshake returns the token's hub grants, the daemon mounts
  the local workspace at `/zone/local`, read-only company knowledge at
  `/zone/company`, and read-write shared knowledge at `/zone/shared`. Search
  results carry `zone_id` and cross-zone dedup/source labels such as
  `zone_qualified_path` so callers can tell which zone produced the hit.
- **Denied:** writes to a read-only hub zone fail before a transport mutation
  is attempted. Writes to `/zone/local` stay on local disk, and writes to an
  `rw` hub zone go through the hub transport.
- **Unavailable:** a bad token or unreachable hub does not prevent local work.
  The handshake is logged as failed, remote zones are not mounted, and the
  sandbox continues in local-only mode. Search may return local BM25S fallback
  hits with `semantic_degraded=true` when all semantic peers are unavailable.

**Correctness assertions:** `federation_client_whoami` must return exactly the
remote zone IDs and `r` / `rw` grants used by the mount table; a write through
`/zone/company` must be denied; a write through `/zone/shared` must be readable
from the hub; and hub-down startup must keep `/zone/local` usable. These are
covered by
`tests/e2e/self_contained/cli/test_sandbox_federation_e2e.py`,
`tests/unit/remote/test_federation_handshake.py`,
`tests/unit/backends/test_remote_zone.py`, and
`tests/integration/bricks/search/test_federated_search.py::TestRemoteZoneSearch`.

**Performance classification:** handshake and hub status are control-plane
paths. Local workspace read/write/list remain the hot file paths benchmarked in
`tests/benchmarks/bench_read_write_overhead.py`. Remote hub reads and
federated search fanout are hot once the agent is running; synthetic guardrails
live in `tests/benchmarks/bench_sandbox_federation_latency.py`
(`TestSandboxFederationHandshakeLatency`, `TestSandboxFederationReadLatency`,
`TestFederatedSearchFanoutLatency`, and
`TestSandboxHubDownDegradedLatency`). The benchmark budgets are for local
dispatch overhead only; live hub latency depends on the network and the hub's
storage/search backend.

**Missing-surface gate verdict:** no new build issue is required for #4130.
The zone-status concern is covered by `nexus hub status --detail --json`,
`nexus hub status --remote ... --json`, `nexus hub zone list`, and the
cluster federation views `nexus federation status`, `nexus federation zones`,
and `nexus federation info <zone-id>`. The remaining `hub.deploy` gap is a
separate hub rollout convenience, not a blocker for the sandbox federation
workflow.

### Sandbox ReBAC, hub-zone, and MCP tool boundaries

**Goal:** let an agent platform owner prove what a sandboxed agent may read,
write, call, and discover across the local workspace, hub-backed zones, and
MCP tools.

**Why this profile:** the sandbox profile includes `permissions`, `mcp`, and
`search`, so the same lightweight runtime can enforce local ReBAC tuples,
mount hub zones with token-scoped `r` or `rw` grants, and filter MCP tools by
`/tools/...` ReBAC grants. The sandbox-provisioning brick remains disabled;
this is about the per-agent Nexus runtime's own access boundary.

Current CLI surface for the ReBAC part is the tuple-level `nexus rebac`
workflow:

```bash
nexus rebac create agent alice direct_owner file /zone/local/notes/todo.txt \
  --zone-id sandbox-agent-1
nexus rebac check agent alice write file /zone/local/notes/todo.txt \
  --zone-id sandbox-agent-1
nexus rebac check agent charlie write file /zone/local/notes/todo.txt \
  --zone-id sandbox-agent-1
nexus rebac explain agent alice write file /zone/local/notes/todo.txt \
  --zone-id sandbox-agent-1 --verbose
```

MCP tool grants use named profiles from `src/nexus/config/tool_profiles.yaml`.
The profile CLI materializes those profiles into the same `/tools/...` ReBAC
namespace used by MCP `tools/list` and `tools/call` filtering:

```bash
nexus mcp profile list
nexus mcp profile show minimal
nexus mcp profile assign agent alice minimal \
  --zone-id sandbox-agent-1
nexus mcp profile inspect agent alice \
  --zone-id sandbox-agent-1 --format json
```

The tuple-level equivalent remains available when you need to inspect or
debug the raw grants:

```bash
nexus rebac list --subject-type agent --subject-id alice \
  --object-type file --zone-id sandbox-agent-1 --format json
nexus rebac create agent alice direct_viewer file /tools/nexus_read_file \
  --zone-id sandbox-agent-1
```

Equivalent RPC / SDK shape:

```python
rebac = nx.service("rebac")

await rebac.rebac_create(
    subject=("agent", "alice"),
    relation="direct_owner",
    object=("file", "/zone/local/notes/todo.txt"),
    zone_id="sandbox-agent-1",
)
assert await rebac.rebac_check(
    subject=("agent", "alice"),
    permission="write",
    object=("file", "/zone/local/notes/todo.txt"),
    zone_id="sandbox-agent-1",
)

# Tool-profile helper used by provisioning code:
from pathlib import Path

from nexus.bricks.mcp.profiles import grant_tools_for_profile, load_profiles

profile = load_profiles(Path("src/nexus/config/tool_profiles.yaml")).get_profile("minimal")
grant_tools_for_profile(
    rebac_manager=nx.service("rebac")._rebac_manager,
    subject=("agent", "alice"),
    profile=profile,
    zone_id="sandbox-agent-1",
)
```

Expected behavior:

- **Success:** Alice's local write check is granted after the tuple exists,
  and MCP `tools/list` or discovery tools show only tools whose
  `/tools/<name>` paths are visible to Alice.
- **Denied:** Charlie's write check is false until a tuple grants access.
  A write to a hub zone mounted with permission `r` fails before a remote
  transport mutation is attempted; the `rw` hub zone path can write.
- **Unavailable:** invisible MCP tools return `not found`, not a permission
  detail, so the agent does not learn restricted tool names.

**Correctness assertions:** the ReBAC tuple/check/list/explain RPC and CLI
rows are covered by `tests/unit/services/test_rebac_service.py` and the
ReBAC story gate in `tests/architecture/test_rebac_surface_story.py`.
Read-only hub-zone fast-fail is covered by
`tests/unit/backends/test_remote_zone.py::TestRemoteZoneBackendReadOnly::test_all_write_mutations_fail_before_remote_transport_call`
and the sandbox federation E2E. Tool-profile assignment and grants affecting
visible and callable tools are covered by `tests/unit/cli/test_mcp_profile_cli.py`
and
`tests/unit/bricks/mcp/test_tool_namespace_middleware.py::TestProfileGrantIntegration::test_profile_grants_drive_list_filtering_and_call_denial`.

**Performance classification:** permission checks are hot path and benchmarked
in `tests/benchmarks/test_rebac_latency.py` and
`tests/benchmarks/bench_rebac_scale.py`. MCP cached `tools/list` filtering
and read-only hub-zone write fast-fail are hot-path guardrails in
`tests/benchmarks/bench_permission_hotpath.py`. Tool-profile assignment is a
control-plane setup operation and is not performance-sensitive.

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

> This walkthrough runs the **FULL deployment profile**. For the full
> brick/driver contract, auth modes, and the three different things
> called "profile", see [FULL deployment profile](../deployment/full-profile.md).

| `nexus init --preset` | Docker stack | Deployment profile |
|---|---|---|
| `local` | none (embedded) | embedded/lite |
| `shared` | postgres+dragonfly (+nexus server) | **full** |
| `demo` | shared + seed data | **full** |

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

## 4.5 Files: lifecycle, batch, streaming, and locks

This is the full file API as a single workflow. Every CLI command below has
an equivalent RPC — the CLI is a thin wrapper, the SDK calls the same
methods. See also [FULL deployment profile — Filesystem
surface](../deployment/full-profile.md#filesystem-surface).

### Lifecycle (write → stat → read → rename → delete)

```bash
echo "hello" | nexus write /workspace/a.txt --stream
nexus stat   /workspace/a.txt --json        # size, content_id, version, is_directory
nexus cat    /workspace/a.txt               # -> hello
nexus rename-batch /workspace/a.txt:/workspace/b.txt --json
nexus rm-batch /workspace/b.txt --json
```

SDK equivalent:

```python
import nexus
nx = nexus.connect()
nx.write("/workspace/a.txt", b"hello")
print(nx.stat("/workspace/a.txt")["size"])     # 5
assert nx.read("/workspace/a.txt") == b"hello"
```

**Correctness check you can run:** `content_id` from `write` equals
`content_id` from `stat` equals the id seen by `cat` — same bytes, one
identity. `nexus stat` proves it without re-reading content.

### Batch (one round-trip for many files)

```bash
nexus read-bulk  /w/a.txt /w/b.txt --json          # {path: content}
nexus stat       /w/a.txt /w/b.txt --json          # multi -> stat_bulk
nexus metadata   /w/a.txt /w/b.txt --json          # extended (mime_type, created_at)
nexus exists     /w/a.txt /w/missing.txt --json    # {path: bool}; exit 1 if any missing
nexus rename-batch /w/a.txt:/w/c.txt --json
nexus rm-batch   /w/b.txt /w/c.txt --json
```

- `read-bulk` skips missing paths (null); `read-bulk --atomic` uses
  `read_batch` and fails on the first missing path.
- `rename-batch`, `rm-batch`, and `metadata` are **per-item independent** —
  one failure does not abort the others; the JSON result reports per-path
  `{success, ...}`.
- `stat` (multi-arg) vs `metadata`: `stat`/`stat_bulk` return the five core
  fields (size, content_id, version, modified_at, is_directory);
  `metadata`/`metadata_batch` adds mime_type, created_at, zone_id.

### Streaming and range reads

```bash
nexus cat /w/big.bin --offset 0 --length 1048576     # first 1 MiB (read_range)
nexus cat /w/big.bin --stream --chunk-size 65536     # chunked (stream)
cat ./local.bin | nexus write /w/big.bin --stream    # chunked write (write_stream)
```

`read_range(path, start, end)` is start-inclusive, end-exclusive;
`nexus cat --offset N --length M` reads bytes `[N, N+M)`. An end past EOF
returns the available bytes (bounded, not an error).

### Locks

```bash
nexus lock list
nexus lock info /w/a.txt
nexus lock release /w/a.txt --force
```

A second acquirer of a held lock is refused/blocked; release frees it.
`nexus lock info` reflects current state.

### Failure and unavailable behavior

- Unauthenticated request → HTTP 401 (not a traceback).
- Authenticated but unpermitted → explicit denial.
- `nexus admin fs backfill-index` / `flush-write-observer` are **admin-only**;
  a non-admin caller is refused server-side.
- The legacy `POST /api/nfs/{method}` HTTP endpoint is **deprecated,
  migration-only**, sunset **2026-06-25** (Issue #1133). Use gRPC `Call` or
  the typed `Read`/`Write`/`Delete` RPCs (what the CLI uses).

### Performance (guidance, not CI gates)

Hot paths benchmarked in `tests/benchmarks/bench_read_write_overhead.py`
(median, dev laptop, in-process FS):

| Operation                        | Median  |
|----------------------------------|---------|
| Typed `nx.read` (1 KiB file)     | ~165 µs |
| `read_range(64 KiB)` of 1 MiB    | ~2.9 ms |
| `stat_bulk` of 100 files         | ~1.7 ms (≈17 µs / path) |
| `sys_lock` + `sys_unlock` cycle  | ~1.0 ms |

These are guidance, not CI gates. Re-run on your hardware with:
`pytest tests/benchmarks/bench_read_write_overhead.py --benchmark-only -k "RangeRead or StatBulk or TypedVsGenericRead or LockAcquireRelease"`.

## 5. Search, Parsing, And Indexing

Think about search in three layers:

1. file discovery: `glob`, `grep`
2. parsed text extraction: PDFs, docs, and other formats
3. semantic and hybrid retrieval: `nexus search ...`

Search surface coverage matrix:

| Task | CLI | HTTP/RPC surface | Use when |
|------|-----|------------------|----------|
| Find paths | `nexus glob "**/*.py" /workspace` | `POST /api/v2/search/glob`, RPC `SearchService.glob`, MCP `nexus_glob` | You need file names, not file contents. |
| Find exact text | `nexus grep "TODO" /workspace` | `POST /api/v2/search/grep`, RPC `SearchService.grep`, MCP `nexus_grep` | You know the token or regex to match. |
| Query retrieved chunks | `nexus search query "auth flow" --mode hybrid` | `GET /api/v2/search/query`, RPC `SearchService.semantic_search`, MCP `nexus_semantic_search` | You need ranked chunks, not only exact text. |
| Build or refresh indexes | `nexus search init`, `nexus search index`, `nexus reindex` | `POST /api/v2/search/index`, `/refresh`, `/index-directory`, `/indexing-mode` | You are preparing a corpus or changing indexing scope. |
| Explain ranking context | `nexus path-context set src/nexus/bricks/search "Hybrid search brick"` | `PUT /api/v2/path-contexts/` | You want path-level descriptions attached to retrieval results. |

Expected outcomes are deliberately boring:

- success returns paths, grep items, or ranked chunks inside the normal response envelope
- permission denial filters paths or candidates and reports truncation/denial metadata where the endpoint supports it
- unavailable providers return a clear unavailable/configuration error instead of pretending semantic or parsed search ran

Correctness is covered by the search router, grep/glob, semantic-search, parser, path-context, and RRF tests. Performance-sensitive rows are classified in `docs/architecture/api-rpc-surface-coverage.yaml`; grep/glob and query paths are hot, indexing is setup work, and health/stats/control endpoints are not performance sensitive. Retrieval-quality benchmark notes live in `docs/benchmarks/2026-04-18-sandbox-vs-gbrain.md`.

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

Parsed grep uses parser output when possible. Raw grep is better for code and
plain text; parsed grep is better for PDFs, Office documents, and markdown
structure. The section-aware grep flow is not available yet; track build issue #4186
for `nexus grep PATTERN PATH --in-section "## API"`.

The parser introspection and direct run-parse commands are also not exposed yet.
Track build issue #4187 for `nexus parsers list` and
`nexus parsers run PATH --provider ...` surfaces.

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
curl -H "Authorization: Bearer $NEXUS_API_KEY" \
  "$NEXUS_URL/api/v2/search/query?q=database%20migration&type=hybrid&limit=5"
```

Hybrid search fuses exact, vector, and provider-backed sources with RRF where
multiple ranked lists are available. RRF makes the final order depend on rank
agreement instead of raw score scale, which is why exact text hits and semantic
neighbors can both appear near the top.

Use `nexus path-context` when the result path alone is ambiguous:

```bash
nexus path-context set src/nexus/bricks/search "Search, semantic indexing, RRF, and parser-backed retrieval"
nexus path-context list
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

### Step 5: Tell a multi-user sharing story

The `full` profile includes the complete ReBAC brick and is the right profile
when several users, agents, or apps need to share one Nexus node. Today the
stable CLI path is tuple-level ReBAC: create the relationship, check it, and
explain the graph path when access surprises you.

```bash
nexus rebac namespace-create file \
  --relations direct_owner \
  --relations direct_viewer \
  --permission read:direct_viewer,direct_owner \
  --permission write:direct_owner

nexus write /workspace/team/report.csv "name,ssn,email\nAlice,111,alice@example.com"
nexus rebac create agent alice direct_owner file /workspace/team/report.csv --zone-id org_acme
nexus rebac create agent bob direct_viewer file /workspace/team/report.csv --zone-id org_acme

nexus rebac check agent bob read file /workspace/team/report.csv --zone-id org_acme
nexus rebac check agent charlie read file /workspace/team/report.csv --zone-id org_acme
nexus rebac explain agent bob read file /workspace/team/report.csv --zone-id org_acme --verbose
```

Expected behavior:

- Bob's read check is granted.
- Charlie's read check is denied until a direct, group, or inherited relation
  grants access.
- If ReBAC is unavailable or disabled for a stripped-down runtime, the command
  must fail with a service-unavailable style error rather than silently allowing
  access.

Equivalent RPC calls use the same service:

```python
rebac = nx.service("rebac")

await rebac.rebac_create(
    subject=("agent", "bob"),
    relation="direct_viewer",
    object=("file", "/workspace/team/report.csv"),
    zone_id="org_acme",
)
assert await rebac.rebac_check(
    subject=("agent", "bob"),
    permission="read",
    object=("file", "/workspace/team/report.csv"),
    zone_id="org_acme",
)
```

Dynamic viewer tuples can be created from the CLI when the object is a CSV:

```bash
nexus rebac create agent bob dynamic_viewer file /workspace/team/report.csv \
  --zone-id org_acme \
  --column-config '{"hidden_columns":["ssn"],"visible_columns":["name","email"]}'
```

The equivalent user/group sharing and dynamic-viewer CLI flows are direct
`nexus rebac` commands:

```bash
nexus rebac share user file /workspace/team/report.csv bob \
  --permission viewer --zone-id org_acme
nexus rebac share incoming user bob --zone-id org_acme --format json
nexus rebac list-objects shared-viewer user bob --zone-id org_acme --format json
nexus rebac dynamic config user bob /workspace/team/report.csv \
  --zone-id org_acme --format json
nexus rebac dynamic read user bob /workspace/team/report.csv \
  --content-file /workspace/team/report.csv --zone-id org_acme --format json
nexus rebac share revoke file /workspace/team/report.csv user bob \
  --permission viewer --zone-id org_acme
```

Public/private discovery and consent are control-plane operations:

```bash
nexus rebac public file /workspace/team/report.csv --zone-id org_acme
nexus rebac private file /workspace/team/report.csv --zone-id org_acme
nexus rebac consent grant user alice user bob --zone-id org_acme
nexus rebac expand-with-privacy read file /workspace/team/report.csv \
  --requester user:bob --zone-id org_acme --format json
```

Dedicated RPCs cover the same workflows for user/group sharing,
public/private resources, consent, privacy-filtered expand, incoming/outgoing
shares, list-objects, and dynamic-viewer reads. The surface coverage map tracks
them under #4134.

Correctness assertions live in `tests/unit/services/test_rebac_service.py`,
`tests/unit/services/test_rebac_share_mixin.py`, and
`tests/e2e/self_contained/test_rebac_full_story_e2e.py`. Performance-sensitive
paths are permission check, batch check, tuple list, expand/list-objects, and
dynamic-viewer read overhead; ReBAC latency and scale coverage is in
`tests/benchmarks/test_rebac_latency.py` and
`tests/benchmarks/bench_rebac_scale.py`. Sharing, namespace, consent, and
public/private mutations are control-plane operations, not request hot paths.

### Step 6: Create and test access manifests

Access manifests let you say which tools and data surfaces an agent may use.

```bash
nexus manifest create agent_alice --name "dev tools" --entry "read_*:allow"
nexus manifest list
nexus manifest evaluate <manifest-id> --tool-name read_file
```

### Step 7: If you are running database auth, create real user keys

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
nexus agent update alice_bot --description "Plans workspace changes" --metadata tier=gold
nexus agent list
nexus agent info alice_bot
nexus agent transition alice_bot CONNECTED
nexus agent heartbeat alice_bot
nexus agent status alice_bot
```

By default, registered agents do not get their own API keys. They use the
owner's auth plus the `X-Agent-ID` model, which is the recommended path.

If you really need an agent-specific key:

```bash
nexus agent register legacy_bot "Legacy Bot" --with-api-key
```

Equivalent RPC / SDK shape:

```python
agent_rpc = nx.service("agent_rpc")

created = await agent_rpc.register_agent(
    agent_id="alice_bot",
    name="Alice Research Bot",
    description="Plans workspace changes",
    context={"user_id": "alice", "zone_id": "root"},
)
await agent_rpc.update_agent(
    "alice_bot",
    metadata={"tier": "gold"},
    context={"user_id": "alice", "zone_id": "root"},
)
state = await agent_rpc.agent_transition("alice_bot", "CONNECTED")
agent_rpc.agent_heartbeat("alice_bot")
assert state["agent_id"] == created["agent_id"]
```

Expected behavior:

- **Success:** register creates the agent config and registry entry; update
  rewrites the config; transition advances lifecycle state; heartbeat records
  liveness.
- **Denied:** deleting an owned `user,agent` id as a different non-admin user
  raises a permission error.
- **Unavailable:** lifecycle calls fail with a clear "AgentRegistry not
  available" error if the registry service is not wired.
- **Stale agent:** `nexus agent transition alice_bot CONNECTED
  --expected-generation 7` rejects the call if the current generation is not 7.

**Correctness assertion:** after the transition and heartbeat, `nexus agent
status alice_bot --json` shows the lifecycle phase and last heartbeat for the
same agent id. CLI wrapper coverage lives in
`tests/unit/cli/test_lifecycle_surface_cli.py`; registration and conflict
behavior lives in `tests/unit/services/test_agent_registration.py`.

**Performance classification:** agent heartbeat and list are hot-path control
plane calls. They should stay O(1) or registry-scan bounded and are classified
in the surface map with the issue #4137 benchmark expectation; registration,
update, transition, and delete are control-plane paths.

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
nexus workspace update /workspace/project --metadata owner=alice
nexus workspace snapshot /workspace/project --description "Before refactor"
nexus workspace log /workspace/project
nexus workspace restore /workspace/project --snapshot 1 --yes
nexus workspace diff /workspace/project --snapshot1 1 --snapshot2 2
```

Load the same registration from config when bootstrapping a repeatable
environment:

```json
{
  "workspaces": [
    {
      "path": "/workspace/project",
      "name": "project",
      "description": "Main project workspace"
    }
  ]
}
```

```bash
nexus workspace config load ./workspaces.json
```

Equivalent RPC / SDK shape:

```python
workspace_rpc = nx.service("workspace_rpc")

await workspace_rpc.register_workspace(
    path="/workspace/project",
    name="project",
    description="Main project workspace",
    context={"user_id": "alice", "zone_id": "root"},
)
workspace_rpc.update_workspace("/workspace/project", metadata={"owner": "alice"})
snap = workspace_rpc.workspace_snapshot(
    workspace_path="/workspace/project",
    description="Before refactor",
    context={"user_id": "alice", "zone_id": "root"},
)
log = workspace_rpc.workspace_log(
    workspace_path="/workspace/project",
    context={"user_id": "alice", "zone_id": "root"},
)
assert log[0]["snapshot_id"] == snap["snapshot_id"]
```

Expected behavior:

- **Success:** registered workspaces are visible to the creating user, and
  snapshots/log/diff/restore operate only after the workspace exists.
- **Denied:** `list_workspaces` requires authenticated context with `user_id`
  and `zone_id`; missing context is rejected.
- **Unavailable:** `workspace_snapshot`, `workspace_restore`, `workspace_log`,
  and `workspace_diff` raise `Workspace not registered: ...` until the path is
  registered.
- **Restore conflict:** restoring a workspace overwrites current workspace
  state; the CLI asks for confirmation unless `--yes` is supplied.

**Correctness assertion:** after snapshot and restore, `nexus workspace log
/workspace/project` contains the restored snapshot number, and
`nexus workspace diff` reports added/removed/modified paths. CLI wrapper
coverage lives in `tests/unit/cli/test_lifecycle_surface_cli.py`; workspace
filtering and auth failure coverage lives in
`tests/unit/core/test_nexus_fs_list_workspaces.py`; HTTP registry behavior is
covered by `tests/e2e/server/test_workspace_registry_api_e2e.py`.

**Performance classification:** workspace register/update/config load are setup
or control-plane paths. Workspace list is a control-plane listing path with the
issue #4137 benchmark expectation in the surface map; snapshot/diff/restore are
size-dependent and treated as hot where they touch file content.

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

### 9.5 Shape An Agent's MCP Tool Profile

MCP tool profiles are the normal way to give an agent the smallest useful
toolbox. They are ReBAC grants over `/tools/<tool-name>`: `tools/list` only
shows granted tools, and `tools/call` returns `not found` for a hidden tool
rather than exposing that it exists.

Use the profile CLI to inspect and assign the matrix:

```bash
nexus mcp profile list
nexus mcp profile show coding
nexus mcp profile assign agent demo-agent coding --zone-id sandbox-agent-1
nexus mcp profile inspect agent demo-agent --zone-id sandbox-agent-1
```

Default task matrix:

| Profile | Use it when the agent needs to... | Direct tools added by this profile |
| --- | --- | --- |
| `minimal` | read and inspect workspace files | `nexus_read_file`, `nexus_list_files`, `nexus_file_info`, `nexus_glob` |
| `coding` | edit code and search text | `nexus_write_file`, `nexus_edit_file`, `nexus_delete_file`, `nexus_mkdir`, `nexus_rmdir`, `nexus_rename_file`, `nexus_grep` |
| `search` | search without mutation rights | `nexus_grep`, `nexus_semantic_search` |
| `execution` | create and use a sandbox runtime | `nexus_python`, `nexus_bash`, `nexus_sandbox_create`, `nexus_sandbox_list`, `nexus_sandbox_stop` |
| `full` | inspect tools, workflows, and hub admin surfaces | `nexus_discovery_search_tools`, `nexus_discovery_list_servers`, `nexus_discovery_get_tool_details`, `nexus_discovery_load_tools`, `nexus_list_workflows`, `nexus_execute_workflow`, `nexus_hub_admin` |

Inheritance matters: `coding` includes `minimal`, `execution` includes
`coding`, and `full` includes `execution`. `search` includes only `minimal`, so
it can grep and semantic-search without write access.

Equivalent MCP JSON-RPC examples:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/list",
  "params": {}
}
```

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/call",
  "params": {
    "name": "nexus_read_file",
    "arguments": {
      "path": "/workspace/hello.txt"
    }
  }
}
```

Expected behavior:

- A `minimal` agent can call `nexus_read_file` and `nexus_glob`; a call to
  `nexus_write_file` returns `not found`.
- A `coding` agent can call write/edit/delete/grep tools; sandbox execution and
  discovery tools are hidden.
- A `search` agent can call grep and semantic search but still cannot mutate
  files.
- An `execution` agent can use `nexus_python`, `nexus_bash`, and sandbox
  lifecycle tools when the sandbox provider is available. If no provider is
  wired, those execution tools are unavailable at server startup.
- A `full` agent can use discovery tools and workflow tools. `nexus_hub_admin`
  still requires an admin bearer token, so a visible tool can still return an
  admin/auth denial.

Correctness assertion: after assigning a profile, `tools/list` must contain only
the profile's inherited tools, and `tools/call` for the next-tier hidden tool
must return `not found`, not a permission-denied message.

Performance classification: `tools/list` filtering and `tools/call` namespace
checks are hot-path operations covered by the MCP namespace filtering benchmark.
Profile assignment is setup/control-plane work and is not on the per-tool-call
latency path.

MCP profile docs link back to the file/search/ReBAC stories in the shared
surface map: file tools use the filesystem rows, grep/semantic search use the
search rows, and profile grants/enforcement use the ReBAC-backed tool namespace
rows.

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
nexus versions diff /workspace/hello.txt --v1 1 --v2 2 --mode metadata
nexus versions rollback /workspace/hello.txt --version 1
```

Equivalent RPC / SDK shape:

```python
version_service = nx.service("version_service")
versions = await version_service.list_versions("/workspace/hello.txt")
content = await version_service.get_version("/workspace/hello.txt", versions[-1]["version"])
diff = await version_service.diff_versions(
    "/workspace/hello.txt",
    v1=versions[-1]["version"],
    v2=versions[0]["version"],
    mode="metadata",
)
assert content
assert "content_changed" in diff
```

Expected behavior: missing versions raise a not-found error, invalid diff modes
raise `ValueError`, and rollback requires the DLC/session wiring used by the
full profile. `list_versions` and `diff_versions` are hot-path/history paths;
`get_version` and `rollback` are content reads/writes and should be benchmarked
when used on large files.

### Transactional snapshots

```bash
nexus snapshot create --description "Before migration"
nexus snapshot list
nexus snapshot info <txn_id>
nexus snapshot entries <txn_id>
nexus snapshot commit <txn_id>
nexus snapshot restore <txn_id>
```

Equivalent RPC / SDK shape:

```python
snapshots = nx.service("snapshots_rpc")
txn = await snapshots.snapshot_create(description="Before migration")
entries = await snapshots.snapshot_list_entries(txn["transaction_id"])
committed = await snapshots.snapshot_commit(txn["transaction_id"])
assert committed["status"] == "committed"
```

Expected behavior: `snapshot list/info/entries` expose transaction state;
`snapshot commit` makes a transaction permanent; `snapshot restore` rolls back
the transaction. If the transactional snapshot service is not wired, the server
returns a stable unavailable error. Snapshot create/restore and list/entries are
performance-sensitive because they can run over many paths; the surface map
links the #4137 benchmark expectation.

### Event replay and live subscriptions

```bash
nexus events replay --since 1h
```

The `nexus events` CLI currently exposes replay over the generic gRPC `Call`
path. Live event subscription is not a CLI/RPC command in this surface.

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

#### Mount External Data Source, List Tools, And Use Them

Goal: in the `full` profile, attach an external source under a Nexus virtual
path, expose the resulting filesystem/search tools through MCP, and verify that
auth and SSRF boundaries fail closed.

CLI flow:

```bash
export NEXUS_PROFILE=full
export NEXUS_URL="http://localhost:2026"
export NEXUS_API_KEY="..."

nexus connectors list --category api
nexus mounts add /sources/hn hn '{}'
nexus mounts list --json
nexus mcp serve --transport http --port 8081 --remote-url "$NEXUS_URL"
```

Use any MCP client against `http://localhost:8081/mcp` with
`Authorization: Bearer $NEXUS_API_KEY`, call `tools/list`, then call a file or
search tool such as `nexus_list_files` with `{"path": "/sources/hn"}`. The mount
is correct when `/sources/hn` appears in `nexus mounts list` and the MCP tool
call can read or list under that path without leaking sibling zones.

Equivalent RPC/SDK flow:

```python
import nexus
from nexus.remote.domain import MCPClient, OAuthClient

nx = nexus.connect(
    config={"profile": "remote", "url": "http://localhost:2026", "api_key": "..."}
)

connectors = nx.list_connectors(category="api")
assert any(c["name"] == "hn" for c in connectors)

mount_id = nx.add_mount(
    mount_point="/sources/hn",
    backend_type="hn",
    backend_config={},
)
assert mount_id == "/sources/hn"

mounts = nx.list_mounts()
assert {"mount_point": "/sources/hn"} in mounts
```

For an external MCP server, use the MCP service RPCs directly:

```python
mcp = MCPClient(nx._call_rpc)
mcp.mount(name="github", url="https://mcp.example.com/sse", transport="sse")
tools = mcp.list_tools("github")
mcp.sync("github")
mcp.unmount("github")
```

These SDK helpers call the underlying RPC names `mcp_mount`, `mcp_list_tools`,
`mcp_sync`, and `mcp_unmount`.

OAuth-backed connectors use the OAuth credential RPCs or CLI helpers:

```bash
nexus oauth list
nexus oauth setup-gdrive --user-email alice@example.com
nexus oauth test google alice@example.com
nexus oauth revoke google alice@example.com
```

```python
oauth = OAuthClient(nx._call_rpc)
auth_url = oauth.get_auth_url(
    provider="google-drive",
    redirect_uri="http://localhost:2026/oauth/callback",
)
oauth.exchange_code(
    provider="google-drive",
    code="4/...",
    user_email="alice@example.com",
)
oauth.list_credentials(provider="google-drive")
```

The helper methods map to `oauth_get_auth_url`, `oauth_exchange_code`,
`oauth_list_credentials`, `oauth_revoke_credential`, and `oauth_test_credential`.

Expected behavior:

- Success: `add_mount` returns the mount point, `list_mounts` returns only mounts
  visible to the caller, and MCP `tools/list`/`tools/call` run with the caller's
  bearer identity.
- Denial: a caller without write permission on the parent path cannot add or
  remove a mount; MCP HTTP with `NEXUS_MCP_REQUIRE_BEARER=true` returns 401
  without a bearer token and 403 for non-admin mount administration.
- SSRF: remote MCP `sse`/`http` URLs are validated before connection. Private,
  loopback, link-local, or operator-denied CIDRs are blocked unless an approval
  policy explicitly allows the host and re-validation still passes.
- Unavailable: `reauth_mount` and `update_mount` preserve stable unavailable or
  no-change results when the runtime has no retained Python backend object for
  that mount; refresh credentials through `nexus oauth setup-*` or
  `oauth_exchange_code` before remounting.

Correctness assertion: after mounting, `nexus mounts list --json` contains the
mount point, `tools/list` shows only tools granted to the subject, and an MCP
tool call under `/sources/hn` succeeds with the same zone visibility as the
equivalent RPC read/list operation.

Performance classification: connector and OAuth setup is control-plane work;
mount add/list is a setup/control-plane path. MCP `tools/list` and `tools/call`
are request hot paths and use the permission hot-path benchmark coverage.

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

## 13. Full-Profile Control Plane For Operators

Use this path when you are operating a shared `full` or `cloud` server and need
database-backed auth, auditable admin changes, and control-plane visibility
without mixing those powers into normal user flows. Normal users should stay on
file, search, pay, and agent workflows with their own API key. Admins hold keys
that can provision users, rotate credentials, inspect audit/event data, and run
governance or federation operations. Operators own the daemon, database,
profile, and runtime configuration.

Start from a database-auth daemon:

```bash
export NEXUS_URL=http://localhost:2026
export NEXUS_GRPC_PORT=2028
export NEXUS_API_KEY="<admin-api-key>"

nexusd --profile full --auth-type database --database-url "$NEXUS_DATABASE_URL"
```

Admin account lifecycle:

```bash
nexus admin provision-user alice alice@example.com --display-name "Alice"
nexus admin create-user bot1 --name "Bot Agent" --subject-type agent --expires-days 7
nexus admin list-users --is-admin
nexus admin deprovision-user alice --zone-id alice --delete-user-record
```

Equivalent generic gRPC calls use the same method names that the CLI calls:

```python
from nexus.remote.rpc_transport import RPCTransport

rpc = RPCTransport("localhost:2028", auth_token="<admin-api-key>")
created = rpc.call_rpc(
    "provision_user",
    {"user_id": "alice", "email": "alice@example.com", "display_name": "Alice"},
)
audit = rpc.call_rpc("audit_list", {"since": "1h", "limit": 50})

print(created["user_id"], created["zone_id"])
print(len(audit.get("transactions", [])))
```

Expected behavior:

- Success: admin-only methods return resource metadata, counts, or exported
  content. Initial API keys are shown once.
- Denial: a non-admin token receives an admin-privilege error for admin,
  provisioning, audit, event replay, governance, and federation mutation RPCs.
- Unavailable: surfaces that need database auth, record stores, pay services,
  governance services, or federation runtime fail as unavailable rather than
  being presented as normal user features.

Correctness assertion:

```bash
nexus admin get-user --user-id alice --json
nexus audit list --since 1h --json
nexus events replay --since 1h --json
nexus governance status --json
```

The JSON output should show the provisioned user or key, audit rows when they
exist, replayed events when they exist, and governance counts. A regular user
key should be able to use normal user surfaces such as `nexus pay balance`, but
should be denied by admin-only methods.

### Control-plane surface matrix

This is the shared model for issue #4138. The source-of-truth test matrix lives
in `nexus.contracts.control_plane_coverage`; it maps each row to CLI/RPC
surfaces, auth expectations, tests, and benchmark classification.

| Group | RPC methods | CLI | Admin-only | Correctness coverage | Performance status |
| --- | --- | --- | --- | --- | --- |
| Admin keys | `admin_create_key`, `admin_list_keys`, `admin_get_key`, `admin_revoke_key`, `admin_update_key`, `admin_write_permission` | `nexus admin create-user`, `create-key`, `create-agent-key`, `list-users`, `get-user`, `revoke-key`, `update-key` | Yes | `tests/unit/server/test_admin_handlers.py`, `tests/unit/server/test_rpc_admin_only.py` | Benchmark gap tracked in #4201 |
| User provisioning | `provision_user`, `deprovision_user` | `nexus admin provision-user`, `deprovision-user` | Yes | `tests/unit/core/test_nexus_fs_provision_user.py` and the #4138 surface test | Setup path, not performance-sensitive |
| Audit | `audit_list`, `audit_export` | `nexus audit list`, `export` | Yes | OpenAPI conformance plus the #4138 surface test | Benchmark gap tracked in #4201 |
| Events | `events_replay` | `nexus events replay` | Yes | event replay/E2E coverage plus the #4138 surface test | Benchmark gap tracked in #4201 |
| Governance | `governance_status`, `governance_alerts`, `governance_rings` | `nexus governance status`, `alerts`, `rings` | Yes | security hardening plus the #4138 surface test | Benchmark gap tracked in #4201 |
| Federation read-only | `federation_client_whoami`, `federation_list_zones`, `federation_cluster_info` | `nexus federation status`, `zones`, `info` | No | federation whoami and docker federation coverage | Benchmark gap tracked in #4201 |
| Federation mutations | `federation_create_zone`, `federation_remove_zone`, `federation_join`, `federation_mount`, `federation_unmount`, `federation_share`, `federation_export_zone`, `federation_import_zone` | `nexus federation mount`, `unmount`; create/remove/share/join CLI parity is #4200 | Yes | docker federation and zone import/export E2E coverage | CLI parity gap #4200, benchmark gap #4201 |
| Pay | `pay_balance`, `pay_transfer`, `pay_history` | `nexus pay balance`, `transfer`, `history` | No | exchange OpenAPI and pay integration coverage | Marketplace user path, not an admin hot path |

Sensitive operation requirements:

- Use an admin API key for every row marked admin-only.
- Use database auth for user provisioning and admin key management.
- Use record-store-backed services for audit and event replay.
- Use a federation-enabled runtime for federation rows. `full` alone does not
  create a federation mesh; use `cluster` or a full/cloud server with federation
  kernel support.
- Use a pay-enabled full/cloud deployment for pay commands. Pay commands are
  normal authenticated marketplace flows, not admin-only flows.

Benchmark note: #4138 names admin key operations, audit list/export, events
replay, governance summaries, and federation zone list/create as benchmark
expectations. This guide does not claim benchmark completion yet; the required
benchmark suite is tracked in #4201.

## 14. Package Map By Use Case

If you want to read the code after using the product, this is the shortest
useful map.

### Kernel and storage

| Package group | What it gives you as a user |
| --- | --- |
| `nexus.core` | the kernel facade, VFS, syscalls, routing, locks |
| `nexus_runtime` (Rust) | Rust kernel binary — DT_PIPE / DT_STREAM registries, mount router, blocking IPC waits, and the syscall fast paths used by background consumers |
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

## 15. Troubleshooting

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
