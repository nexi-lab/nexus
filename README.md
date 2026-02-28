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

The AI-native filesystem for cognitive agents.

⚠️ **Beta**: Nexus is under active development. APIs may change.

## What is Nexus?

Nexus is a virtual filesystem server for AI agents. It unifies files, databases, APIs, and SaaS tools into a single path-based API with built-in permissions, memory, semantic search, and skills lifecycle management. Humans curate context via the Control Panel; agents operate within it.

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

Nexus follows an OS-inspired layered architecture with three tiers:

| Tier | What it contains | Swap time |
|------|-----------------|-----------|
| **Kernel** | VFS, MetastoreABC, ObjectStoreABC, syscall dispatch | Never (static core) |
| **Drivers** | redb, S3, PostgreSQL, Dragonfly, SearchBrick | Config-time (DI at startup) |
| **Services** | 23 protocols — ReBAC, Mount, Auth, Agents, Search, Skills, … | Runtime (load/unload) |

### Four Storage Pillars

| Pillar | ABC | Capability |
|--------|-----|------------|
| **Metastore** | `MetastoreABC` | Ordered KV, CAS, prefix scan (kernel-required) |
| **ObjectStore** | `ObjectStoreABC` | Streaming blob I/O, petabyte scale |
| **RecordStore** | `RecordStoreABC` | Relational ACID, JOINs, vector search |
| **CacheStore** | `CacheStoreABC` | Ephemeral KV, Pub/Sub, TTL |

### Deployment Profiles

| Profile | Target | Bricks |
|---------|--------|--------|
| **minimal** | Bare minimum runnable | 1 (storage only) |
| **embedded** | MCU, WASM (<1 MB) | 2 |
| **lite** | Pi, Jetson, mobile | 8 |
| **full** | Desktop, laptop | 21 |
| **cloud** | k8s, serverless | 22 (all) |
| **remote** | Client-side proxy | 0 (NFS-client model) |

See [Kernel Architecture](docs/architecture/KERNEL-ARCHITECTURE.md) for details.

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
