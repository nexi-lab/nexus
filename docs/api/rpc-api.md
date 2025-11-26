# Nexus RPC Server API Documentation

Complete reference for all Nexus RPC server APIs (v0.5.1+)

## Table of Contents

1. [Overview](#overview)
   - [Special Data Types](#special-data-types) ⚠️ Important for bytes/binary data
2. [Authentication](#authentication)
3. [Endpoints](#endpoints)
4. [Error Codes](#error-codes)
5. [Core File Operations](#core-file-operations)
6. [Directory Operations](#directory-operations)
7. [Search Operations](#search-operations)
8. [Workspace Management](#workspace-management)
9. [Mount Management](#mount-management)
10. [Memory API](#memory-api)
11. [Agent Management](#agent-management)
12. [Sandbox Management](#sandbox-management)
13. [Admin API Management](#admin-api-management)
14. [ReBAC Permissions](#rebac-permissions)
15. [Versioning Operations](#versioning-operations)
16. [Namespace Management](#namespace-management)
17. [OAuth Management](#oauth-management)
18. [Complete Method Reference](#complete-method-reference)

---

## Overview

Nexus provides an HTTP-based RPC server exposing all filesystem operations via JSON-RPC 2.0 protocol.

**Base URL**: `http://localhost:8765` (default)
**Protocol**: JSON-RPC 2.0
**Transport**: HTTP POST
**Content-Type**: `application/json`

### Request Format

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "method_name",
  "params": {
    "param1": "value1",
    "param2": "value2"
  }
}
```

### Response Format (Success)

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": { ... }
}
```

### Response Format (Error)

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "error": {
    "code": -32000,
    "message": "Error description",
    "data": { ... }
  }
}
```

### Special Data Types

The Nexus RPC protocol uses special encoding for certain data types that cannot be directly represented in JSON:

#### Strings (Text)

Regular text data uses standard JSON strings - **no special encoding needed**:

```json
{
  "name": "This is a regular text string",
  "description": "Just use normal JSON strings"
}
```

#### Bytes (Binary Data)

Binary data (file content, images, etc.) **must** be base64-encoded using the following special format:

```json
{
  "__type__": "bytes",
  "data": "<base64-encoded-string>"
}
```

**Example:**
```python
# Python: Encode "Hello, World!" as bytes
import base64
content = base64.b64encode(b"Hello, World!").decode('utf-8')
# Result: "SGVsbG8sIFdvcmxkIQ=="

# JSON-RPC parameter:
{
  "content": {
    "__type__": "bytes",
    "data": "SGVsbG8sIFdvcmxkIQ=="
  }
}
```

**⚠️ Common Error:**

Passing base64 string directly **will fail** with error: *"Strings must be encoded before hashing"*

❌ **Wrong:**
```json
{
  "content": "SGVsbG8sIFdvcmxkIQ=="
}
```

✅ **Correct:**
```json
{
  "content": {
    "__type__": "bytes",
    "data": "SGVsbG8sIFdvcmxkIQ=="
  }
}
```

#### DateTime

Datetime values are encoded in ISO 8601 format (usually in responses):

```json
{
  "__type__": "datetime",
  "data": "2025-01-15T10:35:00+00:00"
}
```

#### TimeDelta

Time duration values are encoded as total seconds (v0.5.0+):

```json
{
  "__type__": "timedelta",
  "seconds": 3600
}
```

**Note:** When using the official Python client (`RemoteNexusFS`), these conversions are handled automatically. Manual API calls (curl, Postman, etc.) **must** use the formats above.

---

## Authentication

### API Key Authentication

Include API key in `Authorization` header:

```bash
Authorization: Bearer YOUR_API_KEY
```

### Identity Headers (Optional)

For permission checks and multi-tenancy:

- `X-Nexus-Subject`: Subject identity (e.g., `user:alice`, `agent:bot123`)
- `X-Nexus-Tenant-ID`: Tenant identifier

### Example Request

```bash
curl -H "Authorization: Bearer secret-key" \
     -H "Content-Type: application/json" \
     -H "X-Nexus-Subject: user:alice" \
     -X POST http://localhost:8765/api/nfs/read \
     -d '{"id":1,"method":"read","params":{"path":"/file.txt"}}'
```

---

## Endpoints

### Health Check

```
GET /health
```

**Response:**
```json
{
  "status": "healthy",
  "service": "nexus-rpc"
}
```

---

### Who Am I

```
GET /api/auth/whoami
```

Returns authenticated user context.

**Response:**
```json
{
  "authenticated": true,
  "subject_type": "user",
  "subject_id": "alice",
  "tenant_id": "tenant-123",
  "is_admin": false
}
```

---

### Status

```
GET /api/nfs/status
```

Returns server status and available methods.

**Response:**
```json
{
  "status": "running",
  "service": "nexus-rpc",
  "version": "0.5.0",
  "methods": ["read", "write", "list", "..."],
  "backend": {
    "type": "gcs",
    "bucket": "nexi-hub"
  },
  "metadata_store": {
    "type": "postgresql",
    "instance": "nexi-lab-888:us-west1:nexus-hub"
  }
}
```

---

### RPC Method Endpoint Pattern

```
POST /api/nfs/{method_name}
```

All filesystem operations use this pattern.

---

## Error Codes

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
| `-32002` | INVALID_PATH | Invalid path format |
| `-32003` | ACCESS_DENIED | Authentication failed |
| `-32004` | PERMISSION_ERROR | Permission denied |
| `-32005` | VALIDATION_ERROR | Validation failed |
| `-32006` | CONFLICT | Optimistic concurrency conflict |

---

## Core File Operations

### read

Read file content with optional metadata.

**Endpoint**: `POST /api/nfs/read`

**Parameters:**
- `path` (string, required): File path
- `return_metadata` (boolean, optional): Return metadata with content (default: false)

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "read",
  "params": {
    "path": "/documents/file.txt",
    "return_metadata": true
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "content": "base64_encoded_content",
    "encoding": "base64",
    "metadata": {
      "size": 1024,
      "etag": "abc123",
      "modified_at": "2025-01-15T10:30:00Z"
    }
  }
}
```

---

### write

Write file content with optimistic concurrency control.

**Endpoint**: `POST /api/nfs/write`

**Parameters:**
- `path` (string, required): File path
- `content` (bytes, required): File content in special bytes format (see example below)
- `if_match` (string, optional): ETag for optimistic concurrency
- `if_none_match` (boolean, optional): Create-only mode (default: false)
- `force` (boolean, optional): Skip version check (default: false)

**IMPORTANT**: The `content` parameter must use the special bytes encoding format:
```json
{
  "__type__": "bytes",
  "data": "<base64-encoded-string>"
}
```

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "write",
  "params": {
    "path": "/documents/file.txt",
    "content": {
      "__type__": "bytes",
      "data": "SGVsbG8sIFdvcmxkIQ=="
    },
    "if_none_match": false
  }
}
```

**cURL Example:**
```bash
curl -X POST http://localhost:8080/api/nfs/write \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "write",
    "params": {
      "path": "/workspace/hello.txt",
      "content": {
        "__type__": "bytes",
        "data": "SGVsbG8sIFdvcmxkIQ=="
      }
    }
  }'
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "result": {
    "etag": "def456",
    "version": 2,
    "size": 1024,
    "modified_at": "2025-01-15T10:35:00Z"
  }
}
```

---

### delete

Delete a file.

**Endpoint**: `POST /api/nfs/delete`

**Parameters:**
- `path` (string, required): File path

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "delete",
  "params": {
    "path": "/documents/old.txt"
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "result": {
    "success": true
  }
}
```

---

### rename

Rename or move a file.

**Endpoint**: `POST /api/nfs/rename`

**Parameters:**
- `old_path` (string, required): Current file path
- `new_path` (string, required): New file path

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "method": "rename",
  "params": {
    "old_path": "/documents/old-name.txt",
    "new_path": "/documents/new-name.txt"
  }
}
```

---

### exists

Check if a file exists.

**Endpoint**: `POST /api/nfs/exists`

**Parameters:**
- `path` (string, required): File path

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 5,
  "method": "exists",
  "params": {
    "path": "/documents/file.txt"
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 5,
  "result": true
}
```

---

### get_metadata

Get file metadata without content.

**Endpoint**: `POST /api/nfs/get_metadata`

**Parameters:**
- `path` (string, required): File path

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 6,
  "result": {
    "path": "/documents/file.txt",
    "size": 1024,
    "etag": "abc123",
    "version": 3,
    "created_at": "2025-01-15T09:00:00Z",
    "modified_at": "2025-01-15T10:30:00Z",
    "content_type": "text/plain"
  }
}
```

**Note:** Returns `null` if workspace not found.

---

## Directory Operations

### mkdir

Create a directory.

**Endpoint**: `POST /api/nfs/mkdir`

**Parameters:**
- `path` (string, required): Directory path
- `parents` (boolean, optional): Create parent directories (default: false)
- `exist_ok` (boolean, optional): Don't error if exists (default: false)

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 7,
  "method": "mkdir",
  "params": {
    "path": "/projects/new-project",
    "parents": true,
    "exist_ok": false
  }
}
```

---

### rmdir

Remove a directory.

**Endpoint**: `POST /api/nfs/rmdir`

**Parameters:**
- `path` (string, required): Directory path
- `recursive` (boolean, optional): Remove recursively (default: false)

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 8,
  "method": "rmdir",
  "params": {
    "path": "/projects/old-project",
    "recursive": true
  }
}
```

---

### list

List directory contents.

**Endpoint**: `POST /api/nfs/list`

**Parameters:**
- `path` (string, optional): Directory path (default: "/")
- `recursive` (boolean, optional): List recursively (default: true)
- `details` (boolean, optional): Include file details (default: false)
- `prefix` (string, optional): Filter by prefix
- `show_parsed` (boolean, optional): Show parsed views (default: true)

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 9,
  "method": "list",
  "params": {
    "path": "/documents",
    "recursive": false,
    "details": true
  }
}
```

**Example Response (details=false):**
```json
{
  "jsonrpc": "2.0",
  "id": 9,
  "result": [
    "/documents/file1.txt",
    "/documents/file2.txt",
    "/documents/subdir/"
  ]
}
```

**Example Response (details=true):**
```json
{
  "jsonrpc": "2.0",
  "id": 9,
  "result": [
    {
      "path": "/documents/file1.txt",
      "size": 1024,
      "etag": "abc123",
      "modified_at": "2025-01-15T10:00:00Z",
      "is_directory": false
    },
    {
      "path": "/documents/subdir/",
      "is_directory": true
    }
  ]
}
```

---

### is_directory

Check if path is a directory.

**Endpoint**: `POST /api/nfs/is_directory`

**Parameters:**
- `path` (string, required): Path to check

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 10,
  "result": true
}
```

---

## Search Operations

### glob

Find files matching a glob pattern.

**Endpoint**: `POST /api/nfs/glob`

**Parameters:**
- `pattern` (string, required): Glob pattern (e.g., `**/*.txt`)
- `path` (string, optional): Search root path (default: "/")

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 11,
  "method": "glob",
  "params": {
    "pattern": "**/*.py",
    "path": "/projects"
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 11,
  "result": [
    "/projects/app/main.py",
    "/projects/app/utils.py",
    "/projects/tests/test_main.py"
  ]
}
```

---

### grep

Search file contents for a pattern.

**Endpoint**: `POST /api/nfs/grep`

**Parameters:**
- `pattern` (string, required): Search pattern (regex)
- `path` (string, optional): Search root path (default: "/")
- `file_pattern` (string, optional): File pattern filter (glob)
- `ignore_case` (boolean, optional): Case-insensitive search (default: false)
- `max_results` (int, optional): Maximum results (default: 1000)

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 12,
  "method": "grep",
  "params": {
    "pattern": "TODO",
    "path": "/projects",
    "file_pattern": "*.py",
    "ignore_case": false,
    "max_results": 100
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 12,
  "result": [
    {
      "file": "/projects/app/main.py",
      "line": 42,
      "content": "# TODO: Implement error handling",
      "match": "TODO"
    },
    {
      "file": "/projects/app/utils.py",
      "line": 15,
      "content": "# TODO: Add validation",
      "match": "TODO"
    }
  ]
}
```

---

## Workspace Management

Workspaces must be registered before creating snapshots.

### register_workspace

Register a directory as a workspace.

**Endpoint**: `POST /api/nfs/register_workspace`

**Parameters:**
- `path` (string, required): Workspace path
- `name` (string, optional): Workspace name
- `description` (string, optional): Description
- `created_by` (string, optional): Creator identity
- `tags` (array[string], optional): Tags
- `metadata` (object, optional): Custom metadata
- `session_id` (string, optional): Session identifier (v0.5.0)
- `ttl` (number, optional): Time-to-live in seconds (v0.5.0)

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 13,
  "method": "register_workspace",
  "params": {
    "path": "/my-workspace",
    "name": "main",
    "description": "Main development workspace",
    "created_by": "user:alice",
    "tags": ["development", "active"],
    "metadata": {
      "project_id": "12345",
      "team": "engineering"
    },
    "session_id": "session-abc",
    "ttl": 86400
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 13,
  "result": {
    "path": "/my-workspace",
    "name": "main",
    "description": "Main development workspace",
    "created_by": "user:alice",
    "created_at": "2025-01-15T10:00:00Z",
    "tags": ["development", "active"],
    "metadata": {
      "project_id": "12345",
      "team": "engineering"
    }
  }
}
```

---

### unregister_workspace

Unregister a workspace (does not delete files).

**Endpoint**: `POST /api/nfs/unregister_workspace`

**Parameters:**
- `path` (string, required): Workspace path

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 14,
  "result": true
}
```

---

### list_workspaces

List all registered workspaces.

**Endpoint**: `POST /api/nfs/list_workspaces`

**Parameters:** None

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 15,
  "result": [
    {
      "path": "/my-workspace",
      "name": "main",
      "description": "Main workspace",
      "created_at": "2025-01-15T10:00:00Z"
    },
    {
      "path": "/team/project",
      "name": "team-project",
      "created_at": "2025-01-15T11:00:00Z"
    }
  ]
}
```

---

### get_workspace_info

Get workspace information.

**Endpoint**: `POST /api/nfs/get_workspace_info`

**Parameters:**
- `path` (string, required): Workspace path

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 16,
  "result": {
    "path": "/my-workspace",
    "name": "main",
    "description": "Main workspace",
    "created_by": "user:alice",
    "created_at": "2025-01-15T10:00:00Z",
    "metadata": {
      "project_id": "12345"
    }
  }
}
```

Returns `null` if workspace not found.

---

### workspace_snapshot

Create a workspace snapshot.

**Endpoint**: `POST /api/nfs/workspace_snapshot`

**Parameters:**
- `workspace_path` (string, optional): Workspace path
- `agent_id` (string, optional): Agent ID (DEPRECATED)
- `description` (string, optional): Snapshot description
- `tags` (array[string], optional): Tags
- `created_by` (string, optional): Creator identity

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 17,
  "method": "workspace_snapshot",
  "params": {
    "workspace_path": "/my-workspace",
    "description": "Before refactoring",
    "tags": ["pre-refactor"],
    "created_by": "user:alice"
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 17,
  "result": {
    "snapshot_number": 1,
    "timestamp": "2025-01-15T12:00:00Z",
    "description": "Before refactoring",
    "file_count": 42,
    "total_size": 1048576,
    "tags": ["pre-refactor"]
  }
}
```

---

### workspace_restore

Restore a workspace from a snapshot.

**Endpoint**: `POST /api/nfs/workspace_restore`

**Parameters:**
- `snapshot_number` (int, required): Snapshot number to restore
- `workspace_path` (string, optional): Workspace path
- `agent_id` (string, optional): Agent ID (DEPRECATED)

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 18,
  "method": "workspace_restore",
  "params": {
    "snapshot_number": 1,
    "workspace_path": "/my-workspace"
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 18,
  "result": {
    "restored": true,
    "files_restored": 42,
    "snapshot_number": 1,
    "timestamp": "2025-01-15T12:00:00Z"
  }
}
```

---

### workspace_log

List workspace snapshots.

**Endpoint**: `POST /api/nfs/workspace_log`

**Parameters:**
- `workspace_path` (string, optional): Workspace path
- `agent_id` (string, optional): Agent ID (DEPRECATED)
- `limit` (int, optional): Maximum snapshots to return (default: 100)

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 19,
  "result": [
    {
      "snapshot_number": 2,
      "timestamp": "2025-01-15T14:00:00Z",
      "description": "After refactoring",
      "file_count": 45,
      "tags": ["post-refactor"]
    },
    {
      "snapshot_number": 1,
      "timestamp": "2025-01-15T12:00:00Z",
      "description": "Before refactoring",
      "file_count": 42,
      "tags": ["pre-refactor"]
    }
  ]
}
```

---

### workspace_diff

Compare two workspace snapshots.

**Endpoint**: `POST /api/nfs/workspace_diff`

**Parameters:**
- `snapshot_1` (int, required): First snapshot number
- `snapshot_2` (int, required): Second snapshot number
- `workspace_path` (string, optional): Workspace path
- `agent_id` (string, optional): Agent ID (DEPRECATED)

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 20,
  "method": "workspace_diff",
  "params": {
    "snapshot_1": 1,
    "snapshot_2": 2,
    "workspace_path": "/my-workspace"
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 20,
  "result": {
    "added": [
      "/my-workspace/new-file.txt",
      "/my-workspace/utils/helper.py"
    ],
    "modified": [
      "/my-workspace/config.json",
      "/my-workspace/main.py"
    ],
    "deleted": [
      "/my-workspace/old-file.txt"
    ],
    "summary": {
      "added_count": 2,
      "modified_count": 2,
      "deleted_count": 1
    }
  }
}
```

---

## Mount Management

Backend mount management for attaching multiple storage backends to different paths (v0.5.0+).

Nexus supports dynamic mount management, allowing you to add/remove storage backends at runtime. This is useful for:
- User-specific storage (personal Google Drive, GCS buckets)
- Multi-tenant scenarios with isolated storage per tenant
- Temporary mounts for specific tasks
- Mounting external buckets or shared storage

### Supported Backend Types

- **`local`** - Local filesystem backend
- **`gcs`** - Google Cloud Storage (content-addressable, CAS-based)
- **`gcs_connector`** - GCS Connector (direct path mapping for external buckets)
- **`google_drive`** - Google Drive backend (requires authentication)

### add_mount

Add a dynamic backend mount at runtime.

**Endpoint**: `POST /api/nfs/add_mount`

**Parameters:**
- `mount_point` (string, required): Virtual path where backend is mounted (e.g., "/personal/alice")
- `backend_type` (string, required): Backend type - "local", "gcs", "gcs_connector", "google_drive"
- `backend_config` (object, required): Backend-specific configuration
- `priority` (int, optional): Mount priority - higher values take precedence (default: 0)
- `readonly` (bool, optional): Whether mount is read-only (default: false)

**Backend Configuration Examples:**

**Local Backend:**
```json
{
  "data_dir": "/path/to/local/directory"
}
```

**GCS Backend (CAS-based):**
```json
{
  "bucket": "bucket-name",
  "project_id": "my-project",
  "credentials_path": "/path/to/credentials.json"  // optional
}
```

**GCS Connector Backend (Direct path mapping):**
```json
{
  "bucket": "external-bucket",
  "project_id": "my-project",
  "prefix": "workspace",  // optional prefix in bucket
  "credentials_path": "/path/to/credentials.json"  // optional
}
```

**Example Request (Add GCS mount):**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "add_mount",
  "params": {
    "mount_point": "/personal/alice",
    "backend_type": "gcs",
    "backend_config": {
      "bucket": "alice-personal-bucket",
      "project_id": "my-project"
    },
    "priority": 10
  }
}
```

**Example Request (Add GCS Connector mount):**
```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "add_mount",
  "params": {
    "mount_point": "/workspace/shared",
    "backend_type": "gcs_connector",
    "backend_config": {
      "bucket": "external-shared-bucket",
      "project_id": "my-project",
      "prefix": "team-workspace"
    },
    "priority": 5
  }
}
```

**Example Request (Add local mount):**
```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "add_mount",
  "params": {
    "mount_point": "/shared/team",
    "backend_type": "local",
    "backend_config": {
      "data_dir": "/mnt/shared/team-data"
    },
    "priority": 5,
    "readonly": true
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": "/personal/alice"
}
```

**cURL Example:**
```bash
curl -X POST http://localhost:8080/api/nfs/add_mount \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "add_mount",
    "params": {
      "mount_point": "/workspace/gcs",
      "backend_type": "gcs_connector",
      "backend_config": {
        "bucket": "my-external-bucket",
        "project_id": "my-project"
      },
      "priority": 10
    }
  }'
```

---

### remove_mount

Remove a backend mount from the filesystem.

**Endpoint**: `POST /api/nfs/remove_mount`

**Parameters:**
- `mount_point` (string, required): Virtual path of mount to remove

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "method": "remove_mount",
  "params": {
    "mount_point": "/personal/alice"
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "result": true
}
```

---

### list_mounts

List all active backend mounts.

**Endpoint**: `POST /api/nfs/list_mounts`

**Parameters:** None

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 5,
  "method": "list_mounts",
  "params": {}
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 5,
  "result": [
    {
      "mount_point": "/personal/alice",
      "priority": 10,
      "readonly": false,
      "backend_type": "GCSBackend"
    },
    {
      "mount_point": "/workspace/shared",
      "priority": 5,
      "readonly": false,
      "backend_type": "GCSConnectorBackend"
    },
    {
      "mount_point": "/shared/team",
      "priority": 5,
      "readonly": true,
      "backend_type": "LocalBackend"
    }
  ]
}
```

---

### get_mount

Get details about a specific mount.

**Endpoint**: `POST /api/nfs/get_mount`

**Parameters:**
- `mount_point` (string, required): Virtual path of mount

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 6,
  "method": "get_mount",
  "params": {
    "mount_point": "/personal/alice"
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 6,
  "result": {
    "mount_point": "/personal/alice",
    "priority": 10,
    "readonly": false,
    "backend_type": "GCSBackend"
  }
}
```

**Response (not found):**
```json
{
  "jsonrpc": "2.0",
  "id": 6,
  "result": null
}
```

---

### has_mount

Check if a mount exists at the given path.

**Endpoint**: `POST /api/nfs/has_mount`

**Parameters:**
- `mount_point` (string, required): Virtual path to check

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 7,
  "method": "has_mount",
  "params": {
    "mount_point": "/personal/alice"
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 7,
  "result": true
}
```

---

### save_mount

Save a mount configuration to the database for persistence.

**Endpoint**: `POST /api/nfs/save_mount`

**Parameters:**
- `mount_point` (string, required): Virtual path where backend is mounted
- `backend_type` (string, required): Backend type - "local", "gcs", "gcs_connector", etc.
- `backend_config` (object, required): Backend-specific configuration
- `priority` (int, optional): Mount priority (default: 0)
- `readonly` (bool, optional): Whether mount is read-only (default: false)
- `owner_user_id` (string, optional): User who owns this mount
- `tenant_id` (string, optional): Tenant ID for multi-tenant isolation
- `description` (string, optional): Human-readable description

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 8,
  "method": "save_mount",
  "params": {
    "mount_point": "/personal/alice",
    "backend_type": "google_drive",
    "backend_config": {
      "access_token": "ya29.xxx",
      "refresh_token": "1//xxx"
    },
    "owner_user_id": "google:alice123",
    "tenant_id": "acme",
    "description": "Alice's personal Google Drive",
    "priority": 10
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 8,
  "result": "4594a9be-7a75-44c6-b96d-605c399ce8f7"
}
```

**Note:** The result is the mount ID (UUID string). This only saves the configuration - use `load_mount` to activate it.

---

### list_saved_mounts

List mount configurations saved in the database.

**Endpoint**: `POST /api/nfs/list_saved_mounts`

**Parameters:**
- `owner_user_id` (string, optional): Filter by owner user ID
- `tenant_id` (string, optional): Filter by tenant ID

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 9,
  "method": "list_saved_mounts",
  "params": {
    "owner_user_id": "google:alice123"
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 9,
  "result": [
    {
      "mount_id": "4594a9be-7a75-44c6-b96d-605c399ce8f7",
      "mount_point": "/personal/alice",
      "backend_type": "google_drive",
      "backend_config": {
        "access_token": "ya29.xxx",
        "refresh_token": "1//xxx"
      },
      "priority": 10,
      "readonly": false,
      "owner_user_id": "google:alice123",
      "tenant_id": "acme",
      "description": "Alice's personal Google Drive",
      "created_at": "2025-01-15T10:00:00Z"
    }
  ]
}
```

---

### load_mount

Load a saved mount configuration and activate it.

**Endpoint**: `POST /api/nfs/load_mount`

**Parameters:**
- `mount_point` (string, required): Virtual path of saved mount to load

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 10,
  "method": "load_mount",
  "params": {
    "mount_point": "/personal/alice"
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 10,
  "result": "/personal/alice"
}
```

**Error Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 10,
  "error": {
    "code": -32000,
    "message": "Mount not found in database: /personal/alice"
  }
}
```

---

### delete_saved_mount

Delete a saved mount configuration from the database.

**Endpoint**: `POST /api/nfs/delete_saved_mount`

**Parameters:**
- `mount_point` (string, required): Virtual path of mount to delete

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 11,
  "method": "delete_saved_mount",
  "params": {
    "mount_point": "/personal/alice"
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 11,
  "result": true
}
```

**Note:** This only removes the saved configuration. Use `remove_mount` to deactivate an active mount.

---

### Mount Workflow Example

Complete workflow for user-specific mounts:

```python
# 1. Add a mount at runtime
mount_id = client.add_mount(
    mount_point="/personal/alice",
    backend_type="gcs",
    backend_config={
        "bucket": "alice-personal-bucket",
        "project_id": "my-project"
    },
    priority=10
)

# 2. Save the mount configuration for persistence
saved_id = client.save_mount(
    mount_point="/personal/alice",
    backend_type="gcs",
    backend_config={
        "bucket": "alice-personal-bucket",
        "project_id": "my-project"
    },
    owner_user_id="google:alice123",
    description="Alice's personal storage",
    priority=10
)

# 3. Use the mount (files are automatically routed)
client.write("/personal/alice/file.txt", b"Hello!")
content = client.read("/personal/alice/file.txt")

# 4. Later: Load saved mount after server restart
client.load_mount("/personal/alice")

# 5. Remove the active mount
client.remove_mount("/personal/alice")

# 6. Delete the saved configuration
client.delete_saved_mount("/personal/alice")
```

---

## Memory API

Agent memory management for trajectories, reflections, and playbooks (v0.5.0).

### register_memory

Register a directory as agent memory storage.

**Endpoint**: `POST /api/nfs/register_memory`

**Parameters:**
- `path` (string, required): Memory path
- `name` (string, optional): Memory name
- `description` (string, optional): Description
- `created_by` (string, optional): Creator identity
- `metadata` (object, optional): Custom metadata
- `session_id` (string, optional): Session identifier
- `ttl` (number, optional): Time-to-live in seconds

---

### unregister_memory

Unregister agent memory.

**Endpoint**: `POST /api/nfs/unregister_memory`

**Parameters:**
- `path` (string, required): Memory path

---

### list_memories

List registered memories.

**Endpoint**: `POST /api/nfs/list_memories`

**Parameters:**
- `limit` (int, optional): Maximum memories (default: 50)
- `scope` (string, optional): Filter by scope
- `memory_type` (string, optional): Filter by type

---

### get_memory_info

Get memory information.

**Endpoint**: `POST /api/nfs/get_memory_info`

**Parameters:**
- `path` (string, required): Memory path

---

### start_trajectory

Start tracking an execution trajectory.

**Endpoint**: `POST /api/nfs/start_trajectory`

**Parameters:**
- `task_description` (string, required): Task description
- `task_type` (string, optional): Task type/category

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 21,
  "method": "start_trajectory",
  "params": {
    "task_description": "Implement user authentication",
    "task_type": "feature-development"
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 21,
  "result": {
    "trajectory_id": "traj-abc123",
    "started_at": "2025-01-15T10:00:00Z",
    "task_description": "Implement user authentication"
  }
}
```

---

### log_trajectory_step

Log a step in an execution trajectory.

**Endpoint**: `POST /api/nfs/log_trajectory_step`

**Parameters:**
- `trajectory_id` (string, required): Trajectory ID
- `step_type` (string, required): Step type (e.g., "read", "write", "execute")
- `description` (string, required): Step description
- `result` (any, optional): Step result

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 22,
  "method": "log_trajectory_step",
  "params": {
    "trajectory_id": "traj-abc123",
    "step_type": "write",
    "description": "Created authentication module",
    "result": {
      "file": "/app/auth.py",
      "lines": 150
    }
  }
}
```

---

### complete_trajectory

Mark a trajectory as complete.

**Endpoint**: `POST /api/nfs/complete_trajectory`

**Parameters:**
- `trajectory_id` (string, required): Trajectory ID
- `status` (string, required): Completion status (e.g., "success", "failure")
- `success_score` (float, optional): Success score (0.0-1.0)
- `error_message` (string, optional): Error message if failed

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 23,
  "method": "complete_trajectory",
  "params": {
    "trajectory_id": "traj-abc123",
    "status": "success",
    "success_score": 0.95
  }
}
```

---

### query_trajectories

Query execution trajectories.

**Endpoint**: `POST /api/nfs/query_trajectories`

**Parameters:**
- `agent_id` (string, optional): Filter by agent ID
- `status` (string, optional): Filter by status
- `limit` (int, optional): Maximum results (default: 50)

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 24,
  "result": [
    {
      "trajectory_id": "traj-abc123",
      "task_description": "Implement user authentication",
      "status": "success",
      "started_at": "2025-01-15T10:00:00Z",
      "completed_at": "2025-01-15T11:00:00Z",
      "success_score": 0.95
    }
  ]
}
```

---

### store_memory

Store an agent memory.

**Endpoint**: `POST /api/nfs/store_memory`

**Parameters:**
- `content` (string, required): Memory content
- `memory_type` (string, optional): Memory type (default: "fact")
- `scope` (string, optional): Memory scope (default: "agent")
- `importance` (float, optional): Importance score (default: 0.5)
- `tags` (array[string], optional): Tags

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 25,
  "method": "store_memory",
  "params": {
    "content": "User prefers Python over JavaScript",
    "memory_type": "preference",
    "scope": "user",
    "importance": 0.8,
    "tags": ["language", "preference"]
  }
}
```

---

### query_memories

Query stored memories.

**Endpoint**: `POST /api/nfs/query_memories`

**Parameters:**
- `memory_type` (string, optional): Filter by type
- `scope` (string, optional): Filter by scope
- `limit` (int, optional): Maximum results (default: 50)

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 26,
  "result": {
    "memories": [
      {
        "memory_id": "mem-123",
        "content": "User prefers Python over JavaScript",
        "memory_type": "preference",
        "scope": "user",
        "importance": 0.8,
        "created_at": "2025-01-15T10:00:00Z"
      }
    ]
  }
}
```

