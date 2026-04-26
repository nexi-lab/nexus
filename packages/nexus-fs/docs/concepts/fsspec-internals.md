# fsspec Internals

This page explains how the `NexusFileSystem` adapter bridges
[fsspec](https://filesystem-spec.readthedocs.io/) to nexus-fs.

## Registration

nexus-fs registers the `nexus` protocol via the `fsspec.specs` entry
point in `pyproject.toml`:

```toml
[project.entry-points."fsspec.specs"]
nexus = "nexus.fs._fsspec:NexusFileSystem"
```

After installing `nexus-fs[fsspec]`, any call to
`fsspec.filesystem("nexus")` returns a `NexusFileSystem` instance.

## Architecture

```mermaid
graph LR
    A[pandas / dask / HF] -->|nexus:// URL| B[fsspec]
    B -->|filesystem protocol| C[NexusFileSystem]
    C -->|kernel calls| D[NexusFS kernel]
    D -->|mount routing| E[Storage Backend]
```

`NexusFileSystem` is a thin synchronous adapter that:

1. Receives fsspec method calls (`cat`, `pipe`, `ls`, `info`, `open`)
2. Strips the `nexus://` protocol prefix
3. Delegates to `NexusFS` kernel `sys_*` methods (passing
   ``LOCAL_CONTEXT`` from ``nexus.fs._helpers``)
4. Converts results to fsspec's expected format

## Auto-discovery

When `NexusFileSystem` is created without an explicit `NexusFS`
kernel, it auto-discovers mounts from the local state directory:

1. Reads `$TMPDIR/nexus-fs/mounts.json` (or `$NEXUS_FS_STATE_DIR/mounts.json` if set)
2. Reconstructs mount URIs from the saved state
3. Calls `mount()` to create a `NexusFS` kernel

This means you can mount backends via the CLI or Python, and then
use `nexus://` URLs in pandas without any manual setup.

## File-like objects

`NexusFileSystem.open()` returns one of two file-like objects:

### NexusBufferedFile (read mode)

For `rb` and `r` modes. Supports:

- `read(length)` — read up to `length` bytes
- `readline()` — read one line
- `readlines()` — read all lines
- `seek(offset, whence)` — seek to position
- `tell()` — current position
- Iteration via `for line in f`
- Context manager (`with fs.open(...) as f`)

Internally, `NexusBufferedFile` fetches the full file content on first
access and buffers it in memory. For large files (>1 GB), call
``kernel.read_range(path, start, end, context=LOCAL_CONTEXT)``
directly on the `NexusFS` kernel instead.

### NexusWriteFile (write mode)

For `wb` and `w` modes. Supports:

- `write(data)` — append data to buffer
- `flush()` — no-op (data is buffered)
- `close()` — flushes buffer to backend via the kernel's `write()`
- Context manager (`with fs.open(...) as f`)

The write buffer has a 1 GB limit. Writing more than 1 GB raises
`ValueError`.

## Method mapping

| fsspec method | nexus-fs kernel call |
|---------------|----------------------|
| `cat(path)` | `sys_read(path, context=LOCAL_CONTEXT)` |
| `cat(path, start, end)` | `read_range(path, start, end, context=LOCAL_CONTEXT)` |
| `pipe(path, data)` | `write(path, data, context=LOCAL_CONTEXT)` |
| `ls(path, detail)` | `sys_readdir(path, recursive=False, details=detail, context=LOCAL_CONTEXT)` |
| `info(path)` | `sys_stat(path, context=LOCAL_CONTEXT)` |
| `rm(path)` | `sys_unlink(path, context=LOCAL_CONTEXT)` or `rmdir(path, recursive=True, context=LOCAL_CONTEXT)` |
| `cp(src, dst)` | `sys_copy(src, dst, context=LOCAL_CONTEXT)` |
| `mkdir(path)` | `mkdir(path, parents=True, exist_ok=True, context=LOCAL_CONTEXT)` |

The `LOCAL_CONTEXT` operation context is importable from
`nexus.fs._helpers` (also re-exported as `nexus.fs.LOCAL_CONTEXT`).

## Sync bridge

`NexusFileSystem` is synchronous (as fsspec requires) and the
`NexusFS` kernel exposes synchronous `sys_*` methods, so no
async-to-sync bridge is needed for the hot path. The
``_runner`` is retained only for resource cleanup hooks that may
still produce coroutines.
