<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/logo.png">
  <source media="(prefers-color-scheme: light)" srcset="assets/logo.png">
  <img alt="Nexus" src="assets/logo.png" width="180">
</picture>

### The filesystem & context plane for AI agents

Give every agent one place to read, write, search, remember, and collaborate — from a single-file script to a fleet of thousands.

[![CI](https://github.com/nexi-lab/nexus/actions/workflows/test.yml/badge.svg)](https://github.com/nexi-lab/nexus/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/nexus-ai-fs?color=blue)](https://pypi.org/project/nexus-ai-fs/)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache_2.0-blue)](LICENSE)
[![Discord](https://img.shields.io/badge/Discord-community-5865F2?logo=discord&logoColor=white)](https://discord.gg/nexus)

[Documentation](https://nexi-lab.github.io/nexus/) · [Quickstart](https://nexi-lab.github.io/nexus/getting-started/quickstart/) · [Examples](examples/) · [PyPI](https://pypi.org/project/nexus-ai-fs/) · [Roadmap](https://github.com/nexi-lab/nexus/issues)

</div>

---

## Why Nexus

Every agent framework gives you tool calling. None gives you a shared filesystem. Without one, agents duplicate files, lose context between runs, step on each other's writes, and can't discover what's already been built.

Nexus fixes this. One VFS-style interface — start embedded in a single Python process, scale to a daemon-backed deployment with auth, permissions, federation, and multi-tenant isolation. No code changes.

## How it works

```
┌─────────────────────────────────────────────────────────────────────────┐
│  BRICKS (runtime-loadable)                                              │
│  ReBAC · Auth · Agents · Delegation · Search · Memory · Governance      │
│  Workflows · Pay · MCP · Snapshots · Skills · Catalog · 30+ more        │
└─────────────────────────────────────────────────────────────────────────┘
                              ↓ protocol interface
┌─────────────────────────────────────────────────────────────────────────┐
│  KERNEL                                                                 │
│  VFS · Metastore · ObjectStore · Syscall dispatch · Pipes ·             │
│  Lock manager · Three-phase write (LSM hooks) · CAS dedup              │
└─────────────────────────────────────────────────────────────────────────┘
                              ↓ dependency injection
┌─────────────────────────────────────────────────────────────────────────┐
│  DRIVERS                                                                │
│  redb · PostgreSQL (pgvector) · S3 · GCS · Dragonfly · Zoekt · gRPC    │
└─────────────────────────────────────────────────────────────────────────┘
```

**Kernel** never changes. **Drivers** swap at config time. **Bricks** mount and unmount at runtime — like `insmod`/`rmmod` for an AI filesystem.

## Get started in 30 seconds

### Option A: Docker (recommended)

```bash
pip install nexus-ai-fs          # CLI + SDK
nexus init --preset demo          # writes nexus.yaml + docker-compose
nexus up                          # pulls image, starts Nexus + Postgres + Dragonfly + Zoekt
```

Open `http://localhost:2026`. That's it.

### Option B: Python SDK (no Docker)

```bash
pip install nexus-ai-fs
```

```python
import nexus

nx = nexus.connect(config={"data_dir": "./my-data"})

nx.write("/notes/meeting.md", b"# Q3 Planning\n- Ship Nexus 1.0")
print(nx.read("/notes/meeting.md").decode())

results = nx.search("planning", limit=5)      # semantic + keyword hybrid
history = nx.versions("/notes/meeting.md")     # full version history

nx.close()
```

### Option C: CLI

```bash
nexus write /hello.txt "hello world"
nexus cat /hello.txt
nexus ls /
nexus search "hello" --mode hybrid
nexus versions /hello.txt
```

## What you get

| Capability | What it does | How agents use it |
|---|---|---|
| **Filesystem** | POSIX-style read/write/mkdir/ls with CAS dedup | Shared workspace — no more temp files |
| **Versioning** | Every write creates an immutable version | Rollback mistakes, diff changes, audit trails |
| **Snapshots** | Atomic multi-file transactions | Commit or rollback a batch of changes together |
| **Search** | Keyword + semantic + hybrid, powered by Zoekt + pgvector | Find anything by content or meaning |
| **Memory** | Persistent agent memory with consolidation + versioning | Remember across runs and sessions |
| **Delegation** | SSH-style agent-to-agent permission narrowing | Safely sub-delegate work with scoped access |
| **ReBAC** | Relationship-based access control (Google Zanzibar model) | Fine-grained per-file, per-agent permissions |
| **MCP** | Mount external MCP servers, expose Nexus as 30+ MCP tools | Bridge any tool ecosystem |
| **Workflows** | Trigger → condition → action pipelines | Automate file processing, notifications, etc. |
| **Governance** | Fraud detection, collusion rings, trust scores | Safety rails for autonomous agent fleets |
| **Pay** | Credit ledger with reserves, policies, approvals | Metered compute for multi-tenant deployments |
| **IPC** | Inbox-based inter-agent messaging via pipes | Agents talk to each other without polling |
| **Federation** | Multi-zone Raft consensus with mTLS TOFU | Span data centers without a central coordinator |

<details>
<summary><strong>30+ more bricks →</strong></summary>

Access Manifests · Agent Registry · Artifact Index · Auth (API key, OAuth, mTLS) · Catalog (schema extraction) · Context Manifests · Discovery · Event Log · Identity (DID + credentials) · LLM Provider · Parsers (50+ formats via MarkItDown) · Portability (import/export) · Reputation · RLM (retrieval-augmented reasoning) · Sandbox (Docker/Monty) · Scheduler (fair-share, priority tiers) · Share Links (capability URLs) · Skills · Snapshots · TUS Uploads (resumable) · Watch (filesystem events) · Workspace Registry

</details>

## Framework integrations

Every major agent framework works out of the box:

| Framework | What the example shows | Link |
|---|---|---|
| **Claude Agent SDK** | ReAct agent with Nexus as tool provider | [examples/claude_agent_sdk/](examples/claude_agent_sdk/) |
| **OpenAI Agents** | Multi-tenant agents with shared memory | [examples/openai_agents/](examples/openai_agents/) |
| **LangGraph** | Permission-scoped workflows | [examples/langgraph_integration/](examples/langgraph_integration/) |
| **CrewAI** | Multi-agent collaboration on shared files | [examples/crewai/](examples/crewai/) |
| **Google ADK** | Agent Development Kit integration | [examples/google_adk/](examples/google_adk/) |
| **E2B** | Cloud sandbox execution | [examples/e2b/](examples/e2b/) |
| **CLI** | 40+ shell demos covering every feature | [examples/cli/](examples/cli/) |

## Deployment options

| Mode | What | Who it's for |
|---|---|---|
| **Embedded** | `nexus.connect()` — in-process, zero infrastructure | Scripts, notebooks, single-agent apps |
| **Shared daemon** | `nexus up` — Docker stack with Postgres, Dragonfly, Zoekt | Teams, multi-agent systems, staging |
| **Federation** | Multi-zone Raft consensus across data centers | Production fleets, edge deployments |

```bash
# Embedded (no Docker)
pip install nexus-ai-fs

# Shared daemon
nexus init --preset shared && nexus up

# With GPU acceleration
nexus init --preset shared --accelerator cuda

# Pin to a specific version
nexus init --preset shared --image-tag 0.9.3
```

### Docker image

Published to GHCR (multi-arch: amd64 + arm64):

```
ghcr.io/nexi-lab/nexus:stable          # latest release
ghcr.io/nexi-lab/nexus:edge            # latest develop
ghcr.io/nexi-lab/nexus:<version>       # pinned (e.g. 0.9.3)
ghcr.io/nexi-lab/nexus:stable-cuda     # GPU variant
```

## Storage architecture

Four pillars, separated by access pattern — not by domain:

| Pillar | Interface | Capability | Required? |
|---|---|---|---|
| **Metastore** | `MetastoreABC` | Ordered KV, CAS, prefix scan, optional Raft | Yes — sole kernel init param |
| **ObjectStore** | `ObjectStoreABC` | Streaming blob I/O, petabyte scale | Mounted dynamically |
| **RecordStore** | `RecordStoreABC` | Relational ACID, JOINs, vector search | Services only — optional |
| **CacheStore** | `CacheStoreABC` | Ephemeral KV, pub/sub, TTL | Optional (defaults to null) |

The kernel starts with just a Metastore. Everything else is layered on without changing a line of kernel code.

## Contributing

```bash
git clone https://github.com/nexi-lab/nexus.git && cd nexus
uv python install 3.14
uv sync --extra dev --extra test
uv run pre-commit install
uv run pytest tests/
```

For semantic search work: `uv sync --extra semantic-search`
For Rust extensions: `maturin develop --release -m rust/nexus_pyo3/Cargo.toml`

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full guide.

## Troubleshooting

<details>
<summary><code>ModuleNotFoundError: No module named 'nexus'</code></summary>

Install from PyPI: `pip install nexus-ai-fs`. The package name on PyPI is `nexus-ai-fs`, not `nexus`.

</details>

<details>
<summary><code>maturin develop</code> fails at the repo root</summary>

Point maturin at a crate manifest: `maturin develop --release -m rust/nexus_pyo3/Cargo.toml`

</details>

<details>
<summary><code>faiss-cpu</code> resolution fails</summary>

Only install semantic search extras on platforms with compatible `txtai`/`faiss-cpu` wheels: `pip install "nexus-ai-fs[semantic-search]"`

</details>

## License

Apache License 2.0 — see [LICENSE](LICENSE) for details.

Built by [Nexi Labs](https://github.com/nexi-lab).
