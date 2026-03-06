# Audit Trail Tutorial

Every filesystem operation in Nexus (write, delete, rename, mkdir, rmdir) is
recorded in the `operation_log` table. Events persist even in CLI mode
(single-command, no server running) — nothing is silently lost on exit.

## Prerequisites

- PostgreSQL running (Docker or Homebrew)
- `NEXUS_DATABASE_URL` set

```bash
# Start PostgreSQL if needed
docker run -d --name nexus-postgres \
  -p 5432:5432 \
  -e POSTGRES_PASSWORD=nexus \
  -e POSTGRES_DB=nexus \
  postgres

export NEXUS_DATABASE_URL="postgresql://postgres:nexus@localhost:5432/nexus"
```

## Run the demo

```bash
./examples/tutorials/audit-trail/audit_trail_demo.sh
```

The script will:
1. Create a directory and write two files
2. Show the full operation log
3. Filter by operation type (`--type write`, `--type mkdir`)
4. Delete a file and verify the delete appears
5. Filter by path prefix (`--path /demo/`)

## CLI reference

```bash
nexus ops log                          # all recent operations
nexus ops log --limit 100              # show more rows
nexus ops log --type write             # filter by type (write, delete, rename, mkdir, rmdir)
nexus ops log --path /workspace/       # prefix match (trailing slash)
nexus ops log --path /workspace/f.txt  # exact match
nexus ops log --agent my-agent         # filter by agent ID
nexus ops log --status failure         # only failures
```

## How it works

In CLI mode, the `PipedRecordStoreWriteObserver` buffers events in a
`_pre_buffer` deque (since `PipeManager` is not injected). On
`NexusFS.close()`, `flush_sync()` drains the buffer directly to the
database before the connection is closed.

In server mode, the background pipe consumer flushes events
asynchronously. On shutdown, `signal_close()` wakes the consumer to
drain remaining messages with a 5-second timeout before force-cancelling.
