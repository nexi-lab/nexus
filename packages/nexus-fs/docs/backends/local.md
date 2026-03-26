# Local Filesystem

The local backend is included in the base package. No extra dependencies
or credentials required.

## Mount

```python
import nexus.fs

fs = nexus.fs.mount_sync("local://./my-data")
```

The URI path is resolved relative to the current working directory.
Absolute paths work too:

```python
# skip-test
import nexus.fs

fs = nexus.fs.mount_sync("local:///tmp/scratch")
# Mounts at /local/tmp/scratch/
```

## Mount path

| URI | Mount point |
|-----|-------------|
| `local://./data` | `/local/data/` |
| `local://./src/assets` | `/local/src/assets/` |
| `local:///tmp/scratch` | `/local/tmp/scratch/` |

Override with `at=`:

```python
# skip-test
import nexus.fs

fs = nexus.fs.mount_sync("local://./data", at="/files")
# Now at /files/ instead of /local/data/
```

## Common patterns

### Read and write files

```python
import nexus.fs

fs = nexus.fs.mount_sync("local://./my-data")

# Write
fs.write("/local/my-data/notes.txt", b"Meeting notes for Monday")

# Read
content = fs.read("/local/my-data/notes.txt")
print(content)
#> b'Meeting notes for Monday'
```

### List directory contents

```python
import nexus.fs

fs = nexus.fs.mount_sync("local://./my-data")
fs.write("/local/my-data/a.txt", b"a")
fs.write("/local/my-data/b.txt", b"b")

# Simple listing (returns paths)
files = fs.ls("/local/my-data/")
print(files)

# Detailed listing with metadata
entries = fs.ls("/local/my-data/", detail=True)
for entry in entries:
    print(entry["path"], entry["size"])
```

### Check if a file exists

```python
import nexus.fs

fs = nexus.fs.mount_sync("local://./my-data")
fs.write("/local/my-data/check.txt", b"exists")

print(fs.exists("/local/my-data/check.txt"))
#> True
print(fs.exists("/local/my-data/nope.txt"))
#> False
```

### Create directories

```python
import nexus.fs

fs = nexus.fs.mount_sync("local://./my-data")
fs.mkdir("/local/my-data/subdir/nested", parents=True)
```

## Multi-mount

Mount multiple local directories side by side:

```python
# skip-test
import nexus.fs

fs = nexus.fs.mount_sync("local://./src", "local://./data")
# /local/src/ and /local/data/ both available
```
