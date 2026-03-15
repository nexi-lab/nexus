<div align="center">
  <img src="assets/logo.png" alt="Nexus Logo" width="200"/>

  # Nexus

  [![Test](https://github.com/nexi-lab/nexus/actions/workflows/test.yml/badge.svg)](https://github.com/nexi-lab/nexus/actions/workflows/test.yml)
  [![Lint](https://github.com/nexi-lab/nexus/actions/workflows/lint.yml/badge.svg)](https://github.com/nexi-lab/nexus/actions/workflows/lint.yml)

  [![PyPI version](https://badge.fury.io/py/nexus-ai-fs.svg)](https://badge.fury.io/py/nexus-ai-fs)
  [![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://github.com/nexi-lab/nexus/blob/main/LICENSE)
  [![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)

  [Docs](https://nexi-lab.github.io/nexus/) • [Quickstart](https://nexi-lab.github.io/nexus/getting-started/quickstart/) • [PyPI](https://pypi.org/project/nexus-ai-fs/) • [Examples](https://github.com/nexi-lab/nexus/tree/main/examples)
</div>

Nexus = filesystem/context plane.

⚠️ **Beta**: Nexus is under active development. APIs and deployment defaults may change.

## What Nexus Does

Nexus gives agents one place to read, write, search, and carry context across files, services, and runs. The core abstraction is a VFS-style interface that can start local in-process and grow into a daemon-backed deployment when you need remote access, permissions, or multi-tenant control.

## Quick Start

### Install

```bash
pip install nexus-ai-fs
```

### One-command demo (Docker)

```bash
nexus init --preset demo
nexus up
nexus demo init          # seeds sample workspace + prints API key
```

This pulls the prebuilt Nexus image and starts it alongside PostgreSQL (pgvector), Dragonfly (cache), and Zoekt (code search). By default Nexus listens on `localhost:2026`; if that port is busy, `nexus up` auto-resolves to a free port and prints the actual URL. No Rust toolchain, no local build required.

### Shared deployment

```bash
nexus init --preset shared
nexus up
```

Same services as demo (PostgreSQL, Dragonfly, Zoekt) with `static` auth and production-oriented defaults.
Both presets default to the `stable` release channel. Use `nexus status` to see the active image.

### Local SDK (no Docker)

For in-process use without a daemon:

```bash
pip install nexus-ai-fs
```

```python
from nexus.sdk import connect

nx = connect(config={"profile": "minimal", "data_dir": "./nexus-data"})
nx.sys_write("/hello.txt", b"Hello, Nexus!")
print(nx.sys_read("/hello.txt").decode())  # Hello, Nexus!
nx.close()
```

### CLI usage

```bash
nexus write /workspace/hello.txt "hello from cli"
nexus cat /workspace/hello.txt
nexus ls /workspace
```

### Docker image

The prebuilt multi-arch image (amd64 + arm64) is published to GHCR:

```
ghcr.io/nexi-lab/nexus:stable        # Latest release (updated on every tag)
ghcr.io/nexi-lab/nexus:edge          # Latest develop (updated on every push)
ghcr.io/nexi-lab/nexus:<version>     # Pinned to a specific release (e.g. 0.9.2)
ghcr.io/nexi-lab/nexus:<tag>-cuda    # GPU-accelerated variant (stable-cuda, edge-cuda, etc.)
```

`nexus init` writes an `image_ref` into `nexus.yaml` based on the selected channel.
The default channel is `stable`. Override during init:

```bash
nexus init --preset shared                           # stable channel (default)
nexus init --preset shared --channel edge            # pre-release channel
nexus init --preset shared --image-tag 0.9.2         # pin to exact version
nexus init --preset shared --accelerator cuda        # GPU variant
nexus init --preset shared --image-digest sha256:... # immutable digest
```

`nexus up` auto-pulls the latest image for channel-following configs (`stable`/`edge`).
To pull the latest release explicitly, run `nexus upgrade`.
To build from local source (repo checkouts only), pass `--build` to `nexus up` or `nexus restart`.

## Optional Capabilities

- Semantic search: `pip install "nexus-ai-fs[semantic-search]"`
- Rust acceleration: `pip install nexus-fast`
- Full dev/test environment: `uv sync --extra dev --extra test`
- Rust extensions from source: `uv pip install maturin && maturin develop --release -m rust/nexus_pyo3/Cargo.toml`
- Raft federation extensions: `maturin develop --release -m rust/nexus_raft/Cargo.toml --features full`

## Troubleshooting

- `ModuleNotFoundError: No module named 'nexus'`: install `nexus-ai-fs` from PyPI or use `uv pip install -e .` in a source checkout.
- `maturin develop --release` fails at the repo root: point `maturin` at a crate manifest under `rust/`, not the workspace root `Cargo.toml`.
- `Rust BLAKE3 extension not available`: optional performance path. The default uses the Python `blake3` package.
- `faiss-cpu` resolution fails: opt into `semantic-search` only on platforms with compatible `txtai`/`faiss-cpu` wheels.

For the full walkthrough, see the [quickstart page](https://nexi-lab.github.io/nexus/getting-started/quickstart/).

## Three Landing Paths

- **Local SDK**: In-process filesystem/context plane — no daemon, no Docker. Docs: [Local SDK path](https://nexi-lab.github.io/nexus/paths/embedded-sdk/)
- **Shared daemon**: Long-lived `nexusd` service with remote clients, permissions, and operational controls. Docs: [Shared daemon path](https://nexi-lab.github.io/nexus/paths/daemon-and-remote/)
- **Architecture**: Kernel, storage, and proposal docs for contributors. Docs: [Architecture path](https://nexi-lab.github.io/nexus/paths/architecture/)

## Trust Boundaries

- The demo preset runs a single-node stack (Nexus + PostgreSQL + Dragonfly + Zoekt) with default credentials — suitable for evaluation, not production.
- Remote SDK access uses the `remote` profile and depends on a running `nexusd` plus a configured gRPC port.
- Permissions, memory, and federation are deployment capabilities configured via the `shared` or custom presets.

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  SERVICES (user space)                                       │
│  Loadable at runtime. ReBAC, Auth, Agents, Search, Skills…   │
└──────────────────────────────────────────────────────────────┘
                          ↓ protocol interface
┌──────────────────────────────────────────────────────────────┐
│  KERNEL                                                      │
│  VFS, MetastoreABC, ObjectStoreABC, syscall dispatch,        │
│  pipes, lock manager, three-phase dispatch (LSM hooks)       │
└──────────────────────────────────────────────────────────────┘
                          ↓ dependency injection
┌──────────────────────────────────────────────────────────────┐
│  DRIVERS                                                     │
│  redb, S3, PostgreSQL, Dragonfly, LocalDisk, gRPC…           │
└──────────────────────────────────────────────────────────────┘
```

| Tier | Linux Analogue | Nexus | Swap time |
|------|---------------|-------|-----------|
| **Kernel** | vmlinuz (scheduler, mm, VFS) | VFS, MetastoreABC, syscall dispatch | Never |
| **Drivers** | Compiled-in drivers (`=y`) | redb, S3, PostgreSQL, SearchBrick | Config-time (DI) |
| **Services** | Loadable kernel modules (`insmod`/`rmmod`) | 23 protocols — ReBAC, Mount, Auth, Agents, … | Runtime |

**Invariant:** Services depend on kernel interfaces, never the reverse. The kernel operates with zero services loaded.

### Four Storage Pillars

Storage is abstracted by **capability** (access pattern + consistency guarantee), not by domain:

| Pillar | ABC | Capability | Kernel role |
|--------|-----|------------|-------------|
| **Metastore** | `MetastoreABC` | Ordered KV, CAS, prefix scan, optional Raft SC | Required — sole kernel init param |
| **ObjectStore** | `ObjectStoreABC` | Streaming blob I/O, petabyte scale | Interface only — mounted dynamically |
| **RecordStore** | `RecordStoreABC` | Relational ACID, JOINs, vector search | Services only — optional |
| **CacheStore** | `CacheStoreABC` | Ephemeral KV, Pub/Sub, TTL | Optional — defaults to `NullCacheStore` |

### Presets

| Preset | Use Case | Stack |
|--------|----------|-------|
| **local** | Embedded SDK, no Docker | In-process only |
| **shared** | One shared node | Nexus + PostgreSQL + Dragonfly + Zoekt |
| **demo** | Shared + seed data | Same as shared + demo corpus |

Federation and GPU are explicit extensions layered on top of the shared preset, not separate presets.

See [Kernel Architecture](https://nexi-lab.github.io/nexus/architecture/kernel-architecture/) for internal deployment profiles and the full design.

## Examples

| Framework | Description | Location |
|-----------|-------------|----------|
| CrewAI | Multi-agent collaboration | [examples/crewai/](https://github.com/nexi-lab/nexus/tree/main/examples/crewai) |
| LangGraph | Permission-based workflows | [examples/langgraph_integration/](https://github.com/nexi-lab/nexus/tree/main/examples/langgraph_integration) |
| Claude SDK | ReAct agent pattern | [examples/claude_agent_sdk/](https://github.com/nexi-lab/nexus/tree/main/examples/claude_agent_sdk) |
| OpenAI Agents | Multi-tenant with memory | [examples/openai_agents/](https://github.com/nexi-lab/nexus/tree/main/examples/openai_agents) |
| Google ADK | Agent Development Kit | [examples/google_adk/](https://github.com/nexi-lab/nexus/tree/main/examples/google_adk) |
| CLI | 40+ shell demos | [examples/cli/](https://github.com/nexi-lab/nexus/tree/main/examples/cli) |

## Contributing

```bash
git clone https://github.com/nexi-lab/nexus.git
cd nexus
uv python install 3.14
uv sync --extra dev --extra test
uv run pre-commit install
uv run pytest tests/
```

If you are working on the txtai search stack, add `--extra semantic-search`.

## License

© 2026 Nexi Labs, Inc. Licensed under Apache License 2.0 — see [LICENSE](https://github.com/nexi-lab/nexus/blob/main/LICENSE) for details.
