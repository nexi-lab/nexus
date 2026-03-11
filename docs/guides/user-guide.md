# User Guide

Nexus is easiest to understand as a terminal-first filesystem and context plane for agent systems. The current product surface is:

- the embedded Python SDK in `nexus.sdk`
- the rich `nexus` CLI
- the `nexusd` daemon plus FastAPI/gRPC server stack
- the remote thin-client path for shared deployments

There is not a first-party full-screen TUI in this repository today. The shipped terminal UX is a rich CLI with prompts, tables, watch mode, and JSON output. If you want a true TUI, the supported foundation is `nexus.sdk` plus the CLI JSON contracts.

## Setup First

Before choosing a workflow, make sure Nexus is installed in a way that matches your goal.

### Start from a source checkout

Use a source checkout when you are:

- contributing to Nexus
- validating docs or tests against this repository
- changing code and docs together
- working with examples that assume the current branch state

Typical setup:

```bash
uv venv --python 3.14
source .venv/bin/activate
uv pip install -r requirements-minimal.txt
uv pip install -e . --no-deps
```

### Start from PyPI

Use a package install when you are:

- evaluating Nexus as a user rather than a contributor
- embedding Nexus in another project
- following the stable package path instead of the repo checkout path

Typical setup:

```bash
python -m venv .venv
source .venv/bin/activate
pip install nexus-ai-fs
```

### Which should the guide assume?

The user guide assumes the package is importable and the CLI is available. In practice:

- if you are in a source checkout, use the editable install path above
- if you are outside the repo, use the PyPI install path

For CLI examples:

- after a package install, use `nexus ...`
- from a source checkout without console scripts on `PATH`, `python -m nexus.cli.main ...` is the safest fallback

## Choose Your Path

| Use case | Start here | Main packages |
| --- | --- | --- |
| Embed Nexus in a Python tool, notebook, script, or custom UI | `from nexus.sdk import connect` | `sdk`, `core`, `contracts`, `storage`, `backends`, `factory` |
| Operate Nexus from the shell | `nexus ...` | `cli`, `core`, `remote`, `server` |
| Run a shared Nexus node | `nexusd ...` | `daemon`, `server`, `grpc`, `security`, `remote` |
| Mount Nexus like a filesystem | `nexus mount ...` and FUSE workflows | `fuse`, `core`, `backends`, `bricks.parsers` |
| Build agent workflows, memory, search, or sandboxed execution | feature commands and SDK services | `bricks.*`, `system_services`, `validation`, `storage` |
| Run multi-zone federation or remote access | daemon plus remote profile | `remote`, `grpc`, `server`, `raft`, `network`, `security` |

## 1. Start Local

Use the embedded path when you want the smallest working setup and do not need a shared server.

```python
from nexus.sdk import connect

nx = connect(
    config={
        "profile": "minimal",
        "data_dir": "./nexus-data",
    }
)

nx.sys_write("/workspace/hello.txt", b"hello")
print(nx.sys_read("/workspace/hello.txt").decode())
nx.close()
```

This path is powered by:

- `sdk`: stable import surface and the recommended programmatic entrypoint
- `core`: `NexusFS`, syscall-style file operations, routing, locking, and kernel behavior
- `contracts`: filesystem and service contracts, exceptions, operation context, deployment profiles
- `storage`: metadata, record stores, persistent views, versioning, audit, and domain persistence
- `backends`: local CAS/path backends, connector registry, cloud/object-store implementations
- `factory`: boot wiring for profiles, services, and optional bricks

Use this path for:

- local prototypes
- unit and integration tests
- notebook workflows
- custom GUIs or TUIs
- local automation where a daemon would be unnecessary overhead

## 2. Operate From The Terminal

The CLI is the main human-facing interface. It is broad, but it clusters naturally by workflow.

### File and directory work

Use `init`, `write`, `append`, `cat`, `cp`, `copy`, `move`, `rm`, `ls`, `mkdir`, `rmdir`, and `tree` for the core filesystem lifecycle.

These flows primarily exercise:

- `cli.commands.file_ops`
- `cli.commands.directory`
- `core`
- `storage`
- `backends`

### Search and inspection

Use `glob`, `grep`, semantic search commands, `inspect`, `metadata`, and `graph` when you need discovery rather than raw path access.

These flows primarily exercise:

- `cli.commands.search`
- `cli.commands.inspect`
- `bricks.search`
- `bricks.parsers`
- `storage`

### Profiles, configuration, and health

Use `profile`, `connect`, `config`, `status`, `doctor`, and `migrate` to manage connection targets, inspect node health, and validate install/runtime state.

