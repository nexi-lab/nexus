# Google Drive Connector Backend

**Version:** 0.7.0
**Issue:** #136
**Type:** Path-based connector backend (not CAS)

## Overview

The Google Drive connector backend provides seamless integration with Google Drive using OAuth 2.0 authentication. Unlike CAS-based backends, this is a **path-based connector** that stores files at their actual paths in Google Drive, making them browsable by external tools.

### Key Features

- ✅ **OAuth 2.0 Authentication** - User-scoped credentials via TokenManager
- ✅ **Path-based Storage** - Files stored at actual paths (not content hash)
- ✅ **Automatic Token Refresh** - Handles expired tokens automatically
- ✅ **Google Workspace Export** - Export Docs/Sheets/Slides to various formats
- ✅ **Folder Hierarchy** - Maintains directory structure in Drive
- ✅ **Shared Drive Support** - Optional shared drive integration
- ✅ **User-scoped** - Each user has their own Drive access

### Architecture

```
Google Drive/
├── nexus-data/              # Root folder (configurable)
│   ├── workspace/
│   │   ├── file.txt         # Regular file
│   │   └── data/
│   │       └── output.json
│   └── reports/
│       ├── report.gdoc      # Google Docs (exportable)
│       └── data.gsheet      # Google Sheets (exportable)
```

**Path-based vs CAS-based:**
- ❌ No content deduplication (Drive handles this)
- ❌ No content-addressable storage
- ✅ Files browsable in Google Drive web UI
- ✅ External tools can access files
- ✅ Natural file paths preserved

## Quick Start

### 1. Install Dependencies

```bash
pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib
```

### 2. Setup OAuth Credentials

