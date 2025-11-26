# Nexus MCP Server API Documentation

Complete reference for Nexus Model Context Protocol (MCP) server API

## Table of Contents

1. [Overview](#overview)
2. [Authentication](#authentication)
3. [Transport Types](#transport-types)
4. [Client Configuration](#client-configuration)
5. [Tools](#tools)
   - [File Operations](#file-operations)
   - [Search Operations](#search-operations)
   - [Memory Operations](#memory-operations)
   - [Workflow Operations](#workflow-operations)
6. [Resources](#resources)
7. [Prompts](#prompts)
8. [Error Handling](#error-handling)
9. [Complete Tool Reference](#complete-tool-reference)
10. [Examples](#examples)
11. [Production Deployment](#production-deployment)

---

## Overview

Nexus provides a Model Context Protocol (MCP) server that exposes filesystem operations, search capabilities, memory management, and workflows to AI agents and tools.

**Protocol**: Model Context Protocol (MCP) 2024-11-05
**Transport**: stdio (default), HTTP, or SSE
**Base URL**: `http://localhost:8081` (HTTP/SSE transport)
**Framework**: FastMCP

### MCP Protocol

MCP is a standardized protocol for AI agents to interact with external tools and resources. Nexus implements MCP to expose:

- **14 Tools**: File operations, search, memory, and workflows
- **Resources**: Browse files via `nexus://files/{path}` URIs
- **Prompts**: Pre-built prompt templates for common tasks

### Request Format

MCP uses JSON-RPC 2.0 for communication:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "nexus_read_file",
    "arguments": {
      "path": "/workspace/data.txt"
    }
  }
}
```

### Response Format (Success)

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "File content here..."
      }
    ]
  }
}
```

### Response Format (Error)

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "error": {
    "code": -32603,
    "message": "File not found: /workspace/data.txt",
    "data": {
      "path": "/workspace/data.txt"
    }
  }
}
```

---

## Authentication

### Local Mode

No authentication required. Operations run with the user context from environment:

```bash
NEXUS_SUBJECT=user:alice nexus mcp serve --transport stdio
```

### Remote Mode

API key authentication required for remote Nexus server:

**Environment Variable:**
```bash
NEXUS_URL=http://localhost:8080
NEXUS_API_KEY=sk-your-api-key
nexus mcp serve --transport stdio
```

**HTTP Header (HTTP/SSE transport):**
```bash
curl -X POST http://localhost:8081/mcp \
  -H "X-Nexus-API-Key: sk-your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize",...}'
```

**Authorization Header (Alternative):**
```bash
curl -X POST http://localhost:8081/mcp \
  -H "Authorization: Bearer sk-your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize",...}'
```

### Per-Request API Keys (HTTP/SSE)

For multi-user scenarios, infrastructure (middleware, proxy, gateway) can set per-request API keys without exposing them to AI agents:

```python
from nexus.mcp.server import set_request_api_key, _request_api_key

# In middleware/proxy code:
token = set_request_api_key("sk-user-api-key-xyz")
try:
    # MCP tool calls here will use this API key
    result = mcp_server.call_tool("nexus_read_file", path="/data.txt")
finally:
    _request_api_key.reset(token)
```

---

## Transport Types

### stdio (Default)

Standard input/output transport for Claude Desktop and local clients.

**Usage:**
```bash
nexus mcp serve --transport stdio
```

**Claude Desktop Configuration:**
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

### HTTP

HTTP transport with Server-Sent Events (SSE) for web clients.

**Usage:**
```bash
nexus mcp serve --transport http --host 0.0.0.0 --port 8081
```

**Endpoints:**
- `POST /mcp` - MCP JSON-RPC endpoint
- `GET /health` - Health check endpoint

**Client Configuration (Cursor, etc.):**

For MCP clients that support HTTP transport, use this configuration format:

```json
{
  "nexus": {
    "url": "http://localhost:8081/mcp",
    "type": "http",
    "headers": {
      "X-Nexus-API-Key": "sk-your-api-key"
    }
  }
}
```

**Example Request:**
```bash
curl -X POST http://localhost:8081/mcp \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  -H "X-Nexus-API-Key: sk-your-api-key" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/list",
    "params": {}
  }'
```

### SSE

Server-Sent Events transport (similar to HTTP but optimized for streaming).

**Usage:**
```bash
nexus mcp serve --transport sse --port 8081
```

---

## Client Configuration

Different MCP clients use different configuration formats. Here are examples for common clients:

### Cursor IDE / HTTP Clients

For clients that connect via HTTP transport (like Cursor IDE), use this format:

```json
{
  "nexus": {
    "url": "http://localhost:8081/mcp",
    "type": "http",
    "headers": {
      "X-Nexus-API-Key": "sk-your-api-key"
    }
  }
}
```

### Claude Desktop (stdio)

For Claude Desktop, use the stdio transport configuration:

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

**Local Mode (No Authentication):**
```json
{
  "mcpServers": {
    "nexus": {
      "command": "nexus",
      "args": ["mcp", "serve", "--transport", "stdio"],
      "env": {
        "NEXUS_DATA_DIR": "/Users/you/nexus-data",
        "NEXUS_SUBJECT": "user:alice"
      }
    }
  }
}
```

---

## Tools

Nexus MCP server exposes 14 tools organized into four categories.

### File Operations

#### nexus_read_file

Read file content from Nexus filesystem.

**Parameters:**
- `path` (string, required): Absolute file path (e.g., `/workspace/data.txt`)

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "nexus_read_file",
    "arguments": {
      "path": "/workspace/data.txt"
    }
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "Hello, World!"
      }
    ]
  }
}
```

---

#### nexus_write_file

Write content to a file in Nexus filesystem.

**Parameters:**
- `path` (string, required): Absolute file path
- `content` (string, required): File content to write

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/call",
  "params": {
    "name": "nexus_write_file",
    "arguments": {
      "path": "/workspace/hello.txt",
      "content": "Hello from MCP!"
    }
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "File written successfully"
      }
    ]
  }
}
```

---

#### nexus_delete_file

Delete a file from Nexus filesystem.

**Parameters:**
- `path` (string, required): Absolute file path

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "tools/call",
  "params": {
    "name": "nexus_delete_file",
    "arguments": {
      "path": "/workspace/old.txt"
    }
  }
}
```

---

#### nexus_list_files

List directory contents.

**Parameters:**
- `path` (string, optional): Directory path (default: `/`)
- `recursive` (boolean, optional): List recursively (default: false)
- `details` (boolean, optional): Include file details (default: false)

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "method": "tools/call",
  "params": {
    "name": "nexus_list_files",
    "arguments": {
      "path": "/workspace",
      "recursive": false,
      "details": true
    }
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "[\"/workspace/file1.txt\", \"/workspace/file2.txt\", \"/workspace/subdir/\"]"
      }
    ]
  }
}
```

---

#### nexus_file_info

Get file metadata without reading content.

**Parameters:**
- `path` (string, required): Absolute file path

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 5,
  "method": "tools/call",
  "params": {
    "name": "nexus_file_info",
    "arguments": {
      "path": "/workspace/data.txt"
    }
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 5,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "{\"path\": \"/workspace/data.txt\", \"size\": 1024, \"is_directory\": false, \"modified_at\": \"2025-01-15T10:30:00Z\"}"
      }
    ]
  }
}
```

---

#### nexus_mkdir

Create a directory.

**Parameters:**
- `path` (string, required): Directory path
- `parents` (boolean, optional): Create parent directories (default: false)
- `exist_ok` (boolean, optional): Don't error if exists (default: false)

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 6,
  "method": "tools/call",
  "params": {
    "name": "nexus_mkdir",
    "arguments": {
      "path": "/workspace/new-dir",
      "parents": true
    }
  }
}
```

---

#### nexus_rmdir

Remove a directory.

**Parameters:**
- `path` (string, required): Directory path
- `recursive` (boolean, optional): Remove recursively (default: false)

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 7,
  "method": "tools/call",
  "params": {
    "name": "nexus_rmdir",
    "arguments": {
      "path": "/workspace/old-dir",
      "recursive": true
    }
  }
}
```

---

### Search Operations

#### nexus_glob

Find files matching a glob pattern.

**Parameters:**
- `pattern` (string, required): Glob pattern (e.g., `**/*.py`)
- `path` (string, optional): Search root path (default: `/`)

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 8,
  "method": "tools/call",
  "params": {
    "name": "nexus_glob",
    "arguments": {
      "pattern": "**/*.py",
      "path": "/workspace"
    }
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 8,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "[\"/workspace/app/main.py\", \"/workspace/app/utils.py\", \"/workspace/tests/test_main.py\"]"
      }
    ]
  }
}
```

---

#### nexus_grep

Search file contents for a pattern (regex).

**Parameters:**
- `pattern` (string, required): Search pattern (regex)
- `path` (string, optional): Search root path (default: `/`)
- `file_pattern` (string, optional): File pattern filter (glob)
- `ignore_case` (boolean, optional): Case-insensitive search (default: false)
- `max_results` (int, optional): Maximum results (default: 1000)

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 9,
  "method": "tools/call",
  "params": {
    "name": "nexus_grep",
    "arguments": {
      "pattern": "TODO",
      "path": "/workspace",
      "file_pattern": "*.py",
      "ignore_case": false,
      "max_results": 100
    }
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 9,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "[{\"file\": \"/workspace/app/main.py\", \"line\": 42, \"content\": \"# TODO: Implement error handling\", \"match\": \"TODO\"}]"
      }
    ]
  }
}
```

