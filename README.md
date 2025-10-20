# Nexus: AI-Native Distributed Filesystem

[![Test](https://github.com/nexi-lab/nexus/actions/workflows/test.yml/badge.svg)](https://github.com/nexi-lab/nexus/actions/workflows/test.yml)
[![Lint](https://github.com/nexi-lab/nexus/actions/workflows/lint.yml/badge.svg)](https://github.com/nexi-lab/nexus/actions/workflows/lint.yml)
[![PyPI version](https://badge.fury.io/py/nexus-ai-fs.svg)](https://badge.fury.io/py/nexus-ai-fs)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

**Version 0.1.0** | AI Agent Infrastructure Platform

Nexus is a complete AI agent infrastructure platform that combines distributed unified filesystem, self-evolving agent memory, intelligent document processing, and seamless deployment from local development to hosted production—all from a single codebase.

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
- **Prompt Version Control**: Track prompt evolution with lineage
- **Training Data Management**: Version-controlled datasets with deduplication
- **Prompt Optimization**: Multi-candidate testing, execution traces, tradeoff analysis
- **Experiment Tracking**: Organize optimization runs, per-example results, regression detection

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
- **Work Queue Detection**: SQL views for efficient task scheduling and dependency resolution

## Deployment Modes

Nexus supports two deployment modes from a single codebase:

| Mode | Use Case | Setup Time | Scaling |
|------|----------|------------|---------|
| **Local** | Individual developers, CLI tools, prototyping | 60 seconds | Single machine (~10GB) |
| **Hosted** | Teams and production (auto-scales) | Sign up | Automatic (GB to Petabytes) |

**Note**: Hosted mode automatically scales infrastructure under the hood—you don't choose between "monolithic" or "distributed". Nexus handles that for you based on your usage.

### Quick Start: Local Mode

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
mode: local
data_dir: ./nexus-data
cache_size_mb: 100
enable_vector_search: true
```

### Quick Start: Hosted Mode

**Coming Soon!** Sign up for early access at [nexus.ai](https://nexus.ai)

```python
import nexus

# Connect to Nexus hosted instance
# Infrastructure scales automatically based on your usage
nx = nexus.connect(
    api_key="your-api-key",
    endpoint="https://api.nexus.ai"
)

async with nx:
    # Same API as local mode!
    await nx.write("/workspace/data.txt", b"Hello World")
    content = await nx.read("/workspace/data.txt")
```

**For self-hosted deployments**, see the [S3-Compatible HTTP Server](#s3-compatible-http-server) section below for deployment instructions.

## Storage Backends

Nexus supports multiple storage backends through a unified API. All backends use **Content-Addressable Storage (CAS)** for automatic deduplication.

### Local Backend (Default)

Store files on local filesystem:

```python
import nexus

# Auto-detected from config or uses default
nx = nexus.connect()

# Or explicitly configure
nx = nexus.connect(config={
    "backend": "local",
    "data_dir": "./nexus-data"
})
```

### Google Cloud Storage (GCS) Backend

Store files in Google Cloud Storage with local metadata:

```python
import nexus

# Connect with GCS backend
nx = nexus.connect(config={
    "backend": "gcs",
    "gcs_bucket_name": "my-nexus-bucket",
    "gcs_project_id": "my-gcp-project",  # Optional
    "gcs_credentials_path": "/path/to/credentials.json",  # Optional
})
```

**Authentication Methods:**
1. **Service Account Key**: Provide `gcs_credentials_path`
2. **Application Default Credentials** (if not provided):
   - `GOOGLE_APPLICATION_CREDENTIALS` environment variable
   - `gcloud auth application-default login` credentials
   - GCE/Cloud Run service account (when running on GCP)

**Using Config File (`nexus.yaml`):**
```yaml
backend: gcs
gcs_bucket_name: my-nexus-bucket
gcs_project_id: my-gcp-project  # Optional
# gcs_credentials_path: /path/to/credentials.json  # Optional
```

**Using Environment Variables:**
```bash
export NEXUS_BACKEND=gcs
export NEXUS_GCS_BUCKET_NAME=my-nexus-bucket
export NEXUS_GCS_PROJECT_ID=my-gcp-project  # Optional
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/credentials.json  # Optional
```

**CLI Usage with GCS:**
```bash
# Write file to GCS
nexus write /workspace/data.txt "Hello GCS!" \
  --backend=gcs \
  --gcs-bucket=my-nexus-bucket

# Or use config file (simpler!)
nexus write /workspace/data.txt "Hello GCS!" --config=nexus.yaml
```

### Advanced: Direct Backend API

For advanced use cases, instantiate backends directly:

```python
from nexus import NexusFS, LocalBackend, GCSBackend

# Local backend
nx_local = NexusFS(
    backend=LocalBackend("/path/to/data"),
    db_path="./metadata.db"
)

# GCS backend
nx_gcs = NexusFS(
    backend=GCSBackend(
        bucket_name="my-bucket",
        project_id="my-project",
        credentials_path="/path/to/creds.json"
    ),
    db_path="./gcs-metadata.db"
)

# Same API for both!
nx_local.write("/file.txt", b"data")
nx_gcs.write("/file.txt", b"data")
```

### Backend Comparison

| Feature | Local Backend | GCS Backend |
|---------|--------------|-------------|
| **Content Storage** | Local filesystem | Google Cloud Storage |
| **Metadata Storage** | Local SQLite | Local SQLite |
| **Deduplication** | ✅ CAS (30-50% savings) | ✅ CAS (30-50% savings) |
| **Multi-machine Access** | ❌ Single machine | ✅ Shared across machines |
| **Durability** | Single disk | 99.999999999% (11 nines) |
| **Latency** | <1ms (local) | 10-50ms (network) |
| **Cost** | Free (local disk) | GCS storage pricing |
| **Use Case** | Development, single machine | Teams, production, backup |

### Coming Soon

- **Amazon S3 Backend** (v0.7.0)
- **Azure Blob Storage** (v0.7.0)
- **Google Drive** (v0.7.0)
- **SharePoint** (v0.7.0)

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

# Search modes (v0.2.0+)
nexus grep "revenue" --file-pattern "**/*.pdf"  # Auto mode: tries parsed first
nexus grep "revenue" --file-pattern "**/*.pdf" --search-mode=parsed  # Only parsed content
nexus grep "TODO" --search-mode=raw  # Only raw text (skip parsing)

# Result shows source type
# Match: TODO (parsed) ← from parsed PDF
# Match: TODO (raw) ← from source code
```

#### File Permissions (v0.3.0)

```bash
# Change file permissions
nexus chmod 755 /workspace/script.sh
nexus chmod rw-r--r-- /workspace/data.txt

# Change file owner and group
nexus chown alice /workspace/file.txt
nexus chgrp developers /workspace/code/

# View ACL entries
nexus getfacl /workspace/file.txt

# Manage ACL entries
nexus setfacl user:alice:rw- /workspace/file.txt
nexus setfacl group:developers:r-x /workspace/code/
nexus setfacl deny:user:bob /workspace/secret.txt
nexus setfacl user:alice:rwx /workspace/file.txt --remove
```

**Supported Formats:**
- **Octal**: `755`, `0o644`, `0755`
- **Symbolic**: `rwxr-xr-x`, `rw-r--r--`
- **ACL Entries**: `user:<name>:rwx`, `group:<name>:r-x`, `deny:user:<name>`

#### ReBAC - Relationship-Based Access Control (v0.3.0)

Nexus implements Zanzibar-style relationship-based authorization for team-based permissions, hierarchical access, and dynamic permission inheritance.

```bash
# Create relationship tuples
nexus rebac create agent alice member-of group eng-team
nexus rebac create group eng-team owner-of file project-docs
nexus rebac create file folder-parent parent-of file folder-child

# Check permissions (with graph traversal)
nexus rebac check agent alice member-of group eng-team  # Direct check
nexus rebac check agent alice owner-of file project-docs  # Inherited via group

# Find all subjects with a permission
nexus rebac expand owner-of file project-docs  # Returns: alice (via eng-team)
nexus rebac expand member-of group eng-team    # Returns: alice, bob, ...

# Delete relationships
nexus rebac delete <tuple-id>

# Create temporary access (expires automatically)
nexus rebac create agent alice viewer-of file temp-report \
  --expires "2025-12-31T23:59:59"
```

**ReBAC Features:**
- **Relationship Types**: `member-of`, `owner-of`, `viewer-of`, `editor-of`, `parent-of`
- **Graph Traversal**: Recursive permission checking through relationship chains
- **Permission Inheritance**: Team ownership, hierarchical folders, group membership
- **Caching**: 5-minute TTL with automatic invalidation on changes
- **Expiring Access**: Temporary permissions with automatic cleanup
- **Cycle Detection**: Prevents infinite loops in relationship graphs

**Example Use Cases:**
```bash
# Team-based file access
nexus rebac create agent alice member-of group engineering
nexus rebac create group engineering owner-of file /projects/backend
# alice now has owner permission on /projects/backend

# Hierarchical folder permissions
nexus rebac create agent bob owner-of file /workspace/parent-folder
nexus rebac create file /workspace/parent-folder parent-of file /workspace/parent-folder/child
# bob automatically has owner permission on child folder

# Temporary collaborator access
nexus rebac create agent charlie viewer-of file /reports/q4.pdf \
  --expires "2025-01-31T23:59:59"
# charlie's access expires automatically on Jan 31, 2025
```

#### Work Queue Operations

```bash
# Query work items by status
nexus work ready --limit 10  # Get ready work items (high priority first)
nexus work pending  # Get pending work items
nexus work blocked  # Get blocked work items (with dependency info)
nexus work in-progress  # Get currently processing items

# View aggregate statistics
nexus work status  # Show counts for all work queues

# Output as JSON (for scripting)
nexus work ready --json
nexus work status --json
```

**Note**: Work items are files with special metadata (status, priority, depends_on, worker_id). See `docs/SQL_VIEWS_FOR_WORK_DETECTION.md` for details on setting up work queues.

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

## Remote Nexus Server

Nexus includes a JSON-RPC server that exposes the full NexusFileSystem interface over HTTP, enabling remote filesystem access and FUSE mounts to remote servers.

### Quick Start

#### Method 1: Using the Startup Script (Recommended)

```bash
# Navigate to nexus directory
cd /path/to/nexus

# Start with defaults (host: 0.0.0.0, port: 8080, no auth)
./start-server.sh

# Or with custom options
./start-server.sh --host localhost --port 8080 --api-key mysecret
```

#### Method 2: Direct Command

```bash
# Start the server (optional API key authentication)
nexus serve --host 0.0.0.0 --port 8080 --api-key mysecret

# Use remote filesystem from Python
from nexus import RemoteNexusFS

nx = RemoteNexusFS(
    server_url="http://localhost:8080",
    api_key="mysecret"  # Optional
)

# Same API as local NexusFS!
nx.write("/workspace/hello.txt", b"Hello Remote!")
content = nx.read("/workspace/hello.txt")
files = nx.list("/workspace", recursive=True)
```

### Features

- **Full NFS Interface**: All filesystem operations exposed over RPC (read, write, list, glob, grep, mkdir, etc.)
- **JSON-RPC 2.0 Protocol**: Standard RPC protocol with proper error handling
- **API Key Authentication**: Optional Bearer token authentication for security
- **Backend Agnostic**: Works with local and GCS backends
- **FUSE Compatible**: Mount remote Nexus servers as local filesystems

### Remote Client Usage

```python
from nexus import RemoteNexusFS

# Connect to remote server
nx = RemoteNexusFS(
    server_url="http://your-server:8080",
    api_key="your-api-key"  # Optional
)

# All standard operations work
nx.write("/workspace/data.txt", b"content")
content = nx.read("/workspace/data.txt")
files = nx.list("/workspace", recursive=True)
results = nx.glob("**/*.py")
matches = nx.grep("TODO", file_pattern="*.py")
```

### Server Options

```bash
# Start with custom host/port
nexus serve --host 0.0.0.0 --port 8080

# Start with API key authentication
nexus serve --api-key mysecret

# Start with GCS backend
nexus serve --backend=gcs --gcs-bucket=my-bucket --api-key mysecret

# Custom data directory
nexus serve --data-dir /path/to/data
```

### Testing the Server

Once the server is running, verify it's working:

```bash
# Health check
curl http://localhost:8080/health
# Expected: {"status": "healthy", "service": "nexus-rpc"}

# Check available methods
curl http://localhost:8080/api/nfs/status
# Expected: {"status": "running", "service": "nexus-rpc", "version": "1.0", "methods": [...]}

# List files (JSON-RPC)
curl -X POST http://localhost:8080/api/nfs/list \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "method": "list",
    "params": {"path": "/", "recursive": false, "details": true},
    "id": 1
  }'

# With API key
curl -X POST http://localhost:8080/api/nfs/list \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer mysecretkey" \
  -d '{"jsonrpc": "2.0", "method": "list", "params": {"path": "/"}, "id": 1}'
```

### Troubleshooting

**Port Already in Use:**
```bash
# Find and kill process using port 8080
lsof -ti:8080 | xargs kill -9

# Or use a different port
nexus serve --port 8081
```

**Module Not Found:**
```bash
# Activate virtual environment and install
source .venv/bin/activate
pip install -e .
```

**Permission Denied:**
```bash
# Use a directory you have write access to
nexus serve --data-dir ~/nexus-data
```

### Deploying Nexus Server

#### Google Cloud Platform (Recommended)

Deploy to GCP with a single command using the automated deployment script:

```bash
# Quick start
./deploy-gcp.sh --project-id YOUR-PROJECT-ID --api-key mysecret

# With GCS backend
./deploy-gcp.sh \
  --project-id YOUR-PROJECT-ID \
  --gcs-bucket your-nexus-bucket \
  --api-key mysecret \
  --machine-type e2-standard-2
```

**Features:**
- ✅ Automated VM provisioning (Ubuntu 22.04)
- ✅ Systemd service with auto-restart
- ✅ Firewall configuration
- ✅ GCS backend support
- ✅ Production-ready setup

**See [GCP Deployment Guide](docs/deployment/GCP_DEPLOYMENT.md) for complete instructions.**

#### Docker Deployment

Deploy using Docker for consistent environments and easy management:

```bash
# Quick start with Docker Compose
cp .env.docker.example .env
# Edit .env with your configuration
docker-compose up -d

# Or run directly
docker build -t nexus-server:latest .
docker run -d \
  --name nexus-server \
  --restart unless-stopped \
  -p 8080:8080 \
  -v nexus-data:/app/data \
  -e NEXUS_API_KEY="your-api-key" \
  nexus-server:latest

# Deploy to GCP with Docker (automated)
./deploy-gcp-docker.sh \
  --project-id your-project-id \
  --api-key mysecret \
  --build-local
```

**Features:**
- ✅ Multi-stage build for optimized image size (~300MB)
- ✅ Non-root user for security
- ✅ Health checks and auto-restart
- ✅ GCS backend support
- ✅ Docker Compose for easy orchestration

**See [Docker Deployment Guide](docs/deployment/DOCKER_DEPLOYMENT.md) for complete instructions.**

**Deployment Features:**
- **Persistent Metadata**: SQLite database stored on VM disk at `/var/lib/nexus/`
- **Content Storage**: All file content stored in configured backend (GCS, local, etc.)
- **Content Deduplication**: CAS-based storage with 30-50% savings
- **Full NFS API**: All operations available remotely

## FUSE Mount: Use Standard Unix Tools (v0.2.0)

Mount Nexus to a local path and use **any standard Unix tool** seamlessly - `ls`, `cat`, `grep`, `vim`, and more!

### Installation

First, install FUSE support:

```bash
# Install Nexus with FUSE support
pip install nexus-ai-fs[fuse]

# Platform-specific FUSE library:
# macOS: Install macFUSE from https://osxfuse.github.io/
# Linux: sudo apt-get install fuse3  # or equivalent for your distro
```

### Quick Start

```bash
# Mount Nexus to local path (smart mode by default)
nexus mount /mnt/nexus

# Now use ANY standard Unix tools!
ls -la /mnt/nexus/workspace/
cat /mnt/nexus/workspace/notes.txt
grep -r "TODO" /mnt/nexus/workspace/
find /mnt/nexus -name "*.py"
vim /mnt/nexus/workspace/code.py
git clone /some/repo /mnt/nexus/repos/myproject

# Unmount when done
nexus unmount /mnt/nexus
```

### Quick Start Examples

**Example 1: Default (Explicit Views) - Best for Mixed Workflows**

```bash
# Mount normally
nexus mount /mnt/nexus

# Binary tools work directly
evince /mnt/nexus/docs/report.pdf     # PDF viewer works ✓

# Add .txt for text operations
cat /mnt/nexus/docs/report.pdf.txt    # Read as text
grep "results" /mnt/nexus/docs/*.pdf.txt

# Virtual views auto-generated
ls /mnt/nexus/docs/
# → report.pdf
# → report.pdf.txt  (virtual)
# → report.pdf.md   (virtual)
```

**Example 2: Auto-Parse - Best for Search-Heavy Workflows**

```bash
# Mount with auto-parse
nexus mount /mnt/nexus --auto-parse

# grep works directly on PDFs!
grep "results" /mnt/nexus/docs/*.pdf      # No .txt needed! ✓
cat /mnt/nexus/docs/report.pdf            # Returns text ✓

# Search across everything
grep -r "TODO" /mnt/nexus/workspace/      # Searches PDFs, Excel, etc.

# Binary via .raw/ when needed
evince /mnt/nexus/.raw/docs/report.pdf   # For PDF viewer
```

**Example 3: Real-World Script**

```bash
#!/bin/bash
# Find all PDFs mentioning "invoice"

# Mount in background - command returns immediately!
nexus mount /mnt/nexus --auto-parse --daemon
# (No blocking - script continues immediately)

# Mount is ready - grep works on PDFs!
grep -l "invoice" /mnt/nexus/documents/*.pdf

# Process results
for pdf in $(grep -l "invoice" /mnt/nexus/documents/*.pdf); do
    echo "Found in: $pdf"
    grep -n "invoice" "$pdf" | head -5
done

# Clean up
nexus unmount /mnt/nexus
```

**Remote server example:**

```bash
#!/bin/bash
# Search PDFs on remote Nexus server

# Mount remote server in background
nexus mount /mnt/nexus \
  --remote-url http://nexus-server:8080 \
  --auto-parse \
  --daemon

# Command returns immediately - daemon process runs in background
# You can now use standard Unix tools on remote filesystem!

# Search across remote PDFs
grep -r "TODO" /mnt/nexus/workspace/ | head -20

# Find large files
find /mnt/nexus -type f -size +10M

# Clean up when done
nexus unmount /mnt/nexus
```

### File Access: Two Modes

Nexus supports **two ways** to access files - choose what fits your workflow:

#### 1. Explicit Views (Default) - Best for Compatibility

Binary files return binary, use `.txt`/`.md` suffixes for parsed content:

```bash
nexus mount /mnt/nexus

# Binary files work with native tools
evince /mnt/nexus/docs/report.pdf      # PDF viewer gets binary ✓
libreoffice /mnt/nexus/data/sheet.xlsx # Excel app gets binary ✓

# Add .txt to search/read as text
cat /mnt/nexus/docs/report.pdf.txt     # Returns parsed text
grep "pattern" /mnt/nexus/docs/*.pdf.txt

# Virtual views appear automatically
ls /mnt/nexus/docs/
# → report.pdf
# → report.pdf.txt  (virtual view)
# → report.pdf.md   (virtual view)
```

**When to use:** You want both binary tools AND text search to work

#### 2. Auto-Parse Mode - Best for Search/Grep

Binary files return parsed text directly, use `.raw/` for binary:

```bash
nexus mount /mnt/nexus --auto-parse

# Binary files return text directly - perfect for grep!
cat /mnt/nexus/docs/report.pdf         # Returns parsed text ✓
grep "pattern" /mnt/nexus/docs/*.pdf   # Works directly! ✓
less /mnt/nexus/docs/report.pdf        # Page through text ✓

# Access binary via .raw/ when needed
evince /mnt/nexus/.raw/docs/report.pdf # PDF viewer gets binary

# No .txt/.md suffixes - files return text by default
ls /mnt/nexus/docs/
# → report.pdf  (returns text when read)
```

**When to use:** Text search is your primary use case, binary tools are secondary

### Mount Modes (Content Parsing)

Control **what** gets parsed:

```bash
# Smart mode (default) - Auto-detect file types
nexus mount /mnt/nexus --mode=smart
# ✅ PDFs, Excel, Word → parsed
# ✅ .py, .txt, .md → pass-through
# ✅ Best for mixed content

# Text mode - Parse everything aggressively
nexus mount /mnt/nexus --mode=text
# ✅ All files parsed to text
# ⚠️  Slower (always parses)

# Binary mode - No parsing at all
nexus mount /mnt/nexus --mode=binary
# ✅ All files return binary
# ❌ grep won't work on PDFs
```

### Comparison Table

| Feature | Explicit Views (default) | Auto-Parse Mode (`--auto-parse`) |
|---------|-------------------------|-----------------------------------|
| **PDF viewers work** | ✅ `evince file.pdf` | ⚠️  `evince .raw/file.pdf` |
| **grep on PDFs** | ⚠️  `grep *.pdf.txt` | ✅ `grep *.pdf` |
| **Excel apps work** | ✅ `libreoffice file.xlsx` | ⚠️  `libreoffice .raw/file.xlsx` |
| **Best for** | Binary tools + search | Text search primary use case |
| **Virtual views** | `.txt`, `.md` suffixes | No suffixes needed |
| **Binary access** | Direct (`file.pdf`) | Via `.raw/` directory |

### Background (Daemon) Mode

Run the mount in the background and return to your shell immediately:

```bash
# Mount in background - command returns immediately
nexus mount /mnt/nexus --daemon
# ✓ Mounted Nexus to /mnt/nexus
#
# To unmount:
#   nexus unmount /mnt/nexus
#
# (Shell prompt returns immediately, mount runs in background)

# Mount is active - you can use it immediately
ls /mnt/nexus
cat /mnt/nexus/workspace/file.txt

# Check daemon status
ps aux | grep "nexus mount" | grep -v grep
# jinjingzhou  43097  ... nexus mount /mnt/nexus --daemon

# Later, unmount when done
nexus unmount /mnt/nexus
```

**How it works:**
- Command returns to shell immediately (using double-fork technique)
- Background daemon process keeps mount active
- Daemon survives terminal close and persists until unmount
- Safe to close your terminal - mount stays active

**Local Mount:**
```bash
# Mount local Nexus data in background
nexus mount /mnt/nexus --daemon
```

**Remote Mount:**
```bash
# Mount remote Nexus server in background
nexus mount /mnt/nexus --remote-url http://your-server:8080 --daemon

# With API key authentication
nexus mount /mnt/nexus \
  --remote-url http://your-server:8080 \
  --api-key your-secret-key \
  --daemon
```

### Performance & Caching (v0.2.0)

FUSE mounts include automatic caching for improved performance. Caching is **enabled by default** with sensible defaults - no configuration needed for most users.

**Default Performance:**
- ✅ Attribute caching (1024 entries, 60s TTL) - Makes `ls` and `stat` operations faster
- ✅ Content caching (100 files) - Speeds up repeated file reads
- ✅ Parsed content caching (50 files) - Accelerates PDF/Excel text extraction
- ✅ Automatic cache invalidation on writes/deletes - Always consistent

**Advanced: Custom Cache Configuration**

For power users with specific performance requirements:

```python
from nexus import connect
from nexus.fuse import mount_nexus

nx = connect(config={"data_dir": "./nexus-data"})

# Custom cache configuration
cache_config = {
    "attr_cache_size": 2048,      # Double the attribute cache (default: 1024)
    "attr_cache_ttl": 120,         # Cache attributes for 2 minutes (default: 60s)
    "content_cache_size": 200,     # Cache 200 files (default: 100)
    "parsed_cache_size": 100,      # Cache 100 parsed files (default: 50)
    "enable_metrics": True         # Track cache hit/miss rates (default: False)
}

fuse = mount_nexus(
    nx,
    "/mnt/nexus",
    mode="smart",
    cache_config=cache_config,
    foreground=False
)

# View cache performance (if metrics enabled)
# Note: Access via fuse.fuse.operations.cache
```

**Cache Configuration Options:**

| Option | Default | Description |
|--------|---------|-------------|
| `attr_cache_size` | 1024 | Max number of cached file attribute entries |
| `attr_cache_ttl` | 60 | Time-to-live for attributes in seconds |
| `content_cache_size` | 100 | Max number of cached file contents |
| `parsed_cache_size` | 50 | Max number of cached parsed contents (PDFs, etc.) |
| `enable_metrics` | False | Enable cache hit/miss tracking |

**When to Tune Cache Settings:**

- **Large directory listings**: Increase `attr_cache_size` to 2048+ and `attr_cache_ttl` to 120+
- **Many small files**: Increase `content_cache_size` to 500+
- **Heavy PDF/Excel use**: Increase `parsed_cache_size` to 200+
- **Performance analysis**: Enable `enable_metrics` to measure cache effectiveness
- **Memory-constrained**: Decrease all cache sizes (e.g., 512 / 50 / 25)

**Notes:**
- Caches are **thread-safe** - safe for concurrent access
- Caches are **automatically invalidated** on file writes, deletes, and renames
- Default settings work well for most use cases - tune only if needed

### Troubleshooting FUSE Mounts

#### Check Mount Status

```bash
# Check if daemon process is running
ps aux | grep "nexus mount" | grep -v grep

# Check mount points
mount | grep nexus

# List files in mount point (should show files, not empty)
ls -la /mnt/nexus/
```

#### Common Issues

**Mount appears empty or shows "Transport endpoint is not connected":**
```bash
# Unmount the stale mount point
nexus unmount /mnt/nexus

# Or force unmount (macOS)
umount -f /mnt/nexus

# Or force unmount (Linux)
fusermount -u /mnt/nexus

# Then remount
nexus mount /mnt/nexus --daemon
```

**Process won't die (stuck in 'D' or 'U' state):**
```bash
# Find stuck processes
ps aux | grep nexus | grep -E "D|U"

# Force kill
kill -9 <PID>

# If process is still stuck (uninterruptible I/O), try:
# macOS: umount -f /mnt/nexus
# Linux: fusermount -uz /mnt/nexus

# Note: Stuck processes in 'D' state typically resolve after unmount
# If they persist, they'll be cleaned up on system reboot
```

**"Directory not empty" error when mounting:**
```bash
# Unmount first
nexus unmount /mnt/nexus

# Or remove and recreate the mount point
rm -rf /mnt/nexus && mkdir /mnt/nexus

# Then mount
nexus mount /mnt/nexus --daemon
```

**Permission denied errors:**
```bash
# Ensure FUSE is installed
# macOS: Install macFUSE from https://osxfuse.github.io/
# Linux: sudo apt-get install fuse3

# Check mount point permissions
ls -ld /mnt/nexus
# Should be owned by your user

# Create mount point with correct permissions
mkdir -p /mnt/nexus
chmod 755 /mnt/nexus
```

**Connection refused (remote mounts):**
```bash
# Check server is running
curl http://your-server:8080/health

# Test connectivity
ping your-server

# Verify API key (if required)
nexus mount /mnt/nexus \
  --remote-url http://your-server:8080 \
  --api-key your-key \
  --daemon
```

**Multiple mounts to same mount point:**
```bash
# Check for existing mounts
mount | grep /mnt/nexus

# Unmount all instances
nexus unmount /mnt/nexus

# Kill any lingering processes
pkill -f "nexus mount /mnt/nexus"

# Clean mount and remount
rm -rf /mnt/nexus && mkdir /mnt/nexus
nexus mount /mnt/nexus --daemon
```

#### Debug Mode

For detailed debugging output:

```bash
# Run in foreground with debug output
nexus mount /mnt/nexus --debug

# This will show all FUSE operations in real-time
# Press Ctrl+C to stop
```

### rclone-style CLI Commands (v0.2.0)

Nexus provides efficient file operations inspired by rclone, with automatic deduplication and progress tracking:

#### Sync Command
One-way synchronization with hash-based change detection:

```bash
# Sync local directory to Nexus (only copies changed files)
nexus sync ./local/dataset/ /workspace/training/

# Preview changes before syncing (dry-run)
nexus sync ./data/ /workspace/backup/ --dry-run

# Mirror sync - delete extra files in destination
nexus sync /workspace/source/ /workspace/dest/ --delete

# Disable hash comparison (force copy all files)
nexus sync ./data/ /workspace/ --no-checksum
```

#### Copy Command
Smart copy with automatic deduplication:

```bash
# Copy directory recursively (skips identical files)
nexus copy ./local/data/ /workspace/project/ --recursive

# Copy within Nexus (leverages CAS deduplication)
nexus copy /workspace/source/ /workspace/dest/ --recursive

# Copy Nexus to local
nexus copy /workspace/data/ ./backup/ --recursive

# Copy single file
nexus copy /workspace/file.txt /workspace/copy.txt

# Disable checksum verification
nexus copy ./data/ /workspace/ --recursive --no-checksum
```

#### Move Command
Efficient file/directory moves with confirmation prompts:

```bash
# Move file (rename if possible, copy+delete otherwise)
nexus move /workspace/old.txt /workspace/new.txt

# Move directory without confirmation
nexus move /workspace/old_dir/ /archives/2024/ --force
```

#### Tree Command
Visualize directory structure as ASCII tree:

```bash
# Show full directory tree
nexus tree /workspace/

# Limit depth to 2 levels
nexus tree /workspace/ -L 2

# Show file sizes
nexus tree /workspace/ --show-size
```

#### Size Command
Calculate directory sizes with human-readable output:

```bash
# Calculate total size
nexus size /workspace/project/

# Human-readable output (KB, MB, GB)
nexus size /workspace/ --human

# Show top 10 largest files
nexus size /workspace/ --human --details
```

**Features:**
- **Hash-based deduplication** - Only copies changed files
- **Progress bars** - Visual feedback for long operations
- **Dry-run mode** - Preview changes before execution
- **Cross-platform paths** - Works with local filesystem and Nexus paths
- **Automatic deduplication** - Leverages Content-Addressable Storage (CAS)

### Performance Comparison

| Method | Speed | Content-Aware | Use Case |
|--------|-------|---------------|----------|
| `grep -r /mnt/nexus/` | Medium | ✅ Yes (via mount) | Interactive use |
| `nexus grep "pattern"` | **Fast** (DB-backed) | ✅ Yes | Large-scale search |
| Standard tools | Familiar | ✅ Yes (via mount) | Day-to-day work |

### Use Cases

**Interactive Development**:
```bash
# Mount for interactive work
nexus mount /mnt/nexus
vim /mnt/nexus/workspace/code.py
git clone /mnt/nexus/repos/myproject
```

**Bulk Operations**:
```bash
# Use rclone-style commands for efficiency
nexus sync /local/dataset/ /workspace/training-data/
nexus tree /workspace/ > structure.txt
```

**Automated Workflows**:
```bash
# Standard Unix tools in scripts
find /mnt/nexus -name "*.pdf" -exec grep -l "invoice" {} \;
rsync -av /mnt/nexus/workspace/ /backup/
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

# Works in both local and hosted modes
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

### Prompt Optimization (Coming in v0.9.5)

```python
# Track multiple prompt candidates during optimization
async with nexus.connect() as nx:
    # Start optimization run
    run_id = await nx.start_optimization_run(
        module_name="SearchModule",
        objectives=["accuracy", "latency", "cost"]
    )

    # Store prompt candidates with detailed traces
    for candidate in prompt_variants:
        version_id = await nx.store_prompt_version(
            module_name="SearchModule",
            prompt_template=candidate.template,
            metrics={"accuracy": 0.85, "latency_ms": 450},
            run_id=run_id
        )

        # Store execution traces for debugging
        await nx.store_execution_trace(
            prompt_version_id=version_id,
            inputs=test_inputs,
            outputs=predictions,
            intermediate_steps=reasoning_chain
        )

    # Analyze tradeoffs across candidates
    analysis = await nx.analyze_prompt_tradeoffs(
        run_id=run_id,
        objectives=["accuracy", "latency_ms", "cost_per_query"]
    )

    # Get per-example results to find failure patterns
    failures = await nx.get_failing_examples(
        prompt_version_id=version_id,
        limit=20
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

### Skills System (v0.3.0)

Manage reusable AI agent skills with SKILL.md format, progressive disclosure, and dependency resolution:

```python
from nexus.skills import SkillRegistry, SkillExporter

# Initialize filesystem
nx = nexus.connect()

# Create skill registry
registry = SkillRegistry(nx)

# Discover skills from three tiers (agent > tenant > system)
# Loads metadata only - lightweight and fast
await registry.discover()

# List available skills
skills = registry.list_skills()
# ['analyze-code', 'data-processing', 'report-generation']

# Get skill metadata (no content loading)
metadata = registry.get_metadata("analyze-code")
print(f"{metadata.name}: {metadata.description}")
# analyze-code: Analyzes code quality and structure

# Load full skill content (lazy loading + caching)
skill = await registry.get_skill("analyze-code")
print(skill.content)  # Full markdown content

# Resolve dependencies automatically (DAG with cycle detection)
deps = await registry.resolve_dependencies("complex-skill")
# ['base-skill', 'helper-skill', 'complex-skill']

# Export skills to .zip (vendor-neutral)
exporter = SkillExporter(registry)

# Export with dependencies
await exporter.export_skill(
    "analyze-code",
    output_path="analyze-code.zip",
    format="claude",  # Enforces 8MB limit
    include_dependencies=True
)

# Validate before export
valid, msg, size = await exporter.validate_export("large-skill", format="claude")
if not valid:
    print(f"Cannot export: {msg}")
```

**SKILL.md Format:**

```markdown
---
name: analyze-code
description: Analyzes code quality and structure
version: 1.0.0
author: Your Name
requires:
  - base-parser
  - ast-analyzer
---

# Code Analysis Skill

This skill analyzes code for quality metrics...

## Usage

1. Parse the code files
2. Run static analysis
3. Generate report
```

**Features:**
- **Progressive Disclosure**: Load metadata during discovery, full content on-demand
- **Lazy Loading**: Skills cached only when accessed
- **Three-Tier Hierarchy**: Agent skills override tenant/system skills
- **Dependency Resolution**: Automatic DAG resolution with cycle detection
- **Vendor-Neutral Export**: Generic .zip format with Claude/OpenAI validation
- **85-89% Test Coverage**: 74 passing tests

**Skill Tiers:**
- **Agent** (`/workspace/.nexus/skills/`) - Personal skills (highest priority)
- **Tenant** (`/shared/skills/`) - Team-shared skills
- **System** (`/system/skills/`) - Built-in skills (lowest priority)

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
| Prompt Versioning | Full lineage | Track optimization history |
| Training Data Dedup | 30-50% | CAS-based deduplication |
| Prompt Optimization | Multi-candidate | Test multiple strategies in parallel |
| Trace Storage | Full execution logs | Debug failures, analyze patterns |

## Configuration

### Local Mode

```python
import nexus

# Config via Python (useful for programmatic configuration)
nx = nexus.connect(config={
    "mode": "local",
    "data_dir": "./nexus-data",
    "cache_size_mb": 100,
    "enable_vector_search": True
})

# Or let it auto-discover from nexus.yaml
nx = nexus.connect()
```

### Self-Hosted Deployment

For organizations that want to run their own Nexus instance, create `config.yaml`:

```yaml
mode: server  # local or server

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
3. **Type-Level Validation**: Fail-fast validation before database operations
4. **UNIX-Style Permissions**: Owner, group, and mode bits (v0.3.0)
5. **ACL Permissions**: Fine-grained access control lists (v0.3.0)
6. **ReBAC (Relationship-Based Access Control)**: Zanzibar-style authorization (v0.3.0)

### Type-Level Validation (NEW in v0.1.0)

All domain types have validation methods that are called automatically before database operations. This provides:

- **Fail Fast**: Catch invalid data before expensive database operations
- **Clear Error Messages**: Actionable feedback for developers and API consumers
- **Data Integrity**: Prevent invalid data from entering the database
- **Consistent Validation**: Same rules across all code paths

```python
from nexus.core.metadata import FileMetadata
from nexus.core.exceptions import ValidationError

# Validation happens automatically on put()
try:
    metadata = FileMetadata(
        path="/data/file.txt",  # Must start with /
        backend_name="local",
        physical_path="/storage/file.txt",
        size=1024,  # Must be >= 0
    )
    store.put(metadata)  # Validates before DB operation
except ValidationError as e:
    print(f"Validation failed: {e}")
    # Example: "size cannot be negative, got -1"
```

**Validation Rules:**
- Paths must start with `/` and not contain null bytes
- File sizes and ref counts must be non-negative
- Required fields (path, backend_name, physical_path, etc.) must not be empty
- Content hashes must be valid 64-character SHA-256 hex strings
- Metadata keys must be ≤ 255 characters

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

### v0.1.0 - Local Mode Foundation (Current)
- [x] Core embedded filesystem (read/write/delete)
- [x] SQLite metadata store
- [x] Local filesystem backend
- [x] Basic file operations (list, glob, grep)
- [x] Virtual path routing
- [x] Directory operations (mkdir, rmdir, is_directory)
- [x] Basic CLI interface with Click and Rich
- [x] Metadata export/import (JSONL format)
- [x] SQL views for ready work detection
- [x] In-memory caching
- [x] Batch operations (avoid N+1 queries)
- [x] Type-level validation

### v0.2.0 - FUSE Mount & Content-Aware Operations (Current)
- [x] **FUSE filesystem mount** - Mount Nexus to local path (e.g., `/mnt/nexus`)
- [x] **Smart read mode** - Return parsed text for binary files (PDFs, Excel, etc.)
- [x] **Virtual file views** - Auto-generate `.txt` and `.md` views for binary files
- [x] **Content parser framework** - Extensible parser system for document types (MarkItDown)
- [x] **PDF parser** - Extract text and markdown from PDFs
- [x] **Excel/CSV parser** - Parse spreadsheets to structured data
- [x] **Content-aware file access** - Access parsed content via virtual views
- [x] **Document type detection** - Auto-detect MIME types and route to parsers
- [x] **Mount CLI commands** - `nexus mount`, `nexus unmount`
- [x] **Mount modes** - Binary, text, and smart modes
- [x] **.raw directory** - Access original binary files
- [x] **Background daemon mode** - Run mount in background with `--daemon`
- [x] **All FUSE operations** - read, write, create, delete, mkdir, rmdir, rename, truncate
- [x] **Unit tests** - Comprehensive test coverage for FUSE operations
- [x] **rclone-style CLI commands** - `sync`, `copy`, `move`, `tree`, `size` with progress bars
- [ ] **Background parsing** - Async content parsing on write
- [x] **FUSE performance optimizations** - Caching (TTL/LRU), cache invalidation, metrics
- [ ] **Image OCR parser** - Extract text from images (PNG, JPEG)

### v0.3.0 - File Permissions & Skills System

**Permissions (Complete):**
- [x] **UNIX-style file permissions** (owner, group, mode)
- [x] **Permission operations** (chmod, chown, chgrp)
- [x] **ACL (Access Control List)** support
- [x] **CLI commands** (getfacl, setfacl)
- [x] **Database schema** for permissions and ACL entries
- [x] **Comprehensive tests** (91 passing tests)
- [x] **ReBAC (Relationship-Based Access Control)** - Zanzibar-style authorization
- [x] **Relationship types** - member-of, owner-of, viewer-of, editor-of, parent-of
- [x] **Permission inheritance via relationships** - Team ownership, group membership
- [x] **Relationship graph queries** - Graph traversal with cycle detection
- [x] **Namespaced tuples** - (subject, relation, object) authorization model
- [x] **Check API** - Fast permission checks with 5-minute TTL caching
- [x] **Expand API** - Discover all subjects with specific permissions
- [x] **Relationship management** - Create, delete, query relationships via CLI
- [x] **Expiring tuples** - Temporary permissions with automatic cleanup
- [x] **Comprehensive ReBAC tests** (14 passing tests, 100% pass rate)

**Permissions (Remaining):**
- [ ] **Default permission policies** per namespace
- [ ] **Permission inheritance** for new files
- [ ] **Permission checking** in all file operations
- [ ] **Permission migration** for existing files

**Skills System (Core - Vendor Neutral):**
- [x] **SKILL.md parser** - Parse Anthropic-compatible SKILL.md with frontmatter
- [x] **Skill registry** - Progressive disclosure, lazy loading, three-tier hierarchy
- [x] **Skill discovery** - Scan `/workspace/.nexus/skills/`, `/shared/skills/`, `/system/skills/`
- [x] **Dependency resolution** - Automatic DAG resolution with cycle detection
- [x] **Skill export** - Export to generic formats (validate, pack, size check)
- [x] **Comprehensive tests** - 74 passing tests (85-89% coverage)
- [ ] **Skill templates** - Pre-built templates for common skill patterns
- [ ] **Skill versioning** - CAS-backed version control with history tracking
- [ ] **Skill lifecycle** - Create, fork, publish workflows
- [ ] **Skill analytics** - Usage tracking, success rates, execution time
- [ ] **Semantic skill search** - Vector search across skill descriptions
- [ ] **CLI commands** - `list`, `create`, `fork`, `publish`, `search`, `info`, `analytics` (see issue #88)

**Note**: External integrations (Claude API upload/download, OpenAI, etc.) will be implemented as **plugins** in v0.3.5+ to maintain vendor neutrality. Core Nexus provides generic skill export (`nexus skills export --format claude`), while `nexus-plugin-anthropic` handles API-specific operations.

### v0.3.5 - Plugin System & External Integrations
- [ ] **Plugin discovery** - Entry point-based plugin discovery
- [ ] **Plugin registry** - Register and manage installed plugins
- [ ] **Plugin CLI namespace** - `nexus <plugin-name> <command>` pattern
- [ ] **Plugin hooks** - Lifecycle hooks (before_write, after_read, etc.)
- [ ] **Plugin configuration** - Per-plugin config in `~/.nexus/plugins/<name>/`
- [ ] **Plugin manager** - `nexus plugins list/install/uninstall/info`
- [ ] **First-party plugins:**
  - [ ] `nexus-plugin-anthropic` - Claude API integration (upload/download skills)
  - [ ] `nexus-plugin-openai` - OpenAI API integration
  - [ ] `nexus-plugin-skill-seekers` - Integration with Skill_Seekers scraper

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

### v0.6.0 - Server Mode (Self-Hosted & Managed)
- [ ] FastAPI REST API
- [ ] API key authentication
- [ ] Multi-tenancy support
- [ ] PostgreSQL support
- [ ] Redis caching
- [ ] Docker deployment
- [ ] Batch/transaction APIs (atomic multi-operation updates)
- [ ] Optimistic locking for concurrent writes
- [ ] Auto-scaling configuration (for hosted deployments)

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

### v0.9.5 - Prompt Engineering & Optimization
- [ ] Prompt version control with lineage tracking
- [ ] Training dataset storage with CAS deduplication
- [ ] Evaluation metrics time series (performance tracking)
- [ ] Frozen inference snapshots (immutable program state)
- [ ] Experiment tracking export (MLflow, W&B integration)
- [ ] Prompt diff viewer (compare versions)
- [ ] Regression detection alerts (performance drops)
- [ ] Multi-candidate pool management (concurrent prompt testing)
- [ ] Execution trace storage (detailed run logs for debugging)
- [ ] Per-example evaluation results (granular performance tracking)
- [ ] Optimization run grouping (experiment management)
- [ ] Multi-objective tradeoff analysis (accuracy vs latency vs cost)

### v0.10.0 - Production Infrastructure & Auto-Scaling
- [ ] Automatic infrastructure scaling
- [ ] Redis distributed locks (for large deployments)
- [ ] PostgreSQL replication (for high availability)
- [ ] Kubernetes deployment templates
- [ ] Multi-region load balancing
- [ ] Automatic migration from single-node to distributed

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
