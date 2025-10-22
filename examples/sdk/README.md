# Nexus SDK Examples

This directory contains example projects demonstrating how to build custom tools and applications using the Nexus SDK.

## Examples

### 1. Basic Usage (`basic_usage.py`)

Demonstrates basic Nexus SDK operations:
- Connecting to Nexus
- Writing and reading files
- Listing directories
- Getting file metadata
- Searching for files
- Deleting files

**Run:**
```bash
python basic_usage.py
```

### 2. Custom File Browser (`custom_file_browser.py`)

A simple interactive terminal-based file browser built entirely with the Nexus SDK, without any CLI dependencies.

**Features:**
- Navigate directories (ls, cd, pwd)
- View file contents (cat)
- Search files (search with glob patterns)
- Interactive command interface

**Run:**
```bash
python custom_file_browser.py
```

**Example session:**
```
/> ls
/> cd /workspace
/workspace> search **/*.py
/workspace> cat /workspace/file.txt
/workspace> quit
```

### 3. Web API (`web_api.py`)

A simple REST API built with Flask and Nexus SDK, demonstrating how to expose Nexus filesystem operations over HTTP.

**Requirements:**
```bash
pip install flask
```

**Endpoints:**
- `GET /` - API documentation
- `GET /files?path=/&recursive=true` - List files
- `GET /files/<path>` - Read file
- `POST /files/<path>` - Write file (JSON: `{"content": "..."}`)
- `DELETE /files/<path>` - Delete file
- `GET /search?pattern=**/*.py` - Search files
- `GET /stats` - Get filesystem statistics

**Run:**
```bash
python web_api.py
```

Then visit http://localhost:5000 in your browser or use curl:

```bash
# List files
curl http://localhost:5000/files?path=/workspace

# Read a file
curl http://localhost:5000/files/workspace/file.txt

# Write a file
curl -X POST http://localhost:5000/files/workspace/new.txt \
  -H "Content-Type: application/json" \
  -d '{"content": "Hello from API"}'

# Search files
curl "http://localhost:5000/search?pattern=**/*.py"

# Get statistics
curl http://localhost:5000/stats
```

## Building Your Own Tools

These examples demonstrate the **Library/CLI separation** - you can build custom tools using the SDK without depending on CLI code:

```python
# ✅ Use the SDK for programmatic access
from nexus.sdk import connect

nx = connect()
nx.write("/file.txt", b"content")
```

```python
# ❌ Don't use CLI via subprocess
import subprocess
subprocess.run(["nexus", "write", "/file.txt", "content"])
```

## More Ideas

Here are more ideas for custom tools you can build with the SDK:

1. **GUI File Manager** - Build a graphical file manager with PyQt, Tkinter, or wxPython
2. **TUI with Rich** - Create a beautiful terminal UI with the Rich library
3. **VS Code Extension** - Build a VS Code extension to browse Nexus files
4. **Jupyter Plugin** - Create a Jupyter notebook integration
5. **Language Bindings** - Build bindings for other languages (JavaScript, Go, Rust)
6. **Backup Tool** - Create a custom backup solution
7. **Sync Tool** - Build a Dropbox-like sync client
8. **Monitoring Dashboard** - Create a web dashboard for monitoring Nexus
9. **CI/CD Integration** - Integrate Nexus into your CI/CD pipeline
10. **Custom Automation** - Build domain-specific automation tools

## Documentation

For complete SDK documentation, see:
- [SDK Usage Guide](../../docs/SDK_USAGE.md)
- [API Reference](../../docs/api/)

## Contributing

Have a cool example? Submit a PR! We'd love to see what you build with the Nexus SDK.