---

#### nexus_semantic_search

Natural language search across files.

**Parameters:**
- `query` (string, required): Natural language search query
- `path` (string, optional): Search root path (default: `/`)
- `limit` (int, optional): Maximum results (default: 10)

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 10,
  "method": "tools/call",
  "params": {
    "name": "nexus_semantic_search",
    "arguments": {
      "query": "authentication setup",
      "path": "/workspace/docs",
      "limit": 5
    }
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 10,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "[{\"file\": \"/workspace/docs/auth.md\", \"score\": 0.95, \"snippet\": \"Authentication is configured using JWT tokens...\"}]"
      }
    ]
  }
}
```

---

### Memory Operations

#### nexus_store_memory

Store agent memory for later retrieval.

**Parameters:**
- `content` (string, required): Memory content
- `memory_type` (string, optional): Memory type (default: `"fact"`)
- `scope` (string, optional): Memory scope (default: `"agent"`)
- `importance` (float, optional): Importance score 0.0-1.0 (default: 0.5)
- `tags` (array[string], optional): Tags

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 11,
  "method": "tools/call",
  "params": {
    "name": "nexus_store_memory",
    "arguments": {
      "content": "User prefers Python over JavaScript",
      "memory_type": "preference",
      "scope": "user",
      "importance": 0.8,
      "tags": ["language", "preference"]
    }
  }
}
```

