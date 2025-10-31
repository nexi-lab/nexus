# Model Context Protocol (MCP) Integration

← [Integrations](index.md) | [Documentation Index](../DOCUMENTATION_INDEX.md)

Expose Nexus to AI agents via the Model Context Protocol (MCP).

## Quick Start

```bash
# Test the setup (2 minutes)
./examples/mcp/quick_test.sh

# Start MCP server
nexus mcp serve --transport stdio

# With remote Nexus
NEXUS_URL=http://localhost:8080 \
NEXUS_API_KEY=your-key \
nexus mcp serve --transport stdio
```

## Architecture

```
AI Client (Claude Desktop)
         ↓ MCP (stdio/HTTP)
    Nexus MCP Server
         ↓
Local NexusFS  OR  Remote Nexus Server
```

**Two Modes:**
- **Local**: MCP → Local filesystem (personal use)
- **Remote**: MCP → Remote Nexus server (team use, authenticated)

## Claude Desktop Setup

Edit `~/.config/claude/claude_desktop_config.json`:

### Local Mode (No authentication)
```json
{
  "mcpServers": {
    "nexus": {
      "command": "nexus",
      "args": ["mcp", "serve", "--transport", "stdio"],
      "env": {
        "NEXUS_DATA_DIR": "/Users/you/nexus-data"
      }
    }
  }
}
```

### Remote Mode (With authentication)
```json
{
  "mcpServers": {
    "nexus": {
      "command": "nexus",
      "args": ["mcp", "serve", "--transport", "stdio"],
      "env": {
        "NEXUS_URL": "http://localhost:8080",
        "NEXUS_API_KEY": "sk-your-api-key"
      }
    }
  }
}
```

**Setup:**
1. Edit config file
2. Completely quit and restart Claude Desktop
3. Try: "List files in /workspace"

## Available Tools (14 total)

### File Operations
- `nexus_read_file` - Read file content
- `nexus_write_file` - Write file content
- `nexus_delete_file` - Delete file
- `nexus_list_files` - List directory
- `nexus_file_info` - Get file metadata
- `nexus_mkdir` - Create directory
- `nexus_rmdir` - Remove directory

### Search
- `nexus_glob` - Pattern search (`**/*.py`)
- `nexus_grep` - Content search (regex)
- `nexus_semantic_search` - Natural language search

### Memory
- `nexus_store_memory` - Store agent memory
- `nexus_query_memory` - Query memories

### Workflows
- `nexus_list_workflows` - List workflows
- `nexus_execute_workflow` - Run workflow

## Usage Examples

### Basic Operations
Ask Claude Desktop:
```
Read the file at /workspace/data.txt
```

### Semantic Search
```
Find all Python files related to authentication
```

### Memory
```
Remember that our API uses JWT tokens
```

## Authentication

MCP server is a **thin protocol adapter** - authentication happens on the Nexus server.

### Local Mode
- No API key needed
- User context from environment: `NEXUS_SUBJECT=user:alice`
- All operations run as that user

### Remote Mode
- API key required: `NEXUS_API_KEY=sk-...`
- Nexus server validates key and maps to user
- ReBAC enforced on server side

**Architecture:**
```
Claude Desktop
    ↓ (with NEXUS_API_KEY)
MCP Server (passes api_key through)
    ↓
Nexus Server (validates key, enforces ReBAC)
```

## Deployment Patterns

### Personal Use
```bash
# Claude Desktop config with local data
{
  "env": {
    "NEXUS_DATA_DIR": "/Users/alice/nexus"
  }
}
```

### Team Use - Option 1: Separate Instances
```bash
# Alice's Claude Desktop
{"env": {"NEXUS_URL": "...", "NEXUS_API_KEY": "alice-key"}}

# Bob's Claude Desktop
{"env": {"NEXUS_URL": "...", "NEXUS_API_KEY": "bob-key"}}
```

Each user's API key determines their permissions via Nexus server's ReBAC.

### Team Use - Option 2: Shared HTTP MCP
Start MCP server with HTTP transport:
```bash
nexus mcp serve --transport http --port 8081
```

*Note: HTTP multi-user needs per-request auth (see [Authentication Concepts](../../examples/mcp/demo_mcp_auth_concept.sh))*

## Troubleshooting

### "Command not found: nexus"
```bash
pip install nexus-ai-fs
```

### "Module 'fastmcp' not found"
```bash
pip install fastmcp
```

### Claude Desktop Not Showing Tools
1. Check config file path (macOS: `~/.config/claude/claude_desktop_config.json`)
2. Verify JSON syntax
3. **Completely quit** Claude Desktop (not just close window)
4. Restart Claude Desktop

### Tools Returning Errors
1. Check paths are absolute (`/workspace/file.txt` not `workspace/file.txt`)
2. Verify API key if using remote mode
3. Check Nexus server logs for auth/permission errors

### MCP Server Not Starting
```bash
# Test manually
nexus mcp serve --transport stdio

# Check help
nexus mcp --help
```

## Advanced

### Python Client Example
```python
from mcp import ClientSession
from mcp.client.stdio import stdio_client

# Connect to Nexus MCP server
server_params = StdioServerParameters(
    command="nexus",
    args=["mcp", "serve", "--transport", "stdio"],
    env={
        "NEXUS_URL": "http://localhost:8080",
        "NEXUS_API_KEY": "your-key"
    }
)

async with stdio_client(server_params) as (read, write):
    async with ClientSession(read, write) as session:
        await session.initialize()

        # List available tools
        tools = await session.list_tools()
        print(f"Available tools: {[t.name for t in tools.tools]}")

        # Call a tool
        result = await session.call_tool("nexus_read_file", {
            "path": "/workspace/data.txt"
        })
        print(result.content)
```

### Custom Transport
```bash
# HTTP transport (for web clients)
nexus mcp serve --transport http --host 0.0.0.0 --port 8081

# SSE transport
nexus mcp serve --transport sse --port 8081
```

### Environment Variables
- `NEXUS_DATA_DIR` - Local data directory (local mode)
- `NEXUS_URL` - Remote server URL (remote mode)
- `NEXUS_API_KEY` - API key for authentication (remote mode)
- `NEXUS_SUBJECT` - User context (local mode, e.g., `user:alice`)

## CLI Reference

See [MCP CLI Documentation](../api/cli/mcp.md) for complete command reference.

## Examples

See [examples/mcp/](../../examples/mcp/) for:
- `quick_test.sh` - Full authentication test (~2 minutes)
- `demo_mcp_auth_concept.sh` - Authentication architecture explained
- `verify_mcp_tools.py` - Verify MCP installation

## Resources

- [MCP Specification](https://modelcontextprotocol.io/)
- [FastMCP Documentation](https://gofastmcp.com/)
- [Claude Desktop](https://claude.ai/download)
- [Issue #139](https://github.com/nexi-lab/nexus/issues/139) - Original feature request