These flows primarily exercise:

- `cli`
- `daemon`
- `server`
- `remote`
- `config`

### Terminal UX notes

The CLI already behaves like a lightweight terminal UI:

- rich human output for interactive terminals
- automatic JSON mode when output is piped
- `status --watch` style live views
- interactive connection setup in `connect`
- explicit `--json`, `--fields`, `--quiet`, and verbosity levels for automation

If you are planning a future TUI, build it on:

- `nexus.sdk` for state and actions
- CLI JSON contracts for parity
- the remote profile when it needs to manage shared infrastructure

## 3. Search, Parse, and Read Context

Nexus has a larger “read and retrieve context” stack than a basic filesystem.

| Capability | What users do | Main packages |
| --- | --- | --- |
| Raw file discovery | glob, grep, list, inspect | `core`, `cli`, `bricks.search` |
| Parsed document search | search PDFs and other parsed formats | `bricks.parsers`, `bricks.search`, `storage` |
| Hybrid and semantic retrieval | build indices, run semantic queries, contextual chunking | `bricks.search`, `storage`, `backends`, `cache` |
| LLM-mediated reading | ask for summaries or structured extraction | `bricks.llm`, `cli.commands.llm`, `remote.domain.llm` |
| MCP tool serving and discovery | expose or consume tools through MCP | `bricks.mcp`, `bricks.discovery`, `cli.commands.mcp` |
| Artifact indexing | index outputs for later retrieval | `bricks.artifact_index`, `storage` |

For practical use, think in layers:

1. `core` gives you paths and bytes.
2. `bricks.parsers` turns supported files into searchable text or structured views.
3. `bricks.search` gives you lexical, hybrid, and semantic retrieval.
4. `bricks.llm` and `bricks.mcp` connect the retrieval layer to agent tooling.

## 4. Control Access and Identity

Nexus is not only storage. It also carries identity, authorization, and inter-agent control surfaces.

| Use case | Main commands | Main packages |
| --- | --- | --- |
| Relationship-based access control | `rebac`, `manifest` | `bricks.rebac`, `bricks.access_manifest`, `contracts`, `storage` |
| Authentication and OAuth | `oauth`, server auth routes | `bricks.auth`, `server.auth`, `storage` |
| Agent identity and credentials | `identity` | `bricks.identity`, `storage`, `security` |
| Delegation | `delegation` | `bricks.delegation`, `identity`, `storage` |
| Reputation and dispute flows | `reputation` | `bricks.reputation`, `storage` |
| Governance and anti-fraud | `governance` | `bricks.governance`, `storage`, `graph` |
| IPC and A2A | `ipc`, `agent`, A2A APIs | `bricks.ipc`, `bricks.a2a`, `system_services` |

These are the packages to read first if your product story is “shared agents with policy” rather than “filesystem with search.”

## 5. Build Long-Running Agent Systems

Several packages exist specifically for persistent agent workflows rather than one-off file commands.

| Capability | Main packages | Notes |
| --- | --- | --- |
| Memory and memory evolution | `bricks.memory`, `storage`, `system_services.workspace` | memory APIs, paging, versioned and append-only flows |
| Workspaces and branching | `cli.commands.workspace`, `cli.commands.context`, `bricks.workspace`, `bricks.context_manifest` | workspace-scoped work, context branching, pre-execution manifests |
| Workflow automation | `bricks.workflows`, `cli.commands.workflows` | event-driven workflow execution |
| Sandboxed execution | `bricks.sandbox`, `cli.commands.sandbox`, `validation` | Docker, Monty, and routing logic for code execution |
| Validation | `validation` | use `nexus.validation` as the canonical public validation entrypoint |
| Durable background work | `tasks`, `scheduler_cli`, `system_services.scheduler` | queueing, workers, fair-share scheduling |
| Eventing and watch flows | `events`, `watch`, `system_services.event_subsystem` | subscriptions, replay, notifications |
| Agent-tool integration | `tools`, `bricks.tools`, `bricks.mcp`, `bricks.llm` | LangGraph-style tools, prompts, file/memory/search actions |

When these features are running in a daemon-backed node, the supporting control plane is mostly:

- `server`
- `system_services`
- `storage`
- `tasks`

## 6. Version, Snapshot, Sync, and Recover

Nexus includes several ways to preserve or move state:

