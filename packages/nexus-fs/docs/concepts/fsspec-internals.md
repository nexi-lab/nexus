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
    C -->|sync bridge| D[SlimNexusFS]
    D -->|mount routing| E[Storage Backend]
```

`NexusFileSystem` is a thin synchronous adapter that:

1. Receives fsspec method calls (`cat`, `pipe`, `ls`, `info`, `open`)
2. Strips the `nexus://` protocol prefix
3. Delegates to `SlimNexusFS` methods via `anyio.from_thread.run()`
4. Converts results to fsspec's expected format

## Auto-discovery

When `NexusFileSystem` is created without an explicit `SlimNexusFS`
instance, it auto-discovers mounts from the local state directory:

1. Reads `$TMPDIR/nexus-fs/mounts.json` (or `$NEXUS_FS_STATE_DIR/mounts.json` if set)
2. Reconstructs mount URIs from the saved state
3. Calls `mount()` to create a `SlimNexusFS` instance

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
access and buffers it in memory. For large files (>1 GB), use
`SlimNexusFS.read_range()` instead.

### NexusWriteFile (write mode)

For `wb` and `w` modes. Supports:

- `write(data)` — append data to buffer
- `flush()` — no-op (data is buffered)
- `close()` — flushes buffer to backend via `SlimNexusFS.write()`
- Context manager (`with fs.open(...) as f`)

The write buffer has a 1 GB limit. Writing more than 1 GB raises
`ValueError`.

## Method mapping

| fsspec method | nexus-fs call |
|---------------|---------------|
| `cat(path)` | `read(path)` |
| `cat(path, start, end)` | `read_range(path, start, end)` |
| `pipe(path, data)` | `write(path, data)` |
| `ls(path, detail)` | `ls(path, detail)` |
| `info(path)` | `stat(path)` |
| `rm(path)` | `delete(path)` or `rmdir(path, recursive=True)` |
| `cp(src, dst)` | `copy(src, dst)` |
| `mkdir(path)` | `mkdir(path, parents=True)` |

## Sync bridge

`NexusFileSystem` is synchronous (as fsspec requires), but `SlimNexusFS`
is async. The bridge uses `anyio.from_thread.run()` to call async methods
from synchronous fsspec code. This is the same approach used by httpx
and other async-first libraries with sync wrappers.
