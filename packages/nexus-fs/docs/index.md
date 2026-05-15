---
search:
  boost: 2
---

# nexus-fs

Mount S3, GCS, and local storage with two lines of Python.

```python
import nexus.fs

fs = nexus.fs.mount_sync("s3://my-bucket", "local://./data")
content = fs.read("/s3/my-bucket/README.md")
```

## What is nexus-fs?

nexus-fs is a unified filesystem abstraction for cloud storage.
You mount backends by URI, and the library exposes them under a single
namespace with a consistent API — `read`, `write`, `ls`, `stat`, `delete`,
`rename`, `copy`, `mkdir`.

It works as a standalone package (~15 dependencies) or as the foundation
layer of [nexus-ai-fs](https://github.com/nexi-lab/nexus), which adds
versioning, search, permissions, and multi-agent coordination.

## Install

```bash
pip install nexus-fs            # core (local backend only)
pip install nexus-fs[s3]        # + Amazon S3
pip install nexus-fs[gcs]       # + Google Cloud Storage
pip install nexus-fs[all]       # everything
```

## Next steps

<div class="grid cards" markdown>

-   **Getting Started**

    Install nexus-fs and mount your first backend in under 5 minutes.

    [:octicons-arrow-right-24: Quickstart](getting-started/quickstart.md)

-   **Backend Guides**

    Set up S3, GCS, Google Drive, or local storage.

    [:octicons-arrow-right-24: Backends](backends/index.md)

-   **Integrations**

    Use nexus-fs with pandas, fsspec, LangChain, and more.

    [:octicons-arrow-right-24: Integrations](integrations/index.md)

-   **API Reference**

    Auto-generated reference for all public modules.

    [:octicons-arrow-right-24: Reference](reference/)

</div>