---

### batch_reflect

Batch reflection across multiple trajectories.

**Endpoint**: `POST /api/nfs/batch_reflect`

**Parameters:**
- `agent_id` (string, optional): Filter by agent ID
- `since` (string, optional): ISO timestamp to reflect since
- `min_trajectories` (int, optional): Minimum trajectories (default: 10)
- `task_type` (string, optional): Filter by task type

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 27,
  "method": "batch_reflect",
  "params": {
    "agent_id": "agent-alice",
    "since": "2025-01-15T00:00:00Z",
    "min_trajectories": 5,
    "task_type": "feature-development"
  }
}
```

---

### get_playbook

Retrieve an agent playbook.

**Endpoint**: `POST /api/nfs/get_playbook`

**Parameters:**
- `playbook_name` (string, optional): Playbook name (default: "default")

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 28,
  "result": {
    "playbook_name": "default",
    "patterns": [
      {
        "pattern": "authentication-implementation",
        "description": "Best practices for implementing authentication",
        "steps": ["..."]
      }
    ],
    "updated_at": "2025-01-15T12:00:00Z"
  }
}
```

---

### curate_playbook

Auto-curate playbook from reflections.

**Endpoint**: `POST /api/nfs/curate_playbook`

**Parameters:**
- `reflection_memory_ids` (array[string], required): Reflection memory IDs
- `playbook_name` (string, optional): Playbook name (default: "default")
- `merge_threshold` (float, optional): Similarity threshold (default: 0.7)

