# CLI: Server & Mounting

← [CLI Reference](index.md) | [API Documentation](../README.md)

This document describes CLI commands for server management and FUSE mounting.

## serve - Start RPC server

Start the Nexus RPC server for remote access.

**CLI:**
```bash
# Start server (default: localhost:8765)
nexus serve

# Specify host and port
nexus serve --host 0.0.0.0 --port 8080

# With specific data directory
nexus serve --data-dir /var/lib/nexus

# With API key authentication
nexus serve --api-key secret123

# With PostgreSQL database
nexus serve --database-url postgresql://user:pass@localhost/nexus
```

**Python API:**
```python
# Server is typically run from CLI, but you can start programmatically
from nexus.server.rpc_server import start_server

# Start server
start_server(
    host="0.0.0.0",
    port=8765,
    data_dir="./nexus-data",
    api_key="secret123"
)
```

**Options:**
- `--host TEXT`: Host to bind to (default: localhost)
- `--port INT`: Port to listen on (default: 8765)
- `--data-dir PATH`: Data directory path
- `--api-key TEXT`: API key for authentication
- `--database-url TEXT`: PostgreSQL connection string

**Environment Variables:**
- `NEXUS_DATA_DIR`: Default data directory
- `NEXUS_DATABASE_URL`: Default database URL

**See Also:**
- [Python API: RPC Server](../rpc-api.md)
- [Server Setup Guide](../../deployment/server-setup.md)

---

## mount - Mount as filesystem

Mount Nexus as a FUSE filesystem.

**CLI:**
```bash
# Mount Nexus as FUSE filesystem
nexus mount /mnt/nexus

# Mount with specific data directory
nexus mount /mnt/nexus --data-dir ./nexus-data

# Mount in foreground (for debugging)
nexus mount /mnt/nexus --foreground
```

**Python API:**
```python
# Mounting is typically done from CLI, but you can mount programmatically
from nexus.fuse.mount import mount_nexus

# Mount filesystem
mount_nexus(
    mount_point="/mnt/nexus",
    data_dir="./nexus-data",
    foreground=False
)
```

**Options:**
- `mount_point`: Directory to mount at
- `--data-dir PATH`: Data directory path
- `--foreground`: Run in foreground (don't daemonize)

**Requirements:**
- macOS: macFUSE installed
- Linux: FUSE installed

**See Also:**
- [Python API: FUSE Mount](../mounts.md#fuse-mounting)

---

## unmount - Unmount filesystem

Unmount a FUSE filesystem.

**CLI:**
```bash
# Unmount
nexus unmount /mnt/nexus
```

**On macOS/Linux:**
```bash
# Can also use standard unmount commands
umount /mnt/nexus  # Linux
diskutil unmount /mnt/nexus  # macOS
```

**Python API:**
```python
# Unmounting is typically done from CLI
from nexus.fuse.mount import unmount_nexus

unmount_nexus("/mnt/nexus")
```

**See Also:**
- [Python API: FUSE Mount](../mounts.md#fuse-mounting)

---

## Common Workflows

### Remote server setup
```bash
# Terminal 1: Start server
nexus serve --host 0.0.0.0 --port 8765 --api-key secret123

# Terminal 2: Use remote client
export NEXUS_URL=http://localhost:8765
export NEXUS_API_KEY=secret123

nexus write /workspace/file.txt "remote data"
nexus cat /workspace/file.txt
nexus ls /workspace
```

### FUSE filesystem
```bash
# Mount Nexus
nexus mount /mnt/nexus --data-dir ./nexus-data

# Use as regular filesystem
echo "Hello World" > /mnt/nexus/workspace/hello.txt
cat /mnt/nexus/workspace/hello.txt
ls -la /mnt/nexus/workspace/

# Unmount when done
nexus unmount /mnt/nexus
```

### Production server
```bash
# Start with PostgreSQL and authentication
export NEXUS_DATABASE_URL=postgresql://nexus:password@localhost/nexus
export NEXUS_DATA_DIR=/var/lib/nexus

nexus serve \
  --host 0.0.0.0 \
  --port 8080 \
  --api-key $(cat /etc/nexus/api-key.txt) \
  --database-url $NEXUS_DATABASE_URL \
  --data-dir $NEXUS_DATA_DIR
```

---

## See Also

- [CLI Reference Overview](index.md)
- [Python API: RPC API](../rpc-api.md)
- [Server Setup](../../deployment/server-setup.md)
- [PostgreSQL Setup](../../deployment/postgresql.md)
