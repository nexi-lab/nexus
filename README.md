<div align="center">
  <img src="logo.png" alt="Nexus Logo" width="200"/>

  # Nexus

  [![Test](https://github.com/nexi-lab/nexus/actions/workflows/test.yml/badge.svg)](https://github.com/nexi-lab/nexus/actions/workflows/test.yml)
  [![Lint](https://github.com/nexi-lab/nexus/actions/workflows/lint.yml/badge.svg)](https://github.com/nexi-lab/nexus/actions/workflows/lint.yml)

  [![PyPI version](https://badge.fury.io/py/nexus-ai-fs.svg)](https://badge.fury.io/py/nexus-ai-fs)
  [![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
  [![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

  [Documentation](docs/api/README.md) • [Quickstart](docs/getting-started/quickstart.md) • [PyPI](https://pypi.org/project/nexus-ai-fs/) • [Examples](examples/)
</div>

**AI-native filesystem for building intelligent agents**

Nexus is an AI-native filesystem that unifies files, memory, and permissions into one programmable layer. Build agents that remember, collaborate securely, and scale effortlessly from prototype to production—without changing code.

**Made to build AI agents, fast.**

## TL;DR

Nexus is a programmable filesystem for AI agents. It combines storage, memory, and permissions into one layer so agents can securely remember, collaborate, and evolve over time.

**Jump to:** [Server Mode](#server-mode-recommended-for-production) • [Embedded Mode](#embedded-mode-development--testing-only) • [Permissions](#permission-system-example) • [Core Concepts](#core-concepts) • [Documentation](#documentation)

## Quick Start

### Installation

```bash
pip install nexus-ai-fs
```

### Server Mode (Recommended for Production)

**Why Server Mode?**
- ✅ Multi-user support with authentication
- ✅ Centralized permissions and access control
- ✅ Scalable architecture for production workloads
- ✅ Thin SDK client (just HTTP calls)

**Step 1: Start the server**

```bash
# Configure PostgreSQL backend
export NEXUS_DATABASE_URL="postgresql://user:pass@localhost/nexus"

# Initialize with authentication
./scripts/init-nexus-with-auth.sh

# Server runs at http://localhost:8080
```

**Step 2: Connect from Python**

```python
import nexus

# Auto-detect server from environment
# export NEXUS_URL=http://localhost:8080
# export NEXUS_API_KEY=your-api-key
nx = nexus.connect()

# Or provide explicitly
nx = nexus.connect(config={
    "url": "http://localhost:8080",
    "api_key": "your-api-key"
})

# Same API as embedded mode
nx.write("/workspace/hello.txt", b"Hello, Nexus!")
content = nx.read("/workspace/hello.txt")
print(content.decode())  # → Hello, Nexus!

# Agent memory
nx.memory.store("Python best practices learned from code review")
memories = nx.memory.query(user_id="alice", scope="project")

# Workflow automation - events trigger automatically!
from nexus.workflows import WorkflowAPI
workflows = WorkflowAPI()
workflows.load("invoice-processor.yaml", enabled=True)
nx.write("/uploads/invoice.pdf", pdf_data)  # Workflow fires!

# Semantic search
results = nx.semantic_search("/docs/**/*.md", query="authentication setup")

nx.close()
```

**Step 3: Manage users and permissions**

```bash
# Create users and grant permissions
python3 scripts/create-api-key.py alice "Alice's API key" --days 90
# Output: sk-alice_abc123_...

nexus rebac create user alice direct_owner file /workspace/project1 \
  --tenant-id default --remote-url $NEXUS_URL

# Alice can now access with her API key
export NEXUS_API_KEY='sk-alice_abc123_...'
nexus write /workspace/project1/data.txt "Alice's data" --remote-url $NEXUS_URL
```

### Embedded Mode (Development & Testing Only)

**⚠️ Warning:** Embedded mode is suitable for development and testing only. For production, use Server Mode.

**Hello World (10 Seconds)**

```python
import nexus

# Zero-config start - data stored in ./nexus-data by default
nx = nexus.connect(config={"mode": "embedded"})

# Write and read a file
nx.write("/workspace/hello.txt", b"Hello, Nexus!")
content = nx.read("/workspace/hello.txt")
print(content.decode())  # → Hello, Nexus!

nx.close()
```

**Full Features (Embedded)**

```python
import nexus

# Explicit embedded mode configuration
nx = nexus.connect(config={
    "mode": "embedded",
    "data_dir": "./nexus-data"
})

# File operations
nx.write("/workspace/doc.txt", b"Hello Nexus")
content = nx.read("/workspace/doc.txt")

# Agent memory
nx.memory.store("Python best practices learned from code review")
memories = nx.memory.query(user_id="alice", scope="project")

# Workflow automation
from nexus.workflows import WorkflowAPI
workflows = WorkflowAPI()
workflows.load("invoice-processor.yaml", enabled=True)
nx.write("/uploads/invoice.pdf", pdf_data)  # Workflow fires automatically!

# Semantic search
results = nx.semantic_search("/docs/**/*.md", query="authentication setup")

# LLM-powered document reading (async)
import asyncio
answer = asyncio.run(nx.llm_read(
    "/docs/**/*.md",
    "How does authentication work?",
    model="claude-sonnet-4"
))

nx.close()
```

### Permission System Example

```bash
# Grant different permission levels
nexus rebac create user bob direct_editor file /workspace/project1 \
  --tenant-id default --remote-url $NEXUS_URL
# Bob can read/write

nexus rebac create user charlie direct_viewer file /workspace/project1 \
  --tenant-id default --remote-url $NEXUS_URL
# Charlie can only read

# Check permissions
nexus rebac check user charlie write file /workspace/project1 --remote-url $NEXUS_URL
# Output: ✗ DENIED

nexus rebac check user charlie read file /workspace/project1 --remote-url $NEXUS_URL
# Output: ✓ GRANTED

# Explain permission paths
nexus rebac explain user bob write file /workspace/project1 --remote-url $NEXUS_URL
# Shows: direct_editor → editor → write
```

## Core Concepts

**Workspace** - A versioned directory for agent state. Create snapshots, rollback changes, and reproduce any historical state for debugging.

**Memory** - Persistent agent knowledge stored as files. Query memories semantically, consolidate learnings automatically, and share across agents within tenants.

**Semantic Search** - Vector-based search across files and memories using natural language queries. Powered by [pgvector](https://github.com/pgvector/pgvector) or [sqlite-vec](https://github.com/asg017/sqlite-vec).

**ReBAC** - [Relationship-Based Access Control](https://research.google/pubs/pub48190/) inspired by Google Zanzibar. Fine-grained permissions with inheritance, multi-tenancy, and delegation.

## Why Nexus?

Nexus combines files, memory, and access control into a single programmable layer. Build agents that persist knowledge, collaborate securely, and scale from local experiments to distributed systems.

**Key Capabilities:**
- **AI Memory with Learning Loops** - ACE system automatically consolidates agent experiences into reusable knowledge
- **Database as Files** - Access PostgreSQL, Redis, MongoDB through unified file interface with backend-aware permissions
- **LLM-Powered Reading** - Query documents with natural language, get answers with citations and cost tracking
- **Unified Fabric** - Files, databases, vectors, and permissions in one programmable layer
- **Agent-Native Design** - Built for LLM agents and automation frameworks with event-driven orchestration

**For AI Agent Developers:**
- **Self-Evolving Memory**: Agents store and retrieve context across sessions with automatic consolidation
- **Time-Travel Debugging**: Reproduce any agent state with workspace snapshots and version history
- **Semantic Search**: Find relevant files and memories using natural language queries

**For Enterprise Teams:**
- **Fine-Grained Permissions**: [ReBAC](https://research.google/pubs/pub48190/) with backend-aware object types (file, table, row-level access) and multi-tenancy
- **Multi-Backend Abstraction**: Unified file API for storage (S3, GCS, local) and data sources (PostgreSQL, Redis, MongoDB)
- **Content Deduplication**: Save 30-50% storage costs with content-addressable architecture

**For Platform Engineers:**
- **Embedded or Remote**: Start local (`pip install`), scale to distributed without code changes
- **Complete Audit Trail**: Track every operation with built-in versioning and operation logs
- **Production-Ready**: PostgreSQL backend, API key authentication, and comprehensive observability

## Features at a Glance

| Category | Highlights |
|----------|-----------|
| **Storage** | Multi-backend (S3, GCS, local), versioning, 30-50% deduplication savings |
| **Access Control** | ReBAC (Zanzibar-style), multi-tenancy, permission inheritance |
| **AI Intelligence** | LLM document Q&A, memory API, semantic search, workspace snapshots, time-travel debugging |
| **Developer UX** | Embedded/remote parity, full-featured CLI + SDK, 100% feature compatibility |
| **Extensibility** | Plugin system with lifecycle hooks, custom CLI commands, auto-discovery |
| **AI Tool Integration** | MCP server for Claude Desktop and other AI agents (14 tools) |

## Performance Highlights

- **4x Faster Uploads** - Batch write API for checkpoint and log files
- **30-50% Storage Savings** - Content-addressable deduplication
- **Instant Queries** - Semantic search with vector indexes (pgvector/sqlite-vec)
- **Zero Downtime** - Optimistic concurrency control for multi-agent access

## Key Features

### Storage & Operations
- **Multi-Backend Abstraction**: Storage backends (S3, GCS, local) and data backends (PostgreSQL, Redis, MongoDB) through unified file API
- **Backend-Aware Permissions**: Different object types per backend (file vs. database table vs. row-level access)
- **Content Deduplication**: 30-50% storage savings via content-addressable architecture
- **Versioning**: Complete history tracking with rollback and diff capabilities
- **Batch Operations**: 4x faster bulk uploads for checkpoints and large datasets

### Access Control
- **ReBAC**: Relationship-based permissions with Google Zanzibar-style authorization
- **Permission Inheritance**: Directory-based access control with automatic propagation
- **Multi-Tenancy**: Complete isolation between tenants with namespace support
- **API Key Authentication**: Database-backed keys with expiration and rotation

### Agent Intelligence
- **ACE Learning Loops**: Autonomous Cognitive Entity system with trajectories, reflection, and automatic consolidation
- **LLM Document Reading**: Ask questions about documents with AI-powered answers, citations, and cost tracking
- **Memory API**: Store, query, and consolidate agent memories with automatic knowledge extraction
- **Semantic Search**: Vector-based search across files and memories using natural language
- **Workflow Automation**: Event-driven workflows trigger automatically on file operations - no manual event firing needed
- **Workspace Snapshots**: Save and restore entire agent workspaces for debugging and reproducibility
- **Time-Travel**: Access any file at any historical point with content diffs

### Developer Experience
- **Embedded Mode**: `pip install` and start coding—no infrastructure required
- **Remote Mode**: Same API, distributed execution with automatic scaling
- **CLI**: Full-featured command-line interface with remote support
- **SDK Parity**: 100% feature parity between embedded and remote modes

### Extensibility
- **Plugin System**: Extend Nexus with custom functionality via Python entry points
- **Lifecycle Hooks**: React to file operations (before_write, after_read, etc.)
- **Custom CLI Commands**: Add plugin-specific commands to the `nexus` CLI
- **Auto-Discovery**: Install plugins with `pip`, no manual registration needed
- **Official Plugins**: Anthropic integration, Firecrawl web scraping, and more

### AI Tool Integration
- **Model Context Protocol (MCP)**: Expose Nexus to AI agents via [MCP server](examples/mcp/)
  - 14 tools for files, search, memory, and workflows
  - Claude Desktop integration with authenticated remote access
  - Run `./examples/mcp/quick_test.sh` for 2-minute demo
  - See [MCP docs](docs/integrations/mcp.md) for setup guide

## Use Cases

**AI Agents** - Memory API for context retention, semantic search for knowledge retrieval

**Multi-Tenant SaaS** - ReBAC for tenant isolation, workspace snapshots for backup/restore

**Document Processing** - Batch uploads with deduplication, semantic search across files

**ML Workflows** - Checkpoint versioning, time-travel debugging for reproducibility

**Extensible Pipelines** - Plugin system (Anthropic, Firecrawl) for custom integrations

📚 **See [examples/](examples/)** for complete AI agent, SaaS, ML, and plugin demos with runnable code.

## Architecture

```mermaid
%%{init: {'theme':'base', 'themeVariables': { 'primaryColor':'#e3f2fd','primaryTextColor':'#1a237e','primaryBorderColor':'#5C6BC0','lineColor':'#AB47BC','secondaryColor':'#fce4ec','tertiaryColor':'#fff3e0','fontSize':'14px'}}}%%
graph TB
    subgraph agents[" 🤖 AI Agents "]
        agent1["Agent A<br/>(GPT-4)"]
        agent2["Agent B<br/>(Claude)"]
        agent3["Agent C<br/>(Custom)"]
    end

    subgraph vfs[" 📁 Nexus Virtual File System "]
        api["Unified VFS API<br/>read() write() list() search()"]
        memory["💾 Memory API<br/>Persistent learning & context"]
        rebac["🔒 ReBAC Permissions<br/>Backend-aware object types"]
        version["📦 Versioning<br/>Snapshots & time-travel"]
        router["Smart Router<br/>Path → Backend + Object Type"]
    end

    subgraph backends[" 💾 Storage & Data Backends "]
        subgraph storage[" File Storage "]
            local["Local Filesystem<br/>object: file"]
            gcs["Cloud Storage<br/>object: file"]
        end
        subgraph data[" Data Sources "]
            postgres["PostgreSQL<br/>object: postgres:table/row"]
            redis["Redis<br/>object: redis:instance/key"]
            mongo["MongoDB<br/>object: mongo:collection/doc"]
        end
    end

    agent1 -.->|"write('/workspace/data.json')"| api
    agent2 -.->|"read('/db/public/users')"| api
    agent3 -.->|"memory.store('learned_fact')"| memory

    api --> rebac
    memory --> rebac
    rebac <-->|"Check with object type"| router
    rebac -->|"✓ Allowed"| version
    version --> router

    router -->|"File operations"| local
    router -->|"File operations"| gcs
    router -->|"Queries as files"| postgres
    router -->|"KV as files"| redis
    router -->|"Documents as files"| mongo

    style agents fill:#e3f2fd,stroke:#5C6BC0,stroke-width:2px,color:#1a237e
    style vfs fill:#f3e5f5,stroke:#AB47BC,stroke-width:2px,color:#4a148c
    style backends fill:#fff3e0,stroke:#FF7043,stroke-width:2px,color:#e65100
    style storage fill:#e8f5e9,stroke:#4CAF50,stroke-width:1px
    style data fill:#e1f5fe,stroke:#0288D1,stroke-width:1px
    style api fill:#5C6BC0,stroke:#3949AB,stroke-width:2px,color:#fff
    style memory fill:#AB47BC,stroke:#7B1FA2,stroke-width:2px,color:#fff
    style rebac fill:#EC407A,stroke:#C2185B,stroke-width:2px,color:#fff
    style version fill:#66BB6A,stroke:#388E3C,stroke-width:2px,color:#fff
    style router fill:#42A5F5,stroke:#1976D2,stroke-width:2px,color:#fff
```

**Backend Abstraction:**

Nexus presents everything as files to users, while backends provide appropriate object types for permission control:

- **File Storage** (Local, GCS, S3): Standard file objects
- **Databases** (PostgreSQL, Redis, MongoDB): Backend-specific objects (tables, keys, documents)
- **Unified Interface**: All accessed through the same VFS API (read/write/list)
- **Fine-Grained Permissions**: ReBAC uses backend-appropriate object types (e.g., grant access to a PostgreSQL schema vs. individual rows)

### Deployment Modes

Nexus supports two deployment modes with the same codebase:

| Mode | Use Case | Setup |
|------|----------|-------|
| **Embedded** | Development, CLI tools, prototyping | `pip install nexus-ai-fs` |
| **Server** | Teams, production, multi-tenant | See [Server Setup Guide](docs/api/rpc-api.md) |

**Technology:** Python 3.11+, SQLAlchemy, PostgreSQL/SQLite, [ReBAC](https://research.google/pubs/pub48190/) (Zanzibar-style), [pgvector](https://github.com/pgvector/pgvector)/[sqlite-vec](https://github.com/asg017/sqlite-vec)

See [Configuration Guide](docs/api/configuration.md) for storage backends (S3, GCS, local) and advanced setup.

## Documentation

### Getting Started
- **[Quickstart Guide](docs/getting-started/quickstart.md)** - Step-by-step setup with authentication
- **[API Documentation](docs/api/README.md)** - Complete API reference
- **[CLI Reference](docs/api/cli-reference.md)** - Command-line interface guide

### Core Features
- **[File Operations](docs/api/file-operations.md)** - Read, write, delete, batch operations
- **[Permissions](docs/api/permissions.md)** - ReBAC, multi-tenancy, access control
- **[Memory Management](docs/api/memory-management.md)** - Agent memory API
- **[Semantic Search](docs/api/semantic-search.md)** - Vector search and embeddings
- **[Versioning](docs/api/versioning.md)** - History tracking and time-travel

### Advanced Topics
- **[Configuration](docs/api/configuration.md)** - Backends, environment variables, YAML config
- **[Remote Server Setup](docs/api/rpc-api.md)** - Deploying Nexus server
- **[Multi-Backend Usage](docs/api/advanced-usage.md)** - Mounting multiple storage backends
- **[Plugin Development](docs/development/PLUGIN_DEVELOPMENT.md)** - Extend Nexus with custom plugins
- **[Development Guide](docs/development/PERMISSIONS_IMPLEMENTATION.md)** - Contributing to Nexus

### Examples
- **[Python Demos](examples/py_demo/)** - SDK usage examples
- **[CLI Demos](examples/script_demo/)** - Shell script examples
- **[Multi-Tenant Demo](examples/multi_tenant/)** - SaaS platform patterns
- **[Authentication Examples](examples/auth_demo/)** - API key setup and usage

## Security & Configuration

**Security Features:**
- **ReBAC**: Relationship-based access control (Google Zanzibar-style)
- **Multi-Tenancy**: Complete tenant isolation
- **API Keys**: Database-backed authentication with expiration
- **Audit Trail**: Full operation logging with versioning

**Quick Configuration:**
```bash
# Environment variables
export NEXUS_DATABASE_URL="postgresql://user:pass@localhost/nexus"
export NEXUS_URL="http://localhost:8080"
export NEXUS_API_KEY="sk-alice_abc123_..."
```

See [Configuration Guide](docs/api/configuration.md) for YAML config, multi-backend setup, and advanced options.
See [Permissions Guide](docs/api/permissions.md) for detailed security documentation.

## Contributing

We welcome contributions! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

### Development Setup
```bash
git clone https://github.com/nexi-lab/nexus.git
cd nexus
pip install -e ".[dev]"
pytest tests/
```

## Support

- **Issues**: [GitHub Issues](https://github.com/nexi-lab/nexus/issues)
- **Discussions**: [GitHub Discussions](https://github.com/nexi-lab/nexus/discussions)
- **Roadmap**: [GitHub Projects](https://github.com/nexi-lab/nexus/projects)

⭐ **If you find Nexus useful, please star the repo to support development!**

## Philosophy

Nexus treats data as a first-class citizen in AI systems. Instead of building around files, agents build around knowledge—unified, permissioned, and queryable.

We believe AI infrastructure should be:
- **Intelligent by default** - Storage that understands semantics, not just bytes
- **Composable** - Mix and match backends, plugins, and deployment modes
- **Production-ready** - Security, multi-tenancy, and observability from day one

## License

© 2025 Nexi Labs, Inc. Licensed under Apache License 2.0 - See [LICENSE](LICENSE) for details.

---

**Built for the AI-native era.** [Docs](docs/api/README.md) • [PyPI](https://pypi.org/project/nexus-ai-fs/) • [GitHub](https://github.com/nexi-lab/nexus)
