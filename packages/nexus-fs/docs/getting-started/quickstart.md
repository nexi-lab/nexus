---
search:
  boost: 2
---

# Quickstart

This guide takes you from install to working filesystem in under 5 minutes.
Every code block below is copy-pasteable and runs with only the base
package — no cloud credentials required.

## 1. Install

```bash
pip install nexus-fs
```

## 2. Mount a local directory

=== "Sync"

    ```python
    import nexus.fs

    fs = nexus.fs.mount_sync("local://./my-data")
    ```

=== "Async"

    ```python
    import asyncio
    import nexus.fs

    async def main():
        fs = await nexus.fs.mount("local://./my-data")

    asyncio.run(main())
    ```

This mounts `./my-data` at `/local/my-data/` in the virtual filesystem.

## 3. Write a file

=== "Sync"

    ```python
    import nexus.fs

    fs = nexus.fs.mount_sync("local://./my-data")
    fs.write("/local/my-data/hello.txt", b"Hello from nexus-fs!")
    ```

=== "Async"

    ```python
    import asyncio
    import nexus.fs

    async def main():
        fs = await nexus.fs.mount("local://./my-data")
        await fs.write("/local/my-data/hello.txt", b"Hello from nexus-fs!")

    asyncio.run(main())
    ```

## 4. Read it back

=== "Sync"

    ```python
    import nexus.fs

    fs = nexus.fs.mount_sync("local://./my-data")
    fs.write("/local/my-data/hello.txt", b"Hello from nexus-fs!")
    content = fs.read("/local/my-data/hello.txt")
    print(content)
    #> b'Hello from nexus-fs!'
    ```

=== "Async"

    ```python
    import asyncio
    import nexus.fs

    async def main():
        fs = await nexus.fs.mount("local://./my-data")
        await fs.write("/local/my-data/hello.txt", b"Hello from nexus-fs!")
        content = await fs.read("/local/my-data/hello.txt")
        print(content)

    asyncio.run(main())
    ```

## 5. List files

=== "Sync"

    ```python
    import nexus.fs

    fs = nexus.fs.mount_sync("local://./my-data")
    fs.write("/local/my-data/hello.txt", b"Hello from nexus-fs!")
    files = fs.ls("/local/my-data/")
    print(files)
    ```

=== "Async"

    ```python
    import asyncio
    import nexus.fs

    async def main():
        fs = await nexus.fs.mount("local://./my-data")
        await fs.write("/local/my-data/hello.txt", b"Hello from nexus-fs!")
        files = await fs.ls("/local/my-data/")
        print(files)

    asyncio.run(main())
    ```

## Mount concepts

nexus-fs uses URIs to identify backends. Each URI mounts at an
auto-derived path:

| URI | Mount point |
|-----|-------------|
| `local://./data` | `/local/data/` |
| `s3://my-bucket` | `/s3/my-bucket/` |
| `gcs://project/bucket` | `/gcs/bucket/` |
| `gdrive://shared` | `/gdrive/shared/` |

Override the mount point with `at=`:

```python
# skip-test
import nexus.fs

fs = nexus.fs.mount_sync("s3://my-bucket", at="/data")
# Now accessible at /data/ instead of /s3/my-bucket/
```

Mount multiple backends at once:

```python
# skip-test
import nexus.fs

fs = nexus.fs.mount_sync("s3://my-bucket", "local://./cache")
# /s3/my-bucket/ and /local/cache/ both available
```

## What next?

- Set up a cloud backend: [S3](../backends/s3.md) | [GCS](../backends/gcs.md) | [Google Drive](../backends/gdrive.md)
- Use with pandas or dask: [Data Science](../integrations/data-science.md)
- Explore files interactively: [nexus-fs playground](../cli/playground.md)
- Understand how it works: [How Mounting Works](../concepts/mounting.md)
