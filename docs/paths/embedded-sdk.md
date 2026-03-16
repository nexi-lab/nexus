# Local SDK

Nexus = filesystem/context plane.

Choose this path when you want the shortest working integration: one process, one local data directory, no daemon.

## Best For

- Local agent prototypes
- CLI tools and developer workflows
- Tests and notebooks
- Applications that want Nexus as an in-process dependency

## Starting Point

```python
from nexus.sdk import connect

nx = connect(
    config={
        "profile": "minimal",
        "data_dir": "./nexus-data",
    }
)
```

From there you can use the filesystem API directly:

```python
nx.sys_write("/notes/today.txt", b"hello")
content = nx.sys_read("/notes/today.txt")
```

## What You Get

- A local VFS-style API
- Persistent state under your chosen data directory
- A path that works without remote infrastructure

## When To Leave This Path

Move to the daemon path when you need remote clients, long-lived processes, or operational controls around a shared Nexus instance.

- Next: [Shared daemon](daemon-and-remote.md)
- Verified walkthrough: [Quickstart](../getting-started/quickstart.md)
