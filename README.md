<div align="center">
  <img src="assets/logo.png" alt="Nexus Logo" width="200"/>

  # Nexus

  [![Test](https://github.com/nexi-lab/nexus/actions/workflows/test.yml/badge.svg)](https://github.com/nexi-lab/nexus/actions/workflows/test.yml)
  [![Lint](https://github.com/nexi-lab/nexus/actions/workflows/lint.yml/badge.svg)](https://github.com/nexi-lab/nexus/actions/workflows/lint.yml)

  [![PyPI version](https://badge.fury.io/py/nexus-ai-fs.svg)](https://badge.fury.io/py/nexus-ai-fs)
  [![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
  [![Python 3.13+](https://img.shields.io/badge/python-3.13+-blue.svg)](https://www.python.org/downloads/)

  [Architecture](docs/architecture/KERNEL-ARCHITECTURE.md) • [PyPI](https://pypi.org/project/nexus-ai-fs/) • [Examples](examples/)
</div>

An operating system for AI agents.

⚠️ **Beta**: Nexus is under active development. APIs may change.

## What is Nexus?

Nexus is an operating system for AI agents. Like Linux provides processes with a unified interface to hardware (files, sockets, devices), Nexus provides agents with a unified interface to data (files, databases, APIs, SaaS tools) — through syscalls, a virtual filesystem, permissions, and loadable services.

**Why an OS, not a framework?**
- **Kernel** with VFS, syscall dispatch, and inode-like metadata — not an application-layer wrapper
- **Drivers** swapped at config-time via dependency injection (redb, S3, PostgreSQL) — like compiled-in kernel drivers
- **Services** loaded/unloaded at runtime following the Linux Loadable Kernel Module pattern — not plugins
- **Deployment profiles** that select which services to include from the same codebase — like Linux distros (Ubuntu, Alpine, BusyBox) built from the same kernel
- **Zones** as the fundamental isolation and consensus unit (1 zone = 1 Raft group) — like cgroups/namespaces for data

## Quick Start

### Python SDK

```bash
pip install nexus-ai-fs
```

```python
import nexus

nx = nexus.connect(config={
    "mode": "remote",
    "url": "http://localhost:2026",
    "api_key": "nxk_..."
})

# File operations
nx.sys_write("/workspace/hello.txt", b"Hello, Nexus!")
print(nx.sys_read("/workspace/hello.txt").decode())

# Search across all backends
results = nx.grep("TODO", "/workspace")

# Agent memory
nx.memory.store("User prefers detailed explanations", memory_type="preference")
context = nx.memory.query("What does the user prefer?")

nx.close()
```

### CLI

```bash
pip install nexus-ai-fs

export NEXUS_URL="http://localhost:2026"
export NEXUS_API_KEY="nxk_..."

nexus write /workspace/hello.txt "Hello, Nexus!"
nexus cat /workspace/hello.txt
nexus grep "TODO" /workspace
nexus memory store "Important fact" --type fact
```

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

### Deployment Profiles (Distros)

Same kernel, different service sets — like Linux distros:

| Profile | Linux Analogue | Target | Services |
|---------|---------------|--------|----------|
| **minimal** | initramfs | Bare minimum | 1 |
| **embedded** | BusyBox | MCU, WASM (<1 MB) | 2 |
| **lite** | Alpine | Pi, Jetson, mobile | 8 |
| **full** | Ubuntu Desktop | Desktop, laptop | 21 |
| **cloud** | Ubuntu Server | k8s, serverless | 22 (all) |
| **remote** | NFS client | Client-side proxy | 0 |

See [Kernel Architecture](docs/architecture/KERNEL-ARCHITECTURE.md) for the full design.

## Examples

| Framework | Description | Location |
|-----------|-------------|----------|
| CrewAI | Multi-agent collaboration | [examples/crewai/](examples/crewai/) |
| LangGraph | Permission-based workflows | [examples/langgraph_integration/](examples/langgraph_integration/) |
| Claude SDK | ReAct agent pattern | [examples/claude_agent_sdk/](examples/claude_agent_sdk/) |
| OpenAI Agents | Multi-tenant with memory | [examples/openai_agents/](examples/openai_agents/) |
| Google ADK | Agent Development Kit | [examples/google_adk/](examples/google_adk/) |
| CLI | 40+ shell demos | [examples/cli/](examples/cli/) |

## Contributing

```bash
git clone https://github.com/nexi-lab/nexus.git
cd nexus
pip install -e ".[dev]"
pre-commit install
pytest tests/
```

## License

© 2025 Nexi Labs, Inc. Licensed under Apache License 2.0 — see [LICENSE](LICENSE) for details.