---

#### nexus_query_memory

Query stored memories using semantic search.

**Parameters:**
- `query` (string, required): Search query
- `memory_type` (string, optional): Filter by memory type
- `scope` (string, optional): Filter by scope
- `limit` (int, optional): Maximum results (default: 5)

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 12,
  "method": "tools/call",
  "params": {
    "name": "nexus_query_memory",
    "arguments": {
      "query": "user preferences",
      "scope": "user",
      "limit": 10
    }
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 12,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "[{\"memory_id\": \"mem-123\", \"content\": \"User prefers Python over JavaScript\", \"memory_type\": \"preference\", \"scope\": \"user\", \"importance\": 0.8, \"created_at\": \"2025-01-15T10:00:00Z\"}]"
      }
    ]
  }
}
```

---

### Workflow Operations

#### nexus_list_workflows

List available workflows.

**Parameters:** None

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 13,
  "method": "tools/call",
  "params": {
    "name": "nexus_list_workflows",
    "arguments": {}
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 13,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "[{\"name\": \"data-processing\", \"description\": \"Process data files\"}, {\"name\": \"report-generation\", \"description\": \"Generate reports\"}]"
      }
    ]
  }
}
```

---

#### nexus_execute_workflow

Execute a workflow by name.

**Parameters:**
- `name` (string, required): Workflow name
- `inputs` (object, optional): Workflow input parameters (JSON string or object)

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 14,
  "method": "tools/call",
  "params": {
    "name": "nexus_execute_workflow",
    "arguments": {
      "name": "data-processing",
      "inputs": "{\"file\": \"/workspace/data.csv\", \"format\": \"csv\"}"
    }
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 14,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "{\"workflow_id\": \"wf-123\", \"status\": \"completed\", \"result\": {\"processed_rows\": 1000}}"
      }
    ]
  }
}
```

---

## Resources

MCP resources allow browsing Nexus content via URIs.

### nexus://files/{path}

Browse files and directories as resources.

**URI Format:**
```
nexus://files/workspace/project
nexus://files/workspace/data.txt
```

**Example:**
```json
{
  "jsonrpc": "2.0",
  "id": 15,
  "method": "resources/read",
  "params": {
    "uri": "nexus://files/workspace/data.txt"
  }
}
```

**Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 15,
  "result": {
    "contents": [
      {
        "uri": "nexus://files/workspace/data.txt",
        "mimeType": "text/plain",
        "text": "File content here..."
      }
    ]
  }
}
```