---

### query_playbooks

Query playbooks.

**Endpoint**: `POST /api/nfs/query_playbooks`

**Parameters:**
- `agent_id` (string, optional): Filter by agent ID
- `scope` (string, optional): Filter by scope
- `limit` (int, optional): Maximum results (default: 50)

---

### process_relearning

Process re-learning queue.

**Endpoint**: `POST /api/nfs/process_relearning`

**Parameters:**
- `limit` (int, optional): Maximum items to process (default: 10)

---

## Agent Management

### register_agent

Register a new agent.

**Endpoint**: `POST /api/nfs/register_agent`

**Parameters:**
- `agent_id` (string, required): Agent identifier
- `name` (string, required): Agent name
- `description` (string, optional): Description
- `generate_api_key` (boolean, optional): Generate API key (default: false)

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 29,
  "method": "register_agent",
  "params": {
    "agent_id": "agent-alice",
    "name": "Alice Assistant",
    "description": "Personal coding assistant",
    "generate_api_key": true
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 29,
  "result": {
    "agent_id": "agent-alice",
    "name": "Alice Assistant",
    "description": "Personal coding assistant",
    "api_key": "key-abc123",
    "created_at": "2025-01-15T10:00:00Z"
  }
}
```

---

### list_agents

List all registered agents.

**Endpoint**: `POST /api/nfs/list_agents`

**Parameters:** None

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 30,
  "result": [
    {
      "agent_id": "agent-alice",
      "name": "Alice Assistant",
      "created_at": "2025-01-15T10:00:00Z"
    },
    {
      "agent_id": "agent-bob",
      "name": "Bob Bot",
      "created_at": "2025-01-15T11:00:00Z"
    }
  ]
}
```

