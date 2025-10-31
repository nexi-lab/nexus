# Nexus MCP Server

Expose Nexus to AI agents via [Model Context Protocol (MCP)](https://modelcontextprotocol.io/).

## Quick Start (2 minutes)

```bash
./quick_test.sh
```

This script:
- Starts authenticated Nexus server
- Creates test user (Alice)
- Tests file operations
- Shows MCP server setup
- Displays Claude Desktop config

## Claude Desktop Setup

Add to `~/.config/claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "nexus": {
      "command": "nexus",
      "args": ["mcp", "serve", "--transport", "stdio"],
      "env": {
        "NEXUS_URL": "http://localhost:8080",
        "NEXUS_API_KEY": "your-api-key"
      }
    }
  }
}
```

Then restart Claude Desktop and try: `"List files in /workspace"`

## Available Tools (14 total)

**Files:** `nexus_read_file`, `nexus_write_file`, `nexus_delete_file`, `nexus_list_files`, `nexus_file_info`, `nexus_mkdir`, `nexus_rmdir`

**Search:** `nexus_glob`, `nexus_grep`, `nexus_semantic_search`

**Memory:** `nexus_store_memory`, `nexus_query_memory`

**Workflows:** `nexus_list_workflows`, `nexus_execute_workflow`

## Usage

### Local Mode (No auth)
```bash
nexus mcp serve --transport stdio
```

### Remote Mode (With auth)
```bash
NEXUS_URL=http://localhost:8080 \
NEXUS_API_KEY=your-key \
nexus mcp serve --transport stdio
```

## Documentation

- **CLI Reference**: `../../docs/api/cli/mcp.md`
- **Integration Guide**: `../../docs/integrations/mcp.md`
- **Authentication Concepts**: `./demo_mcp_auth_concept.sh`

## Troubleshooting

**"Command not found: nexus"**
```bash
pip install nexus-ai-fs
```

**"Module 'fastmcp' not found"**
```bash
pip install fastmcp
```

**Claude Desktop not showing tools**
1. Verify config file location
2. Check JSON syntax
3. Completely quit and restart Claude Desktop

## Resources

- [MCP Documentation](https://modelcontextprotocol.io/)
- [FastMCP Documentation](https://gofastmcp.com/)
- [Nexus Documentation](https://github.com/nexi-lab/nexus)