---

## Prompts

MCP prompt templates for common tasks.

### file_analysis_prompt

Analyze a file and provide insights.

**Parameters:**
- `file_path` (string, required): Path to file to analyze

**Example:**
```json
{
  "jsonrpc": "2.0",
  "id": 16,
  "method": "prompts/get",
  "params": {
    "name": "file_analysis_prompt",
    "arguments": {
      "file_path": "/workspace/code.py"
    }
  }
}
```

---

### search_and_summarize_prompt

Search for content and provide a summary.

**Parameters:**
- `query` (string, required): Search query
- `path` (string, optional): Search root path

**Example:**
```json
{
  "jsonrpc": "2.0",
  "id": 17,
  "method": "prompts/get",
  "params": {
    "name": "search_and_summarize_prompt",
    "arguments": {
      "query": "authentication",
      "path": "/workspace/docs"
    }
  }
}
```

---

## Error Handling

### Standard JSON-RPC Errors

| Code | Name | Description |
|------|------|-------------|
| `-32700` | PARSE_ERROR | Invalid JSON |
| `-32600` | INVALID_REQUEST | Invalid request format |
| `-32601` | METHOD_NOT_FOUND | Method does not exist |
| `-32602` | INVALID_PARAMS | Invalid method parameters |
| `-32603` | INTERNAL_ERROR | Internal server error |

### Nexus-Specific Errors

| Code | Name | Description |
|------|------|-------------|
| `-32000` | FILE_NOT_FOUND | File or directory not found |
| `-32001` | FILE_EXISTS | File already exists |
| `-32002` | INVALID_PATH | Invalid path format (must be absolute) |
| `-32003` | ACCESS_DENIED | Authentication failed |
| `-32004` | PERMISSION_ERROR | Permission denied (ReBAC) |

### Common Error Scenarios

**Invalid Path (Missing Leading Slash):**
```json
{
  "error": {
    "code": -32002,
    "message": "Path must be absolute: workspace/data.txt (use /workspace/data.txt)"
  }
}
```

**Authentication Failure:**
```json
{
  "error": {
    "code": -32003,
    "message": "Authentication failed: Invalid API key"
  }
}
```

**Permission Denied:**
```json
{
  "error": {
    "code": -32004,
    "message": "Permission denied: user:alice cannot write to /workspace/protected"
  }
}
```

---

## Complete Tool Reference

### Summary Table

| Tool | Category | Description |
|------|----------|-------------|
| `nexus_read_file` | File Operations | Read file content |
| `nexus_write_file` | File Operations | Write file content |
| `nexus_delete_file` | File Operations | Delete file |
| `nexus_list_files` | File Operations | List directory contents |
| `nexus_file_info` | File Operations | Get file metadata |
| `nexus_mkdir` | File Operations | Create directory |
| `nexus_rmdir` | File Operations | Remove directory |
| `nexus_glob` | Search | Find files by pattern |
| `nexus_grep` | Search | Search file contents (regex) |
| `nexus_semantic_search` | Search | Natural language search |
| `nexus_store_memory` | Memory | Store agent memory |
| `nexus_query_memory` | Memory | Query memories |
| `nexus_list_workflows` | Workflow | List workflows |
| `nexus_execute_workflow` | Workflow | Execute workflow |

**Total: 14 MCP Tools**

---

## Examples

### Python Client Example