---

### get_agent

Get agent information.

**Endpoint**: `POST /api/nfs/get_agent`

**Parameters:**
- `agent_id` (string, required): Agent identifier

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 31,
  "result": {
    "agent_id": "agent-alice",
    "name": "Alice Assistant",
    "description": "Personal coding assistant",
    "created_at": "2025-01-15T10:00:00Z",
    "metadata": {
      "version": "1.0"
    }
  }
}
```

---

### delete_agent

Delete an agent.

**Endpoint**: `POST /api/nfs/delete_agent`

**Parameters:**
- `agent_id` (string, required): Agent identifier

---

## Sandbox Management

Code execution sandbox management for running code in isolated environments (v0.5.0+).

### sandbox_create

Create a new code execution sandbox.

**Endpoint**: `POST /api/nfs/sandbox_create`

**Parameters:**
- `name` (string, required): User-friendly sandbox name (unique per user)
- `ttl_minutes` (int, optional): Idle timeout in minutes (default: 10)
- `provider` (string, optional): Sandbox provider ("e2b", "docker", etc.) (default: "e2b")
- `template_id` (string, optional): Provider template ID
- `context` (dict, optional): Operation context

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "sandbox_create",
  "params": {
    "name": "data-analysis",
    "ttl_minutes": 30,
    "provider": "e2b",
    "template_id": "python-base"
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "sandbox_id": "sbx-abc123",
    "name": "data-analysis",
    "status": "running",
    "provider": "e2b",
    "created_at": "2025-01-15T10:00:00Z",
    "expires_at": "2025-01-15T10:30:00Z"
  }
}
```

