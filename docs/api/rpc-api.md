# RPC/Server API

← [API Documentation](README.md)

This document describes the HTTP-based RPC server for remote access to Nexus.

Nexus provides an HTTP-based RPC server that exposes all filesystem operations over the network using JSON-RPC 2.0 protocol. This enables remote access, client-server deployments, and FUSE mounts.

### Server Setup

Start the RPC server using the CLI or Python API:

**CLI:**
```bash
# Start server (default: localhost:8765)
nexus serve

# Specify host and port
nexus serve --host 0.0.0.0 --port 8080

# With authentication
nexus serve --api-key "your-secret-key"

# With specific data directory
nexus serve --data-dir /var/lib/nexus
```

**Python API:**
```python
from nexus.server.rpc_server import RPCServer
from nexus import connect

# Create filesystem
nx = connect(config={"data_dir": "./nexus-data"})

# Start server
server = RPCServer(nx, host="0.0.0.0", port=8080, api_key="secret")
server.start()  # Runs in background thread

# Or run in foreground
server.serve_forever()
```

---

### Authentication

The RPC server supports Bearer token authentication via the `Authorization` header:

```bash
# All requests must include Authorization header
curl -H "Authorization: Bearer YOUR_API_KEY" \
     -H "Content-Type: application/json" \
     -X POST http://localhost:8765/api/nfs/read \
     -d '{"id": 1, "method": "read", "params": {"path": "/file.txt"}}'
```

Optional identity headers for permission checks:

- `X-Nexus-Subject`: Subject identity (e.g., `user:alice`, `agent:bot123`)
- `X-Nexus-Tenant-ID`: Tenant identifier for multi-tenant isolation

---

### Endpoints

#### Health Check (GET)

Check if server is running:

```bash
GET /health
```

**Response:**
```json
{
  "status": "healthy",
  "service": "nexus-rpc"
}
```

**Example:**
```bash
curl http://localhost:8765/health
```

---

#### Status (GET)

Get server status and available methods:

```bash
GET /api/nfs/status
```

**Response:**
```json
{
  "status": "running",
  "service": "nexus-rpc",
  "version": "1.0",
  "methods": ["read", "write", "delete", "list", "glob", "grep", "..."]
}
```

**Example:**
```bash
curl http://localhost:8765/api/nfs/status
```

---

### RPC Methods

All filesystem operations are exposed as RPC methods following JSON-RPC 2.0:

**Endpoint Pattern:**
```
POST /api/nfs/{method}
```

**Request Format:**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "operation_name",
  "params": {
    "param1": "value1",
    "param2": "value2"
  }
}
```

**Success Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": { ... }
}
```

**Error Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "error": {
    "code": -32600,
    "message": "Error description",
    "data": { "details": "..." }
  }
}
```

**Error Codes:**
- `-32700`: Parse error (invalid JSON)
- `-32600`: Invalid request
- `-32601`: Method not found
- `-32602`: Invalid params
- `-32603`: Internal error
- `-32000`: File not found
- `-32001`: Permission denied
- `-32002`: Invalid path
- `-32003`: File exists
- `-32004`: Conflict (version mismatch)

---

### Common Operations

#### read - Read file

```bash
POST /api/nfs/read
Content-Type: application/json
Authorization: Bearer YOUR_API_KEY

{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "read",
  "params": {
    "path": "/documents/file.txt",
    "return_metadata": false
  }
}
```

**Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "content": "base64_encoded_content",
    "encoding": "base64"
  }
}
```

**Example (curl):**
```bash
curl -H "Authorization: Bearer secret" \
     -H "Content-Type: application/json" \
     -X POST http://localhost:8765/api/nfs/read \
     -d '{"id":1,"method":"read","params":{"path":"/file.txt"}}'
```

---

#### write - Write file

```bash
POST /api/nfs/write

{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "write",
  "params": {
    "path": "/documents/new.txt",
    "content": "base64_encoded_content",
    "if_none_match": false
  }
}
```

**Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "result": {
    "etag": "abc123...",
    "version": 1,
    "size": 1024,
    "modified_at": "2025-01-15T10:30:00Z"
  }
}
```

---

#### list - List files

```bash
POST /api/nfs/list

{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "list",
  "params": {
    "path": "/documents",
    "recursive": true,
    "details": false
  }
}
```

**Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "result": [
    "/documents/file1.txt",
    "/documents/file2.txt",
    "/documents/subdir/file3.txt"
  ]
}
```

---

#### glob - Find files by pattern

