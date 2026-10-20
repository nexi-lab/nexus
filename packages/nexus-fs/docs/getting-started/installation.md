---
search:
  boost: 2
---

# Installation

nexus-fs requires **Python 3.11+**.

## Base install

The base package includes the local filesystem backend:

```bash
pip install nexus-fs
```

## Extras

Add cloud backends by installing extras:

=== "Amazon S3"

    ```bash
    pip install nexus-fs[s3]
    ```

    Installs `boto3`. Requires AWS credentials — see [S3 setup](../backends/s3.md).

=== "Google Cloud Storage"

    ```bash
    pip install nexus-fs[gcs]
    ```

    Installs `google-cloud-storage`. Requires Application Default Credentials — see [GCS setup](../backends/gcs.md).

=== "Google Drive"

    ```bash
    pip install nexus-fs[gdrive]
    ```

    Installs `google-api-python-client` and `google-auth-oauthlib`.
    Requires OAuth setup — see [Google Drive setup](../backends/gdrive.md).

=== "Everything"

    ```bash
    pip install nexus-fs[all]
    ```

    Installs all backend extras.

## Optional: fsspec integration

To use nexus-fs as an fsspec filesystem (for pandas, dask, HuggingFace):

```bash
pip install nexus-fs[fsspec]
```

See [fsspec integration](../integrations/fsspec.md) for usage.

## Terminal client

Use [Nexus TUI](https://github.com/nexi-lab/nexus/tree/develop/packages/nexus-tui) with a native host URL and credential.

## Verify

```bash
nexus-fs doctor
```

This checks your Python version, installed backends, and credential
configuration. See [nexus-fs doctor](../cli/doctor.md) for interpreting
the output.
