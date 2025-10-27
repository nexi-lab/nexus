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

**Jump to:** [Hello World](#hello-world-10-seconds) • [Server Setup](#server-mode-production) • [Permissions](#permission-system-example) • [Core Concepts](#core-concepts) • [Documentation](#documentation)

## Quick Start

### Installation

```bash
pip install nexus-ai-fs
```

### Hello World (10 Seconds)

```python
import nexus

# Zero-config start - data stored in ./nexus-data by default
nx = nexus.connect()

# Write and read a file
nx.write("/workspace/hello.txt", b"Hello, Nexus!")
content = nx.read("/workspace/hello.txt")
print(content.decode())  # → Hello, Nexus!

nx.close()
```

**✅ No configuration needed!** Data is stored locally in `./nexus-data/`. Mount S3, GCS, or other backends later via [configuration](docs/api/configuration.md).

### Embedded Mode (Full Features)

```python
import nexus

# Zero-config local filesystem with AI features
nx = nexus.connect(config={"data_dir": "./nexus-data"})

# File operations
nx.write("/workspace/doc.txt", b"Hello Nexus")
content = nx.read("/workspace/doc.txt")

# Agent memory
nx.memory.store("Python best practices learned from code review")
memories = nx.memory.query(user_id="alice", scope="project")
# Returns: [Memory(content="Python best practices...", timestamp=...)]

# Semantic search
results = nx.semantic_search("/docs/**/*.md", query="authentication setup")
# Returns: [
#   SearchResult(path="/docs/auth.md", score=0.89, snippet="...OAuth setup..."),
#   SearchResult(path="/docs/security.md", score=0.82, snippet="...API keys...")
# ]

nx.close()
```

### Server Mode (Production)

**Start the server:**
```bash
# Configure PostgreSQL backend
export NEXUS_DATABASE_URL="postgresql://user:pass@localhost/nexus"

# Initialize with authentication
./scripts/init-nexus-with-auth.sh

# Server runs at http://localhost:8080
```

**Use the CLI remotely:**
```bash
# Load admin credentials
source .nexus-admin-env

# Create workspace
nexus mkdir /workspace/project1 --remote-url $NEXUS_URL

# Create users and grant permissions
python3 scripts/create-api-key.py alice "Alice's API key" --days 90
# Output: sk-alice_abc123_...

nexus rebac create user alice direct_owner file /workspace/project1 \
  --tenant-id default --remote-url $NEXUS_URL

# Alice can now access with her API key
export NEXUS_API_KEY='sk-alice_abc123_...'
nexus write /workspace/project1/data.txt "Alice's data" --remote-url $NEXUS_URL
```

**Use the SDK remotely:**
```python
import nexus

# Same API, remote execution
nx = nexus.connect(config={
    "remote_url": "http://localhost:8080",
    "api_key": "sk-alice_abc123_..."
})

nx.write("/workspace/project1/data.txt", b"Remote write")
content = nx.read("/workspace/project1/data.txt")
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

Nexus extends traditional storage into the AI era—combining files, memory, and access control into a single programmable layer. Build agents that persist knowledge, collaborate securely, and scale from local experiments to distributed systems.

**Key Capabilities:**
- **AI Memory** - Store contextual embeddings alongside data, not just bytes
- **Unified Fabric** - Files, vectors, and permissions in one system
- **Agent-Native Design** - Built for LLM agents and automation frameworks

**For AI Agent Developers:**
- **Self-Evolving Memory**: Agents store and retrieve context across sessions with automatic consolidation
- **Time-Travel Debugging**: Reproduce any agent state with workspace snapshots and version history
- **Semantic Search**: Find relevant files and memories using natural language queries

**For Enterprise Teams:**
- **Fine-Grained Permissions**: [ReBAC](https://research.google/pubs/pub48190/) (Relationship-Based Access Control, inspired by Google Zanzibar) with inheritance and multi-tenancy
- **Multi-Backend Storage**: Unified API for S3, GCS, SharePoint, Google Drive, and local filesystem
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
| **AI Intelligence** | Memory API, semantic search, workspace snapshots, time-travel debugging |
| **Developer UX** | Embedded/remote parity, full-featured CLI + SDK, 100% feature compatibility |
| **Extensibility** | Plugin system with lifecycle hooks, custom CLI commands, auto-discovery |

## Performance Highlights

- **4x Faster Uploads** - Batch write API for checkpoint and log files
- **30-50% Storage Savings** - Content-addressable deduplication
- **Instant Queries** - Semantic search with vector indexes (pgvector/sqlite-vec)
- **Zero Downtime** - Optimistic concurrency control for multi-agent access

## Key Features

### Storage & Operations
- **Multi-Backend**: S3, GCS, SharePoint, Google Drive, local filesystem with unified API
- **Content Deduplication**: 30-50% storage savings via content-addressable architecture
- **Versioning**: Complete history tracking with rollback and diff capabilities
- **Batch Operations**: 4x faster bulk uploads for checkpoints and large datasets

### Access Control
- **ReBAC**: Relationship-based permissions with Google Zanzibar-style authorization
- **Permission Inheritance**: Directory-based access control with automatic propagation
- **Multi-Tenancy**: Complete isolation between tenants with namespace support
- **API Key Authentication**: Database-backed keys with expiration and rotation

### Agent Intelligence
- **Memory API**: Store, query, and consolidate agent memories automatically
- **Semantic Search**: Vector-based search across files and memories
- **Workspace Snapshots**: Save and restore entire agent workspaces for debugging
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

## Use Cases

**AI Agents** - Memory API for context retention, semantic search for knowledge retrieval

**Multi-Tenant SaaS** - ReBAC for tenant isolation, workspace snapshots for backup/restore

**Document Processing** - Batch uploads with deduplication, semantic search across files

**ML Workflows** - Checkpoint versioning, time-travel debugging for reproducibility

**Extensible Pipelines** - Plugin system (Anthropic, Firecrawl) for custom integrations

📚 **See [examples/](examples/)** for complete AI agent, SaaS, ML, and plugin demos with runnable code.

## Architecture

```mermaid
flowchart TD
    A[👤 Application<br/>SDK/CLI] --> B{nexus.connect}

    B -->|Embedded Mode| C[NexusFS Core]
    B -.->|Remote Mode| D[RPC Client]
    D -.HTTP/RPC.-> E[RPC Server]
    E --> C

    C --> F[File Ops + ReBAC<br/>+ Search + Memory]
    F --> G[Router]
    G --> H[Backend<br/>Local/S3/GCS]
    H --> I[(Metadata DB)]
    H --> J[CAS Storage]

    style A fill:#e3f2fd,stroke:#1976d2,stroke-width:2px
    style C fill:#f3e5f5,stroke:#7b1fa2,stroke-width:3px
    style D fill:#fff3e0,stroke:#f57c00,stroke-width:2px
    style E fill:#fff3e0,stroke:#f57c00,stroke-width:2px
    style I fill:#e8f5e9,stroke:#388e3c,stroke-width:2px
    style J fill:#e8f5e9,stroke:#388e3c,stroke-width:2px
```

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
