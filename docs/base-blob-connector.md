# Base Blob Storage Connector

The `BaseBlobStorageConnector` is an abstract base class that provides shared functionality for cloud blob storage connector backends (S3, GCS, Azure Blob, MinIO, etc.).

## Overview

This base class eliminates code duplication across blob storage connectors by implementing common patterns while allowing cloud-specific customization through abstract methods.

### Design Philosophy

The base class follows the Template Method pattern:
- **Shared logic** is implemented in the base class (path mapping, content-type detection, directory operations)
- **Cloud-specific operations** are defined as abstract methods for subclasses to implement
- **Consistent interface** ensures all blob storage connectors behave predictably

## Architecture

```
Backend (interface)
    │
    └── BaseBlobStorageConnector (abstract class)
            │
            ├── S3ConnectorBackend
            ├── GCSConnectorBackend
            └── AzureBlobConnectorBackend (future)
```

## Shared Functionality

The base class implements these operations:

### Content Operations
- `write_content()` - Write file to cloud storage
- `read_content()` - Read file from cloud storage
- `delete_content()` - Delete file from cloud storage
- `content_exists()` - Check if file exists
- `get_content_size()` - Get file size
- `get_ref_count()` - Always returns 1 (no deduplication)

### Directory Operations
- `mkdir()` - Create directory marker
- `rmdir()` - Remove directory
- `is_directory()` - Check if path is a directory
- `list_dir()` - List directory contents

### File Operations
- `rename_file()` - Rename/move file using cloud-specific copy

### Helper Methods
- `_compute_hash()` - SHA-256 hash computation
- `_detect_content_type()` - MIME type detection with UTF-8 support
- `_get_blob_path()` - Convert backend path to cloud path with prefix

## Abstract Methods

Subclasses must implement these cloud-specific operations:

```python
@abstractmethod
def _upload_blob(self, blob_path: str, content: bytes, content_type: str) -> str:
    """Upload blob to cloud storage. Returns version ID or hash."""
    pass

@abstractmethod
def _download_blob(self, blob_path: str, version_id: str | None = None) -> bytes:
    """Download blob from cloud storage."""
    pass

@abstractmethod
def _delete_blob(self, blob_path: str) -> None:
    """Delete blob from cloud storage."""
    pass

@abstractmethod
def _blob_exists(self, blob_path: str) -> bool:
    """Check if blob exists."""
    pass

@abstractmethod
def _get_blob_size(self, blob_path: str) -> int:
    """Get blob size in bytes."""
    pass

@abstractmethod
def _list_blobs(self, prefix: str, delimiter: str = "/") -> tuple[list[str], list[str]]:
    """List blobs with prefix. Returns (blob_keys, common_prefixes)."""
    pass

@abstractmethod
def _create_directory_marker(self, blob_path: str) -> None:
    """Create directory marker object."""
    pass

@abstractmethod
def _copy_blob(self, source_path: str, dest_path: str) -> None:
    """Copy blob to new location."""
    pass
```

## Implementing a New Connector

Here's how to add support for a new blob storage provider:

```python
from nexus.backends.base_blob_connector import BaseBlobStorageConnector
from nexus.core.exceptions import BackendError, NexusFileNotFoundError

class AzureBlobConnectorBackend(BaseBlobStorageConnector):
    """Azure Blob Storage connector with direct path mapping."""

    def __init__(
        self,
        container_name: str,
        account_name: str,
        account_key: str | None = None,
        prefix: str = "",
    ):
        # Initialize Azure client
        from azure.storage.blob import BlobServiceClient

        connection_string = f"DefaultEndpointsProtocol=https;AccountName={account_name};AccountKey={account_key}"
        self.blob_service_client = BlobServiceClient.from_connection_string(connection_string)
        self.container_client = self.blob_service_client.get_container_client(container_name)

        # Check versioning
        properties = self.container_client.get_container_properties()
        versioning_enabled = properties.get("versioning", False)

        # Initialize base class
        super().__init__(
            bucket_name=container_name,
            prefix=prefix,
            versioning_enabled=versioning_enabled,
        )

    @property
    def name(self) -> str:
        return "azure_blob_connector"

    def _upload_blob(self, blob_path: str, content: bytes, content_type: str) -> str:
        """Upload to Azure Blob Storage."""
        blob_client = self.container_client.get_blob_client(blob_path)
        blob_client.upload_blob(
            content,
            overwrite=True,
            content_settings=ContentSettings(content_type=content_type)
        )

        # Return version ID if versioning enabled
        if self.versioning_enabled:
            properties = blob_client.get_blob_properties()
            return properties.version_id
        else:
            return self._compute_hash(content)

    def _download_blob(self, blob_path: str, version_id: str | None = None) -> bytes:
        """Download from Azure Blob Storage."""
        blob_client = self.container_client.get_blob_client(blob_path)

        if version_id:
            blob_client = blob_client.with_snapshot(version_id)

        try:
            downloader = blob_client.download_blob()
            return downloader.readall()
        except ResourceNotFoundError:
            raise NexusFileNotFoundError(blob_path)

    # Implement other abstract methods...
```

