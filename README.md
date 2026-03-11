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

This is the local SDK path verified against this repo with `PYTHONPATH=src`
and Python 3.13. If you installed from PyPI, drop `PYTHONPATH=src`.

```bash
PYTHONPATH=src python3.13 - <<'PY'
from nexus.sdk import connect

nx = connect(
    config={
        "profile": "minimal",
        "data_dir": "./nexus-data",
    }
)

nx.sys_write("/hello.txt", b"Hello, Nexus!")
print(nx.sys_read("/hello.txt").decode())

nx.close()
PY
```

Expected output:

```text
Hello, Nexus!
```

Local CLI path from a source checkout:

```bash
PYTHONPATH=src python3.13 -m nexus.cli.main init .nexus-cli-demo
export NEXUS_DATA_DIR="$PWD/.nexus-cli-demo/nexus-data"

PYTHONPATH=src python3.13 -m nexus.cli.main write /workspace/hello.txt "hello from cli"
PYTHONPATH=src python3.13 -m nexus.cli.main cat /workspace/hello.txt
PYTHONPATH=src python3.13 -m nexus.cli.main ls /workspace
```

If you installed from PyPI, use `nexus` instead of `python3.13 -m nexus.cli.main`.

For the full walkthrough, see the [quickstart page](https://nexi-lab.github.io/nexus/getting-started/quickstart/).

## Three Landing Paths

- **Local SDK**: Start local, keep the filesystem/context plane inside your process, and integrate from Python. Docs: [Local SDK path](https://nexi-lab.github.io/nexus/paths/embedded-sdk/)
- **Shared daemon**: Run `nexusd` when you need a long-lived service, remote clients, or operational controls. Docs: [Shared daemon path](https://nexi-lab.github.io/nexus/paths/daemon-and-remote/)
- **Architecture**: Read the kernel, storage, and proposal docs before changing the system model. Docs: [Architecture path](https://nexi-lab.github.io/nexus/paths/architecture/)

## Trust Boundaries

- The quickstart above is a local path. It is intentionally smaller than a production deployment.
- Remote SDK access uses the `remote` profile and depends on a running `nexusd` plus a configured gRPC port.
- Permissions, memory, and federation are deployment capabilities, not implied by the basic local write/read example.

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

See [Kernel Architecture](https://nexi-lab.github.io/nexus/architecture/kernel-architecture/) for the full design.

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
pip install -e ".[dev]"
pre-commit install
pytest tests/
```

## License

© 2025 Nexi Labs, Inc. Licensed under Apache License 2.0 — see [LICENSE](https://github.com/nexi-lab/nexus/blob/main/LICENSE) for details.
