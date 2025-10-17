# Nexus: AI-Native Distributed Filesystem

[![Test](https://github.com/nexi-lab/nexus/actions/workflows/test.yml/badge.svg)](https://github.com/nexi-lab/nexus/actions/workflows/test.yml)
[![Lint](https://github.com/nexi-lab/nexus/actions/workflows/lint.yml/badge.svg)](https://github.com/nexi-lab/nexus/actions/workflows/lint.yml)
[![codecov](https://codecov.io/gh/nexi-lab/nexus/branch/main/graph/badge.svg)](https://codecov.io/gh/nexi-lab/nexus)
[![PyPI version](https://badge.fury.io/py/nexus-ai-fs.svg)](https://badge.fury.io/py/nexus-ai-fs)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

**Version 0.1.0** | AI Agent Infrastructure Platform

Nexus is a complete AI agent infrastructure platform that combines distributed unified filesystem, self-evolving agent memory, intelligent document processing, and seamless deployment across three modes—all from a single codebase.

## Features

### Foundation
- **Distributed Unified Filesystem**: Multi-backend abstraction (S3, GDrive, SharePoint, LocalFS)
- **Tiered Storage**: Hot/Warm/Cold tiers with automatic lineage tracking
- **Content-Addressable Storage**: 30-50% storage savings via deduplication
- **"Everything as a File" Paradigm**: Configuration, memory, jobs, and commands as files

### Agent Intelligence
- **Self-Evolving Memory**: Agent memory with automatic consolidation
- **Memory Versioning**: Track knowledge evolution over time
- **Multi-Agent Sharing**: Shared memory spaces within tenants
- **Memory Analytics**: Effectiveness tracking and insights

### Content Processing
- **Rich Format Parsing**: Extensible parsers (PDF, Excel, CSV, JSON, images)
- **LLM KV Cache Management**: 50-90% cost savings on AI queries
- **Semantic Chunking**: Better search via intelligent document segmentation
- **MCP Integration**: Native Model Context Protocol server
- **Document Type Detection**: Automatic routing to appropriate parsers

### Operations
- **Resumable Jobs**: Checkpointing system survives restarts
- **OAuth Token Management**: Auto-refreshing credentials
- **Backend Auto-Mount**: Automatic recognition and mounting
- **Resource Management**: CPU throttling and rate limiting

## Three Deployment Modes

Nexus uniquely supports three deployment modes from a single codebase:

| Mode | Users | Data | Use Case | Setup Time |
|------|-------|------|----------|------------|
| **Embedded** | 1 | ~10GB | Individual developers, CLI tools | 60 seconds |
| **Monolithic** | 1-20 | ~100GB | Small teams, staging | 10 minutes |
| **Distributed** | 100+ | Petabyte+ | Enterprise, production | Hours |

### Quick Start: Embedded Mode

```python
import nexus

# Zero-deployment filesystem with AI features
# Config auto-discovered from nexus.yaml or environment
nx = nexus.connect()

async with nx:
    # Write and read files
    await nx.write("/workspace/data.txt", b"Hello World")
    content = await nx.read("/workspace/data.txt")

    # Semantic search across documents
    results = await nx.semantic_search(
        "/docs/**/*.pdf",
        query="authentication implementation"
    )

    # LLM-powered document reading with KV cache
    answer = await nx.llm_read(
        "/reports/q4.pdf",
        prompt="Summarize key findings",
        model="claude-sonnet-4"
    )
```

**Config file (`nexus.yaml`):**
```yaml
mode: embedded
data_dir: ./nexus-data
cache_size_mb: 100
enable_vector_search: true
```

### Quick Start: Monolithic Server

```bash
# Single Docker container for teams
docker run -d -p 8080:8080 \
  -v ./nexus-data:/data \
  -e NEXUS_MODE=monolithic \
  nexus/nexus:latest server

# Or with docker-compose
docker-compose up -d
```

### Quick Start: Distributed Mode

```bash
# Kubernetes with Helm
helm install nexus nexus/nexus-distributed \
  --set replicas=5 \
  --set postgres.enabled=true \
  --set redis.enabled=true
```

## Installation

### Using pip (Recommended)

```bash
# Install from PyPI
pip install nexus-ai-fs

# Verify installation
nexus --version
```

### From Source (Development)

```bash
# Clone the repository
git clone https://github.com/nexi-lab/nexus.git
cd nexus

# Install using uv (recommended for faster installs)
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
uv pip install -e ".[dev]"

# Or using pip
pip install -e ".[dev]"
```

### Development Setup

```bash
# Install development dependencies
uv pip install -e ".[dev,test]"

# Run tests
pytest

# Run type checking
mypy src/nexus

# Format code
ruff format .

# Lint
ruff check .
```

## CLI Usage

Nexus provides a beautiful command-line interface for all file operations. After installation, the `nexus` command will be available.

### Quick Start

```bash
# Initialize a new workspace
nexus init ./my-workspace

# Write a file
nexus write /workspace/hello.txt "Hello, Nexus!"

# Read a file
nexus cat /workspace/hello.txt

# List files
nexus ls /workspace
nexus ls /workspace --recursive
nexus ls /workspace --long  # Detailed view with metadata
```

### Available Commands

#### File Operations

```bash
# Write content to a file
nexus write /path/to/file.txt "content"
echo "content" | nexus write /path/to/file.txt --input -

# Display file contents (with syntax highlighting)
nexus cat /workspace/code.py

# Copy files
nexus cp /source.txt /dest.txt

# Delete files
nexus rm /workspace/old-file.txt
nexus rm /workspace/old-file.txt --force  # Skip confirmation

# Show file information
nexus info /workspace/data.txt
```

#### Directory Operations

```bash
# Create directory
nexus mkdir /workspace/data
nexus mkdir /workspace/deep/nested/dir --parents

# Remove directory
nexus rmdir /workspace/data
nexus rmdir /workspace/data --recursive --force
```

#### File Discovery

```bash
# List files
nexus ls /workspace
nexus ls /workspace --recursive
nexus ls /workspace --long  # Show size, modified time, etag

# Find files by pattern (glob)
nexus glob "**/*.py"  # All Python files recursively
nexus glob "*.txt" --path /workspace  # Text files in workspace
nexus glob "test_*.py"  # Test files

# Search file contents (grep)
nexus grep "TODO"  # Find all TODO comments
nexus grep "def \w+" --file-pattern "**/*.py"  # Find function definitions
nexus grep "error" --ignore-case  # Case-insensitive search
nexus grep "TODO" --max-results 50  # Limit results
```

### Examples

**Initialize and populate a workspace:**

```bash
# Create workspace
nexus init ./my-project

# Create structure
nexus mkdir /workspace/src --data-dir ./my-project/nexus-data
nexus mkdir /workspace/tests --data-dir ./my-project/nexus-data

# Add files
echo "print('Hello World')" | nexus write /workspace/src/main.py --input - \
  --data-dir ./my-project/nexus-data

# List everything
nexus ls / --recursive --long --data-dir ./my-project/nexus-data
```

**Find and analyze code:**

```bash
# Find all Python files
nexus glob "**/*.py"

# Search for TODO comments
nexus grep "TODO|FIXME" --file-pattern "**/*.py"

# Find all test files
nexus glob "**/test_*.py"

# Search for function definitions
nexus grep "^def \w+\(" --file-pattern "**/*.py"
```

**Work with data:**

```bash
# Write JSON data
echo '{"name": "test", "value": 42}' | nexus write /data/config.json --input -

# Display with syntax highlighting
nexus cat /data/config.json

# Get file information
nexus info /data/config.json
```

### Global Options

All commands support these global options:

```bash
# Use custom config file
nexus ls /workspace --config /path/to/config.yaml

# Override data directory
nexus ls /workspace --data-dir /path/to/nexus-data

# Combine both (config takes precedence)
nexus ls /workspace --config ./my-config.yaml --data-dir ./data
```

### Help

Get help for any command:

```bash
nexus --help  # Show all commands
nexus ls --help  # Show help for ls command
nexus grep --help  # Show help for grep command
```

## Architecture

### Agent Workspace Structure

Every agent gets a structured workspace at `/workspace/{tenant}/{agent}/`:

```
/workspace/acme-corp/research-agent/
├── .nexus/                          # Nexus metadata (Git-trackable)
│   ├── agent.yaml                   # Agent configuration
│   ├── commands/                    # Custom commands (markdown files)
│   │   ├── analyze-codebase.md
│   │   └── summarize-docs.md
│   ├── jobs/                        # Background job definitions
│   │   └── daily-summary.yaml
│   ├── memory/                      # File-based memory
│   │   ├── project-knowledge.md
│   │   └── recent-tasks.jsonl
│   └── secrets.encrypted            # KMS-encrypted credentials
├── data/                            # Agent's working data
│   ├── inputs/
│   └── outputs/
└── INSTRUCTIONS.md                  # Agent instructions (auto-loaded)
```

### Path Namespace

```
/
├── workspace/        # Agent scratch space (hot tier, ephemeral)
├── shared/           # Shared tenant data (warm tier, persistent)
├── external/         # Pass-through backends (no content storage)
├── system/           # System metadata (admin-only)
└── archives/         # Cold storage (read-only)
```

## Core Components

### File System Operations

```python
import nexus

# Works in embedded, monolithic, or distributed mode
# Mode determined by config file or environment
nx = nexus.connect()

async with nx:
    # Basic operations
    await nx.write("/workspace/data.txt", b"content")
    content = await nx.read("/workspace/data.txt")
    await nx.delete("/workspace/data.txt")

    # Batch operations
    files = await nx.list("/workspace/", recursive=True)
    results = await nx.copy_batch(sources, destinations)

    # File discovery
    python_files = await nx.glob("**/*.py")
    todos = await nx.grep(r"TODO:|FIXME:", file_pattern="*.py")
```

### Semantic Search

```python
# Search across documents with vector embeddings
async with nexus.connect() as nx:
    results = await nx.semantic_search(
        path="/docs/",
        query="How does authentication work?",
        limit=10,
        filters={"file_type": "markdown"}
    )

    for result in results:
        print(f"{result.path}:{result.line} - {result.text}")
```

### LLM-Powered Reading

```python
# Read documents with AI, with automatic KV cache
async with nexus.connect() as nx:
    answer = await nx.llm_read(
        path="/reports/q4-2024.pdf",
        prompt="What were the top 3 challenges?",
        model="claude-sonnet-4",
        max_tokens=1000
    )
```

### Agent Memory

```python
# Store and retrieve agent memories
async with nexus.connect() as nx:
    await nx.store_memory(
        content="User prefers TypeScript over JavaScript",
        memory_type="preference",
        tags=["coding", "languages"]
    )

    memories = await nx.search_memories(
        query="programming language preferences",
        limit=5
    )
```

### Custom Commands

Create `/workspace/{tenant}/{agent}/.nexus/commands/semantic-search.md`:

```markdown
---
name: semantic-search
description: Search codebase semantically
allowed-tools: [semantic_read, glob, grep]
required-scopes: [read]
model: sonnet
---

## Your task

Given query: {{query}}

1. Use `glob` to find relevant files by pattern
2. Use `semantic_read` to extract relevant sections
3. Summarize findings with file:line citations
```

Execute via API:

```python
async with nexus.connect() as nx:
    result = await nx.execute_command(
        "semantic-search",
        context={"query": "authentication implementation"}
    )
```

## Technology Stack

### Core
- **Language**: Python 3.11+
- **API Framework**: FastAPI
- **Database**: PostgreSQL (prod) / SQLite (dev)
- **Cache**: Redis (prod) / In-memory (dev)
- **Vector DB**: Qdrant
- **Object Storage**: S3-compatible, GCS, Azure Blob

### AI/ML
- **LLM Providers**: Anthropic Claude, OpenAI, Google Gemini
- **Embeddings**: text-embedding-3-large, voyage-ai
- **Parsing**: PyPDF2, pandas, openpyxl, Pillow

### Infrastructure
- **Orchestration**: Kubernetes (distributed mode)
- **Monitoring**: Prometheus + Grafana
- **Logging**: Structlog + Loki
- **Admin UI**: Simple HTML/JS (jobs, memories, files, operations)

## Performance Targets

| Metric | Target | Impact |
|--------|--------|--------|
| Write Throughput | 500-1000 MB/s | 10-50× vs direct backend |
| Read Latency | <10ms | 10-50× vs remote storage |
| Memory Search | <100ms | Vector search across memories |
| Storage Savings | 30-50% | CAS deduplication |
| Job Resumability | 100% | Survives all restarts |
| LLM Cache Hit Rate | 50-90% | Major cost savings |

## Configuration

### Embedded Mode

```python
import nexus

# Config via Python (useful for programmatic configuration)
nx = nexus.connect(config={
    "mode": "embedded",
    "data_dir": "./nexus-data",
    "cache_size_mb": 100,
    "enable_vector_search": True
})

# Or let it auto-discover from nexus.yaml
nx = nexus.connect()
```

### Server Mode

Create `config.yaml`:

```yaml
mode: monolithic  # embedded, monolithic, distributed

database:
  url: postgresql://user:pass@localhost/nexus
  # or for SQLite: sqlite:///./nexus.db

cache:
  type: redis  # memory, redis
  url: redis://localhost:6379

vector_db:
  type: qdrant
  url: http://localhost:6333

backends:
  - type: s3
    bucket: my-company-files
    region: us-east-1

  - type: gdrive
    credentials_path: ./gdrive-creds.json

auth:
  jwt_secret: your-secret-key
  token_expiry_hours: 24

rate_limits:
  default: "100/minute"
  semantic_search: "10/minute"
  llm_read: "50/hour"
```

Run server:

```bash
nexus server --config config.yaml
```

## Security

### Multi-Layer Security Model

1. **API Key Authentication**: Tenant and agent identification
2. **Row-Level Security (RLS)**: Database-level tenant isolation
3. **UNIX-Style Permissions**: Owner, group, and mode bits (coming in v0.2.0)
4. **ACL Permissions**: Fine-grained access control lists (coming in v0.2.0)

### Example: Multi-Tenancy Isolation

```sql
-- RLS automatically filters queries by tenant
SET LOCAL app.current_tenant_id = '<tenant_uuid>';

-- All queries auto-filtered, even with bugs
SELECT * FROM file_paths WHERE path = '/data';
-- Returns only rows for current tenant
```

## Testing

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=nexus --cov-report=html

# Run specific test file
pytest tests/test_filesystem.py

# Run integration tests
pytest tests/integration/ -v

# Run performance tests
pytest tests/performance/ --benchmark-only
```

## Documentation

- [API Reference](./docs/api.md)
- [Deployment Guide](./docs/deployment.md)
- [Development Guide](./docs/development.md)
- [MCP Integration](./docs/mcp.md)

## Contributing

We welcome contributions! Please see [CONTRIBUTING.md](./CONTRIBUTING.md) for details.

```bash
# Fork the repo and clone
git clone https://github.com/yourusername/nexus.git
cd nexus

# Create a feature branch
git checkout -b feature/your-feature

# Make changes and test
uv pip install -e ".[dev,test]"
pytest

# Format and lint
ruff format .
ruff check .

# Commit and push
git commit -am "Add your feature"
git push origin feature/your-feature
```

## License

Apache 2.0 License - see [LICENSE](./LICENSE) for details.


## Roadmap

### v0.1.0 - Embedded Mode Foundation (Current)
- [x] Core embedded filesystem (read/write/delete)
- [x] SQLite metadata store
- [x] Local filesystem backend
- [x] Basic file operations (list, glob, grep)
- [x] Virtual path routing
- [x] Directory operations (mkdir, rmdir, is_directory)
- [x] Basic CLI interface with Click and Rich
- [ ] In-memory caching
- [ ] Batch operations (avoid N+1 queries)
- [ ] Metadata export/import (JSONL format)
- [ ] SQL views for ready work detection
- [ ] Type-level validation

### v0.2.0 - File Permissions & Security
- [ ] UNIX-style file permissions (owner, group, mode)
- [ ] Permission operations (chmod, chown, chgrp)
- [ ] Default permission policies per namespace
- [ ] Permission inheritance for new files
- [ ] Permission checking in all file operations
- [ ] ACL (Access Control List) support
- [ ] Permission migration for existing files
- [ ] Comprehensive permission tests

### v0.3.0 - Document Processing
- [ ] PDF parser
- [ ] Excel/CSV parser
- [ ] Document type detection
- [ ] Text extraction pipeline
- [ ] Basic semantic chunking
- [ ] Qdrant embedded integration
- [ ] Collision detection and resolution
- [ ] Enhanced audit trail with structured events

### v0.4.0 - AI Integration
- [ ] LLM provider abstraction
- [ ] Anthropic Claude integration
- [ ] OpenAI integration
- [ ] Basic KV cache for prompts
- [ ] Semantic search (vector embeddings)
- [ ] LLM-powered document reading

### v0.5.0 - Agent Workspaces
- [ ] Agent workspace structure
- [ ] File-based configuration (.nexus/)
- [ ] Custom command system (markdown)
- [ ] Basic agent memory storage
- [ ] Memory consolidation
- [ ] Memory reflection phase (ACE-inspired: extract insights from execution trajectories)
- [ ] Strategy/playbook organization (ACE-inspired: organize memories as reusable strategies)

### v0.6.0 - Monolithic Server Mode
- [ ] FastAPI REST API
- [ ] API key authentication
- [ ] Multi-tenancy support
- [ ] PostgreSQL support
- [ ] Redis caching
- [ ] Docker deployment
- [ ] Batch/transaction APIs (atomic multi-operation updates)
- [ ] Optimistic locking for concurrent writes

### v0.7.0 - Extended Features & Event System
- [ ] S3 backend support
- [ ] Google Drive backend
- [ ] Job system with checkpointing
- [ ] OAuth token management
- [ ] MCP server implementation
- [ ] Webhook/event system (file changes, memory updates, job events)
- [ ] Watch API for real-time updates (streaming changes to clients)
- [ ] Server-Sent Events (SSE) support for live monitoring
- [ ] Simple admin UI (jobs, memories, files, operation logs)
- [ ] Operation logs table (track storage operations for debugging)

### v0.8.0 - Advanced AI Features & Rich Query
- [ ] Advanced KV cache with context tracking
- [ ] Memory versioning and lineage
- [ ] Multi-agent memory sharing
- [ ] Enhanced semantic search
- [ ] Importance-based memory preservation (ACE-inspired: prevent brevity bias in consolidation)
- [ ] Context-aware memory retrieval (include execution context in search)
- [ ] Automated strategy extraction (LLM-powered extraction from successful trajectories)
- [ ] Rich memory query language (filter by metadata, importance, task type, date ranges, etc.)
- [ ] Memory query builder API (fluent interface for complex queries)
- [ ] Combined vector + metadata search (hybrid search)

### v0.9.0 - Production Readiness
- [ ] Monitoring and observability
- [ ] Performance optimization
- [ ] Comprehensive testing
- [ ] Security hardening
- [ ] Documentation completion
- [ ] Optional OpenTelemetry export (for framework integration)

### v0.10.0 - Distributed Mode
- [ ] Distributed architecture
- [ ] Redis distributed locks
- [ ] PostgreSQL replication
- [ ] Kubernetes deployment
- [ ] Load balancing

### v1.0.0 - Production Release
- [ ] Complete feature set
- [ ] Production-tested
- [ ] Comprehensive documentation
- [ ] Migration tools
- [ ] Enterprise support

## Support

- **Issues**: [GitHub Issues](https://github.com/yourusername/nexus/issues)
- **Discussions**: [GitHub Discussions](https://github.com/yourusername/nexus/discussions)
- **Email**: support@nexus.example.com
- **Slack**: [Join our community](https://nexus-community.slack.com)

---

Built with ❤️ by the Nexus team
