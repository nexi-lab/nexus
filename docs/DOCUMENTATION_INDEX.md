# Nexus Documentation Index

Complete guide to Nexus documentation organized by topic.

---

## 📖 Main Guides

| Document | Description |
|----------|-------------|
| [Getting Started](getting-started/quickstart.md) | Installation and first steps |
| [Permissions & ReBAC](PERMISSIONS.md) | Complete permission system guide |
| [Multi-Tenant](MULTI_TENANT.md) | Multi-tenancy implementation |
| [Authentication](authentication.md) | Authentication and API keys |
| [Core Tenets](CORE_TENETS.md) | Design philosophy |

---

## 🚀 Getting Started

| Document | Description |
|----------|-------------|
| [Quick Start](getting-started/quickstart.md) | 5-minute start guide |
| [Installation](getting-started/installation.md) | Installation instructions |
| [Configuration](getting-started/configuration.md) | Configuration reference |
| [Deployment Modes](getting-started/deployment-modes.md) | Embedded vs server modes |

---

## 📚 API Reference

| Document | Description |
|----------|-------------|
| [Complete API](api/api.md) | Full API reference |
| [Core API](api/core-api.md) | Core methods |
| [File Operations](api/file-operations.md) | File APIs |
| [File Discovery](api/file-discovery.md) | List, glob, grep |
| [Directory Operations](api/directory-operations.md) | Directory APIs |
| [Permissions](api/permissions.md) | Permission APIs |
| [Workspace Management](api/workspace-management.md) | Workspace APIs |
| [Memory Management](api/memory-management.md) | Memory APIs |
| [Metadata](api/metadata.md) | Metadata operations |
| [Mounts](api/mounts.md) | Backend mounts |
| [Versioning](api/versioning.md) | Time travel |
| [Semantic Search](api/semantic-search.md) | Vector search |
| [LLM Document Reading](api/llm-document-reading.md) | AI-powered Q&A |
| [Configuration](api/configuration.md) | Config options |
| [Error Handling](api/error-handling.md) | Error handling |
| [CLI Reference](api/cli-reference.md) | CLI commands |
| [RPC API](api/rpc-api.md) | Server API |
| [Advanced Usage](api/advanced-usage.md) | Patterns |

---

## 🏗️ Architecture

| Document | Description |
|----------|-------------|
| [System Architecture](architecture/ARCHITECTURE.md) | Overall design |
| [ReBAC Architecture](architecture/rebac.md) | Permission system internals |
| [Tenant Isolation](architecture/tenant-isolation.md) | Multi-tenant isolation |
| [Default Context](architecture/default-context.md) | Context design |

---

## 🔐 Security

| Document | Description |
|----------|-------------|
| [Default Context Security](security/default-context-security.md) | Security implications |

---

## 🚢 Deployment

| Document | Description |
|----------|-------------|
| [Server Setup](deployment/server-setup.md) | Complete server setup |
| [Deployment Guide](deployment/DEPLOYMENT.md) | Production deployment |
| [PostgreSQL Setup](deployment/postgresql.md) | PostgreSQL configuration |
| [Quick Start](deployment/QUICK_START.md) | Quick deployment |
| [Docker](deployment/DOCKER_DEPLOYMENT.md) | Docker setup |
| [GCP](deployment/GCP_DEPLOYMENT.md) | Google Cloud |

---

## 💻 Development

| Document | Description |
|----------|-------------|
| [Development Guide](development/development.md) | Contributing |
| [RPC Parity](development/rpc-parity.md) | Embedded/server parity |
| [Database Compatibility](development/DATABASE_COMPATIBILITY.md) | SQLite vs PostgreSQL |
| [Permissions Implementation](development/PERMISSIONS_IMPLEMENTATION.md) | Permission internals |
| [Permission Enforcement](development/PERMISSION_ENFORCEMENT_GUIDE.md) | Enforcement details |
| [Plugin Development](development/PLUGIN_DEVELOPMENT.md) | Plugins |
| [Parsers](development/parsers.md) | Parser development |

---

## 📖 Guides

| Document | Description |
|----------|-------------|
| [SDK Usage](guides/sdk-usage.md) | Using the SDK |
| [Delegation](guides/delegation.md) | Permission delegation |
| [ReBAC Roles](guides/rebac-roles.md) | Role patterns |

---

## 🔧 Advanced Topics

| Document | Description |
|----------|-------------|
| [FUSE Mounting](advanced/fuse.md) | FUSE filesystem integration |
| [SQL Patterns](advanced/sql-patterns.md) | SQL views and patterns |

---

## 🔌 Integrations

| Document | Description |
|----------|-------------|
| [Model Context Protocol (MCP)](integrations/mcp.md) | MCP server for AI agents (Claude Desktop, etc.) |
| [LLM Provider](integrations/llm.md) | LLM integration |

---

## 📂 Reference

| Document | Description |
|----------|-------------|
| [Mount Management](reference/mount-management.md) | Mount quick reference |
| [ReBAC Features](reference/rebac-features.md) | Feature matrix |

---

## 📋 Authentication & Examples

### Authentication

See [Authentication Guide](authentication.md) for complete documentation.

**Examples:** `examples/auth_demo/`
- Static API key demos
- Database auth demos
- CLI authentication
- Complete auth + permissions

### Examples Directory

| Location | Description |
|----------|-------------|
| `examples/auth_demo/` | Authentication examples |
| `examples/parity_demo/` | Parity testing |
| `examples/py_demo/` | Python SDK demos |
| `examples/script_demo/` | Shell script demos |

---

## 🎯 By Use Case

### "I'm getting started"
1. [Quick Start](getting-started/quickstart.md)
2. [Installation](getting-started/installation.md)
3. [Basic API](api/core-api.md)

### "I need permissions"
1. [Permissions Guide](PERMISSIONS.md)
2. [Permission API](api/permissions.md)
3. Try: `examples/py_demo/rebac_comprehensive_demo.py`

### "I need multi-tenancy"
1. [Multi-Tenant Guide](MULTI_TENANT.md)
2. Try: `examples/py_demo/multi_tenant_demo.py`

### "I need authentication"
1. [Authentication Guide](authentication.md)
2. Try: `examples/auth_demo/`

### "I'm deploying to production"
1. [Server Setup](deployment/server-setup.md)
2. [PostgreSQL Setup](deployment/postgresql.md)
3. [Deployment Guide](deployment/DEPLOYMENT.md)

### "I'm developing features"
1. [Core Tenets](CORE_TENETS.md)
2. [Development Guide](development/development.md)
3. [RPC Parity](development/rpc-parity.md)

### "I want to integrate with AI agents"
1. [MCP Integration Guide](integrations/mcp.md)
2. [MCP CLI Reference](api/cli/mcp.md)
3. [Agent Framework Examples](examples/index.md):
   - [CrewAI](examples/crewai.md) - Multi-agent teams with memory
   - [LangGraph](examples/langgraph.md) - ReAct agents
   - [ACE Learning](examples/ace.md) - Learning from experience
   - [Claude SDK](examples/claude-agent-sdk.md) - Anthropic agents
   - [OpenAI SDK](examples/openai-agents.md) - OpenAI agents
4. Try: `examples/mcp/`, `examples/crewai/`, `examples/langgraph/`

---

## 📞 Getting Help

- **GitHub Issues**: https://github.com/nexi-lab/nexus/issues
- **Examples**: See `examples/` directory
- **Tests**: See `tests/integration/` for working examples

---

**Last Updated:** 2025-10-29
