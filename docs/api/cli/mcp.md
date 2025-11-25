# CLI: Model Context Protocol (MCP) Server

← [CLI Reference](index.md) | [API Documentation](../README.md)

Commands for running Nexus as an MCP server.

## Commands

### nexus mcp serve

Start Nexus MCP server to expose tools to AI agents.

```bash
# For Claude Desktop (stdio)
nexus mcp serve --transport stdio

# For web clients (HTTP)
nexus mcp serve --transport http --port 8081

# With remote Nexus
NEXUS_URL=http://localhost:8080 \
NEXUS_API_KEY=your-key \
nexus mcp serve --transport stdio
```

**Options:**
- `--transport [stdio|http|sse]` - Transport type (default: stdio)
- `--host TEXT` - Server host for HTTP/SSE (default: 0.0.0.0)
- `--port INT` - Server port for HTTP/SSE (default: 8081)
- `--data-dir PATH` - Local data directory
- `--remote-url TEXT` - Remote Nexus URL
- `--backend [local|gcs]` - Backend type (default: local)

**Environment Variables:**
```bash
# Remote mode
NEXUS_URL=http://localhost:8080
NEXUS_API_KEY=your-api-key

# Local mode
NEXUS_DATA_DIR=/path/to/data
NEXUS_SUBJECT=user:alice
```

## Python API

```python
from nexus.mcp import create_mcp_server

# With local filesystem
nx = nexus.connect()
mcp = create_mcp_server(nx=nx)

# With remote server
mcp = create_mcp_server(remote_url="http://localhost:8080")

# Run with transport
mcp.run(transport="stdio")  # For Claude Desktop
mcp.run(transport="http", port=8081)  # For web clients
```

## Available Tools (14 total)

### File Operations

**nexus_read_file** - Read file
```json
{"path": "/workspace/data.txt"}
```

**nexus_write_file** - Write file
```json
{"path": "/workspace/data.txt", "content": "Hello"}
```

**nexus_delete_file** - Delete file
```json
{"path": "/workspace/data.txt"}
```

**nexus_list_files** - List directory
```json
{"path": "/workspace", "recursive": false}
```

**nexus_file_info** - File metadata
```json
{"path": "/workspace/data.txt"}
```

**nexus_mkdir** - Create directory
```json
{"path": "/workspace/new-dir"}
```

**nexus_rmdir** - Remove directory
```json
{"path": "/workspace/old-dir"}
```

### Search Tools

**nexus_glob** - Pattern search
```json
{"pattern": "**/*.py", "path": "/workspace"}
```

**nexus_grep** - Content search (regex)
```json
{"pattern": "TODO", "path": "/workspace"}
```

**nexus_semantic_search** - Natural language search
```json
{
  "query": "authentication setup",
  "path": "/docs",
  "top_k": 5
}
```

### Memory Tools

**nexus_store_memory** - Store memory
```json
{
  "content": "Python best practices",
  "user_id": "alice",
  "scope": "project"
}
```

**nexus_query_memory** - Query memories
```json
{
  "user_id": "alice",
  "scope": "project",
  "limit": 10
}
```

### Workflow Tools

**nexus_list_workflows** - List workflows
```json
{}
```

**nexus_execute_workflow** - Execute workflow
```json
{
  "workflow_id": "wf_123",
  "input_data": {"file": "/data.txt"}
}
```

## Resources

MCP resources for browsing Nexus content:

**nexus://files/{path}** - Browse files
```
nexus://files/workspace/project
```

## Prompts

MCP prompt templates:

**file_analysis_prompt** - Analyze file
```json
{"file_path": "/workspace/code.py"}
```

**search_and_summarize_prompt** - Search and summarize
```json
{
  "query": "authentication",
  "path": "/docs"
}
```

## Claude Desktop Configuration

Edit `~/.config/claude/claude_desktop_config.json`:

### Local Mode
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

### Remote Mode
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

After editing:
1. **Completely quit** Claude Desktop
2. Restart Claude Desktop
3. Try: "List files in /workspace"

## Usage Examples

### Basic File Operations
Ask Claude Desktop:
```
Read /workspace/data.txt
Write "Hello" to /workspace/hello.txt
List files in /workspace
```

### Search
```
Find all Python files with "TODO" comments
Search for files about authentication
```

### Memory
```
Remember that our API uses JWT tokens
What do you remember about our authentication?
```

