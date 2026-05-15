# fsspec Integration

nexus-fs registers as an [fsspec](https://filesystem-spec.readthedocs.io/)
filesystem, making it a drop-in backend for any library that uses fsspec —
pandas, dask, HuggingFace, and more.

## Install

```bash
pip install nexus-fs[fsspec]
```

## How it works

After installing the `fsspec` extra, nexus-fs registers the `nexus`
protocol automatically via the `fsspec.specs` entry point. Any call to
`fsspec.filesystem("nexus")` returns a `NexusFileSystem` instance.

## Usage

### Direct use

```python
# skip-test
import fsspec

fs = fsspec.filesystem("nexus")

# Read
data = fs.cat("/local/data/file.txt")

# Write
fs.pipe("/local/data/output.txt", b"hello")

# List
fs.ls("/local/data/")
```

### With an existing nexus-fs instance

If you already have a `NexusFS` kernel, pass it directly:

```python
# skip-test
import asyncio
import nexus.fs
from nexus.fs._fsspec import NexusFileSystem

kernel = asyncio.run(nexus.fs.mount("local://./data"))
fsspec_fs = NexusFileSystem(nexus_fs=kernel)

data = fsspec_fs.cat("/local/data/file.txt")
```

### Auto-discovery

When no `NexusFS` kernel is provided, `NexusFileSystem`
auto-discovers mounts from the local state directory. By default,
mounts are persisted to `$TMPDIR/nexus-fs/mounts.json` (e.g.,
`/tmp/nexus-fs/mounts.json` on Linux/macOS). Override this with the
`NEXUS_FS_STATE_DIR` environment variable. This means you can mount
backends via Python or the CLI, and then access them from any
fsspec-compatible library in the same session.

## Supported operations

| fsspec method | Description |
|---------------|-------------|
| `cat(path)` | Read file contents |
| `pipe(path, data)` | Write data to a file |
| `ls(path, detail=True)` | List directory |
| `info(path)` | Get file metadata |
| `rm(path)` | Delete a file |
| `cp(src, dst)` | Copy a file |
| `mkdir(path)` | Create a directory |
| `open(path, mode)` | Open a file-like object (`rb`, `wb`, `r`, `w`) |

### File-like objects

`open()` returns buffered file-like objects that support standard
Python I/O:

```python
# skip-test
import fsspec

fs = fsspec.filesystem("nexus")

# Read mode
with fs.open("/local/data/file.txt", "rb") as f:
    content = f.read()
    # Also supports: readline(), readlines(), seek(), tell()

# Write mode
with fs.open("/local/data/output.txt", "wb") as f:
    f.write(b"line 1\n")
    f.write(b"line 2\n")
    # Data is flushed on close
```

## Limits

| Limit | Value |
|-------|-------|
| Max read size (`cat`) | 1 GB |
| Max write buffer (`open("wb")`) | 1 GB |
| Supported modes | `rb`, `wb`, `r`, `w` |

For files larger than 1 GB, use `read_range()` on the `NexusFS`
kernel directly to read in chunks (pass ``LOCAL_CONTEXT`` from
``nexus.fs._helpers`` as the ``context=`` argument).

## Next steps

- [Data Science](data-science.md) — pandas, dask, HuggingFace via fsspec
- [How fsspec Integration Works](../concepts/fsspec-internals.md) — internals
