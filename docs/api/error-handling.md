# Error Handling

← [API Documentation](README.md)

This document describes error handling and exception types in Nexus.

### Exception Hierarchy

```
NexusError (base)
├── NexusFileNotFoundError
├── NexusPermissionError
├── BackendError
├── InvalidPathError
└── MetadataError
```

### Exception Details

#### NexusFileNotFoundError

```python
try:
    content = nx.read("/nonexistent.txt")
except NexusFileNotFoundError as e:
    print(f"File not found: {e.path}")
```

#### InvalidPathError

```python
try:
    nx.write("no-leading-slash.txt", b"content")  # Invalid
except InvalidPathError as e:
    print(f"Invalid path: {e.path}")
```

#### BackendError

```python
try:
    nx.write("/file.txt", b"content")
except BackendError as e:
    print(f"Backend error: {e.message}")
```

### Best Practices

```python
import nexus
from nexus import NexusFileNotFoundError, InvalidPathError

nx = nexus.connect()

try:
    # Check before reading
    if nx.exists("/file.txt"):
        content = nx.read("/file.txt")

    # Handle specific errors
    nx.write("/documents/report.pdf", b"content")

except NexusFileNotFoundError as e:
    print(f"File not found: {e.path}")
except InvalidPathError as e:
    print(f"Invalid path: {e.path}")
except Exception as e:
    print(f"Unexpected error: {e}")
finally:
    nx.close()
```

---

## See Also

- [File Operations](file-operations.md) - File operation errors
- [Permissions](permissions.md) - Permission errors
- [Advanced Usage](advanced-usage.md) - Error handling patterns

## Next Steps

1. Review [file operations](file-operations.md) error conditions
2. Implement proper exception handling
3. Use best practices for error recovery