First, create OAuth 2.0 credentials in [Google Cloud Console](https://console.cloud.google.com/):

1. Go to **APIs & Services > Credentials**
2. Click **Create Credentials > OAuth client ID**
3. Choose **Desktop app**
4. Download credentials JSON

### 3. Authorize Nexus

```bash
# Setup Google Drive OAuth for a user
nexus oauth setup-gdrive \
    --client-id "123456789.apps.googleusercontent.com" \
    --client-secret "GOCSPX-..." \
    --user-email "alice@example.com"
```

**Interactive flow:**
1. Command opens browser for OAuth consent
2. User grants permission
3. Tokens stored encrypted in database
4. Automatic refresh when expired

### 4. Use Google Drive Backend

```python
from nexus import NexusFS
from nexus.backends import GoogleDriveConnectorBackend
from nexus.server.auth import TokenManager

# Initialize token manager
token_manager = TokenManager(db_path="~/.nexus/nexus.db")

# Create Google Drive backend
backend = GoogleDriveConnectorBackend(
    token_manager=token_manager,
    root_folder="nexus-data",  # Root folder in Drive
    use_shared_drives=False,
)

# Initialize Nexus with Drive backend
nx = NexusFS(backend=backend)

# Write file (with user context)
from nexus.core.permissions import OperationContext

context = OperationContext(
    user_id="alice@example.com",  # User's email
    tenant_id="org_acme",
    backend_path="/workspace/file.txt",  # Path in Drive
)

await nx.write("/workspace/file.txt", b"Hello Google Drive!", context=context)

# Read file
content = await nx.read("/workspace/file.txt", context=context)
print(content)  # b"Hello Google Drive!"
```

## Google Workspace File Export

The connector automatically exports Google Workspace files to requested formats:

### Supported Export Formats

| File Type | Formats | Default |
|-----------|---------|---------|
| **Google Docs** | pdf, docx, odt, html, txt | docx |
| **Google Sheets** | pdf, xlsx, ods, csv, tsv | xlsx |
| **Google Slides** | pdf, pptx, odp, txt | pptx |

### Export by Extension

```python
# Export Google Doc as PDF (by filename extension)
pdf_content = await nx.read("/reports/report.gdoc.pdf", context=context)

# Export Google Sheet as CSV
csv_content = await nx.read("/data/sales.gsheet.csv", context=context)

# Export Google Slides as PPTX (default)
pptx_content = await nx.read("/presentations/demo.gslides", context=context)
```

### Format Detection

The connector detects export format from filename extension:
- `report.gdoc.pdf` → Export as PDF
- `report.gdoc.docx` → Export as DOCX
- `report.gdoc` → Export as DOCX (default)

## Configuration

### Basic Configuration

```python
backend = GoogleDriveConnectorBackend(
    token_manager=token_manager,
    root_folder="nexus-data",      # Root folder name in Drive
    use_shared_drives=False,        # Use shared drives?
    shared_drive_id=None,           # Shared drive ID (if applicable)
    provider="google",              # OAuth provider name
)
```

### Shared Drive Configuration

```python
# Use a shared drive (Google Workspace)
backend = GoogleDriveConnectorBackend(
    token_manager=token_manager,
    root_folder="team-data",
    use_shared_drives=True,
    shared_drive_id="0AB1CDEfghIJKL",  # Your shared drive ID
)
```

### Multi-Tenant Setup

```python
# Different users, same tenant
context_alice = OperationContext(
    user_id="alice@acme.com",
    tenant_id="org_acme",
    backend_path="/workspace/alice-file.txt",
)

context_bob = OperationContext(
    user_id="bob@acme.com",
    tenant_id="org_acme",
    backend_path="/workspace/bob-file.txt",
)

# Each user has their own Drive credentials
await nx.write("/file1.txt", b"Alice's file", context=context_alice)
await nx.write("/file2.txt", b"Bob's file", context=context_bob)
```

## CLI Commands

### Setup OAuth

```bash
# Interactive OAuth setup
nexus oauth setup-gdrive \
    --client-id "123.apps.googleusercontent.com" \
    --client-secret "GOCSPX-..." \
    --user-email "alice@example.com"
```

### List Credentials

```bash
# List all OAuth credentials
nexus oauth list

# Filter by provider
nexus oauth list | grep google
```

### Test Credentials

```bash
# Test if credentials are valid (auto-refreshes if needed)
nexus oauth test google alice@example.com
```

### Revoke Credentials

```bash
# Revoke credentials
nexus oauth revoke google alice@example.com
```

## Operation Context

The Google Drive connector is **user-scoped**, meaning it requires `OperationContext` with:

```python
context = OperationContext(
    user_id="user@example.com",     # REQUIRED: User's email
    tenant_id="org_acme",            # Optional: Tenant ID
    backend_path="/path/to/file",   # REQUIRED: Path in Drive
)
```

**Why backend_path?**
Path-based connectors don't use content hashes. The `backend_path` tells the connector where to store/retrieve the file in Google Drive.

## Security

### Token Storage

- **Encryption**: Tokens encrypted at rest using Fernet (AES-128 + HMAC-SHA256)
- **Key Management**: Encryption key stored in `NEXUS_OAUTH_ENCRYPTION_KEY` env var
- **Database**: Tokens stored in `oauth_credentials` table with tenant isolation

### Automatic Token Refresh

```python
# TokenManager handles refresh automatically
access_token = await token_manager.get_valid_token(
    provider="google",
    user_email="alice@example.com",
    tenant_id="org_acme",
)

# If token expired:
# 1. TokenManager detects expiry
# 2. Calls Google OAuth refresh endpoint
# 3. Updates database with new tokens
# 4. Returns valid access token
```

### Tenant Isolation

```python
# Tenant A
context_a = OperationContext(user_id="alice@acme.com", tenant_id="org_acme")

# Tenant B
context_b = OperationContext(user_id="alice@other.com", tenant_id="org_other")

# Different credentials used (isolated by tenant_id)
```

## Error Handling

### Common Errors

```python
from nexus.core.exceptions import BackendError, NexusFileNotFoundError
from nexus.core.exceptions import AuthenticationError

try:
    content = await nx.read("/file.txt", context=context)
except NexusFileNotFoundError:
    # File not found in Drive
    print("File does not exist")
except AuthenticationError:
    # OAuth token invalid/expired and refresh failed
    print("Authentication failed - reauthorize")
except BackendError as e:
    # Other Drive API errors
    print(f"Drive error: {e}")
```

### Rate Limiting

Google Drive API has rate limits:
- **1,000 requests per 100 seconds per user**
- **10,000 requests per 100 seconds per project**

The connector doesn't implement automatic rate limiting yet. For high-volume operations, implement exponential backoff:

```python
import time
from googleapiclient.errors import HttpError

def with_retry(func, max_retries=5):
    for i in range(max_retries):
        try:
            return func()
        except HttpError as e:
            if e.resp.status == 429:  # Rate limit
                wait = 2 ** i  # Exponential backoff
                time.sleep(wait)
            else:
                raise
```

## Performance Considerations

### Folder Caching

The connector caches folder IDs to reduce API calls:

```python
# First access: API call to resolve folder
await nx.write("/workspace/data/file1.txt", b"content1", context=context)

# Subsequent access: Uses cached folder ID
await nx.write("/workspace/data/file2.txt", b"content2", context=context)  # Faster
```

### Batch Operations

For batch uploads, use async operations:

```python
import asyncio

async def upload_files(files):
    tasks = [
        nx.write(path, content, context=context)
        for path, content in files
    ]
    await asyncio.gather(*tasks)

# Upload 100 files concurrently
files = [(f"/file{i}.txt", f"content{i}".encode()) for i in range(100)]
await upload_files(files)
```

### Large Files

For files > 5MB, Drive uses resumable uploads automatically (handled by google-api-python-client).

## Limitations

### No Content Deduplication

Unlike CAS backends, the Drive connector doesn't deduplicate:

```python
# Same content = stored twice
await nx.write("/file1.txt", b"same content", context=context)
await nx.write("/file2.txt", b"same content", context=context)
# Result: 2 files in Drive (not deduplicated)
```

### Requires OperationContext

All operations require context with `backend_path`:

```python
# ❌ This will fail
await nx.write("/file.txt", b"content")  # No context

# ✅ This works
await nx.write("/file.txt", b"content", context=context)
```

### Google Workspace File Limitations

- Can't write to Google Docs/Sheets/Slides directly
- Can only read/export them
- To create Workspace files, use Drive web UI or API

## Troubleshooting

### "No OAuth credential found"

**Problem:** User hasn't authorized Google Drive access.

**Solution:**
```bash
nexus oauth setup-gdrive \
    --client-id "..." \
    --client-secret "..." \
    --user-email "user@example.com"
```

### "Failed to refresh token"

**Problem:** Refresh token revoked or expired.

**Solution:**
1. Revoke old credentials: `nexus oauth revoke google user@example.com`
2. Re-authorize: `nexus oauth setup-gdrive ...`

### "Quota exceeded"

**Problem:** Too many API requests.

**Solution:**
- Implement rate limiting/backoff
- Reduce request frequency
- Request quota increase in Google Cloud Console

### "Folder not found"

**Problem:** Root folder doesn't exist or was deleted.

**Solution:**
- Connector auto-creates root folder on first use
- Check Drive web UI for folder existence
- Verify folder name matches `root_folder` parameter

## Examples

### Example 1: Personal Drive Mount

```python
from nexus import NexusFS
from nexus.backends import GoogleDriveConnectorBackend
from nexus.server.auth import TokenManager
from nexus.core.permissions import OperationContext

# Setup
token_manager = TokenManager(db_path="~/.nexus/nexus.db")
backend = GoogleDriveConnectorBackend(
    token_manager=token_manager,
    root_folder="nexus-personal",
)

nx = NexusFS(backend=backend)

context = OperationContext(
    user_id="alice@gmail.com",
    backend_path="/workspace/notes.txt",
)

# Write notes
await nx.write("/workspace/notes.txt", b"My personal notes", context=context)

# Read notes
notes = await nx.read("/workspace/notes.txt", context=context)
print(notes.decode())
```

### Example 2: Team Shared Drive

```python
# Team shared drive setup
backend = GoogleDriveConnectorBackend(
    token_manager=token_manager,
    root_folder="team-workspace",
    use_shared_drives=True,
    shared_drive_id="0AB1CDEfghIJKL",
)

nx = NexusFS(backend=backend)

# Different team members
context_alice = OperationContext(
    user_id="alice@acme.com",
    tenant_id="org_acme",
    backend_path="/projects/project-a/design.pdf",
)

context_bob = OperationContext(
    user_id="bob@acme.com",
    tenant_id="org_acme",
    backend_path="/projects/project-a/code.py",
)

# Collaborate on shared drive
await nx.write("/design.pdf", design_pdf, context=context_alice)
await nx.write("/code.py", code_bytes, context=context_bob)
```

### Example 3: Export Google Docs

```python
# Export Google Doc to multiple formats
context = OperationContext(
    user_id="alice@example.com",
    backend_path="/reports/monthly-report.gdoc",
)

# Export as PDF
pdf = await nx.read("/reports/monthly-report.gdoc.pdf", context=context)

# Export as DOCX
docx = await nx.read("/reports/monthly-report.gdoc.docx", context=context)

# Export as plain text
txt = await nx.read("/reports/monthly-report.gdoc.txt", context=context)

# Save locally
with open("report.pdf", "wb") as f:
    f.write(pdf)
```

## API Reference

### GoogleDriveConnectorBackend

```python
class GoogleDriveConnectorBackend(Backend):
    def __init__(
        self,
        token_manager: TokenManager,
        root_folder: str = "nexus-data",
        use_shared_drives: bool = False,
        shared_drive_id: str | None = None,
        provider: str = "google",
    )

    @property
    def name(self) -> str
        # Returns: "gdrive"

    @property
    def user_scoped(self) -> bool
        # Returns: True (requires per-user OAuth)

    def write_content(self, content: bytes, context: OperationContext | None = None) -> str
        # Write file to Drive at backend_path

    def read_content(self, content_hash: str, context: OperationContext | None = None) -> bytes
        # Read file from Drive at backend_path (content_hash ignored)

    def delete_content(self, content_hash: str, context: OperationContext | None = None) -> bool
        # Delete file from Drive at backend_path

    def content_exists(self, content_hash: str, context: OperationContext | None = None) -> bool
        # Check if file exists at backend_path
```

## Related Documentation

- [OAuth Token Management](./oauth.md) - OAuth system details
- [Backend Interface](../src/nexus/backends/backend.py) - Backend interface specification
- [GCS Connector](../src/nexus/backends/gcs_connector.py) - Similar path-based connector

## References

- Issue #136: Google Drive Backend Support
- Issue #137: OAuth Token Management System
- Google Drive API v3: https://developers.google.com/drive/api/v3/about-sdk
- OAuth 2.0: https://developers.google.com/identity/protocols/oauth2