---

### sandbox_run

Run code in a sandbox.

**Endpoint**: `POST /api/nfs/sandbox_run`

**Parameters:**
- `sandbox_id` (string, required): Sandbox ID
- `language` (string, required): Programming language ("python", "javascript", "bash")
- `code` (string, required): Code to execute
- `timeout` (int, optional): Execution timeout in seconds (default: 30)
- `context` (dict, optional): Operation context

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "sandbox_run",
  "params": {
    "sandbox_id": "sbx-abc123",
    "language": "python",
    "code": "print('Hello from sandbox!')\nresult = 2 + 2\nprint(f'Result: {result}')",
    "timeout": 30
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "result": {
    "stdout": "Hello from sandbox!\nResult: 4\n",
    "stderr": "",
    "exit_code": 0,
    "execution_time": 0.123
  }
}
```

---

### sandbox_pause

Pause a sandbox to save costs.

**Endpoint**: `POST /api/nfs/sandbox_pause`

**Parameters:**
- `sandbox_id` (string, required): Sandbox ID
- `context` (dict, optional): Operation context

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "sandbox_pause",
  "params": {
    "sandbox_id": "sbx-abc123"
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "result": {
    "sandbox_id": "sbx-abc123",
    "status": "paused",
    "paused_at": "2025-01-15T10:15:00Z"
  }
}
```

---

### sandbox_resume

Resume a paused sandbox.

**Endpoint**: `POST /api/nfs/sandbox_resume`

**Parameters:**
- `sandbox_id` (string, required): Sandbox ID
- `context` (dict, optional): Operation context

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "method": "sandbox_resume",
  "params": {
    "sandbox_id": "sbx-abc123"
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "result": {
    "sandbox_id": "sbx-abc123",
    "status": "running",
    "resumed_at": "2025-01-15T10:20:00Z"
  }
}
```

---

### sandbox_stop

Stop and destroy a sandbox.

**Endpoint**: `POST /api/nfs/sandbox_stop`

**Parameters:**
- `sandbox_id` (string, required): Sandbox ID
- `context` (dict, optional): Operation context

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 5,
  "method": "sandbox_stop",
  "params": {
    "sandbox_id": "sbx-abc123"
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 5,
  "result": {
    "sandbox_id": "sbx-abc123",
    "status": "stopped",
    "stopped_at": "2025-01-15T10:25:00Z"
  }
}
```

---

### sandbox_list

List user's sandboxes.

**Endpoint**: `POST /api/nfs/sandbox_list`

**Parameters:**
- `context` (dict, optional): Operation context

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 6,
  "method": "sandbox_list",
  "params": {}
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 6,
  "result": {
    "sandboxes": [
      {
        "sandbox_id": "sbx-abc123",
        "name": "data-analysis",
        "status": "running",
        "provider": "e2b",
        "created_at": "2025-01-15T10:00:00Z"
      },
      {
        "sandbox_id": "sbx-def456",
        "name": "test-env",
        "status": "paused",
        "provider": "docker",
        "created_at": "2025-01-15T09:00:00Z"
      }
    ]
  }
}
```

---

### sandbox_status

Get sandbox status and metadata.

**Endpoint**: `POST /api/nfs/sandbox_status`

**Parameters:**
- `sandbox_id` (string, required): Sandbox ID
- `context` (dict, optional): Operation context

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 7,
  "method": "sandbox_status",
  "params": {
    "sandbox_id": "sbx-abc123"
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 7,
  "result": {
    "sandbox_id": "sbx-abc123",
    "name": "data-analysis",
    "status": "running",
    "provider": "e2b",
    "created_at": "2025-01-15T10:00:00Z",
    "last_activity": "2025-01-15T10:25:00Z",
    "expires_at": "2025-01-15T10:30:00Z"
  }
}
```

---

### sandbox_connect

Connect and mount Nexus to a sandbox (Nexus-managed or user-managed).

**Endpoint**: `POST /api/nfs/sandbox_connect`

**Parameters:**
- `sandbox_id` (string, required): Sandbox ID (Nexus-managed or external)
- `provider` (string, optional): Sandbox provider ("e2b", etc.) (default: "e2b")
- `sandbox_api_key` (string, optional): Provider API key (only for user-managed sandboxes)
- `mount_path` (string, optional): Path where Nexus will be mounted (default: "/mnt/nexus")
- `nexus_url` (string, optional): Nexus server URL (auto-detected if not provided)
- `nexus_api_key` (string, optional): Nexus API key (from context if not provided)
- `context` (dict, optional): Operation context

**Example Request (Nexus-managed sandbox):**
```json
{
  "jsonrpc": "2.0",
  "id": 8,
  "method": "sandbox_connect",
  "params": {
    "sandbox_id": "sbx-abc123",
    "mount_path": "/mnt/nexus",
    "nexus_url": "https://nexus.example.com",
    "nexus_api_key": "nxk_live_..."
  }
}
```

**Example Request (User-managed sandbox):**
```json
{
  "jsonrpc": "2.0",
  "id": 9,
  "method": "sandbox_connect",
  "params": {
    "sandbox_id": "external-sandbox-id",
    "provider": "e2b",
    "sandbox_api_key": "e2b_api_key_...",
    "mount_path": "/mnt/nexus",
    "nexus_url": "https://nexus.example.com",
    "nexus_api_key": "nxk_live_..."
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 8,
  "result": {
    "sandbox_id": "sbx-abc123",
    "provider": "e2b",
    "mount_path": "/mnt/nexus",
    "mounted_at": "2025-01-15T10:30:00Z",
    "mount_status": "success"
  }
}
```