### Workflows
```
List available workflows
Execute the data-processing workflow
```

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
1. Check config file: `~/.config/claude/claude_desktop_config.json`
2. Verify JSON syntax (use JSON validator)
3. **Completely quit** Claude Desktop (not just close window)
4. Restart Claude Desktop
5. Check Claude Desktop's debug console for errors

### Tools Returning Errors
1. Paths must be absolute: `/workspace/file.txt` not `workspace/file.txt`
2. Verify API key if using remote mode
3. Check Nexus server logs: `tail -f /tmp/nexus.log`

### MCP Server Not Starting
```bash
# Test manually
nexus mcp serve --transport stdio

# Check version
nexus --version

# Verify installation
python -c "from nexus.mcp import create_mcp_server; print('OK')"
```

## Testing with curl (HTTP/SSE Transport)

The MCP server supports HTTP transport using Server-Sent Events (SSE) for testing and integration.

### Start MCP Server with HTTP Transport

```bash
# Start with HTTP transport
nexus mcp serve --transport http --port 8081

# Or with Docker Compose
docker compose -f docker-compose.demo.yml up -d mcp-server
```

### Initialize Session

First, initialize the MCP session to get a session ID:

```bash
API_KEY="sk-your-api-key"

# Initialize and get session ID
curl -s -N -i -X POST http://localhost:8081/mcp \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
      "protocolVersion": "2024-11-05",
      "capabilities": {},
      "clientInfo": {
        "name": "curl-test",
        "version": "1.0"
      }
    }
  }' | grep -i "mcp-session-id"
```

Extract the session ID from the response headers:
```bash
SESSION_ID=$(curl -s -N -i -X POST http://localhost:8081/mcp \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}' \
  | grep -i "mcp-session-id" | cut -d' ' -f2 | tr -d '\r\n')
```

### List Available Tools

```bash
curl -s -N -X POST http://localhost:8081/mcp \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -H "mcp-session-id: $SESSION_ID" \
  -d '{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/list",
    "params": {}
  }' | grep "^data:" | sed 's/^data: //' | python3 -m json.tool
```

### Call Tools

**Write File:**
```bash
curl -s -N -X POST http://localhost:8081/mcp \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -H "mcp-session-id: $SESSION_ID" \
  -d '{
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {
      "name": "nexus_write_file",
      "arguments": {
        "path": "/test/example.txt",
        "content": "Hello from MCP!"
      }
    }
  }' | grep "^data:" | sed 's/^data: //' | python3 -m json.tool
```

**Read File:**
```bash
curl -s -N -X POST http://localhost:8081/mcp \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -H "mcp-session-id: $SESSION_ID" \
  -d '{
    "jsonrpc": "2.0",
    "id": 4,
    "method": "tools/call",
    "params": {
      "name": "nexus_read_file",
      "arguments": {
        "path": "/test/example.txt"
      }
    }
  }' | grep "^data:" | sed 's/^data: //' | python3 -m json.tool
```

**List Files:**
```bash
curl -s -N -X POST http://localhost:8081/mcp \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -H "mcp-session-id: $SESSION_ID" \
  -d '{
    "jsonrpc": "2.0",
    "id": 5,
    "method": "tools/call",
    "params": {
      "name": "nexus_list_files",
      "arguments": {
        "path": "/test",
        "recursive": false
      }
    }
  }' | grep "^data:" | sed 's/^data: //' | python3 -m json.tool
```

### Important Notes

1. **Required Headers:**
   - `Accept: application/json, text/event-stream` (both are required)
   - `Content-Type: application/json`
   - `X-API-Key: <your-api-key>` (for authentication)
   - `mcp-session-id: <session-id>` (for all requests after initialization)

2. **Response Format:**
   - Responses use Server-Sent Events (SSE) format
   - Extract JSON from `data:` lines: `grep "^data:" | sed 's/^data: //'`

3. **Session Management:**
   - Initialize once to get session ID
   - Use the same session ID for all subsequent requests
   - Session persists until the connection is closed

4. **Health Check:**
   ```bash
   curl http://localhost:8081/health
   ```

## Integration Guide

For complete setup guide with authentication examples, see:
- [MCP Integration Guide](../../integrations/mcp.md)
- [Quick Test Script](../../../examples/mcp/quick_test.sh)
- [Authentication Concepts](../../../examples/mcp/demo_mcp_auth_concept.sh)

## Resources

- [MCP Specification](https://modelcontextprotocol.io/)
- [FastMCP Documentation](https://gofastmcp.com/)
- [Claude Desktop](https://claude.ai/download)
