# CLI Reference

← [API Documentation](README.md)

This document provides a complete reference for the Nexus command-line interface.

Nexus provides a comprehensive command-line interface for all operations. The CLI mirrors the Python API and is ideal for shell scripts, automation, and interactive use.

### Installation & Setup

```bash
# Install Nexus
pip install nexus-ai-fs

# Verify installation
nexus --version

# Get help
nexus --help
nexus <command> --help
```

### Global Options

Most commands support these global options:

- `--config PATH`: Path to Nexus config file (nexus.yaml) - **All commands**
- `--backend [local|gcs]`: Backend type (default: local) - **Most commands**
- `--data-dir PATH`: Path to Nexus data directory (can also use `NEXUS_DATA_DIR` env var) - **All commands**
- `--remote-url URL`: Remote Nexus server URL (e.g., http://localhost:8080) - **File, directory, search, versions, metadata, rebac, mounts, operations commands**
- `--remote-api-key KEY`: API key for remote authentication - **Same as --remote-url**
- `--tenant-id TEXT`: Tenant ID for multi-tenant isolation (can also use `NEXUS_TENANT_ID` env var)
- `--subject TEXT`: Subject in format 'type:id' (e.g., 'user:alice') (can also use `NEXUS_SUBJECT` env var)
- `--is-admin`: Run operation with admin privileges
- `--admin-capability TEXT`: Grant specific admin capability (can specify multiple times)

**Remote Mode - Two Ways to Connect:**

1. **Environment Variables (Recommended - Works for ALL commands):**
   ```bash
   export NEXUS_URL=http://localhost:8765
   export NEXUS_API_KEY=your-api-key
   # Now all commands use remote mode
   ```

2. **Command-line Flags (Works for most commands):**
   ```bash
   nexus --remote-url http://localhost:8765 --remote-api-key key <command>
   ```

**Note:** Memory commands (`memory store/query/search/list/get/delete`) and workspace commands only support remote mode via `NEXUS_URL` environment variable, not via `--remote-url` flag.

### File Operations

#### write - Write file content

```bash
# Basic write
nexus write /workspace/file.txt "Hello World"

# Write from stdin
echo "Hello World" | nexus write /workspace/file.txt --input -

# Write from file
nexus write /workspace/file.txt --input local_file.txt

# Optimistic concurrency control
nexus write /doc.txt "Updated" --if-match abc123

# Create-only mode
nexus write /new.txt "Initial" --if-none-match

# Show metadata
nexus write /doc.txt "Content" --show-metadata
```

#### append - Append content to file

Append content to an existing file or create a new file. Useful for building log files, JSONL files, and append-only data structures.

```bash
# Append to a log file
nexus append /logs/app.log "New log entry\n"

# Append from stdin (useful for piping)
echo "New line" | nexus append /logs/data.txt --input -

# Append from file
nexus append /logs/output.txt --input input.txt

# Build JSONL file incrementally
echo '{"event": "login", "user": "alice"}' | nexus append /logs/events.jsonl --input -

# Optimistic concurrency control
nexus append /doc.txt "New content" --if-match abc123

# Show metadata after appending
nexus append /log.txt "Entry\n" --show-metadata
```

**Options:**
- `--input`, `-i`: Read from file or stdin (use `-` for stdin)
- `--if-match`: Only append if current ETag matches (prevents conflicts)
- `--force`: Force append without version check (dangerous)
- `--show-metadata`: Display metadata (etag, version, size) after appending

#### cat - Display file contents

```bash
# Read file
nexus cat /workspace/file.txt

# Show metadata (etag, version)
nexus cat /workspace/file.txt --metadata

# Time-travel: Read at historical operation point
nexus cat /workspace/file.txt --at-operation op_abc123
```

#### rm - Delete file

```bash
# Delete with confirmation
nexus rm /workspace/file.txt

# Force delete (no confirmation)
nexus rm /workspace/file.txt --force
```

#### cp/copy - Copy files

```bash
# Simple copy
nexus cp /source.txt /dest.txt

# Smart copy with deduplication
nexus copy /source.txt /dest.txt
```

#### move - Move files

```bash
# Move file
nexus move /old/path.txt /new/path.txt
```

---

### Directory Operations

#### mkdir - Create directory

```bash
# Create directory
nexus mkdir /workspace/data

# Create with parents
nexus mkdir /workspace/deep/nested/dir --parents
```

#### rmdir - Remove directory

```bash
# Remove empty directory
nexus rmdir /workspace/data

# Remove recursively
nexus rmdir /workspace/data --recursive --force
```

#### ls - List files

```bash
# List directory
nexus ls /workspace

# Recursive listing
nexus ls /workspace --recursive

# Detailed listing
nexus ls /workspace --long

# Time-travel: List at historical point
nexus ls /workspace --at-operation op_abc123
```

#### tree - Display directory tree

```bash
# Show tree structure
nexus tree /workspace
```

---

### Search Operations

#### glob - Find files by pattern

```bash
# Find Python files
nexus glob "**/*.py"

# Find in specific path
nexus glob "*.txt" /workspace

# Show details
nexus glob -l "**/*.py"

# Filter by type
nexus glob -t f "**/*"          # Only files
nexus glob -t d "/workspace/*"  # Only directories
```

#### grep - Search file contents

```bash
# Basic search
nexus grep "TODO"

# With line numbers
nexus grep -n "error" /workspace

# Only filenames
nexus grep -l "TODO" .

# Count matches
nexus grep -c "import" **/*.py

# Context lines
nexus grep -A 3 -B 3 "def main"   # 3 lines before/after
nexus grep -C 5 "class"           # 5 lines context

# Case insensitive
nexus grep -i "error"

# Invert match
nexus grep -v "test" file.txt

# Search PDFs (parsed text)
nexus grep "revenue" -f "**/*.pdf" --search-mode=parsed
```

---

### Version Tracking

#### versions history - Show version history

```bash
# Show all versions
nexus versions history /workspace/file.txt

# Limit results
nexus versions history /workspace/file.txt --limit 10
```

#### versions get - Get specific version

```bash
# Get version 2
nexus versions get /workspace/file.txt --version 2
```

#### versions diff - Compare versions

```bash
# Compare versions 1 and 3
nexus versions diff /workspace/file.txt --v1 1 --v2 3

# Show content diff
nexus versions diff /workspace/file.txt --v1 1 --v2 3 --mode content
```

#### versions rollback - Rollback to version

```bash
# Rollback to version 1
nexus versions rollback /workspace/file.txt --version 1
```

---

### Workspace Management

#### workspace register - Register workspace

```bash
# Register workspace
nexus workspace register /my-workspace --name main --description "My workspace"

# With metadata
nexus workspace register /my-workspace --name main --created-by alice
```

#### workspace list - List workspaces

```bash
# List all registered workspaces
nexus workspace list
```

#### workspace info - Show workspace info

```bash
# Get workspace details
nexus workspace info /my-workspace
```

#### workspace snapshot - Create snapshot

```bash
# Create snapshot
nexus workspace snapshot /my-workspace --description "Before refactor"

# With tags
nexus workspace snapshot /my-workspace --description "Stable" --tag stable --tag v1.0
```

#### workspace log - Show snapshot history

```bash
# Show all snapshots
nexus workspace log /my-workspace

# Limit results
nexus workspace log /my-workspace --limit 10
```

#### workspace restore - Restore snapshot

```bash
# Restore to snapshot 5
nexus workspace restore /my-workspace --snapshot 5
```

#### workspace diff - Compare snapshots

```bash
# Compare snapshots
nexus workspace diff /my-workspace --snapshot-1 5 --snapshot-2 7
```

#### workspace unregister - Unregister workspace

```bash
# Unregister (doesn't delete files)
nexus workspace unregister /my-workspace
```

---

### Memory Management

#### memory register - Register memory

```bash
# Register memory
nexus memory register /knowledge-base --name kb --description "Knowledge base"

# With metadata
nexus memory register /kb --name kb --created-by alice
```

#### memory list-registered - List memories

```bash
# List all registered memories
nexus memory list-registered
```

#### memory info - Show memory info

```bash
# Get memory details
nexus memory info /knowledge-base
```

#### memory unregister - Unregister memory

```bash
# Unregister (doesn't delete files)
nexus memory unregister /knowledge-base
```

#### memory store - Store memory

```bash
# Store a memory entry
nexus memory store --content "Important fact" --tags learning,important
```

#### memory search - Semantic search memories

```bash
# Search memories
nexus memory search "authentication flow"
```

---

### Semantic Search

#### search init - Initialize semantic search

```bash
# Initialize with OpenAI
nexus search init --provider openai --model text-embedding-3-small

# Initialize with mock provider (testing)
nexus search init --provider mock
```

#### search index - Index documents

```bash
# Index all documents
nexus search index

# Index specific path
nexus search index /docs

# Index single file
nexus search index /docs/README.md
```

#### search query - Search documents

```bash
# Basic search
nexus search query "How does authentication work?"

# Limit results
nexus search query "database migration" --limit 5

# Search in specific path
nexus search query "API endpoints" --path /docs
```

#### search stats - Show statistics

```bash
# Show indexing stats
nexus search stats
```

---

### LLM Document Reading

AI-powered document question answering with citations and cost tracking.

#### llm read - Ask questions about documents

```bash
# Basic question answering
nexus llm read /reports/q4.pdf "What were the top 3 challenges?"

# Query multiple documents
nexus llm read "/docs/**/*.md" "How does authentication work?"

# Use different model
nexus llm read /report.pdf "Summarize this" --model gpt-4o
nexus llm read /report.pdf "Question" --model anthropic/claude-sonnet-4.5

# Stream response
nexus llm read /long-report.pdf "Analyze trends" --stream

# Get detailed output with citations
nexus llm read /docs/**/*.md "Explain the API" --detailed

# Disable semantic search (read full document)
nexus llm read /report.txt "Summarize" --no-search

# Use keyword search instead of semantic
nexus llm read /docs/**/*.md "API endpoints" --search-mode keyword

# Use with remote server
export NEXUS_URL=http://localhost:8080
export NEXUS_API_KEY=your-api-key
nexus llm read /doc.pdf "Question"
```

**Options:**
- `--model TEXT`: LLM model to use (default: claude-sonnet-4)
- `--max-tokens INTEGER`: Maximum tokens in response (default: 1000)
- `--api-key TEXT`: API key for LLM provider (or set ANTHROPIC_API_KEY/OPENAI_API_KEY/OPENROUTER_API_KEY)
- `--no-search`: Disable semantic search (read entire document)
- `--search-mode [semantic|keyword|hybrid]`: Search mode for context retrieval (default: semantic)
- `--stream`: Stream the response (show output as it's generated)
- `--detailed`: Show detailed output with citations and metadata

**Supported Models:**

**Anthropic Claude** (set `ANTHROPIC_API_KEY`):
- `claude-sonnet-4` - Balanced performance (recommended)
- `claude-opus-4` - Most capable
- `claude-haiku-4` - Fastest and cheapest

**OpenAI GPT** (set `OPENAI_API_KEY`):
- `gpt-4o` - Latest GPT-4
- `gpt-4o-mini` - Faster and cheaper

**OpenRouter** (set `OPENROUTER_API_KEY` - 100+ models):
- `anthropic/claude-sonnet-4.5` - Latest Claude
- `anthropic/claude-haiku-4.5` - Fast Claude
- `openrouter/google/gemini-pro-1.5` - Google Gemini
- See all: https://openrouter.ai/models

**Examples:**

```bash
# Document Q&A
nexus llm read /manual.pdf "How do I configure SSL?"

# Code documentation
nexus llm read "/src/**/*.py" "What design patterns are used?"

# Research analysis
nexus llm read "/papers/**/*.pdf" "Compare methodologies" --detailed

# Report summarization
nexus llm read "/reports/*.txt" "Summarize key metrics" --stream

# Multi-language
nexus llm read /doc.pdf "请用中文总结" --model claude-sonnet-4
```

See [LLM Document Reading API](llm-document-reading.md) for Python SDK usage and advanced examples.

---

### Permissions (ReBAC)

#### rebac create - Create relationship

```bash
# Make alice a member of eng-team
nexus rebac create agent alice member-of group eng-team

# Give alice viewer access to file
nexus rebac create agent alice direct_viewer file file123

# With tenant isolation (via flag)
nexus rebac create agent alice member-of group eng-team --tenant-id org_acme

# With tenant isolation (via environment variable)
export NEXUS_TENANT_ID=org_acme
nexus rebac create agent alice member-of group eng-team

# With expiration
nexus rebac create agent bob direct_viewer file secret --expires 2025-12-31T23:59:59
```

#### rebac check - Check permission

```bash
# Check if alice can read file
nexus rebac check agent alice read file file123
```

#### rebac explain - Explain permission check

```bash
# Explain why alice has read permission on file
nexus rebac explain agent alice read file file123

# Show detailed path information
nexus rebac explain agent alice read file file123 --verbose

# Explain why permission is denied
nexus rebac explain agent bob write workspace main
```

**Output:**
```
✓ GRANTED

Reason: agent:alice has 'read' on file:file123 (expanded to relations: viewer) via parent inheritance

Access granted via:
  • Checking read on file:file123
    → Expanded to relations: viewer
    • Checking viewer on file:file123
      → Union of: direct_viewer, parent_viewer, editor
      • Checking parent_viewer on file:file123
        → Via parent relationship:
          Tupleset: parent
          Computed userset: viewer
          Found parent: workspace:main
```

#### rebac expand - Find all subjects with permission

```bash
# Find everyone who can read file123
nexus rebac expand read file file123
```

#### rebac delete - Delete relationship

```bash
# Delete relationship tuple
nexus rebac delete <tuple-id>
```

#### rebac namespace-create - Create custom namespace

```bash
# Create from config file (JSON/YAML)
nexus rebac namespace-create document --config-file document.json

# Create inline
nexus rebac namespace-create project \
  --relations owner \
  --relations maintainer \
  --relations contributor \
  --relations viewer:union:maintainer,contributor,owner \
  --permission read:viewer \
  --permission write:maintainer,owner \
  --permission admin:owner
```

**Config file format (JSON):**
```json
{
  "relations": {
    "owner": {},
    "editor": {},
    "viewer": {"union": ["editor", "owner"]}
  },
  "permissions": {
    "read": ["viewer", "editor", "owner"],
    "write": ["editor", "owner"],
    "delete": ["owner"]
  }
}
```

#### rebac namespace-list - List namespaces

```bash
# List as table
nexus rebac namespace-list

# JSON output
nexus rebac namespace-list --format json
```

**Output:**
```
                    ReBAC Namespaces
┏━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━━━━┓
┃ Object Type ┃ Relations     ┃ Permissions   ┃ Created    ┃
┡━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━━━━┩
│ file        │ owner, editor │ read, write   │ 2025-10-25 │
│ memory      │ owner, viewer │ read, write   │ 2025-10-25 │
│ document    │ owner, editor │ read, write   │ 2025-10-25 │
└─────────────┴───────────────┴───────────────┴────────────┘
```

#### rebac namespace-get - View namespace config

```bash
# View file namespace (YAML)
nexus rebac namespace-get file

# JSON output
nexus rebac namespace-get memory --format json
```

**Output:**
```yaml
object_type: file
config:
  relations:
    owner: {}
    editor: {}
    viewer:
      union: [editor, owner]
  permissions:
    read: [viewer, editor, owner]
    write: [editor, owner]
created_at: '2025-10-25T17:14:30+00:00'
```

#### rebac namespace-delete - Delete namespace

```bash
# Delete with confirmation
nexus rebac namespace-delete document

# Skip confirmation
nexus rebac namespace-delete document --yes
```

**Warning:** This does not delete existing tuples for this object type.

---

### Backend Mounts

#### mounts list - List mounts

```bash
# Show all mounts
nexus mounts list
```

#### mounts add - Add mount

```bash
# Add GCS mount
nexus mounts add /personal/alice gcs '{"bucket":"alice-bucket"}' --priority 10

# Add local mount
nexus mounts add /shared local '{"path":"/shared-data"}' --priority 5
```

#### mounts info - Show mount info

```bash
# Get mount details
nexus mounts info /personal/alice
```

#### mounts remove - Remove mount

```bash
# Remove mount
nexus mounts remove /personal/alice
```

---

### Server & Mounting

#### serve - Start RPC server

```bash
# Start server (default: localhost:8765)
nexus serve

# Specify host and port
nexus serve --host 0.0.0.0 --port 8080

# With specific data directory
nexus serve --data-dir /var/lib/nexus
```

#### mount - Mount as filesystem

```bash
# Mount Nexus as FUSE filesystem
nexus mount /mnt/nexus

# Mount with specific data directory
nexus mount /mnt/nexus --data-dir ./nexus-data
```

#### unmount - Unmount filesystem

```bash
# Unmount
nexus unmount /mnt/nexus
```

---

### Advanced Operations

#### ops log - View operation history

```bash
# Show recent operations
nexus ops log

# Limit results
nexus ops log --limit 20

# Filter by type
nexus ops log --type write
```

#### undo - Undo last operation

```bash
# Undo last operation
nexus undo

# View what would be undone (dry-run)
nexus undo --dry-run
```

#### work - Query work items

```bash
# Query work items using SQL
nexus work --view active_work
```

#### size - Calculate size

```bash
# Get total size of path
nexus size /workspace
```

#### find-duplicates - Find duplicate files

```bash
# Find duplicates by content hash
nexus find-duplicates /workspace
```

#### sync - One-way sync

```bash
# Sync from source to destination
nexus sync /source /destination
```

---

### Plugins & Skills

#### plugins list - List plugins

```bash
# List installed plugins
nexus plugins list
```

#### skills - Manage AI skills

```bash
# Discover and list skills
nexus skills list
nexus skills list --tier agent
nexus skills show my-analyzer

# Create and manage skills
nexus skills create my-skill --description "Description"
nexus skills fork code-reviewer my-fork

# Approval workflow (requires database)
nexus skills submit-approval my-analyzer \
    --submitted-by alice \
    --reviewers bob,charlie \
    --comments "Ready for team use"

nexus skills list-approvals --status pending
nexus skills approve <approval-id> --reviewed-by bob
nexus skills reject <approval-id> --reviewed-by bob --comments "Needs work"

# Export and share
nexus skills export my-analyzer --output skill.zip

# Full help
nexus skills --help
```

#### workflows - Workflow automation

```bash
# Workflow management
nexus workflows --help
```

---

### Environment Variables

Key environment variables for configuration:

```bash
# Remote mode (takes priority over local)
export NEXUS_URL=http://localhost:8765
export NEXUS_API_KEY=your-api-key

# Local mode - Data directory
export NEXUS_DATA_DIR=/path/to/data

# Tenant ID
export NEXUS_TENANT_ID=org_acme

# Subject identity
export NEXUS_SUBJECT=user:alice

# Database URL (PostgreSQL) - Required for skills approval workflow
export NEXUS_DATABASE_URL=postgresql://user:pass@localhost/nexus

# GCS configuration
export GCS_PROJECT_ID=my-project
export GCS_BUCKET_NAME=my-bucket
```

**Remote vs Local Priority:**
- If `NEXUS_URL` is set, all commands use remote mode (connect to server)
- Otherwise, commands use local mode with `NEXUS_DATA_DIR`

---

### Configuration File

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

Then use it:

```bash
nexus --config nexus.yaml ls /workspace
```

---

### CLI vs Python API Comparison

| CLI Command | Python API Equivalent |
|-------------|----------------------|
| `nexus write /file.txt "data"` | `nx.write("/file.txt", b"data")` |
| `nexus append /file.txt "data"` | `nx.append("/file.txt", b"data")` |
| `nexus cat /file.txt` | `nx.read("/file.txt")` |
| `nexus ls /workspace` | `nx.list("/workspace")` |
| `nexus glob "*.py"` | `nx.glob("*.py")` |
| `nexus grep "TODO"` | `nx.grep("TODO")` |
| `nexus versions history /file.txt` | `nx.list_versions("/file.txt")` |
| `nexus workspace snapshot /ws` | `nx.workspace_snapshot("/ws")` |
| `nexus search query "auth"` | `await nx.semantic_search("auth")` |

---

### Common CLI Workflows

#### 1. Initialize and use Nexus

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

#### 2. Version tracking workflow

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

#### 3. Workspace snapshot workflow

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

#### 4. Remote server workflow

```bash
# Start server (in one terminal)
nexus serve --host 0.0.0.0 --port 8765 --api-key secret123

# Use remote mode via flag (in another terminal)
nexus --remote-url http://localhost:8765 --remote-api-key secret123 ls /
nexus --remote-url http://localhost:8765 --remote-api-key secret123 write /file.txt "data"

# Or set environment variables
export NEXUS_URL=http://localhost:8765
export NEXUS_API_KEY=secret123

# Now all commands use remote mode
nexus write /workspace/file.txt "remote data"
nexus cat /workspace/file.txt
nexus memory store "Important fact" --scope user
nexus workspace snapshot /my-workspace --description "Checkpoint"
```

#### 5. Multi-backend workflow

```bash
# Use local backend
nexus --backend local ls /

# Use GCS backend
nexus --backend gcs --gcs-bucket my-bucket ls /
```

---

## See Also

- [Getting Started](getting-started.md) - Installation
- [File Operations](file-operations.md) - Python API equivalent
- [RPC API](rpc-api.md) - Remote access

## Next Steps

1. Try [common workflows](#common-cli-workflows)
2. Set up [environment variables](#environment-variables)
3. Create a [config file](#configuration-file)
