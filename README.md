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

### Agent Engine (Embedded)

Run an AI agent directly against a local NexusFS — no server required:

```python
import asyncio
from pathlib import Path
from pydantic import SecretStr

from nexus.backends.storage.cas_local import CASLocalBackend
from nexus.core.config import PermissionConfig
from nexus.factory import create_nexus_fs
from nexus.storage.raft_metadata_store import RaftMetadataStore
from nexus.storage.record_store import SQLAlchemyRecordStore

from nexus.bricks.llm.config import LLMConfig
from nexus.bricks.llm.provider import LiteLLMProvider
from nexus.system_services.agent_runtime.process_manager import ProcessManager
from nexus.system_services.agent_runtime.types import AgentProcessConfig, TextDelta
from nexus.contracts.llm_types import Message, MessageRole


async def main():
    # 1. Bootstrap NexusFS (local storage, no server)
    data = Path("/tmp/nexus-agent")
    data.mkdir(exist_ok=True)
    nx = create_nexus_fs(
        backend=CASLocalBackend(root_path=str(data / "files")),
        metadata_store=RaftMetadataStore.embedded(str(data / "raft")),
        record_store=SQLAlchemyRecordStore(),
        permissions=PermissionConfig(enforce=False),
    )

    # 2. Create LLM provider
    llm = LiteLLMProvider(LLMConfig(
        model="claude-haiku-4-5-20251001",
        api_key=SecretStr("sk-ant-..."),  # your API key
    ))

    # 3. Configure and spawn agent
    pm = ProcessManager(vfs=nx, llm_provider=llm)
    proc = await pm.spawn("user1", "default", config=AgentProcessConfig(
        name="my-coder",
        model="claude-haiku-4-5-20251001",
        system_prompt="You are a helpful coding assistant.",
        tools=("read_file", "write_file", "grep", "glob"),
        max_turns=50,
        max_context_tokens=128_000,
    ))

    # 4. Chat — resume() streams events (TextDelta, ToolCallStart, Completed, ...)
    async for event in pm.resume(
        proc.pid,
        Message(role=MessageRole.USER, content="List the files in /default"),
    ):
        if isinstance(event, TextDelta):
            print(event.text, end="", flush=True)
    print()

    # Cleanup
    await pm.terminate(proc.pid)
    await llm.cleanup()
    nx.close()

asyncio.run(main())
```

### Registry-Based Spawn (Production)

In production, agents are pre-registered with an `AgentSpec` — like installed binaries in `/usr/bin/`. Spawn by name instead of passing inline config:

```python
from nexus.contracts.agent_types import AgentSpec, AgentResources, QoSClass

# Register agent spec once (e.g. at app startup)
await registry.set_spec("my-coder", AgentSpec(
    agent_type="coder",
    capabilities=frozenset({"code", "search"}),
    resource_requests=AgentResources(),
    resource_limits=AgentResources(),
    qos_class=QoSClass.STANDARD,
    model="claude-sonnet-4-6",
    system_prompt="You are a senior software engineer.",
    tools=("read_file", "write_file", "edit_file", "bash", "grep", "glob"),
    max_turns=100,
    max_context_tokens=200_000,
    sandbox_timeout=300,
))

# Spawn by agent_id — ProcessManager looks up the AgentSpec from registry
pm = ProcessManager(vfs=nx, llm_provider=llm, agent_registry=registry)
proc = await pm.spawn("user1", "default", agent_id="my-coder")
# proc.model == "claude-sonnet-4-6", proc.name == "my-coder", etc.
```

### Copilot/Worker Delegation

A copilot spawns restricted workers with budget caps, permission inheritance, and real-time streaming:

```python
from nexus.system_services.agent_runtime.copilot_orchestrator import CopilotOrchestrator
from nexus.contracts.agent_runtime_types import WorkerConfig, DeliveryPolicy

orchestrator = CopilotOrchestrator(
    process_manager=pm, task_manager=tm, tool_dispatcher=td,
)

# Delegate work — worker inherits a SUBSET of copilot's permissions
result = await orchestrator.delegate(
    copilot_pid=copilot.pid,
    message="Research competitor pricing",
    worker_config=WorkerConfig(
        agent_id="researcher",           # registered agent to spawn
        zone_id="zone-1",                # zone isolation
        tool_allowlist=("web_search", "read_file"),  # inherit-and-restrict
        budget_tokens=50_000,            # per-worker budget cap
        delivery_policy=DeliveryPolicy.IMMEDIATE,
    ),
)

# Stream worker results in real-time (IMMEDIATE delivery policy)
async for event in orchestrator.stream(result.task_id, zone_id="zone-1"):
    print(event)  # real-time results, no polling

# Or await completion directly (ON_DEMAND / DEFERRED policy)
task = await orchestrator.collect(result.task_id, zone_id="zone-1")
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
