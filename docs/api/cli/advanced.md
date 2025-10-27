# CLI: Advanced Operations

← [CLI Reference](index.md) | [API Documentation](../README.md)

This document describes advanced CLI commands and their Python API equivalents.

## ops log - View operation history

View the history of operations performed on Nexus.

**CLI:**
```bash
# Show recent operations
nexus ops log

# Limit results
nexus ops log --limit 20

# Filter by type
nexus ops log --type write
```

**Python API:**
```python
# Get operation log
operations = nx.get_operation_log(limit=20)
for op in operations:
    print(f"{op['operation_id']}: {op['operation_type']}")
    print(f"  Path: {op['path']}")
    print(f"  Timestamp: {op['timestamp']}")

# Filter by type
write_ops = nx.get_operation_log(operation_type="write")
```

**Options:**
- `--limit NUM`: Maximum number of operations to show
- `--type TEXT`: Filter by operation type (write, delete, mkdir, etc.)

**See Also:**
- [Python API: get_operation_log()](../advanced-usage.md#get_operation_log)

---

## undo - Undo last operation

Undo the most recent operation.

**CLI:**
```bash
# Undo last operation
nexus undo

# View what would be undone (dry-run)
nexus undo --dry-run
```

**Python API:**
```python
# Undo last operation
result = nx.undo()
print(f"Undone operation: {result['operation_type']}")

# Dry-run to see what would be undone
result = nx.undo(dry_run=True)
print(f"Would undo: {result['operation_type']} on {result['path']}")
```

**Options:**
- `--dry-run`: Show what would be undone without actually undoing

**See Also:**
- [Python API: undo()](../advanced-usage.md#undo)

---

## work - Query work items

Query work items using SQL views.

**CLI:**
```bash
# Query work items using SQL
nexus work --view active_work
```

**Python API:**
```python
# Query work items
work_items = nx.query_work_items(view="active_work")
for item in work_items:
    print(f"{item['path']}: {item['status']}")
```

**See Also:**
- [Python API: query_work_items()](../advanced-usage.md#query_work_items)

---

## size - Calculate size

Calculate total size of a path.

**CLI:**
```bash
# Get total size of path
nexus size /workspace
```

**Python API:**
```python
# Calculate directory size
total_size = nx.calculate_size("/workspace")
print(f"Total size: {total_size} bytes")

# Calculate recursively with details
def calculate_size_detailed(path):
    entries = nx.list(path, recursive=True)
    total = 0
    for entry in entries:
        if entry['type'] == 'file':
            total += entry['size']
    return total

size = calculate_size_detailed("/workspace")
print(f"Total: {size:,} bytes ({size / 1024 / 1024:.2f} MB)")
```

**See Also:**
- [Python API: calculate_size()](../advanced-usage.md#calculate_size)

---

## find-duplicates - Find duplicate files

Find files with duplicate content.

**CLI:**
```bash
# Find duplicates by content hash
nexus find-duplicates /workspace
```

**Python API:**
```python
# Find duplicate files
duplicates = nx.find_duplicates("/workspace")
for hash_value, files in duplicates.items():
    if len(files) > 1:
        print(f"\nDuplicate files (hash: {hash_value}):")
        for file_path in files:
            print(f"  - {file_path}")
```

**See Also:**
- [Python API: find_duplicates()](../advanced-usage.md#find_duplicates)

---

## sync - One-way sync

Synchronize files from source to destination.

**CLI:**
```bash
# Sync from source to destination
nexus sync /source /destination
```

**Python API:**
```python
# Sync directories
result = nx.sync("/source", "/destination")
print(f"Copied: {result['copied_count']} files")
print(f"Deleted: {result['deleted_count']} files")
print(f"Updated: {result['updated_count']} files")
```

**See Also:**
- [Python API: sync()](../advanced-usage.md#sync)

---

## Common Workflows

### Operation tracking and undo
```bash
# Make some changes
nexus write /test/file1.txt "content 1"
nexus write /test/file2.txt "content 2"
nexus mkdir /test/subdir --parents

# View operation history
nexus ops log --limit 10

# Undo last operation
nexus undo --dry-run  # Preview
nexus undo  # Actually undo
```

### Python equivalent
```python
# Make some changes
nx.write("/test/file1.txt", b"content 1")
nx.write("/test/file2.txt", b"content 2")
nx.mkdir("/test/subdir", parents=True)

# View operation history
ops = nx.get_operation_log(limit=10)
for op in ops:
    print(f"{op['timestamp']}: {op['operation_type']} {op['path']}")

# Undo last operation
preview = nx.undo(dry_run=True)
print(f"Would undo: {preview['operation_type']}")

result = nx.undo()
print(f"Undone: {result['operation_type']}")
```

### Storage analysis
```bash
# Calculate total size
nexus size /workspace

# Find duplicate files to save space
nexus find-duplicates /workspace
```

### Python equivalent
```python
# Calculate total size
total = nx.calculate_size("/workspace")
print(f"Total size: {total:,} bytes ({total / 1024 / 1024:.2f} MB)")

# Find duplicates
duplicates = nx.find_duplicates("/workspace")
duplicate_count = sum(len(files) - 1 for files in duplicates.values() if len(files) > 1)
print(f"Found {duplicate_count} duplicate files")

# Calculate wasted space
wasted_space = 0
for hash_value, files in duplicates.items():
    if len(files) > 1:
        # Get size of one file, multiply by number of duplicates - 1
        file_size = nx.get_metadata(files[0])['size']
        wasted_space += file_size * (len(files) - 1)

print(f"Wasted space: {wasted_space:,} bytes ({wasted_space / 1024 / 1024:.2f} MB)")
```

---

## See Also

- [CLI Reference Overview](index.md)
- [Python API: Advanced Usage](../advanced-usage.md)
- [File Operations](file-operations.md)
- [Directory Operations](directory-operations.md)
