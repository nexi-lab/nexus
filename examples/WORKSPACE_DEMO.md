# Workspace Versioning Demo

This directory contains interactive demos for Nexus Workspace Versioning - Time-Travel for Agent Workspaces.

## Features Demonstrated

- **Snapshot Creation**: Capture complete workspace state with descriptions and tags
- **Snapshot History**: View all snapshots with metadata
- **Snapshot Diff**: Compare two snapshots to see what changed
- **Workspace Restore**: Rollback workspace to any previous snapshot
- **CAS Deduplication**: Zero storage overhead through content-addressable storage

## Demos Available

### 1. Bash Script Demo

**Location**: `script_demo/workspace_demo.sh`

**Run**:
```bash
bash examples/script_demo/workspace_demo.sh
```

**What it does**:
- Creates a demo workspace with initial files
- Makes incremental changes across 3 snapshots
- Shows beautiful snapshot history table
- Compares snapshots with detailed diff output
- Demonstrates time-travel restoration
- All with colorful terminal output!

**Duration**: ~30 seconds

**Database Support**:
- Automatically detects PostgreSQL if `NEXUS_DATABASE_URL` is set
- Cleans up old demo data before each run
- Falls back to SQLite if PostgreSQL not configured

### 2. Python Script Demo

**Location**: `py_demo/workspace_demo.py`

**Run**:
```bash
python examples/py_demo/workspace_demo.py
```

**What it does**:
- Same workflow as bash demo
- Uses the Python API directly
- Beautiful Rich console output
- Shows how to use the API in your own code
- Demonstrates all workspace versioning methods

**Duration**: ~10 seconds

**Database Support**:
- Automatically uses PostgreSQL if `NEXUS_DATABASE_URL` is set
- Falls back to SQLite otherwise
- Cleans up old demo data before each run

## Key Concepts

### Snapshots are CAS-backed Manifests

Each snapshot is a JSON manifest stored in CAS:
```json
{
  "README.md": {
    "hash": "abc123...",
    "size": 1234,
    "mime_type": "text/markdown"
  },
  "config.json": {
    "hash": "def456...",
    "size": 567,
    "mime_type": "application/json"
  }
}
```

### Zero Storage Overhead

- Content is stored once in CAS
- Multiple snapshots can reference the same content
- Deduplication happens automatically
- Only manifest (~few KB) is stored per snapshot

### Fast Operations

- **Snapshot**: Instant (just creates manifest)
- **Diff**: Fast (compares two small JSON files)
- **Restore**: Fast (updates metadata pointers)

## CLI Usage

After running either demo, try these commands:

```bash
# View snapshot history
nexus workspace log --agent demo-agent

# Compare snapshots
nexus workspace diff --agent demo-agent --snapshot1 1 --snapshot2 2

# Create your own snapshot
nexus workspace snapshot --agent demo-agent --description "My snapshot"

# Restore to previous state
nexus workspace restore --agent demo-agent --snapshot 1
```

## Python API Usage

```python
from nexus import LocalBackend, NexusFS

# Initialize with agent ID
nx = NexusFS(backend, agent_id="my-agent")

# Create snapshot
snapshot = nx.workspace_snapshot(
    description="Before major changes",
    tags=["experiment", "v1.0"]
)
print(f"Created snapshot #{snapshot['snapshot_number']}")

# View history
snapshots = nx.workspace_log(limit=20)
for snap in snapshots:
    print(f"#{snap['snapshot_number']}: {snap['description']}")

# Compare snapshots
diff = nx.workspace_diff(snapshot_1=1, snapshot_2=2)
print(f"Added: {len(diff['added'])} files")
print(f"Modified: {len(diff['modified'])} files")
print(f"Removed: {len(diff['removed'])} files")

# Restore workspace
result = nx.workspace_restore(snapshot_number=1)
print(f"Restored {result['files_restored']} files")
```

## Use Cases

### Time-Travel Debugging
```bash
# Before risky operation
nexus workspace snapshot --agent agent1 --description "Before refactor"

# Make changes...
# Oops, something broke!

# Rollback instantly
nexus workspace restore --agent agent1 --snapshot 1
```

### Experiment Tracking
```python
# Try different approaches
for experiment_id in range(10):
    # Modify code...
    run_tests()
    nx.workspace_snapshot(
        description=f"Experiment {experiment_id}",
        tags=["experiment", f"run-{experiment_id}"]
    )

# Compare best vs worst
diff = nx.workspace_diff(best_snapshot, worst_snapshot)
```

### Collaboration Safety
```bash
# Before pulling changes
nexus workspace snapshot --agent agent1 --description "Before merge"

# Pull changes from collaborator
# If conflicts arise, restore to pre-merge state
nexus workspace restore --agent agent1 --snapshot 1
```

## Performance

Tested on MacBook Pro M1:
- **Snapshot creation**: <10ms (1000 files)
- **Snapshot diff**: <50ms
- **Workspace restore**: <100ms (100 files)
- **Storage overhead**: ~2KB per snapshot (manifest only)

## Limitations

- Snapshots are per-agent workspace
- Cross-agent snapshots not supported yet
- Deleted content may be garbage collected (CAS reference counting)

## Learn More

- [README.md](../../README.md) - Full Nexus documentation
- [Issue #183](https://github.com/nexi-lab/nexus/issues/183) - Original feature request
- [Workspace Manager](../../src/nexus/core/workspace_manager.py) - Implementation
