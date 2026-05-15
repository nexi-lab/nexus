# Google Cloud Storage

## Install

```bash
pip install nexus-fs[gcs]
```

This installs `google-cloud-storage`.

## Credential setup

nexus-fs uses Google Application Default Credentials (ADC). Configure
one of:

### Option 1: gcloud CLI

```bash
gcloud auth application-default login
```

This stores credentials at
`~/.config/gcloud/application_default_credentials.json`.

### Option 2: Service account key

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
```

### Option 3: Compute Engine / Cloud Run / GKE

No configuration needed. The metadata service provides credentials
automatically.

### Verify credentials

```bash
nexus-fs doctor --mount gcs://your-project/your-bucket
```

## Mount

```python
# skip-test
import nexus.fs

fs = nexus.fs.mount_sync("gcs://my-project/my-bucket")
```

The GCS URI format is `gcs://<project>/<bucket>`. The mount point uses
the bucket name only: `/gcs/my-bucket/`.

## Mount path

| URI | Mount point |
|-----|-------------|
| `gcs://my-project/data-bucket` | `/gcs/data-bucket/` |
| `gcs://analytics/warehouse` | `/gcs/warehouse/` |

Override with `at=`:

```python
# skip-test
import nexus.fs

fs = nexus.fs.mount_sync("gcs://my-project/my-bucket", at="/storage")
```

## Common patterns

### Read and write

```python
# skip-test
import nexus.fs

fs = nexus.fs.mount_sync("gcs://my-project/my-bucket")

fs.write("/gcs/my-bucket/config.yaml", b"key: value\n")
content = fs.read("/gcs/my-bucket/config.yaml")
```

### List objects

```python
# skip-test
import nexus.fs

fs = nexus.fs.mount_sync("gcs://my-project/my-bucket")

files = fs.ls("/gcs/my-bucket/datasets/")
entries = fs.ls("/gcs/my-bucket/datasets/", detail=True)
```

### Cross-backend operations

```python
# skip-test
import nexus.fs

fs = nexus.fs.mount_sync(
    "gcs://my-project/my-bucket",
    "s3://my-s3-bucket",
    "local://./scratch",
)

# Read from GCS, write to S3 — same API
data = fs.read("/gcs/my-bucket/input.csv")
fs.write("/s3/my-s3-bucket/input.csv", data)
```

## Async usage

```python
# skip-test
import asyncio
import nexus.fs

async def main():
    fs = await nexus.fs.mount("gcs://my-project/my-bucket")
    content = await fs.read("/gcs/my-bucket/data.parquet")
    print(f"Read {len(content)} bytes")

asyncio.run(main())
```