---

### sandbox_disconnect

Disconnect and unmount Nexus from a user-managed sandbox.

**Endpoint**: `POST /api/nfs/sandbox_disconnect`

**Parameters:**
- `sandbox_id` (string, required): External sandbox ID
- `provider` (string, optional): Sandbox provider ("e2b", etc.) (default: "e2b")
- `sandbox_api_key` (string, optional): Provider API key
- `context` (dict, optional): Operation context

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 10,
  "method": "sandbox_disconnect",
  "params": {
    "sandbox_id": "external-sandbox-id",
    "provider": "e2b",
    "sandbox_api_key": "e2b_api_key_..."
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 10,
  "result": {
    "sandbox_id": "external-sandbox-id",
    "provider": "e2b",
    "unmounted_at": "2025-01-15T10:35:00Z"
  }
}
```

---

## Admin API Management

Admin-only APIs for managing API keys (v0.5.1). All operations require admin privileges.

### admin_create_key

Create a new API key for a user (admin only).

**Endpoint**: `POST /api/nfs/admin_create_key`

**Parameters:**
- `user_id` (string, required): User identifier
- `name` (string, required): Key name/description
- `is_admin` (boolean, optional): Grant admin privileges (default: false)
- `expires_days` (int, optional): Expiration in days (null = never expires)
- `tenant_id` (string, optional): Tenant identifier (default: "default")
- `subject_type` (string, optional): Subject type (default: "user")
- `subject_id` (string, optional): Subject identifier (defaults to user_id)

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "admin_create_key",
  "params": {
    "user_id": "alice",
    "name": "Alice's API Key",
    "is_admin": false,
    "expires_days": 365,
    "tenant_id": "default",
    "subject_type": "user"
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "key_id": "key-abc123",
    "api_key": "nxk_live_1234567890abcdef",
    "user_id": "alice",
    "name": "Alice's API Key",
    "subject_type": "user",
    "subject_id": "alice",
    "tenant_id": "default",
    "is_admin": false,
    "expires_at": "2026-10-30T12:00:00Z"
  }
}
```

**Important:** The `api_key` field is only shown once during creation. Save it securely!

---

### admin_list_keys

List API keys with optional filtering (admin only).

**Endpoint**: `POST /api/nfs/admin_list_keys`

**Parameters:**
- `user_id` (string, optional): Filter by user ID
- `tenant_id` (string, optional): Filter by tenant ID
- `is_admin` (boolean, optional): Filter by admin status
- `include_revoked` (boolean, optional): Include revoked keys (default: false)
- `include_expired` (boolean, optional): Include expired keys (default: false)
- `limit` (int, optional): Maximum results (default: 100)
- `offset` (int, optional): Pagination offset (default: 0)

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "admin_list_keys",
  "params": {
    "user_id": "alice",
    "include_revoked": false,
    "limit": 50
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "result": {
    "keys": [
      {
        "key_id": "key-abc123",
        "user_id": "alice",
        "subject_type": "user",
        "subject_id": "alice",
        "name": "Alice's API Key",
        "tenant_id": "default",
        "is_admin": false,
        "created_at": "2025-10-30T12:00:00Z",
        "expires_at": "2026-10-30T12:00:00Z",
        "revoked": false,
        "revoked_at": null,
        "last_used_at": "2025-10-30T14:30:00Z"
      }
    ],
    "total": 1
  }
}
```

**Note:** API key hashes and raw keys are never included in list responses.

---

### admin_get_key

Get details of a specific API key (admin only).

**Endpoint**: `POST /api/nfs/admin_get_key`

**Parameters:**
- `key_id` (string, required): Key identifier

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "admin_get_key",
  "params": {
    "key_id": "key-abc123"
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "result": {
    "key_id": "key-abc123",
    "user_id": "alice",
    "subject_type": "user",
    "subject_id": "alice",
    "name": "Alice's API Key",
    "tenant_id": "default",
    "is_admin": false,
    "created_at": "2025-10-30T12:00:00Z",
    "expires_at": "2026-10-30T12:00:00Z",
    "revoked": false,
    "revoked_at": null,
    "last_used_at": "2025-10-30T14:30:00Z"
  }
}
```

**Error Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "error": {
    "code": -32000,
    "message": "API key not found: key-abc123"
  }
}
```

---

### admin_revoke_key

Revoke an API key (admin only).

**Endpoint**: `POST /api/nfs/admin_revoke_key`

**Parameters:**
- `key_id` (string, required): Key identifier

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "method": "admin_revoke_key",
  "params": {
    "key_id": "key-abc123"
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "result": {
    "success": true,
    "key_id": "key-abc123"
  }
}
```

**Note:** Revoked keys cannot be restored. The user must request a new key.

---

### admin_update_key

Update API key properties (admin only).

**Endpoint**: `POST /api/nfs/admin_update_key`

**Parameters:**
- `key_id` (string, required): Key identifier
- `expires_days` (int, optional): New expiration in days from now
- `is_admin` (boolean, optional): Update admin privileges
- `name` (string, optional): Update key name

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 5,
  "method": "admin_update_key",
  "params": {
    "key_id": "key-abc123",
    "expires_days": 730,
    "name": "Alice's Production Key"
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 5,
  "result": {
    "key_id": "key-abc123",
    "user_id": "alice",
    "subject_type": "user",
    "subject_id": "alice",
    "name": "Alice's Production Key",
    "tenant_id": "default",
    "is_admin": false,
    "created_at": "2025-10-30T12:00:00Z",
    "expires_at": "2027-10-30T12:00:00Z",
    "revoked": false,
    "revoked_at": null,
    "last_used_at": "2025-10-30T14:30:00Z"
  }
}
```

**Safety Check:** Cannot remove admin privileges from the last admin key in the system.

---

## ReBAC Permissions

Relationship-Based Access Control (ReBAC) operations.

### Available Relations

ReBAC supports the following relation types for creating relationship tuples:

#### Direct Relations (Concrete)

These are concrete relations that grant specific permissions. **Always use these when creating tuples:**

- **`direct_owner`** - Full ownership (read, write, delete, share) - **Use this for granting ownership**
- **`direct_editor`** - Editor access (read, write) - **Use this for granting edit permissions**
- **`direct_viewer`** - Viewer access (read-only) - **Use this for granting read permissions**
- `parent` - Hierarchical parent relationship (for directory inheritance)
- `member` - Group membership

#### Computed Relations (Union/Intersection)

These relations are computed from direct relations during permission checks. **Do NOT use these when creating tuples:**

- `owner` - Computed union of direct_owner (used in permission checks only)
- `editor` - Computed union of direct_editor and direct_owner (used in permission checks only)
- `viewer` - Computed union of direct_viewer, direct_editor, and direct_owner (used in permission checks only)

#### Legacy Relations (Deprecated)

- `member-of` - Legacy group membership (use `member` instead)
- `owner-of` - Legacy ownership (use `direct_owner` instead)
- `viewer-of` - Legacy viewer (use `direct_viewer` instead)
- `editor-of` - Legacy editor (use `direct_editor` instead)

**Important:** When creating tuples with `rebac_create`, always use the **direct** relations (`direct_owner`, `direct_editor`, `direct_viewer`). The computed relations (`owner`, `editor`, `viewer`) are automatically expanded during permission checks via `rebac_check`.

### Available Object Types

- `file` - Files and directories (including workspaces)
- `workspace` - Registered workspaces (alias for file)
- `memory` - Agent memory storage
- `agent` - AI agents
- `user` - Human users
- `group` - User groups
- `tenant` - Multi-tenant organizations

### File Path Format Requirements

**Important:** When creating ReBAC tuples for file objects, the `object_id` MUST have a leading slash.

**Correct Format:**
```json
{
  "object": ["file", "/workspace/alice"]  // ✅ Correct - has leading slash
}
```

**Automatic Normalization:**
The system automatically normalizes paths during permission checks. If you create a tuple without a leading slash, it will still work due to automatic normalization, but it's recommended to always include the leading slash for consistency:

```json
{
  "object": ["file", "workspace/alice"]  // ⚠️ Will be normalized to "/workspace/alice"
}
```

**Why This Matters:**
- The router strips leading slashes from backend paths for relative path handling
- ReBAC tuples should use absolute paths (with leading slash) for consistency
- The permission enforcer automatically adds the leading slash if missing during checks

### rebac_create

Create a relationship tuple.

**Endpoint**: `POST /api/nfs/rebac_create`

**Parameters:**
- `subject` (tuple[string, string], required): Subject (type, id)
- `relation` (string, required): Relation name (see Available Relations above)
- `object` (tuple[string, string], required): Object (type, id)
- `expires_at` (string, optional): Expiration timestamp (ISO)
- `tenant_id` (string, optional): Tenant identifier

**Example Request (Grant ownership):**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "rebac_create",
  "params": {
    "subject": ["user", "alice"],
    "relation": "direct_owner",
    "object": ["file", "/workspace"],
    "tenant_id": "default"
  }
}
```

