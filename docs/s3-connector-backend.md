# S3 Connector Backend

The S3 Connector Backend provides direct path mapping to AWS S3 buckets, allowing Nexus to mount external S3 buckets where files remain at their original paths and are browsable by external tools.

## Overview

Unlike CAS (Content Addressable Storage) backends that store files by content hash, the S3 connector stores files at their actual paths in S3. This makes the bucket browsable by external tools like the AWS Console, AWS CLI, or any S3-compatible client.

### Key Features

- **Direct path mapping**: Files stored at actual paths (e.g., `file.txt` → `s3://bucket/prefix/file.txt`)
- **Write-through storage**: Changes are immediately reflected in S3
- **External tool compatibility**: Files are browsable via AWS Console, CLI, etc.
- **Versioning support**: Leverages S3 versioning if enabled on the bucket
- **Multiple authentication methods**: IAM roles, credentials file, environment variables
- **Automatic retry**: Built-in retry logic for transient errors (503, throttling, network issues)
- **Optimized operations**: Efficient rename/move operations using S3 native copy

### Use Cases

- Mount existing S3 buckets for agent access
- Store outputs that need to be accessible by other systems
- Integration with S3-based workflows and pipelines
- Multi-cloud deployments with AWS infrastructure

## Quick Start

### 1. Create an S3 Bucket

```bash
# Create a new bucket
aws s3 mb s3://my-nexus-bucket --region us-east-1

# Optional: Enable versioning
aws s3api put-bucket-versioning \
    --bucket my-nexus-bucket \
    --versioning-configuration Status=Enabled
```

### 2. Configure AWS Credentials

The S3 connector supports multiple authentication methods:

#### Option A: AWS CLI Configuration (Recommended)

```bash
aws configure
```

This creates `~/.aws/credentials` and `~/.aws/config`.

#### Option B: Environment Variables

```bash
export AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE
export AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
export AWS_DEFAULT_REGION=us-east-1
```

#### Option C: IAM Roles (for EC2/ECS/Lambda)

No configuration needed - credentials are automatically obtained from the instance metadata service.

### 3. Add to Nexus Configuration

Add the S3 connector to your `config.yaml`:

```yaml
backends:
  - type: s3_connector
    mount_point: /mnt/s3
    config:
      bucket: my-nexus-bucket
      region_name: us-east-1
      prefix: nexus-data
    priority: 10
    readonly: false
    description: "S3 bucket for agent outputs"
```

### 4. Use with Nexus

```python
from nexus.sdk import connect

nx = connect()

# Write a file
nx.write("/mnt/s3/hello.txt", b"Hello from Nexus!")

# Read a file
content = nx.read("/mnt/s3/hello.txt")
print(content.decode())  # "Hello from Nexus!"

# List files
files = nx.list("/mnt/s3")
print(files)  # ['hello.txt']
```

## Configuration Options

### Backend Configuration

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `bucket` | string | Yes | S3 bucket name |
| `region_name` | string | No | AWS region (e.g., 'us-east-1') |
| `prefix` | string | No | Path prefix for all files (default: "") |
| `credentials_path` | string | No | Path to JSON credentials file |
| `access_key_id` | string | No | AWS access key ID |
| `secret_access_key` | string | No | AWS secret access key |
| `session_token` | string | No | AWS session token (for temporary credentials) |

### Mount Configuration

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `mount_point` | string | Yes | Virtual path where backend is mounted |
| `priority` | int | No | Mount priority (higher takes precedence) |
| `readonly` | bool | No | Whether mount is read-only |
| `description` | string | No | Human-readable description |

### Example Configurations

#### Basic Configuration

```yaml
- type: s3_connector
  mount_point: /mnt/s3
  config:
    bucket: my-bucket
    region_name: us-east-1
```

#### With Prefix

```yaml
- type: s3_connector
  mount_point: /workspace/outputs
  config:
    bucket: my-bucket
    region_name: us-east-1
    prefix: agent-outputs/prod
```

#### With Explicit Credentials

```yaml
- type: s3_connector
  mount_point: /mnt/s3
  config:
    bucket: my-bucket
    region_name: us-east-1
    access_key_id: AKIAIOSFODNN7EXAMPLE
    secret_access_key: wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
```

#### Read-Only Mount

```yaml
- type: s3_connector
  mount_point: /mnt/s3-readonly
  config:
    bucket: shared-data-bucket
    region_name: us-west-2
  readonly: true
```

## Docker Integration

### Using docker-demo.sh

The `docker-demo.sh` script automatically detects and mounts AWS credentials:

```bash
# Uses ~/.aws/credentials by default
./docker-demo.sh

# Or specify custom credentials path
export AWS_CREDENTIALS_PATH=/path/to/credentials
./docker-demo.sh
```

### Manual Docker Configuration

Add AWS credentials to `docker-compose.yml`:

```yaml
services:
  nexus:
    environment:
      AWS_SHARED_CREDENTIALS_FILE: /app/aws-credentials
      AWS_CONFIG_FILE: /app/aws-config
      AWS_DEFAULT_REGION: us-east-1
    volumes:
      - ~/.aws/credentials:/app/aws-credentials:ro
      - ~/.aws/config:/app/aws-config:ro
```

## Authentication

### Credential Priority

The S3 connector uses credentials in this priority order:

1. **Explicit credentials** in config (`access_key_id`, `secret_access_key`)
2. **Credentials file** specified by `credentials_path`
3. **AWS default chain**:
   - Environment variables (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`)
   - Shared credentials file (`~/.aws/credentials`)
   - IAM role (EC2/ECS/Lambda)

### IAM Permissions

The IAM user/role needs these S3 permissions:

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "s3:GetObject",
                "s3:PutObject",
                "s3:DeleteObject",
                "s3:ListBucket",
                "s3:GetBucketVersioning"
            ],
            "Resource": [
                "arn:aws:s3:::my-nexus-bucket",
                "arn:aws:s3:::my-nexus-bucket/*"
            ]
        }
    ]
}
```

For read-only mounts, remove `PutObject` and `DeleteObject`.

## Versioning Support

If S3 versioning is enabled on the bucket, the connector automatically:

- Returns version IDs when writing files
- Allows reading specific versions using version ID
- Preserves all file versions for history

### Enable Versioning

```bash
aws s3api put-bucket-versioning \
    --bucket my-nexus-bucket \
    --versioning-configuration Status=Enabled
```

### Check Versioning Status

```bash
aws s3api get-bucket-versioning --bucket my-nexus-bucket
```

## Storage Structure

Files are stored at their actual paths in S3:

```
s3://bucket-name/
├── prefix/
│   ├── workspace/
│   │   ├── file.txt          # Actual file path
│   │   └── data/
│   │       └── output.json
│   └── another-dir/
│       └── report.pdf
```

This differs from CAS-based backends:

| Feature | CAS Backend | S3 Connector |
|---------|-------------|--------------|
| Storage Path | `cas/ab/cd/hash...` | `actual/path.txt` |
| Deduplication | Yes (ref counting) | No |
| External Browsable | No (hash-based) | Yes (path-based) |
| Use Case | Nexus-managed | External buckets |

## Best Practices

### Security

1. **Use IAM roles** in production instead of access keys
2. **Enable bucket encryption** for data at rest
3. **Use bucket policies** to restrict access
4. **Enable access logging** for audit trails
5. **Never commit credentials** to version control

### Performance

1. **Choose the right region** - same as your application
2. **Use S3 Transfer Acceleration** for large files
3. **Consider multipart uploads** for files >100MB (automatic in SDK)
4. **Minimize cross-region transfers**

### Cost Optimization

1. **Use S3 Intelligent-Tiering** for infrequently accessed data
2. **Enable lifecycle policies** to archive/delete old files
3. **Monitor S3 costs** with Cost Explorer
4. **Use S3 Inventory** to analyze storage usage

## Troubleshooting

### Common Issues

#### "Bucket does not exist"

```
BackendError: Bucket 'my-bucket' does not exist
```

**Solution**: Verify bucket name and region, or create the bucket:
```bash
aws s3 mb s3://my-bucket --region us-east-1
```

#### "Access Denied"

```
ClientError: An error occurred (AccessDenied) when calling the GetObject operation
```

**Solution**: Check IAM permissions and bucket policy. Ensure the credentials have required S3 permissions.

#### "NoCredentialsError"

```
botocore.exceptions.NoCredentialsError: Unable to locate credentials
```

**Solution**: Configure AWS credentials using one of the methods described above.

#### "Invalid Region"

```
botocore.exceptions.EndpointConnectionError
```

**Solution**: Verify the region name is correct (e.g., `us-east-1`, not `us-east`).

### Debug Mode

Enable debug logging for boto3:

```python
import logging
logging.getLogger('botocore').setLevel(logging.DEBUG)
```

### Verify Credentials

Test your credentials with AWS CLI:

```bash
aws sts get-caller-identity
aws s3 ls s3://my-bucket/
```

## API Reference

### S3ConnectorBackend Class

```python
from nexus.backends.s3_connector import S3ConnectorBackend

backend = S3ConnectorBackend(
    bucket_name="my-bucket",
    region_name="us-east-1",
    prefix="nexus-data",
    access_key_id=None,         # Optional: explicit credentials
    secret_access_key=None,     # Optional: explicit credentials
    session_token=None,         # Optional: for temporary credentials
    credentials_path=None,      # Optional: path to credentials file
)

# Properties
backend.name                    # "s3_connector"
backend.bucket_name             # "my-bucket"
backend.prefix                  # "nexus-data"
backend.versioning_enabled      # True/False
```

### Operations

```python
from nexus.core.permissions import OperationContext

context = OperationContext(user="agent", groups=[], backend_path="file.txt")

# Write content
version_id = backend.write_content(b"content", context=context)

# Read content
content = backend.read_content(version_id, context=context)

# Delete content
backend.delete_content(version_id, context=context)

# Check existence
exists = backend.content_exists(version_id, context=context)

# Get size
size = backend.get_content_size(version_id, context=context)

# Directory operations
backend.mkdir("dir", parents=True, exist_ok=True)
backend.rmdir("dir", recursive=False)
is_dir = backend.is_directory("dir")
entries = backend.list_dir("dir")

# Rename/move
backend.rename_file("old.txt", "new.txt")
```

## Examples

### Demo Script

Run the included demo script:

```bash
# With server
export NEXUS_URL=http://localhost:8080
export NEXUS_API_KEY=your-api-key
export S3_BUCKET_NAME=my-bucket
export AWS_REGION=us-east-1
python examples/python/s3_connector_demo.py

# Local mode (no server)
export S3_BUCKET_NAME=my-bucket
export AWS_REGION=us-east-1
python examples/python/s3_connector_demo.py --local
```

### Dynamic Mount via API

```python
from nexus.sdk import connect

nx = connect()

# Add mount dynamically
mount_id = nx.add_mount(
    mount_point="/workspace/s3",
    backend_type="s3_connector",
    backend_config={
        "bucket": "my-bucket",
        "region_name": "us-east-1",
        "prefix": "agent-workspace"
    },
    priority=10,
)

# Use the mount
nx.write("/workspace/s3/result.json", b'{"status": "complete"}')

# Remove mount when done
nx.remove_mount("/workspace/s3")
```

## Performance and Reliability

### Automatic Retry

The S3 connector includes built-in retry logic for transient errors:

```python
# Configured with adaptive retry mode
- Max attempts: 3
- Retry mode: adaptive (handles throttling intelligently)
- Automatic backoff for 503 errors and throttling
```

This improves reliability when dealing with:
- Temporary network issues
- S3 service throttling (503 SlowDown)
- Transient AWS infrastructure issues

### Efficient Rename Operations

The `rename_file` operation uses S3's native `copy_object` API for efficient file moves:

1. Copy object to new location (metadata-only operation when possible)
2. Delete old location
3. No data transfer for same-region renames

This is significantly faster than download-upload approaches, especially for large files.

## Architecture

### Base Class Design

The S3 connector is built on the `BaseBlobStorageConnector` abstract base class, which provides:

- Shared functionality for blob storage backends (S3, GCS, Azure, MinIO)
- Content hash computation and Content-Type detection
- Common directory operations and path mapping
- Consistent error handling and retry patterns

This architecture makes it easy to add support for additional blob storage providers:

```python
from nexus.backends.base_blob_connector import BaseBlobStorageConnector

class AzureBlobConnectorBackend(BaseBlobStorageConnector):
    # Implement cloud-specific operations
    def _upload_blob(self, ...): ...
    def _download_blob(self, ...): ...
    # ... other abstract methods
```

## Comparison with GCS Connector

| Feature | S3 Connector | GCS Connector |
|---------|--------------|---------------|
| Cloud Provider | AWS | Google Cloud |
| Client Library | boto3 | google-cloud-storage |
| Versioning ID | String (opaque) | Integer (generation) |
| Auth Methods | IAM, credentials file, env vars | ADC, service account, OAuth |
| Regional Buckets | Yes | Yes |
| Base Class | BaseBlobStorageConnector | BaseBlobStorageConnector |
| Retry Logic | ✓ Adaptive (3 attempts) | ✓ Deadline-based (120s) |

## Related Documentation

- [GCS Connector Backend](./gcs-connector-backend.md)
- [Local Backend](./local-backend.md)
- [Mounting Backends](./mounting-backends.md)
- [AWS S3 Documentation](https://docs.aws.amazon.com/s3/)
- [boto3 Documentation](https://boto3.amazonaws.com/v1/documentation/api/latest/index.html)