```python
from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters

# Connect to Nexus MCP server
server_params = StdioServerParameters(
    command="nexus",
    args=["mcp", "serve", "--transport", "stdio"],
    env={
        "NEXUS_URL": "http://localhost:8080",
        "NEXUS_API_KEY": "sk-your-api-key"
    }
)

async with stdio_client(server_params) as (read, write):
    async with ClientSession(read, write) as session:
        # Initialize session
        await session.initialize()

        # List available tools
        tools = await session.list_tools()
        print(f"Available tools: {[t.name for t in tools.tools]}")

        # Read a file
        result = await session.call_tool("nexus_read_file", {
            "path": "/workspace/data.txt"
        })
        print(result.content[0].text)

        # Write a file
        await session.call_tool("nexus_write_file", {
            "path": "/workspace/hello.txt",
            "content": "Hello from MCP!"
        })

        # Search for files
        result = await session.call_tool("nexus_glob", {
            "pattern": "**/*.py",
            "path": "/workspace"
        })
        print(result.content[0].text)

        # Semantic search
        result = await session.call_tool("nexus_semantic_search", {
            "query": "authentication setup",
            "limit": 5
        })
        print(result.content[0].text)

        # Store memory
        await session.call_tool("nexus_store_memory", {
            "content": "User prefers Python over JavaScript",
            "memory_type": "preference",
            "importance": 0.8
        })

        # Query memory
        result = await session.call_tool("nexus_query_memory", {
            "query": "user preferences",
            "limit": 10
        })
        print(result.content[0].text)
```

### cURL Example (HTTP Transport)

```bash
# Initialize session
SESSION_ID=$(curl -s -N -i -X POST http://localhost:8081/mcp \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  -H "X-Nexus-API-Key: sk-your-api-key" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
      "protocolVersion": "2024-11-05",
      "capabilities": {},
      "clientInfo": {"name": "curl-test", "version": "1.0"}
    }
  }' | grep -i "mcp-session-id" | cut -d' ' -f2 | tr -d '\r\n')

# List tools
curl -s -N -X POST http://localhost:8081/mcp \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  -H "X-Nexus-API-Key: sk-your-api-key" \
  -H "mcp-session-id: $SESSION_ID" \
  -d '{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/list",
    "params": {}
  }' | grep "^data:" | sed 's/^data: //' | python3 -m json.tool

# Read file
curl -s -N -X POST http://localhost:8081/mcp \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  -H "X-Nexus-API-Key: sk-your-api-key" \
  -H "mcp-session-id: $SESSION_ID" \
  -d '{
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {
      "name": "nexus_read_file",
      "arguments": {
        "path": "/workspace/data.txt"
      }
    }
  }' | grep "^data:" | sed 's/^data: //' | python3 -m json.tool
```

### Claude Desktop Usage

After configuring Claude Desktop (see [Transport Types](#transport-types)), you can interact with Nexus via natural language:

**File Operations:**
```
Read the file at /workspace/data.txt
Write "Hello, World!" to /workspace/hello.txt
List all files in /workspace
Delete /workspace/old.txt
```

**Search:**
```
Find all Python files in /workspace
Search for "TODO" comments in Python files
Find files about authentication
```

**Memory:**
```
Remember that our API uses JWT tokens
What do you remember about user preferences?
```

**Workflows:**
```
List available workflows
Execute the data-processing workflow with input file /workspace/data.csv
```

---

## Production Deployment

### Security Best Practices

1. **Always use API keys** for remote mode
2. **Use HTTPS** via reverse proxy (Nginx, Caddy) for HTTP/SSE transport
3. **Restrict host binding** (`--host 127.0.0.1` for local-only)
4. **Enable ReBAC permissions** on Nexus server
5. **Use per-request API keys** for multi-user scenarios
6. **Rotate API keys regularly**
7. **Monitor MCP server logs** for errors

### Docker Deployment

```dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY . .

RUN pip install nexus-ai-fs fastmcp

EXPOSE 8081

CMD ["nexus", "mcp", "serve", "--transport", "http", "--host", "0.0.0.0", "--port", "8081"]
```

### Nginx Configuration

```nginx
server {
    listen 443 ssl;
    server_name mcp.example.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://127.0.0.1:8081;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # For SSE
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 86400;
    }
}
```

### Health Check

```bash
# Check MCP server health
curl http://localhost:8081/health

# Expected response:
# {"status": "healthy", "service": "nexus-mcp"}
```

### Monitoring

Monitor MCP server for:
- Tool call success/failure rates
- Authentication failures
- Response times
- Error rates by tool type

---

## See Also

- [RPC API Documentation](rpc-api.md) - Nexus RPC server API
- [CLI Reference](cli/mcp.md) - MCP CLI commands
- [Integration Guide](../integrations/mcp.md) - Setup and integration guide
- [MCP Specification](https://modelcontextprotocol.io/) - Official MCP protocol docs
- [FastMCP Documentation](https://gofastmcp.com/) - FastMCP framework docs

---

**Last Updated**: 2025-01-15
**Version**: 0.5.1+ (MCP Server API)