**Example Request (Grant editor access):**
```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "rebac_create",
  "params": {
    "subject": ["user", "bob"],
    "relation": "direct_editor",
    "object": ["file", "/workspace/project"],
    "tenant_id": "default"
  }
}
```

**Example Request (Grant viewer access):**
```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "rebac_create",
  "params": {
    "subject": ["user", "charlie"],
    "relation": "direct_viewer",
    "object": ["file", "/workspace/docs"],
    "tenant_id": "default"
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": "4594a9be-7a75-44c6-b96d-605c399ce8f7"
}
```

Note: The result is the tuple_id (string) of the created relationship.

**cURL Examples:**

```bash
# Grant admin user ownership of /workspace
curl -X POST http://localhost:8080/api/nfs/rebac_create \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "rebac_create",
    "params": {
      "subject": ["user", "admin"],
      "relation": "direct_owner",
      "object": ["file", "/workspace"]
    }
  }'

# Grant agent editor access to a file
curl -X POST http://localhost:8080/api/nfs/rebac_create \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "rebac_create",
    "params": {
      "subject": ["agent", "my_agent"],
      "relation": "direct_editor",
      "object": ["file", "/workspace/data.json"]
    }
  }'
```

---

### rebac_check

Check if subject has permission on object.

**Endpoint**: `POST /api/nfs/rebac_check`

**Parameters:**
- `subject` (tuple[string, string], required): Subject (type, id)
- `permission` (string, required): Permission name
- `object` (tuple[string, string], required): Object (type, id)
- `tenant_id` (string, optional): Tenant identifier

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 33,
  "method": "rebac_check",
  "params": {
    "subject": ["user", "alice"],
    "permission": "read",
    "object": ["file", "/documents/report.pdf"]
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 33,
  "result": {
    "allowed": true
  }
}
```

---

### rebac_expand

Find all subjects with a permission on an object.

**Endpoint**: `POST /api/nfs/rebac_expand`

**Parameters:**
- `permission` (string, required): Permission name
- `object` (tuple[string, string], required): Object (type, id)

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 34,
  "result": {
    "subjects": [
      ["user", "alice"],
      ["user", "bob"],
      ["group", "engineering"]
    ]
  }
}
```

---

### rebac_explain

Explain why a subject has permission on an object.

**Endpoint**: `POST /api/nfs/rebac_explain`

**Parameters:**
- `subject` (tuple[string, string], required): Subject (type, id)
- `permission` (string, required): Permission name
- `object` (tuple[string, string], required): Object (type, id)
- `tenant_id` (string, optional): Tenant identifier

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 35,
  "result": {
    "allowed": true,
    "path": [
      {
        "subject": ["user", "alice"],
        "relation": "member",
        "object": ["group", "engineering"]
      },
      {
        "subject": ["group", "engineering"],
        "relation": "owner",
        "object": ["file", "/documents/report.pdf"]
      }
    ]
  }
}
```

---

### rebac_delete

Delete a relationship tuple.

**Endpoint**: `POST /api/nfs/rebac_delete`

**Parameters:**
- `tuple_id` (string, required): Tuple identifier

---

### rebac_list_tuples

List relationship tuples.

**Endpoint**: `POST /api/nfs/rebac_list_tuples`

**Parameters:**
- `subject` (tuple[string, string], optional): Filter by subject
- `relation` (string, optional): Filter by relation
- `object` (tuple[string, string], optional): Filter by object

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 36,
  "result": [
    {
      "tuple_id": "tuple-abc123",
      "subject": ["user", "alice"],
      "relation": "owner",
      "object": ["file", "/documents/report.pdf"],
      "created_at": "2025-01-15T10:00:00Z"
    }
  ]
}
```

---

## Versioning Operations

### get_version

Get a specific version of a file.

**Endpoint**: `POST /api/nfs/get_version`

**Parameters:**
- `path` (string, required): File path
- `version` (int, required): Version number

---

### list_versions

List all versions of a file.

**Endpoint**: `POST /api/nfs/list_versions`

**Parameters:**
- `path` (string, required): File path

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 37,
  "result": [
    {
      "version": 3,
      "etag": "abc123",
      "size": 1024,
      "modified_at": "2025-01-15T12:00:00Z"
    },
    {
      "version": 2,
      "etag": "def456",
      "size": 512,
      "modified_at": "2025-01-15T10:00:00Z"
    },
    {
      "version": 1,
      "etag": "ghi789",
      "size": 256,
      "modified_at": "2025-01-15T08:00:00Z"
    }
  ]
}
```

---

### rollback

Rollback a file to a previous version.

**Endpoint**: `POST /api/nfs/rollback`

**Parameters:**
- `path` (string, required): File path
- `version` (int, required): Version number to rollback to

---

### diff_versions

Compare two versions of a file.

**Endpoint**: `POST /api/nfs/diff_versions`

**Parameters:**
- `path` (string, required): File path
- `v1` (int, required): First version number
- `v2` (int, required): Second version number
- `mode` (string, optional): Diff mode (default: "metadata")

---

## Namespace Management

### namespace_create

Create a ReBAC namespace configuration.

**Endpoint**: `POST /api/nfs/namespace_create`

**Parameters:**
- `object_type` (string, required): Object type name
- `config` (object, required): Namespace configuration

---

### namespace_get

Get namespace configuration.

**Endpoint**: `POST /api/nfs/namespace_get`

**Parameters:**
- `object_type` (string, required): Object type name

---

### namespace_list

List all namespace configurations.

**Endpoint**: `POST /api/nfs/namespace_list`

**Parameters:** None

---

### namespace_delete

Delete a namespace configuration.

**Endpoint**: `POST /api/nfs/namespace_delete`

**Parameters:**
- `object_type` (string, required): Object type name

---

### get_available_namespaces

Get all available ReBAC namespaces.

**Endpoint**: `POST /api/nfs/get_available_namespaces`

**Parameters:** None

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 38,
  "result": {
    "namespaces": ["file", "workspace", "agent", "memory"]
  }
}
```

---

## OAuth Management

Nexus provides OAuth 2.0 authentication support for connecting to external services like Google Drive. These APIs allow you to manage OAuth credentials, get authorization URLs, and exchange authorization codes for access tokens.

### oauth_exchange_code

Exchange OAuth authorization code for access tokens and store credentials.

**Endpoint**: `POST /api/nfs/oauth_exchange_code`

**Parameters:**
- `provider` (string, required): OAuth provider name (e.g., "google")
- `code` (string, required): Authorization code from OAuth callback
- `user_email` (string, required): User email address for credential storage
- `state` (string, optional): CSRF state token for validation
- `redirect_uri` (string, optional): OAuth redirect URI (must match authorization request)

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "oauth_exchange_code",
  "params": {
    "provider": "google",
    "code": "4/0AY0e-g5xyz...",
    "user_email": "user@example.com",
    "state": "csrf_state_token_abc123",
    "redirect_uri": "http://localhost:3000/oauth/callback"
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "credential_id": "cred_abc123",
    "user_email": "user@example.com",
    "expires_at": "2025-11-20T12:00:00Z",
    "success": true
  }
}
```

**Notes:**
- Authorization codes expire after ~10 minutes
- Codes can only be used once
- Tokens are encrypted and stored in the Nexus database
- Refresh tokens enable automatic token renewal

---

### oauth_list_credentials

List all OAuth credentials for the current user or tenant.

**Endpoint**: `POST /api/nfs/oauth_list_credentials`

**Parameters:**
- `provider` (string, optional): Filter by provider (e.g., "google")
- `include_revoked` (boolean, optional): Include revoked credentials. Default: false

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "oauth_list_credentials",
  "params": {
    "provider": "google",
    "include_revoked": false
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": [
    {
      "credential_id": "cred_abc123",
      "provider": "google",
      "user_email": "user@example.com",
      "scopes": [
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/drive.file"
      ],
      "expires_at": "2025-11-20T12:00:00Z",
      "created_at": "2025-11-19T10:00:00Z",
      "last_used_at": "2025-11-19T11:30:00Z",
      "revoked": false
    }
  ]
}
```

