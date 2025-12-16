# Nexus

The AI-native filesystem for cognitive agents.


⚠️ **Beta**: Nexus is under active development. APIs may change.

## 🎯 What is Nexus?

Nexus is a virtual filesystem server for AI agents. It unifies files, databases, APIs, and SaaS tools into a single path-based API with built-in permissions, memory, semantic search, and skills lifecycle management.

**Humans manage context. Agents operate within it.**

The Nexus Control Panel lets humans curate the files, memories, permissions, and integrations that agents can access—providing oversight and control while agents focus on execution.

This repo contains the open-source server, SDK, CLI, and examples.

### Highlights

**Hero Features**
- **Universal Connectors** — One API for local files, S3/GCS, Gmail, Google Drive, X/Twitter, and custom MCP servers
- **Sandboxed Execution** — Run code safely with Docker/E2B integration
- **Grep Everything** — `nexus grep` and semantic search across all backends with connector cache
- **Skills System** — Package agent capabilities with versioning, governance, and exporters (LangGraph, MCP, CLI)

**Core Platform**
- **Control Panel** — User management, permissions, integrations, audit & versioning
- **Runtime Modules** — Workspaces, Memory, Workflows, Files, Skills, Sandbox
- **ReBAC Permissions** — Zanzibar-style access control with multi-tenant isolation

**Advanced**
- Event-driven workflows, semantic search + LLM document reading, content deduplication + versioning, 14 MCP tools, plugin system, batch operations

## 🧑‍💻 Getting Started

### Local Server

```bash
git clone https://github.com/nexi-lab/nexus.git
cd nexus
pip install -e ".[dev]"

export NEXUS_DATABASE_URL="postgresql://user:pass@localhost/nexus"
./scripts/init-nexus-with-auth.sh

# Server runs at http://localhost:8080
# Admin API key saved to .nexus-admin-env
```

### Docker Server

```bash
cp .env.example .env
./docker-start.sh --build

# Get admin API key
docker logs nexus-server 2>&1 | grep "API Key:"

# Server runs at http://localhost:8080
```

### Python SDK

```bash
pip install nexus-ai-fs
```

```python
import nexus

nx = nexus.connect(config={
    "url": "http://localhost:8080",
    "api_key": "nxk_..."
})

# File operations
nx.write("/workspace/hello.txt", b"Hello, Nexus!")
print(nx.read("/workspace/hello.txt").decode())

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

export NEXUS_URL="http://localhost:8080"
export NEXUS_API_KEY="nxk_..."

nexus write /workspace/hello.txt "Hello, Nexus!"
nexus cat /workspace/hello.txt
nexus grep "TODO" /workspace
nexus memory store "Important fact" --type fact
```

## 💡 Why Nexus?

| Problem | Nexus Solution |
|---------|----------------|
| Fragmented APIs | One path-based API for all connectors |
| Permission chaos | Zanzibar-style ReBAC with audit trails |
| Ephemeral memory | Persistent memory with semantic retrieval |
| Agent silos | Shared workspaces and skills registry |
| No oversight | Control Panel for human-in-the-loop management |
| Ad-hoc tools | [Skills lifecycle management](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview) with versioning, governance, and multi-format export |

## 🔧 How Nexus Works

![Nexus Architecture](docs/images/nexus-architecture.png)

**Access Layer**: Agents connect via CLI, MCP Server, Python SDK, or REST API.

**Application Layer**:
- *Control Panel* — Humans manage users, permissions, integrations, and audit logs
- *Runtime* — Core operations (glob, grep, semantic search, file ops, run code) and modules (workspaces, memory, workflows, files, skills, sandbox)

**Infrastructure Layer**: OAuth, ReBAC permissions engine, FUSE mounting, RAG pipelines.

**Cache Layer**: Vector store for embeddings, ReBAC tuples for fast permission checks, metadata cache.

**Connectors**: X/Twitter, MCP servers, Gmail, Local FS, GCS, Google Drive, AWS S3, and custom connectors.

## 📚 Examples

| Framework | Description | Location |
|-----------|-------------|----------|
| CrewAI | Multi-agent collaboration | [examples/crewai/](examples/crewai/) |
| LangGraph | Permission-based workflows | [examples/langgraph_integration/](examples/langgraph_integration/) |
| Claude SDK | ReAct agent pattern | [examples/claude_agent_sdk/](examples/claude_agent_sdk/) |
| OpenAI Agents | Multi-tenant with memory | [examples/openai_agents/](examples/openai_agents/) |
| CLI | 40+ shell demos | [examples/cli/](examples/cli/) |

## 📖 Documentation

- [Installation](docs/getting-started/installation.md)
- [Quick Start](docs/getting-started/quickstart.md)
- [Control Panel Guide](docs/portal/overview.md)
- [API Reference](docs/api/)
- [Skills System](docs/concepts/skills-system.md)
- [Multi-Tenant Architecture](docs/MULTI_TENANT.md)
- [Permissions & ReBAC](docs/PERMISSIONS.md)

## 🤝 Contributing

```bash
git clone https://github.com/nexi-lab/nexus.git
cd nexus
pip install -e ".[dev]"
pre-commit install
pytest tests/
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## 📝 License

Apache 2.0

---

If Nexus helps your project, please ⭐ the repo!
