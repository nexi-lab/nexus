# Nexus Examples

This directory contains comprehensive examples for using Nexus with a remote server, demonstrating both the Python SDK and CLI.

## Quick Start

### 1. Prerequisites

- PostgreSQL running (Docker or Homebrew)
- Nexus installed: `pip install nexus-ai-fs`
- Python 3.x

### 2. Setup Server

Initialize a Nexus server with authentication:

```bash
# From the root of the repository
./scripts/init-nexus-with-auth.sh
```

This will:
- Initialize the PostgreSQL database
- Start the Nexus server on `http://localhost:8080`
- Create an admin API key
- Save credentials to `.nexus-admin-env`

**Keep terminal 1 running** (server stays active)

### 3. Run Examples

**Open a new terminal** (terminal 2) and run:

```bash
# Load admin credentials (sets NEXUS_URL and NEXUS_API_KEY)
source .nexus-admin-env

# Run Python example
python examples/python/advanced_usage_demo.py

# Run CLI example
./examples/cli/advanced_usage_demo.sh
```

**Note**: The CLI now automatically uses `NEXUS_URL` and `NEXUS_API_KEY` environment variables - no need to specify `--remote-url` flag!

## Examples Overview

### Python SDK Examples

#### Advanced Usage Demo
**File**: [`python/advanced_usage_demo.py`](python/advanced_usage_demo.py)

Demonstrates:
- Connecting to remote Nexus server with authentication
- Creating directory structures
- Writing and reading files (text and JSON)
- Listing files recursively
- Getting file metadata and version history
- Searching file contents
- Creating workspace snapshots

**Run**:
```bash
python examples/python/advanced_usage_demo.py
```

#### Directory Operations Demo
**File**: [`python/directory_operations_demo.py`](python/directory_operations_demo.py)

Demonstrates:
- Creating directories (mkdir with parents)
- Removing directories (rmdir, recursive deletion)
- Checking directory existence (is_directory)
- Listing directory contents
- Directory permissions with operation contexts
- Working with nested directory structures
- Directory statistics

**Run**:
```bash
python examples/python/directory_operations_demo.py
```

### CLI Examples

#### Advanced Usage Demo
**File**: [`cli/advanced_usage_demo.sh`](cli/advanced_usage_demo.sh)

Demonstrates:
- Using Nexus CLI with remote server
- All basic file operations (mkdir, write, read, ls, stat)
- File search with grep
- Permission management basics
- Workspace snapshots

**Run**:
```bash
./examples/cli/advanced_usage_demo.sh
```

#### Directory Operations Demo
**File**: [`cli/directory_operations_demo.sh`](cli/directory_operations_demo.sh)

Demonstrates:
- Creating directories with --parents flag
- Removing directories with --recursive flag
- Checking directory existence
- Listing directory contents (recursive and non-recursive)
- Creating project structures
- Directory permissions
- Path operations

**Run**:
```bash
./examples/cli/directory_operations_demo.sh
```

#### File Operations Demo
**File**: [`cli/file_operations_demo.sh`](cli/file_operations_demo.sh)

Demonstrates:
- Writing files (inline, stdin, from file, JSON, binary)
- Reading files (basic, with metadata)
- Copying files (simple, cross-directory)
- Moving/renaming files (same directory, cross-directory)
- Deleting files (with/without confirmation)
- Optimistic concurrency control (create-only, conditional updates)
- Complete workflow example (document versioning)
- Permission-aware operations (read-only, read-write)

**Run**:
```bash
./examples/cli/file_operations_demo.sh
```

## Example Workflow

Here's a typical workflow using both Python and CLI:

### Setup (Terminal 1)
```bash
# Start server with authentication
./scripts/init-nexus-with-auth.sh

# Server is now running at http://localhost:8080
# Credentials saved to .nexus-admin-env
```

### Python Usage (Terminal 2)
```python
from nexus.remote.client import RemoteNexusFS
import os

# Connect to server
nx = RemoteNexusFS(
    server_url=os.environ['SERVER_URL'],
    api_key=os.environ['NEXUS_API_KEY']
)

# Create and write files
nx.mkdir("/workspace/my-project", parents=True)
nx.write("/workspace/my-project/data.json", b'{"key": "value"}')

# Read back
content = nx.read("/workspace/my-project/data.json")
print(content)

nx.close()
```

### CLI Usage (Terminal 2)
```bash
# Load credentials (sets NEXUS_URL and NEXUS_API_KEY automatically)
source .nexus-admin-env

# Use CLI commands (automatically uses NEXUS_URL from env var)
nexus mkdir /workspace/cli-project
nexus write /workspace/cli-project/notes.txt "Hello World"
nexus cat /workspace/cli-project/notes.txt
nexus ls /workspace/cli-project
```

## Environment Variables

Both examples require these environment variables (automatically set by `.nexus-admin-env`):

- `NEXUS_API_KEY`: Authentication API key
- `NEXUS_URL`: Server URL (e.g., `http://localhost:8080`)

