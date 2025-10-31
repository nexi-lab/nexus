# CLI Reference

← [API Documentation](../README.md)

The Nexus command-line interface provides comprehensive access to all Nexus operations. The CLI mirrors the Python API and is ideal for shell scripts, automation, and interactive use.

## Quick Start

```bash
# Install Nexus
pip install nexus-ai-fs

# Verify installation
nexus --version

# Get help
nexus --help
nexus <command> --help
```

## Command Categories

### File Operations
Commands for reading, writing, and manipulating files.

- [`write`](file-operations.md#write---write-file-content) - Write file content
- [`cat`](file-operations.md#cat---display-file-contents) - Display file contents
- [`rm`](file-operations.md#rm---delete-file) - Delete files
- [`cp/copy`](file-operations.md#cpcopy---copy-files) - Copy files
- [`move`](file-operations.md#move---move-files) - Move/rename files

[**→ Full File Operations Reference**](file-operations.md)

### Directory Operations
Commands for managing directories and listing files.

- [`mkdir`](directory-operations.md#mkdir---create-directory) - Create directories
- [`rmdir`](directory-operations.md#rmdir---remove-directory) - Remove directories
- [`ls`](directory-operations.md#ls---list-files) - List directory contents
- [`tree`](directory-operations.md#tree---display-directory-tree) - Display directory tree

[**→ Full Directory Operations Reference**](directory-operations.md)

### Search Operations
Commands for finding and searching files.

- [`glob`](search.md#glob---find-files-by-pattern) - Find files by pattern
- [`grep`](search.md#grep---search-file-contents) - Search file contents

[**→ Full Search Operations Reference**](search.md)

### Version Tracking
Commands for version history and time-travel.

- [`versions history`](versioning.md#versions-history---show-version-history) - Show version history
- [`versions get`](versioning.md#versions-get---get-specific-version) - Get specific version
- [`versions diff`](versioning.md#versions-diff---compare-versions) - Compare versions
- [`versions rollback`](versioning.md#versions-rollback---rollback-to-version) - Rollback to version

[**→ Full Versioning Reference**](versioning.md)

### Workspace Management
Commands for workspace snapshots and management.

- [`workspace register`](workspace.md#workspace-register---register-workspace) - Register workspace
- [`workspace list`](workspace.md#workspace-list---list-workspaces) - List workspaces
- [`workspace snapshot`](workspace.md#workspace-snapshot---create-snapshot) - Create snapshot
- [`workspace log`](workspace.md#workspace-log---show-snapshot-history) - Show snapshot history
- [`workspace restore`](workspace.md#workspace-restore---restore-snapshot) - Restore snapshot
- [`workspace diff`](workspace.md#workspace-diff---compare-snapshots) - Compare snapshots

[**→ Full Workspace Reference**](workspace.md)

### Memory Management
Commands for memory storage and retrieval.

- [`memory register`](memory.md#memory-register---register-memory) - Register memory
- [`memory list-registered`](memory.md#memory-list-registered---list-memories) - List memories
- [`memory store`](memory.md#memory-store---store-memory) - Store memory entry
- [`memory search`](memory.md#memory-search---semantic-search-memories) - Search memories

[**→ Full Memory Reference**](memory.md)

### Semantic Search
Commands for AI-powered semantic search.

- [`search init`](semantic-search.md#search-init---initialize-semantic-search) - Initialize semantic search
- [`search index`](semantic-search.md#search-index---index-documents) - Index documents
- [`search query`](semantic-search.md#search-query---search-documents) - Search documents
- [`search stats`](semantic-search.md#search-stats---show-statistics) - Show statistics

[**→ Full Semantic Search Reference**](semantic-search.md)

### LLM Document Reading
Commands for AI-powered document question answering.

- [`llm read`](llm-reading.md#llm-read---ask-questions-about-documents) - Ask questions about documents

[**→ Full LLM Document Reading Reference**](llm-reading.md)

### Permissions (ReBAC)
Commands for relationship-based access control.

- [`rebac create`](permissions.md#rebac-create---create-relationship) - Create relationship
- [`rebac check`](permissions.md#rebac-check---check-permission) - Check permission
- [`rebac explain`](permissions.md#rebac-explain---explain-permission-check) - Explain permission
- [`rebac expand`](permissions.md#rebac-expand---find-all-subjects-with-permission) - Find subjects with permission
- [`rebac namespace-*`](permissions.md#rebac-namespace-create---create-custom-namespace) - Manage namespaces

[**→ Full Permissions Reference**](permissions.md)

### Backend Mounts
Commands for managing storage backend mounts.

- [`mounts list`](mounts.md#mounts-list---list-mounts) - List mounts
- [`mounts add`](mounts.md#mounts-add---add-mount) - Add mount
- [`mounts info`](mounts.md#mounts-info---show-mount-info) - Show mount info
- [`mounts remove`](mounts.md#mounts-remove---remove-mount) - Remove mount

[**→ Full Mounts Reference**](mounts.md)

### Server & Mounting
Commands for server management and FUSE mounting.

- [`serve`](server.md#serve---start-rpc-server) - Start RPC server
- [`mount`](server.md#mount---mount-as-filesystem) - Mount as FUSE filesystem
- [`unmount`](server.md#unmount---unmount-filesystem) - Unmount filesystem

[**→ Full Server Reference**](server.md)

### Model Context Protocol (MCP)
Commands for MCP server to integrate with AI agents and tools.

- [`mcp serve`](mcp.md#mcp-serve---start-mcp-server) - Start MCP server for Claude Desktop and other MCP clients

[**→ Full MCP Reference**](mcp.md)

### Advanced Operations
Advanced commands for operation tracking, analysis, and sync.

- [`ops log`](advanced.md#ops-log---view-operation-history) - View operation history
- [`undo`](advanced.md#undo---undo-last-operation) - Undo last operation
- [`size`](advanced.md#size---calculate-size) - Calculate directory size
- [`find-duplicates`](advanced.md#find-duplicates---find-duplicate-files) - Find duplicate files
- [`sync`](advanced.md#sync---one-way-sync) - One-way sync

[**→ Full Advanced Operations Reference**](advanced.md)

---

## Global Options

Most commands support these global options:

### Configuration
- `--config PATH` - Path to Nexus config file (nexus.yaml)
- `--data-dir PATH` - Path to Nexus data directory (or use `NEXUS_DATA_DIR` env var)
- `--backend [local|gcs]` - Backend type (default: local)

### Remote Mode
- `--remote-url URL` - Remote Nexus server URL (or use `NEXUS_URL` env var)
- `--remote-api-key KEY` - API key for authentication (or use `NEXUS_API_KEY` env var)

### Multi-Tenancy & Identity
- `--tenant-id TEXT` - Tenant ID for multi-tenant isolation (or use `NEXUS_TENANT_ID` env var)
- `--subject TEXT` - Subject in format 'type:id' (e.g., 'user:alice') (or use `NEXUS_SUBJECT` env var)

### Admin Operations
- `--is-admin` - Run operation with admin privileges
- `--admin-capability TEXT` - Grant specific admin capability (can specify multiple times)

### Remote Mode - Two Ways to Connect

**1. Environment Variables (Recommended - Works for ALL commands):**
```bash
export NEXUS_URL=http://localhost:8765
export NEXUS_API_KEY=your-api-key
# Now all commands use remote mode
nexus ls /
nexus write /file.txt "data"
```

**2. Command-line Flags (Works for most commands):**
```bash
nexus --remote-url http://localhost:8765 --remote-api-key key ls /
```

**Note:** Memory commands and some workspace commands only support remote mode via `NEXUS_URL` environment variable.

---

## Environment Variables

Key environment variables for configuration:

### Remote Mode (takes priority over local)
```bash
export NEXUS_URL=http://localhost:8765
export NEXUS_API_KEY=your-api-key
```

### Local Mode
```bash
export NEXUS_DATA_DIR=/path/to/data
```

### Multi-Tenancy & Identity
```bash
export NEXUS_TENANT_ID=org_acme
export NEXUS_SUBJECT=user:alice
```

### Database
```bash
export NEXUS_DATABASE_URL=postgresql://user:pass@localhost/nexus
```

### GCS Configuration
```bash
export GCS_PROJECT_ID=my-project
export GCS_BUCKET_NAME=my-bucket
```

**Priority:** If `NEXUS_URL` is set, all commands use remote mode. Otherwise, commands use local mode with `NEXUS_DATA_DIR`.

---

## Configuration File

Create `nexus.yaml` for persistent configuration:

```yaml
# Backend configuration
backend:
  type: local
  data_dir: ./nexus-data

# Or use GCS
backend:
  type: gcs
  gcs_bucket: my-bucket
  gcs_project: my-project

# Multi-tenant
tenant_id: org_acme

# Subject identity
subject: user:alice

# Permissions
enforce_permissions: true

# Database (optional)
database_url: postgresql://user:pass@localhost/nexus
```

Use it:
```bash
nexus --config nexus.yaml ls /workspace
```

---

## CLI vs Python API

Every CLI command has a Python API equivalent:

| CLI Command | Python API |
|-------------|-----------|
| `nexus write /file.txt "data"` | `nx.write("/file.txt", b"data")` |
| `nexus cat /file.txt` | `nx.read("/file.txt")` |
| `nexus ls /workspace` | `nx.list("/workspace")` |
| `nexus glob "*.py"` | `nx.glob("*.py")` |
| `nexus grep "TODO"` | `nx.grep("TODO")` |
| `nexus versions history /file.txt` | `nx.list_versions("/file.txt")` |
| `nexus workspace snapshot /ws` | `nx.workspace_snapshot("/ws")` |
| `nexus search query "auth"` | `await nx.semantic_search("auth")` |
| `nexus llm read /doc.pdf "Question"` | `await nx.llm_read("/doc.pdf", "Question")` |

See individual command references for detailed Python API examples.

---

## Common Workflows

### 1. Initialize and use Nexus
```bash
# Set data directory
export NEXUS_DATA_DIR=./my-nexus

# Write files
nexus write /docs/README.md "# My Project"
nexus write /src/main.py "print('hello')"

# List files
nexus ls / --recursive

# Search
nexus grep "hello" /src
```

### 2. Version tracking workflow
```bash
# Write initial version
nexus write /config.json '{"version": "1.0"}'

# Update file
nexus write /config.json '{"version": "2.0"}'

# View history
nexus versions history /config.json

# Rollback
nexus versions rollback /config.json --version 1
```

### 3. Workspace snapshot workflow
```bash
# Register workspace
nexus workspace register /my-project --name main

# Make changes
nexus write /my-project/file.txt "changes"

# Create snapshot
nexus workspace snapshot /my-project --description "Before refactor"

# Make more changes
nexus write /my-project/file.txt "more changes"

# Restore if needed
nexus workspace restore /my-project --snapshot 1
```

### 4. Remote server workflow
```bash
# Terminal 1: Start server
nexus serve --host 0.0.0.0 --port 8765 --api-key secret123

# Terminal 2: Set environment and use remote mode
export NEXUS_URL=http://localhost:8765
export NEXUS_API_KEY=secret123

nexus write /workspace/file.txt "remote data"
nexus cat /workspace/file.txt
nexus memory store "Important fact" --scope user
```

### 5. Multi-backend workflow
```bash
# Use local backend
nexus --backend local ls /

# Use GCS backend
nexus --backend gcs --gcs-bucket my-bucket ls /
```

### 6. LLM document reading workflow
```bash
# Set up API key
export ANTHROPIC_API_KEY=sk-ant-...

# Index documents for semantic search
nexus search init --provider openai
nexus search index /docs

# Ask questions
nexus llm read /docs/**/*.md "How does authentication work?"

# Get detailed output with citations
nexus llm read /reports/q4.pdf "What were the challenges?" --detailed

# Stream long analysis
nexus llm read /data/**/*.csv "Analyze trends" --stream
```

---

## See Also

- [Getting Started](../getting-started.md) - Installation and setup
- [Python API Documentation](../README.md) - Python SDK reference
- [RPC API](../rpc-api.md) - Remote server API
- [Configuration Guide](../configuration.md) - Configuration options
- [Server Setup](../../deployment/server-setup.md) - Production deployment
