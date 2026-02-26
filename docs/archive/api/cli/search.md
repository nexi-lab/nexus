# CLI: Search Operations

← [CLI Reference](index.md) | [API Documentation](../README.md)

This document describes CLI commands for searching files and content, and their Python API equivalents.

## glob - Find files by pattern

Find files matching glob patterns.

**CLI:**
```bash
# Find Python files
nexus glob "**/*.py"

# Find in specific path
nexus glob "*.txt" /workspace

# Show details
nexus glob -l "**/*.py"

# Filter by type
nexus glob -t f "**/*"          # Only files
nexus glob -t d "/workspace/*"  # Only directories
```

**Python API:**
```python
# Find Python files
files = nx.glob("**/*.py")
for file in files:
    print(file)

# Find in specific path
files = nx.glob("*.txt", path="/workspace")

# Filter by type
files = nx.glob("**/*", file_type="file")  # Only files
dirs = nx.glob("*", path="/workspace", file_type="directory")  # Only directories

# Get file details
files = nx.glob("**/*.py")
for file_path in files:
    metadata = nx.get_metadata(file_path)
    print(f"{file_path}: {metadata['size']} bytes")
```

**Options:**
- `-l, --long`: Show detailed file information
- `-t, --type [f|d]`: Filter by type (f=file, d=directory)
- `--remote-url URL`: Connect to remote server
- `--remote-api-key KEY`: API key for authentication

**See Also:**
- [Python API: glob()](../file-discovery.md#glob)

---

## grep - Search file contents

Search for text patterns in file contents.

**CLI:**
```bash
# Basic search
nexus grep "TODO"

# With line numbers
nexus grep -n "error" /workspace

# Only filenames
nexus grep -l "TODO" .

# Count matches
nexus grep -c "import" **/*.py

# Context lines
nexus grep -A 3 -B 3 "def main"   # 3 lines before/after
nexus grep -C 5 "class"           # 5 lines context

# Case insensitive
nexus grep -i "error"

# Invert match
nexus grep -v "test" file.txt

# Search PDFs (parsed text)
nexus grep "revenue" -f "**/*.pdf" --search-mode=parsed
```

**Python API:**
```python
# Basic search
results = nx.grep("TODO")
for match in results:
    print(f"{match['path']}:{match['line_number']}: {match['line']}")

# Search in specific path
results = nx.grep("error", path="/workspace")

# Only get filenames
files = nx.grep("TODO", path=".", only_filenames=True)

# Case insensitive
results = nx.grep("error", case_sensitive=False)

# Search with context
results = nx.grep("def main", context_before=3, context_after=3)
for match in results:
    print(f"\n{match['path']}:{match['line_number']}")
    for line in match['context_before']:
        print(f"  {line}")
    print(f"> {match['line']}")
    for line in match['context_after']:
        print(f"  {line}")

# Search PDFs
results = nx.grep("revenue", file_pattern="**/*.pdf", search_mode="parsed")
```

**Options:**
- `-n`: Show line numbers
- `-l`: Only show filenames (don't show matching lines)
- `-c`: Show count of matches per file
- `-A NUM`: Show NUM lines after each match
- `-B NUM`: Show NUM lines before each match
- `-C NUM`: Show NUM lines of context (before and after)
- `-i`: Case insensitive search
- `-v`: Invert match (show non-matching lines)
- `-f PATTERN`: File pattern to search within
- `--search-mode [text|parsed]`: Search mode (parsed for PDFs)
- `--remote-url URL`: Connect to remote server
- `--remote-api-key KEY`: API key for authentication

**See Also:**
- [Python API: grep()](../file-discovery.md#grep)

---

## Common Workflows

### Find and search files
```bash
# Find all Python files
nexus glob "**/*.py"

# Search for TODOs in Python files
nexus grep "TODO" -f "**/*.py" -n

# Find large files
nexus glob "**/*" -l | grep -v "^d" | sort -k5 -n -r | head -10

# Search configuration files
nexus glob "*.json" /config
nexus grep "database" -f "/config/*.json"
```

### Python equivalent
```python
import nexus

# Initialize
nx = nexus.Nexus(data_dir="./nexus-data")

# Find all Python files
python_files = nx.glob("**/*.py")
print(f"Found {len(python_files)} Python files")

# Search for TODOs in Python files
todos = nx.grep("TODO", file_pattern="**/*.py")
for match in todos:
    print(f"{match['path']}:{match['line_number']}: {match['line']}")

# Find large files
all_files = nx.glob("**/*", file_type="file")
file_sizes = []
for file_path in all_files:
    metadata = nx.get_metadata(file_path)
    file_sizes.append((file_path, metadata['size']))

# Sort by size
file_sizes.sort(key=lambda x: x[1], reverse=True)
for path, size in file_sizes[:10]:
    print(f"{path}: {size} bytes")

# Search configuration files
config_files = nx.glob("*.json", path="/config")
for config_file in config_files:
    results = nx.grep("database", path=config_file)
    if results:
        print(f"\nFound in {config_file}:")
        for match in results:
            print(f"  Line {match['line_number']}: {match['line']}")
```

### Advanced pattern matching
```bash
# Find all markdown files
nexus glob "**/*.md"

# Find files modified recently (combine with ls)
nexus ls /docs --long --recursive

# Search for function definitions in Python
nexus grep "^def " -f "**/*.py" -n

# Search for imports
nexus grep "^import\|^from.*import" -f "**/*.py" -n
```

### Python equivalent
```python
import re

# Find all markdown files
md_files = nx.glob("**/*.md")

# Get recently modified files
all_files = nx.list("/docs", recursive=True)
recent_files = sorted(
    [f for f in all_files if f['type'] == 'file'],
    key=lambda x: x['modified_at'],
    reverse=True
)[:10]

for file in recent_files:
    print(f"{file['name']}: {file['modified_at']}")

# Search for function definitions in Python
python_files = nx.glob("**/*.py")
for file_path in python_files:
    content = nx.read(file_path).decode('utf-8')
    for i, line in enumerate(content.split('\n'), 1):
        if line.strip().startswith('def '):
            print(f"{file_path}:{i}: {line}")

# Search for imports
for file_path in python_files:
    content = nx.read(file_path).decode('utf-8')
    for i, line in enumerate(content.split('\n'), 1):
        if re.match(r'^import |^from .* import ', line):
            print(f"{file_path}:{i}: {line}")
```

---

## See Also

- [CLI Reference Overview](index.md)
- [Python API: File Discovery](../file-discovery.md)
- [File Operations](file-operations.md)
- [Directory Operations](directory-operations.md)
- [Semantic Search](semantic-search.md)