**Quick setup**:
```bash
source .nexus-admin-env
```

That's it! The CLI automatically uses `NEXUS_URL` and `NEXUS_API_KEY` from the environment.

## Authentication

The examples use **database-backed API keys** for production-ready authentication:

1. Admin API key is created during setup
2. Stored in `.nexus-admin-env` file
3. Automatically validated by the server
4. Used for all operations

For more details on authentication and creating user API keys, see:
- [`docs/getting-started/quickstart.md`](../docs/getting-started/quickstart.md)
- [`scripts/create-api-key.py`](../scripts/create-api-key.py)

## Permission Management

To set up user permissions, see the quickstart guide:

```bash
# Create user API key
python3 scripts/create-api-key.py alice "Alice's key" --days 90

# Grant permissions (as admin)
nexus rebac create user alice direct_owner file /workspace/project1 \
  --tenant-id default --remote-url $SERVER_URL

# Check permissions
nexus rebac check user alice write file /workspace/project1 --remote-url $SERVER_URL
```

## Cleanup

To remove demo data:

```bash
# Remove Python demo files
nexus rm -r /workspace/demo-project --remote-url $SERVER_URL

# Remove CLI demo files
nexus rm -r /workspace/cli-demo --remote-url $SERVER_URL
```

To stop the server:
```bash
# In terminal 1, press Ctrl+C
```

## AI Agent Framework Examples

Nexus integrates seamlessly with popular AI agent frameworks, providing persistent storage, memory, and collaboration capabilities.

### CrewAI + Nexus

**Directory**: [`crewai/`](crewai/)

Multi-agent AI systems with Nexus filesystem integration. Demonstrates:
- CrewAI agents with Nexus tools (file operations, search, memory)
- Long-term memory persistence across sessions
- Multi-agent collaboration via shared storage
- 3 demo scenarios: file analysis, research with memory, agent collaboration

**Features**:
- 8 Nexus tools: read, write, glob, grep, semantic search, memory operations
- Remote server mode with MCP-like architecture
- Memory API for agent learning
- Production-ready patterns

**Quick Start**:
```bash
cd examples/crewai

# Terminal 1: Start Nexus server
./start_nexus_server.sh

# Terminal 2: Run demo (requires ANTHROPIC_API_KEY or OPENAI_API_KEY)
export ANTHROPIC_API_KEY="your-key"
./run_demo.sh
```

See [crewai/README.md](crewai/README.md) for detailed documentation.

### LangGraph + Nexus

**Directory**: [`langgraph/`](langgraph/)

ReAct (Reasoning + Acting) agents with Nexus filesystem. Demonstrates:
- LangGraph state management with Nexus tools
- File search, read, and write operations
- Code analysis and documentation generation
- Educational ReAct pattern implementation

**Features**:
- 4 core tools: grep, glob, read, write
- Multi-LLM support (Claude, GPT-4, OpenRouter)
- Remote Nexus server connection
- Clear, commented code for learning

**Quick Start**:
```bash
cd examples/langgraph

# Install dependencies
pip install -r requirements.txt

# Set API key
export ANTHROPIC_API_KEY="your-key"

# Setup test data (optional)
python setup_test_data.py

# Run demo
python langgraph_react_demo.py
```

See [langgraph/README.md](langgraph/README.md) for detailed documentation.

### Framework Comparison

| Framework | Best For | Complexity | Key Features |
|-----------|----------|------------|--------------|
| **CrewAI** | Business workflows, multi-agent teams | Higher level | Role-based agents, task delegation |
| **LangGraph** | Custom control flows, research | Lower level | State graphs, fine control |

Both support:
- ✅ Remote Nexus server
- ✅ Persistent storage
- ✅ Memory/context retention
- ✅ Multi-agent collaboration
- ✅ Production deployment

## Next Steps

- **Advanced Usage**: See [`docs/api/advanced-usage.md`](../docs/api/advanced-usage.md)
- **API Reference**: See [`docs/api/`](../docs/api/)
- **Permission System**: See [`docs/api/permissions.md`](../docs/api/permissions.md)
- **Multi-backend Setup**: See [`docs/api/configuration.md`](../docs/api/configuration.md)
- **Agent Examples**: See [crewai/](crewai/) and [langgraph/](langgraph/)

## Troubleshooting

### Server won't start
- Check PostgreSQL is running: `psql $NEXUS_DATABASE_URL -c "SELECT 1"`
- Check port 8080 is available: `lsof -i :8080`

### Authentication errors
- Ensure `.nexus-admin-env` exists: `ls -la .nexus-admin-env`
- Load credentials: `source .nexus-admin-env`
- Verify key is set: `echo $NEXUS_API_KEY`

### Connection refused
- Verify server is running: `curl http://localhost:8080/health`
- Check `SERVER_URL` is set: `echo $SERVER_URL`

## Support

For issues or questions:
- GitHub Issues: https://github.com/anthropics/nexus/issues
- Documentation: [`docs/`](../docs/)