| Capability | Main packages |
| --- | --- |
| File history and rollback | `bricks.versioning`, `cli.commands.versions`, `storage` |
| Transactional snapshots | `bricks.snapshot`, `cli.commands.snapshots`, `storage` |
| Operation logs and audit | `cli.commands.operations`, `cli.commands.audit`, `storage` |
| Upload and resumable transfer | `bricks.upload`, `cli.commands.upload`, `backends` |
| Mount persistence and external mounts | `bricks.mount`, `cli.commands.mounts`, `backends.connectors` |
| Proxying and edge sync | `proxy`, `remote`, `server`, `backends.transports` |
| Zone export/import portability | `bricks.portability`, `zone`, `storage`, `raft` |

Use these packages when the question is “how do we keep, move, replay, or recover state?”

## 7. Run Nexus As A Service

When you leave the embedded path, the important components are:

- `daemon`: bootstraps `nexusd`
- `server`: FastAPI app, auth, RPC dispatch, health, subscriptions, websockets, observability
- `grpc`: typed remote transport for the thin client path
- `remote`: thin filesystem and service proxies used by the remote SDK profile
- `security`: federation TLS and trust bootstrap

Choose this path when you need:

- a long-lived process
- remote SDK clients
- shared auth and policy
- websockets or subscriptions
- operational observability
- deployment-profile-specific features enabled centrally

## 8. Federation, Networking, and Mounting

The advanced distributed path is split across a few packages:

- `raft`: consensus, zone management, federated metadata, distributed sharing
- `network`: WireGuard-oriented network setup and peer config for federation
- `security`: TLS, trust-on-first-use bootstrap, join tokens
- `fuse`: mount Nexus into the host filesystem

These are not the best place to start unless your use case is explicitly:

- multi-zone data sharing
- remote joins and trust establishment
- mounted filesystem UX for existing Unix tools

## 9. Extensibility

Two extension stories are present in the codebase:

- `plugins`: package-level extension model for extra commands and hooks
- `backends.connectors`: data-plane extension model for external systems and storage providers

Use plugins when you want to extend Nexus behavior without forking the core package. Use connectors/backends when you want new storage or external service integrations.

## Package Map

The table below is the shortest practical package-by-package map for the checked-in source.

| Package | Role |
| --- | --- |
| `backends` | Concrete storage backends, connectors, wrappers, and transport adapters |
| `bricks` | Optional feature modules such as auth, search, memory, sandbox, workflows, MCP, governance, pay, and versioning |
| `cache` | Cache drivers and cache-aware acceleration around retrieval and authorization |
| `cli` | Human/operator terminal interface |
| `config` | Unified config model and environment/file loading |
| `contracts` | Shared contracts and types across the whole stack |
| `core` | The kernel and filesystem/VFS implementation |
| `daemon` | `nexusd` bootstrap and process management |
| `factory` | Default service and brick wiring for each profile |
| `fuse` | Mount Nexus into the host filesystem |
| `grpc` | Typed remote transport surface |
| `lib` | Shared internal utilities used across tiers |
| `migrations` | Version upgrades and migration flows |
| `network` | WireGuard and future federation transport helpers |
| `plugins` | Extension/plugin mechanism |
| `proxy` | Edge proxying, replay, offline queue, conflict handling |
| `raft` | Federation, consensus, and zone-aware metadata |
| `remote` | Thin remote filesystem and service client |
| `sdk` | Stable programmatic entrypoint |
| `security` | Federation TLS and trust bootstrap |
| `server` | FastAPI application, RPC exposure, auth, websockets, observability |
| `storage` | Persistent models, record/metastore implementations, caches, repositories |
| `system_services` | Internal long-running system services for agents, workspaces, scheduling, sync, and lifecycle |
| `tasks` | Durable task-queue surface and runners |
| `tools` | Tool-facing integrations around Nexus server interactions |
| `utils` | Small shared helpers such as edit/timing utilities |
| `validation` | Validation pipeline for sandboxed and pre-execution checking |

### Package Names That Are Not Primary Source Roots

Some top-level directories currently act more like placeholders or compatibility namespaces than primary checked-in source roots. In practice, current functionality for these areas lives under `bricks/*`, `system_services/*`, `server/*`, or `storage/*`:

- `llm`
- `parsers`
- `search`
- `rlm`
- `services`

When you document product behavior, prefer the feature packages under `bricks/*` and the runtime packages under `server/*` or `system_services/*`.

## Recommended Reading Order

If you are new to the codebase:

1. Quickstart and local SDK path
2. CLI commands you expect to use daily
3. `core`, `contracts`, and `storage`
4. The specific `bricks/*` packages for your feature area
5. `daemon`, `server`, `remote`, and `grpc` if you need shared deployments
6. `raft`, `network`, `security`, and `fuse` only when you are ready for advanced operations