```bash
POST /api/nfs/glob

{
  "jsonrpc": "2.0",
  "id": 4,
  "method": "glob",
  "params": {
    "pattern": "**/*.txt",
    "path": "/documents"
  }
}
```

**Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "result": [
    "/documents/file1.txt",
    "/documents/subdir/file2.txt"
  ]
}
```

---

#### delete - Delete file

```bash
POST /api/nfs/delete

{
  "jsonrpc": "2.0",
  "id": 5,
  "method": "delete",
  "params": {
    "path": "/documents/old.txt"
  }
}
```

**Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 5,
  "result": null
}
```

---

### Workspace Registry

Before using workspace snapshots, directories must be registered as workspaces.

#### register_workspace - Register a directory as a workspace

```bash
POST /api/nfs/register_workspace

{
  "jsonrpc": "2.0",
  "id": 6,
  "method": "register_workspace",
  "params": {
    "path": "/my-workspace",
    "name": "main",
    "description": "My main workspace",
    "created_by": "alice",
    "metadata": {
      "project_id": "12345",
      "team": "engineering"
    }
  }
}
```

**Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 6,
  "result": {
    "path": "/my-workspace",
    "name": "main",
    "description": "My main workspace",
    "created_by": "alice",
    "created_at": "2025-01-15T10:30:00Z",
    "metadata": {
      "project_id": "12345",
      "team": "engineering"
    }
  }
}
```

---

#### list_workspaces - List all registered workspaces

```bash
POST /api/nfs/list_workspaces

{
  "jsonrpc": "2.0",
  "id": 7,
  "method": "list_workspaces",
  "params": {}
}
```

**Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 7,
  "result": [
    {
      "path": "/my-workspace",
      "name": "main",
      "description": "My main workspace",
      "created_by": "alice",
      "created_at": "2025-01-15T10:30:00Z"
    },
    {
      "path": "/team/project",
      "name": "team-project",
      "description": "Team collaboration workspace",
      "created_at": "2025-01-15T11:00:00Z"
    }
  ]
}
```

---

#### get_workspace_info - Get workspace information

```bash
POST /api/nfs/get_workspace_info

{
  "jsonrpc": "2.0",
  "id": 8,
  "method": "get_workspace_info",
  "params": {
    "path": "/my-workspace"
  }
}
```

**Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 8,
  "result": {
    "path": "/my-workspace",
    "name": "main",
    "description": "My main workspace",
    "created_by": "alice",
    "created_at": "2025-01-15T10:30:00Z",
    "metadata": {
      "project_id": "12345",
      "team": "engineering"
    }
  }
}
```

**Note:** Returns `null` if workspace not found.

---

#### unregister_workspace - Unregister a workspace

```bash
POST /api/nfs/unregister_workspace

{
  "jsonrpc": "2.0",
  "id": 9,
  "method": "unregister_workspace",
  "params": {
    "path": "/my-workspace"
  }
}
```

**Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 9,
  "result": true
}
```

**Note:** This only removes the workspace registration. Files are NOT deleted.

---

### Workspace Snapshots

Create, restore, and compare workspace snapshots for version control.

#### workspace_snapshot - Create a snapshot

```bash
POST /api/nfs/workspace_snapshot

{
  "jsonrpc": "2.0",
  "id": 10,
  "method": "workspace_snapshot",
  "params": {
    "workspace_path": "/my-workspace",
    "description": "Before refactoring",
    "agent_id": "agent-123"
  }
}
```

**Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 10,
  "result": {
    "snapshot_number": 1,
    "timestamp": "2025-01-15T12:00:00Z",
    "description": "Before refactoring",
    "file_count": 42,
    "total_size": 1048576
  }
}
```

---

#### workspace_log - List workspace snapshots

```bash
POST /api/nfs/workspace_log

{
  "jsonrpc": "2.0",
  "id": 11,
  "method": "workspace_log",
  "params": {
    "workspace_path": "/my-workspace",
    "limit": 10
  }
}
```

**Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 11,
  "result": [
    {
      "snapshot_number": 2,
      "timestamp": "2025-01-15T14:00:00Z",
      "description": "After refactoring",
      "file_count": 45
    },
    {
      "snapshot_number": 1,
      "timestamp": "2025-01-15T12:00:00Z",
      "description": "Before refactoring",
      "file_count": 42
    }
  ]
}
```

---

#### workspace_restore - Restore a snapshot

```bash
POST /api/nfs/workspace_restore

{
  "jsonrpc": "2.0",
  "id": 12,
  "method": "workspace_restore",
  "params": {
    "snapshot_number": 1,
    "workspace_path": "/my-workspace"
  }
}
```

**Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 12,
  "result": {
    "restored": true,
    "files_restored": 42,
    "snapshot_number": 1,
    "timestamp": "2025-01-15T12:00:00Z"
  }
}
```

---

#### workspace_diff - Compare snapshots

```bash
POST /api/nfs/workspace_diff

{
  "jsonrpc": "2.0",
  "id": 13,
  "method": "workspace_diff",
  "params": {
    "snapshot_1": 1,
    "snapshot_2": 2,
    "workspace_path": "/my-workspace"
  }
}
```

**Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 13,
  "result": {
    "added": ["/my-workspace/new-file.txt"],
    "modified": ["/my-workspace/config.json"],
    "deleted": ["/my-workspace/old-file.txt"],
    "summary": {
      "added_count": 1,
      "modified_count": 1,
      "deleted_count": 1
    }
  }
}
```

---

### Python Client

Use the remote client to connect to a Nexus RPC server:

```python
from nexus.remote.client import RemoteNexusClient

# Connect to remote server
client = RemoteNexusClient(
    url="http://localhost:8765",
    api_key="your-secret-key"
)

# Use like regular Nexus instance
client.write("/file.txt", b"Hello, Remote!")
content = client.read("/file.txt")
files = client.list("/")

# With subject identity
from nexus.core.permissions import OperationContext

ctx = OperationContext(user="alice", groups=["team-engineering"])
content = client.read("/workspace/file.txt", context=ctx)

# Close connection
client.close()
```

---

### CORS Support

The RPC server includes CORS headers for browser-based clients:

- `Access-Control-Allow-Origin: *`
- `Access-Control-Allow-Methods: GET, POST, OPTIONS`
- `Access-Control-Allow-Headers: Content-Type, Authorization`

This enables direct API calls from web applications.

---

### Production Deployment

For production use, run the server behind a reverse proxy (nginx, Caddy, etc.):

**Nginx Example:**
```nginx
server {
    listen 443 ssl;
    server_name nexus.example.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

**Systemd Service:**
```ini
[Unit]
Description=Nexus RPC Server
After=network.target

[Service]
Type=simple
User=nexus
Environment="NEXUS_DATA_DIR=/var/lib/nexus"
Environment="NEXUS_API_KEY=your-secret-key"
ExecStart=/usr/local/bin/nexus serve --host 127.0.0.1 --port 8765
Restart=always

[Install]
WantedBy=multi-user.target
```

---

### Security Best Practices

1. **Always use API keys** in production (`--api-key` flag)
2. **Use HTTPS** in production (via reverse proxy)
3. **Restrict host binding** (`--host 127.0.0.1` for local-only access)
4. **Enable permission enforcement** (`enforce_permissions=True` in config)
5. **Use subject headers** (`X-Nexus-Subject`) to identify callers
6. **Enable audit logging** for compliance tracking
7. **Rotate API keys** regularly

---

### Complete RPC Method List

All Python SDK methods are available via RPC:

**File Operations:**
- `read`, `write`, `delete`, `rename`, `exists`

**Directory Operations:**
- `mkdir`, `rmdir`, `is_directory`, `list`

**Search:**
- `glob`, `grep`

**Versions:**
- `get_version`, `list_versions`, `rollback`, `diff_versions`

**Workspace:**
- `workspace_snapshot`, `workspace_restore`, `workspace_log`, `workspace_diff`
- `register_workspace`, `unregister_workspace`, `list_workspaces`, `get_workspace_info`

**Memory:**
- `register_memory`, `unregister_memory`, `list_memories`, `get_memory_info`

**Metadata:**
- `export_metadata`, `import_metadata`, `batch_get_content_ids`

**Mounts:**
- `add_mount`, `remove_mount`, `list_mounts`, `get_mount_info`

**ReBAC:**
- `rebac_create`, `rebac_check`, `rebac_explain`, `rebac_expand`, `rebac_delete`

**Semantic Search:**
- `initialize_semantic_search`, `semantic_search`, `semantic_search_index`, `semantic_search_stats`

---

## See Also

- [CLI Reference](cli-reference.md) - Server commands
- [Configuration](configuration.md) - Server configuration
- [Permissions](permissions.md) - Access control

## Next Steps

1. Start server with [nexus serve](cli-reference.md#serve---start-rpc-server)
2. Configure [authentication](#authentication)
3. Set up [production deployment](#production-deployment)
