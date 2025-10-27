# CLI: Version Tracking

← [CLI Reference](index.md) | [API Documentation](../README.md)

This document describes CLI commands for version tracking and time-travel, and their Python API equivalents.

## versions history - Show version history

Display the complete version history of a file.

**CLI:**
```bash
# Show all versions
nexus versions history /workspace/file.txt

# Limit results
nexus versions history /workspace/file.txt --limit 10
```

**Python API:**
```python
# Get version history
versions = nx.list_versions("/workspace/file.txt")
for version in versions:
    print(f"Version {version['version_number']}: {version['modified_at']}")
    print(f"  Size: {version['size']} bytes")
    print(f"  ETag: {version['etag']}")

# Limit results
versions = nx.list_versions("/workspace/file.txt", limit=10)
```

**Options:**
- `--limit NUM`: Limit number of versions returned
- `--remote-url URL`: Connect to remote server
- `--remote-api-key KEY`: API key for authentication

**See Also:**
- [Python API: list_versions()](../versioning.md#list_versions)

---

## versions get - Get specific version

Retrieve a specific version of a file.

**CLI:**
```bash
# Get version 2
nexus versions get /workspace/file.txt --version 2
```

**Python API:**
```python
# Get specific version
content = nx.get_version("/workspace/file.txt", version=2)
print(content.decode())

# Get version with metadata
result = nx.get_version("/workspace/file.txt", version=2, return_metadata=True)
content = result['content']
metadata = result['metadata']
print(f"Version {metadata['version']}: {len(content)} bytes")
```

**Options:**
- `--version NUM`: Version number to retrieve
- `--remote-url URL`: Connect to remote server
- `--remote-api-key KEY`: API key for authentication

**See Also:**
- [Python API: get_version()](../versioning.md#get_version)

---

## versions diff - Compare versions

Compare two versions of a file.

**CLI:**
```bash
# Compare versions 1 and 3
nexus versions diff /workspace/file.txt --v1 1 --v2 3

# Show content diff
nexus versions diff /workspace/file.txt --v1 1 --v2 3 --mode content
```

**Python API:**
```python
# Compare versions (metadata only)
diff = nx.diff_versions("/workspace/file.txt", version1=1, version2=3)
print(f"Size change: {diff['size_diff']} bytes")
print(f"Modified: {diff['time_diff']}")

# Compare with content diff
diff = nx.diff_versions("/workspace/file.txt", version1=1, version2=3, mode="content")
print(diff['content_diff'])

# Generate unified diff manually
import difflib
v1_content = nx.get_version("/workspace/file.txt", version=1).decode()
v2_content = nx.get_version("/workspace/file.txt", version=3).decode()

diff = difflib.unified_diff(
    v1_content.splitlines(),
    v2_content.splitlines(),
    lineterm='',
    fromfile='version 1',
    tofile='version 3'
)
print('\n'.join(diff))
```

**Options:**
- `--v1 NUM`: First version number
- `--v2 NUM`: Second version number
- `--mode [metadata|content]`: Comparison mode
- `--remote-url URL`: Connect to remote server
- `--remote-api-key KEY`: API key for authentication

**See Also:**
- [Python API: diff_versions()](../versioning.md#diff_versions)

---

## versions rollback - Rollback to version

Restore a file to a previous version.

**CLI:**
```bash
# Rollback to version 1
nexus versions rollback /workspace/file.txt --version 1
```

**Python API:**
```python
# Rollback to version 1
nx.rollback("/workspace/file.txt", version=1)

# Verify the rollback
current_content = nx.read("/workspace/file.txt")
version_content = nx.get_version("/workspace/file.txt", version=1)
assert current_content == version_content
```

**Options:**
- `--version NUM`: Version number to rollback to
- `--remote-url URL`: Connect to remote server
- `--remote-api-key KEY`: API key for authentication

**See Also:**
- [Python API: rollback()](../versioning.md#rollback)

---

## Time-Travel Operations

Read files and list directories at specific points in time.

**CLI:**
```bash
# Read file at historical operation point
nexus cat /workspace/file.txt --at-operation op_abc123

# List directory at historical point
nexus ls /workspace --at-operation op_abc123
```

**Python API:**
```python
from nexus.core.permissions import OperationContext

# Read at specific operation
ctx = OperationContext(at_operation="op_abc123")
content = nx.read("/workspace/file.txt", context=ctx)

# List directory at specific operation
entries = nx.list("/workspace", context=ctx)
```

**See Also:**
- [Python API: Time Travel](../versioning.md#time-travel)

---

## Common Workflows

### Track file changes
```bash
# Create initial version
nexus write /config.json '{"version": "1.0"}'

# Update file multiple times
nexus write /config.json '{"version": "1.1"}'
nexus write /config.json '{"version": "2.0"}'

# View history
nexus versions history /config.json

# Compare versions
nexus versions diff /config.json --v1 1 --v2 3 --mode content

# Rollback if needed
nexus versions rollback /config.json --version 1

# Verify
nexus cat /config.json
```

### Python equivalent
```python
import nexus
import json

# Initialize
nx = nexus.Nexus(data_dir="./nexus-data")

# Create initial version
config = {"version": "1.0"}
nx.write("/config.json", json.dumps(config).encode())

# Update file multiple times
config["version"] = "1.1"
nx.write("/config.json", json.dumps(config).encode())

config["version"] = "2.0"
nx.write("/config.json", json.dumps(config).encode())

# View history
versions = nx.list_versions("/config.json")
for v in versions:
    print(f"Version {v['version_number']}: {v['modified_at']}")

# Get and compare versions
v1 = nx.get_version("/config.json", version=1)
v3 = nx.get_version("/config.json", version=3)
print(f"Version 1: {v1.decode()}")
print(f"Version 3: {v3.decode()}")

# Rollback if needed
nx.rollback("/config.json", version=1)

# Verify
current = nx.read("/config.json")
print(f"Current: {current.decode()}")
```

### Audit trail
```bash
# View all versions of important file
nexus versions history /sensitive/data.json

# Check what changed between versions
nexus versions diff /sensitive/data.json --v1 5 --v2 6 --mode content

# Restore to known good version
nexus versions rollback /sensitive/data.json --version 5
```

### Python equivalent
```python
# View all versions
versions = nx.list_versions("/sensitive/data.json")
print(f"Total versions: {len(versions)}")

for v in versions:
    print(f"\nVersion {v['version_number']}:")
    print(f"  Modified: {v['modified_at']}")
    print(f"  Size: {v['size']} bytes")
    print(f"  ETag: {v['etag']}")

# Compare specific versions
import difflib
v5 = nx.get_version("/sensitive/data.json", version=5).decode()
v6 = nx.get_version("/sensitive/data.json", version=6).decode()

diff = difflib.unified_diff(
    v5.splitlines(),
    v6.splitlines(),
    lineterm='',
    fromfile='version 5',
    tofile='version 6'
)

print("\nChanges between v5 and v6:")
print('\n'.join(diff))

# Restore to known good version
nx.rollback("/sensitive/data.json", version=5)
print("\nRestored to version 5")
```

---

## See Also

- [CLI Reference Overview](index.md)
- [Python API: Versioning](../versioning.md)
- [File Operations](file-operations.md)
- [Advanced Usage](../advanced-usage.md)
