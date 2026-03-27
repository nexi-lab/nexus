# nexus-fs

Unified filesystem abstraction for cloud storage. Mount S3, GCS, Google Workspace, and local storage with two lines of Python.

```python
import nexus.fs

fs = nexus.fs.mount_sync("s3://my-bucket", "local://./data")
content = fs.read("/s3/my-bucket/README.md")
```

## Install

```bash
pip install nexus-fs            # core (local only)
pip install nexus-fs[s3]        # + Amazon S3
pip install nexus-fs[gcs]       # + Google Cloud Storage
pip install nexus-fs[all]       # everything
```

## Quick Start

### Async

```python
import asyncio
import nexus.fs

async def main():
    fs = await nexus.fs.mount("s3://my-bucket", "local://./data")
    await fs.write("/local/data/hello.txt", b"Hello!")
    content = await fs.read("/local/data/hello.txt")
    print(content)

asyncio.run(main())
```

### Sync

```python
import nexus.fs

fs = nexus.fs.mount_sync("local://./data")
fs.write("/local/data/hello.txt", b"Hello!")
print(fs.read("/local/data/hello.txt"))
```

### Connectors (Google Workspace, GitHub, Slack, etc.)

```python
import nexus.fs

fs = nexus.fs.mount_sync("gws://sheets", "gws://docs")
# Uses gws CLI under the hood — no server needed
```

## API

| Method | Description |
|--------|-------------|
| `read(path)` | Read file content |
| `write(path, content)` | Write/overwrite file |
| `ls(path, detail, recursive)` | List directory |
| `stat(path)` | Get file metadata |
| `exists(path)` | Check if path exists |
| `delete(path)` | Delete a file |
| `rename(old, new)` | Rename/move |
| `copy(src, dst)` | Copy a file |
| `mkdir(path)` | Create directory |
| `list_mounts()` | List mount points |

## TUI Playground

```bash
pip install nexus-fs[tui]
nexus-fs playground s3://my-bucket local://./data
```

> **Note:** The TUI uses direct backend access for low-latency browsing.
> File operation semantics in the playground may differ from the library API
> (e.g., metadata fields, error messages). The library API (`mount()` /
> `mount_sync()`) is the authoritative interface. TUI/library unification
> is planned for a future release.

## State Directory

nexus-fs stores runtime state (metadata DB, mount config) in a platform-specific
directory:

| Platform | Default path |
|----------|-------------|
| Linux | `~/.local/state/nexus-fs/` |
| macOS | `~/Library/Application Support/nexus-fs/` |
| Windows | `%LOCALAPPDATA%/nexus-fs/` |

Override with the `NEXUS_FS_STATE_DIR` environment variable.

Persistent secrets (OAuth tokens, encryption keys) are stored under `~/.nexus/`.
Override with `NEXUS_FS_PERSISTENT_DIR`.

## Relationship to `nexus-ai-fs`

`nexus-fs` is the **slim standalone** cloud storage package (~16 dependencies).
`nexus-ai-fs` is the **full** Nexus filesystem/context plane (~100+ dependencies)
that includes server, bricks, gRPC, federation, and more.

Both packages install into the `nexus` Python namespace. **Do not install both
in the same environment** — they will conflict. Choose one:

- `pip install nexus-fs` — lightweight cloud storage only
- `pip install nexus-ai-fs` — full Nexus system (includes all `nexus-fs` functionality)

## License

Apache-2.0
