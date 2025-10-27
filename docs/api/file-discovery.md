# File Discovery Operations

← [API Documentation](README.md)

This document describes operations for finding and searching files in Nexus.

## list()

List files in a directory with optional filtering.

```python
def list(
    path: str = "/",
    recursive: bool = True,
    details: bool = False,
    prefix: str | None = None,
    context: OperationContext | EnhancedOperationContext | None = None
) -> list[str] | list[dict[str, Any]]
```

**Parameters:**
- `path` (str): Directory path to list (default: "/")
- `recursive` (bool): If True, list all files recursively; if False, list only direct children
- `details` (bool): If True, return detailed metadata; if False, return paths only
- `prefix` (str, optional): Path prefix to filter by (deprecated, use path parameter instead)
- `context` (OperationContext | EnhancedOperationContext, optional): Operation context for permission filtering (uses default if None)

**Returns:**
- `list[str]`: List of file paths (if details=False) - filtered by READ permission
- `list[dict]`: List of file metadata dicts (if details=True) - filtered by READ permission

**Examples:**

```python
# List all files recursively (default)
all_files = nx.list()

# List files in root directory only (non-recursive)
root_files = nx.list("/", recursive=False)

# List files with metadata
files_with_metadata = nx.list(details=True)
for file in files_with_metadata:
    print(f"{file['path']}: {file['size']} bytes")

# List files in specific directory
docs = nx.list("/documents")

# List with specific user context (permission filtering)
from nexus.core.permissions import OperationContext
ctx = OperationContext(user="alice", groups=["team-engineering"])
user_files = nx.list("/workspace", context=ctx)  # Only shows files alice can read
```

---

## glob()

Find files matching a glob pattern.

```python
def glob(
    pattern: str,
    path: str = "/",
    context: OperationContext | EnhancedOperationContext | None = None
) -> list[str]
```

Supports standard glob patterns:
- `*` matches any sequence of characters (except `/`)
- `**` matches any sequence of characters including `/` (recursive)
- `?` matches any single character
- `[...]` matches any character in the brackets

**Parameters:**
- `pattern` (str): Glob pattern to match
- `path` (str): Base path to search from (default: "/")
- `context` (OperationContext | EnhancedOperationContext, optional): Operation context for permission filtering (uses default if None)

**Returns:**
- `list[str]`: List of matching file paths, sorted by name - filtered by READ permission

**Examples:**

```python
# Find all Python files recursively
python_files = nx.glob("**/*.py")

# Find all CSV files in data directory
csv_files = nx.glob("*.csv", "/data")

# Find all test files
test_files = nx.glob("test_*.py")

# Find files with specific naming pattern
logs = nx.glob("2025-01-*.log", "/logs")

# Find files with permission filtering
from nexus.core.permissions import OperationContext
ctx = OperationContext(user="bob", groups=["project-alpha"])
visible_files = nx.glob("**/*.txt", context=ctx)  # Only returns files bob can read
```

---

## grep()

Search file contents using regex patterns.

```python
def grep(
    pattern: str,
    path: str = "/",
    file_pattern: str | None = None,
    ignore_case: bool = False,
    max_results: int = 1000,
    search_mode: str = "auto",
    context: OperationContext | EnhancedOperationContext | None = None
) -> list[dict[str, Any]]
```

**Parameters:**
- `pattern` (str): Regex pattern to search for in file contents
- `path` (str): Base path to search from (default: "/")
- `file_pattern` (str, optional): Optional glob pattern to filter files
- `ignore_case` (bool): If True, perform case-insensitive search
- `max_results` (int): Maximum number of results to return (default: 1000)
- `search_mode` (str): Content search mode
  - `"auto"`: Try parsed text first, fallback to raw (default)
  - `"parsed"`: Only search parsed text
  - `"raw"`: Only search raw file content
- `context` (OperationContext | EnhancedOperationContext, optional): Operation context for permission filtering (uses default if None)

**Returns:**
- `list[dict]`: List of match dicts with keys (only includes files user has READ permission for):
  - `file`: File path
  - `line`: Line number (1-indexed)
  - `content`: Matched line content
  - `match`: The matched text
  - `source`: Source type - "parsed" or "raw"

**Examples:**

```python
# Search for "TODO" in all files
matches = nx.grep("TODO")
for match in matches:
    print(f"{match['file']}:{match['line']}: {match['content']}")

# Search for function definitions in Python files
matches = nx.grep(r"def \w+", file_pattern="**/*.py")

# Search only parsed PDFs
matches = nx.grep("revenue", file_pattern="**/*.pdf", search_mode="parsed")

# Case-insensitive search
matches = nx.grep("error", ignore_case=True)

# Search with permission filtering
from nexus.core.permissions import OperationContext
ctx = OperationContext(user="charlie", groups=["team-data"])
matches = nx.grep("SELECT", file_pattern="**/*.sql", context=ctx)  # Only searches files charlie can read
```

## See Also

- [File Operations](file-operations.md) - Basic file operations
- [Semantic Search](semantic-search.md) - AI-powered semantic search
- [Permissions](permissions.md) - Access control and filtering
- [CLI Reference](cli-reference.md) - Command-line search tools

## Next Steps

1. Try [semantic search](semantic-search.md) for natural language queries
2. Learn about [permissions](permissions.md) for filtering results
3. Explore [CLI tools](cli-reference.md) for shell-based workflows