---

### oauth_revoke_credential

Revoke an OAuth credential.

**Endpoint**: `POST /api/nfs/oauth_revoke_credential`

**Parameters:**
- `provider` (string, required): OAuth provider name (e.g., "google")
- `user_email` (string, required): User email address

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "oauth_revoke_credential",
  "params": {
    "provider": "google",
    "user_email": "user@example.com"
  }
}
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "success": true
  }
}
```

**Notes:**
- Revoked credentials cannot be used for API access
- Mounts using revoked credentials will fail authentication
- Users must re-authorize to restore access

---

### oauth_test_credential

Test if an OAuth credential is valid and can be refreshed.

**Endpoint**: `POST /api/nfs/oauth_test_credential`

**Parameters:**
- `provider` (string, required): OAuth provider name (e.g., "google")
- `user_email` (string, required): User email address

**Example Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "oauth_test_credential",
  "params": {
    "provider": "google",
    "user_email": "user@example.com"
  }
}
```

**Example Response (Valid):**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "valid": true,
    "refreshed": true,
    "expires_at": "2025-11-20T12:00:00Z"
  }
}
```

**Example Response (Invalid):**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "valid": false,
    "error": "Refresh token revoked or expired"
  }
}
```

**Notes:**
- Automatically attempts to refresh expired tokens
- Returns `refreshed: true` if token was refreshed
- Useful for verifying credentials before creating mounts

---

## Complete Method Reference

### Summary Table

| Method | Category | Description |
|--------|----------|-------------|
| `read` | File Operations | Read file content |
| `write` | File Operations | Write file content |
| `delete` | File Operations | Delete file |
| `rename` | File Operations | Rename/move file |
| `exists` | File Operations | Check file exists |
| `get_metadata` | File Operations | Get file metadata |
| `mkdir` | Directory Operations | Create directory |
| `rmdir` | Directory Operations | Remove directory |
| `list` | Directory Operations | List directory |
| `is_directory` | Directory Operations | Check if directory |
| `glob` | Search | Find files by pattern |
| `grep` | Search | Search file contents |
| `register_workspace` | Workspace | Register workspace |
| `unregister_workspace` | Workspace | Unregister workspace |
| `list_workspaces` | Workspace | List workspaces |
| `get_workspace_info` | Workspace | Get workspace info |
| `workspace_snapshot` | Workspace | Create snapshot |
| `workspace_restore` | Workspace | Restore snapshot |
| `workspace_log` | Workspace | List snapshots |
| `workspace_diff` | Workspace | Compare snapshots |
| `add_mount` | Mount Management | Add dynamic backend mount |
| `remove_mount` | Mount Management | Remove backend mount |
| `list_mounts` | Mount Management | List all active mounts |
| `get_mount` | Mount Management | Get mount details |
| `has_mount` | Mount Management | Check if mount exists |
| `save_mount` | Mount Management | Save mount configuration |
| `list_saved_mounts` | Mount Management | List saved mounts |
| `load_mount` | Mount Management | Load and activate mount |
| `delete_saved_mount` | Mount Management | Delete saved mount |
| `register_memory` | Memory | Register memory |
| `unregister_memory` | Memory | Unregister memory |
| `list_memories` | Memory | List memories |
| `get_memory_info` | Memory | Get memory info |
| `start_trajectory` | Memory | Start trajectory |
| `log_trajectory_step` | Memory | Log trajectory step |
| `complete_trajectory` | Memory | Complete trajectory |
| `query_trajectories` | Memory | Query trajectories |
| `store_memory` | Memory | Store memory |
| `query_memories` | Memory | Query memories |
| `batch_reflect` | Memory | Batch reflection |
| `get_playbook` | Memory | Get playbook |
| `curate_playbook` | Memory | Curate playbook |
| `query_playbooks` | Memory | Query playbooks |
| `process_relearning` | Memory | Process re-learning |
| `register_agent` | Agent | Register agent |
| `list_agents` | Agent | List agents |
| `get_agent` | Agent | Get agent info |
| `delete_agent` | Agent | Delete agent |
| `sandbox_create` | Sandbox | Create sandbox |
| `sandbox_run` | Sandbox | Run code in sandbox |
| `sandbox_pause` | Sandbox | Pause sandbox |
| `sandbox_resume` | Sandbox | Resume sandbox |
| `sandbox_stop` | Sandbox | Stop sandbox |
| `sandbox_list` | Sandbox | List sandboxes |
| `sandbox_status` | Sandbox | Get sandbox status |
| `sandbox_connect` | Sandbox | Connect to sandbox |
| `sandbox_disconnect` | Sandbox | Disconnect from sandbox |
| `admin_create_key` | Admin | Create API key |
| `admin_list_keys` | Admin | List API keys |
| `admin_get_key` | Admin | Get API key details |
| `admin_revoke_key` | Admin | Revoke API key |
| `admin_update_key` | Admin | Update API key |
| `rebac_create` | ReBAC | Create relationship |
| `rebac_check` | ReBAC | Check permission |
| `rebac_expand` | ReBAC | Expand permissions |
| `rebac_explain` | ReBAC | Explain permission |
| `rebac_delete` | ReBAC | Delete relationship |
| `rebac_list_tuples` | ReBAC | List relationships |
| `get_version` | Versioning | Get file version |
| `list_versions` | Versioning | List versions |
| `rollback` | Versioning | Rollback version |
| `diff_versions` | Versioning | Compare versions |
| `namespace_create` | Namespace | Create namespace |
| `namespace_get` | Namespace | Get namespace |
| `namespace_list` | Namespace | List namespaces |
| `namespace_delete` | Namespace | Delete namespace |
| `get_available_namespaces` | Namespace | Get available namespaces |

**Total: 76 RPC Methods** (9 Mount Management methods added)

---

## Python Client Example

```python
from nexus.remote.client import RemoteNexusClient

# Connect to server
client = RemoteNexusClient(
    url="http://localhost:8765",
    api_key="your-api-key"
)

# File operations
client.write("/file.txt", b"Hello, World!")
content = client.read("/file.txt")
files = client.list("/")

# Workspace operations
client.register_workspace("/my-workspace", name="main")
client.workspace_snapshot("/my-workspace", description="Initial state")

# Memory operations
traj = client.start_trajectory("Implement feature X")
client.log_trajectory_step(traj["trajectory_id"], "write", "Created module")
client.complete_trajectory(traj["trajectory_id"], "success")

# ReBAC permissions
client.rebac_create(
    subject=("user", "alice"),
    relation="owner",
    object=("file", "/file.txt")
)

allowed = client.rebac_check(
    subject=("user", "alice"),
    permission="read",
    object=("file", "/file.txt")
)

# Close connection
client.close()
```

---

## CORS Support

CORS headers are automatically included for browser-based clients:

- `Access-Control-Allow-Origin: *`
- `Access-Control-Allow-Methods: GET, POST, OPTIONS`
- `Access-Control-Allow-Headers: Content-Type, Authorization`
- `Access-Control-Max-Age: 86400`

---

## Production Deployment

### Security Best Practices

1. **Always use API keys** (`--api-key` flag)
2. **Use HTTPS** via reverse proxy (Nginx, Caddy)
3. **Restrict host binding** (`--host 127.0.0.1` for local-only)
4. **Enable permissions** (`enforce_permissions=True`)
5. **Use identity headers** (`X-Nexus-Subject`)
6. **Enable audit logging**
7. **Rotate API keys regularly**

### Example Nginx Configuration

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

---

## See Also

- [RPC API Documentation](docs/api/rpc-api.md) - Additional examples
- [CLI Reference](docs/api/cli-reference.md) - Server commands
- [Configuration](docs/api/configuration.md) - Server configuration
- [Permissions](docs/api/permissions.md) - Access control

---

**Last Updated**: 2025-11-17
**Version**: 0.5.1+ (Mount Management API added)