## Benefits

### Code Reuse
- **~500 lines of shared code** across all blob storage connectors
- Only ~400-450 lines of cloud-specific code per connector
- DRY principle applied to common operations

### Consistency
- All blob storage connectors have identical behavior for:
  - Path mapping and prefix handling
  - Content-Type detection
  - Directory operations
  - Error handling patterns

### Extensibility
- Easy to add new cloud providers (Azure, MinIO, Backblaze B2, etc.)
- Consistent testing patterns across connectors
- Shared improvements benefit all implementations

### Maintainability
- Bug fixes in base class automatically apply to all connectors
- Easier to understand connector implementations
- Clear separation of concerns

## Implementation Details

### Path Mapping

The base class handles path normalization and prefix handling:

```python
def _get_blob_path(self, backend_path: str) -> str:
    """Convert backend-relative path to full blob path."""
    backend_path = backend_path.lstrip("/")
    if self.prefix:
        if backend_path:
            return f"{self.prefix}/{backend_path}"
        else:
            return self.prefix
    return backend_path
```

Usage:
- Input: `/workspace/file.txt` (backend path)
- Prefix: `nexus-data`
- Output: `nexus-data/workspace/file.txt` (cloud storage path)

### Content-Type Detection

Automatically detects MIME types and adds UTF-8 charset for text files:

```python
def _detect_content_type(self, backend_path: str, content: bytes) -> str:
    """Detect Content-Type with UTF-8 charset for text files."""
    content_type, _ = mimetypes.guess_type(backend_path)

    if not content_type or content_type.startswith("text/"):
        try:
            content.decode("utf-8")
            if content_type and content_type.startswith("text/"):
                return f"{content_type}; charset=utf-8"
            else:
                return "text/plain; charset=utf-8"
        except UnicodeDecodeError:
            return content_type or "application/octet-stream"

    return content_type
```

This ensures proper display in cloud consoles (S3 Console, GCP Console, Azure Portal).

### Directory Operations

Blob storage doesn't have native directories, so the base class implements virtual directories using marker objects:

**Directory Marker**: Empty blob with trailing slash (`workspace/data/`)

**Virtual Directory**: Prefix that has blobs under it (no marker needed)

The base class handles both patterns consistently:

```python
def is_directory(self, path: str) -> bool:
    """Check if path is a directory."""
    blob_path = self._get_blob_path(path)

    # Check 1: Explicit directory marker
    if self._blob_exists(blob_path + "/"):
        return True

    # Check 2: Virtual directory (has children)
    blobs, prefixes = self._list_blobs(prefix=blob_path + "/", delimiter="/")
    return len(blobs) > 0 or len(prefixes) > 0
```

### Versioning Support

The base class provides a pattern for version handling:

```python
def _is_version_id(self, value: str) -> bool:
    """Check if value is a version ID (not a content hash)."""
    # Default: 64-char hex strings are hashes
    if len(value) == 64:
        try:
            int(value, 16)
            return False  # It's a hash
        except ValueError:
            pass
    return True  # Likely a version ID
```

Subclasses can override for cloud-specific logic:
- **S3**: Keep default (version IDs are opaque strings)
- **GCS**: Override to check `value.isdigit()` (generation numbers)

## Testing

The base class enables consistent testing patterns:

```python
@pytest.fixture
def mock_connector(mock_cloud_client):
    """Create connector with mocked cloud client."""
    return MyConnectorBackend(...)

def test_write_content(mock_connector):
    """Test writing content (base class logic)."""
    context = OperationContext(user="test", groups=[], backend_path="file.txt")
    content = b"test content"

    version = mock_connector.write_content(content, context=context)

    # Verify cloud-specific upload was called
    mock_connector._upload_blob.assert_called_once()
```

Test the base class logic once, then only test cloud-specific behavior in subclass tests.

## Related Documentation

- [S3 Connector Backend](./s3-connector-backend.md) - AWS S3 implementation
- [GCS Connector Backend](./gcs-connector-backend.md) - Google Cloud Storage implementation
- [Local Backend](./local-backend.md) - Local filesystem backend
- [Backend Interface](./backend-interface.md) - Base Backend interface
