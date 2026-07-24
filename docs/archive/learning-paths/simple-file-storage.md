# Simple File Storage

**Learn the basics of file operations with Nexus in server mode**

⏱️ **Time:** 5 minutes | 💡 **Difficulty:** Easy

## What You'll Learn

- Start a Nexus server with authentication
- Write, read, and list files
- Copy and move files
- Delete files and directories
- Basic error handling

## Prerequisites

✅ Python 3.8+ installed
✅ Nexus installed (`pip install nexus-ai-fs`)
✅ Basic command-line knowledge

## Overview

This tutorial introduces you to Nexus's core file operations using **server mode**. Server mode is ideal for multi-user scenarios, production deployments, and when you need centralized storage with authentication.

**Architecture:**

```
┌─────────────────┐
│  Python Client  │  ← Your application
└────────┬────────┘
         │ HTTP + API Key
         ↓
┌─────────────────┐
│  Nexus Server   │  ← nexus serve
│  (Port 2026)    │
└────────┬────────┘
         │
         ↓
┌─────────────────┐
│  Local Storage  │  ← Files on disk
│  (./nexus-data) │
└─────────────────┘
```

---

## Step 1: Start the Nexus Server

First, start a Nexus server with authentication enabled:

```bash
# Start server in the background
nexus serve --host 0.0.0.0 --port 2026 --data-dir ./nexus-data &

# Wait a moment for server to start
sleep 2

# Check if server is running
curl http://localhost:2026/health
```

**Expected output:**
```json
{"status":"ok","version":"0.5.0"}
```

The server is now running and ready to accept connections!

---

## Step 2: Get Your API Key

Create an admin user and get an API key:

```bash
# Create admin user
nexus admin create-user admin \
  --name "Admin User" \
  --email "admin@example.com"

# Create API key for the user
nexus admin create-user-key admin \
  --description "Tutorial key"
```

**Expected output:**
```
API Key created: nxk_1234567890abcdef...
User ID: usr_abc123
```

**💾 Save this key!** You'll need it for all operations. Export it as an environment variable:

```bash
export NEXUS_URL=http://localhost:2026
export NEXUS_API_KEY=nxk_1234567890abcdef...  # Use YOUR key
```

---

## Step 3: Write Your First File

Create a Python script to write a file:

```python
# file_storage_demo.py
import nexus

# Connect to server with API key
nx = nexus.connect(
    config={
        "url": "http://localhost:2026",
        "api_key": "nxk_1234567890abcdef...",  # Replace with YOUR key
    }
)

# Write a simple text file
content = b"Hello from Nexus! This is my first file."
nx.write("/workspace/hello.txt", content)
print("✅ File written successfully!")

# Read it back
result = nx.read("/workspace/hello.txt")
print(f"📄 File content: {result.decode()}")
```

**Run it:**

```bash
python file_storage_demo.py
```

**Expected output:**
```
✅ File written successfully!
📄 File content: Hello from Nexus! This is my first file.
```

🎉 **Congratulations!** You just wrote and read your first file with Nexus.

---

## Step 4: List Files

Let's see what files exist in our workspace:

```python
# Add to file_storage_demo.py

# List all files in workspace
files = nx.list_files("/workspace")
print(f"\n📁 Files in /workspace:")
for file_info in files:
    print(f"  - {file_info['path']} ({file_info['size']} bytes)")
```

**Output:**
```
📁 Files in /workspace:
  - /workspace/hello.txt (42 bytes)
```

---

## Step 5: Copy and Move Files

```python
# Add to file_storage_demo.py

# Copy file to backup
nx.copy("/workspace/hello.txt", "/workspace/hello-backup.txt")
print("\n✅ File copied to hello-backup.txt")

# Move file to archive
nx.move("/workspace/hello-backup.txt", "/workspace/archive/hello-old.txt")
print("✅ File moved to archive/hello-old.txt")

# List again to see changes
files = nx.list_files("/workspace", recursive=True)
print("\n📁 All files:")
for f in files:
    print(f"  - {f['path']}")
```

**Output:**
```
✅ File copied to hello-backup.txt
✅ File moved to archive/hello-old.txt

📁 All files:
  - /workspace/hello.txt
  - /workspace/archive/hello-old.txt
```

---

## Step 6: Delete Files

```python
# Add to file_storage_demo.py

# Delete a single file
nx.delete("/workspace/archive/hello-old.txt")
print("\n✅ Deleted hello-old.txt")

# Delete entire directory (recursive)
nx.rmdir("/workspace/archive", recursive=True)
print("✅ Deleted archive/ directory")

# Verify deletion
files = nx.list_files("/workspace", recursive=True)
print(f"\n📁 Remaining files: {len(files)}")
for f in files:
    print(f"  - {f['path']}")
```

**Output:**
```
✅ Deleted hello-old.txt
✅ Deleted archive/ directory

📁 Remaining files: 1
  - /workspace/hello.txt
```

---

## Complete Working Example

Here's the full script you can copy and run:

```python
#!/usr/bin/env python3
"""
Simple File Storage Demo with Nexus
Prerequisites: Nexus server running on localhost:2026
"""

import nexus

# Configuration
NEXUS_URL = "http://localhost:2026"
NEXUS_API_KEY = "nxk_1234567890abcdef..."  # Replace with YOUR key


def main():
    # Connect to Nexus server
    nx = nexus.connect(config={"url": NEXUS_URL, "api_key": NEXUS_API_KEY})

    print("=== Nexus File Storage Demo ===\n")

    # 1. Write a file
    print("1️⃣ Writing file...")
    content = b"Hello from Nexus! This is my first file."
    nx.write("/workspace/demo/hello.txt", content)
    print("   ✅ File written to /workspace/demo/hello.txt\n")

    # 2. Read the file
    print("2️⃣ Reading file...")
    result = nx.read("/workspace/demo/hello.txt")
    print(f"   📄 Content: {result.decode()}\n")

    # 3. List files
    print("3️⃣ Listing files...")
    files = nx.list_files("/workspace/demo")
    for f in files:
        print(f"   - {f['path']} ({f['size']} bytes)")
    print()

    # 4. Copy file
    print("4️⃣ Copying file...")
    nx.copy("/workspace/demo/hello.txt", "/workspace/demo/hello-copy.txt")
    print("   ✅ Copied to hello-copy.txt\n")

    # 5. Move file
    print("5️⃣ Moving file...")
    nx.move("/workspace/demo/hello-copy.txt", "/workspace/demo/backup/hello.txt")
    print("   ✅ Moved to backup/hello.txt\n")

    # 6. List all files recursively
    print("6️⃣ Listing all files (recursive)...")
    files = nx.list_files("/workspace/demo", recursive=True)
    for f in files:
        print(f"   - {f['path']}")
    print()

    # 7. Delete file
    print("7️⃣ Cleaning up...")
    nx.delete("/workspace/demo/backup/hello.txt")
    nx.rmdir("/workspace/demo/backup", recursive=True)
    print("   ✅ Deleted backup directory\n")

    # 8. Final status
    print("8️⃣ Final status:")
    files = nx.list_files("/workspace/demo", recursive=True)
    print(f"   📁 {len(files)} file(s) remaining")

    print("\n✨ Demo complete!")


if __name__ == "__main__":
    main()
```

**Run the complete demo:**

```bash
# Make sure server is running and API key is set
python file_storage_demo.py
```

---

## Using the CLI

You can also perform all these operations using the CLI:

```bash
# Write file
echo "Hello from CLI" | nexus write /workspace/cli-demo.txt --input -

# Read file
nexus cat /workspace/cli-demo.txt

# List files
nexus ls /workspace

# Copy file
nexus cp /workspace/cli-demo.txt /workspace/cli-copy.txt

# Move file
nexus mv /workspace/cli-copy.txt /workspace/archive/cli.txt

# Delete file
nexus rm /workspace/archive/cli.txt
nexus rmdir /workspace/archive
```

---

## Error Handling

Handle common errors gracefully:

```python
import nexus
from nexus import NexusError, NexusFileNotFoundError, NexusPermissionError

nx = nexus.connect()  # Uses NEXUS_URL and NEXUS_API_KEY from environment

try:
    # Try to read non-existent file
    content = nx.read("/workspace/does-not-exist.txt")
except NexusFileNotFoundError as e:
    print(f"❌ File not found: {e}")
except NexusPermissionError as e:
    print(f"❌ Permission denied: {e}")
except NexusError as e:
    print(f"❌ Nexus error: {e}")
```

---

## Troubleshooting

### Issue: Server won't start

**Error:** `Address already in use`

**Solution:**
```bash
# Kill existing server
pkill -f "nexus serve"

# Or use different port
nexus serve --port 8081
```

---

### Issue: Authentication failed

**Error:** `401 Unauthorized`

**Solution:**
```bash
# Verify server is running
curl http://localhost:2026/health

# Verify API key is correct
echo $NEXUS_API_KEY

# If needed, create new API key
nexus admin create-user-key admin --description "New key"
```

---

### Issue: File not found

**Error:** `NexusFileNotFoundError: /workspace/file.txt`

**Solution:**
```python
from nexus import NexusFileNotFoundError

# Check if file exists first
try:
    info = nx.stat("/workspace/file.txt")
    print(f"File exists: {info['size']} bytes")
except NexusFileNotFoundError:
    print("File does not exist - creating it")
    nx.write("/workspace/file.txt", b"New file")
```

---

### Issue: Permission denied

**Error:** `NexusPermissionError`

**Solution:**
```bash
# Grant read/write permissions
nexus rebac grant user admin file /workspace --relation owner

# Check permissions
nexus rebac check user admin file /workspace --relation can_read
```

---

## Key Concepts

### Server Mode vs. Embedded Mode

**Server Mode** (this tutorial):
- ✅ Multi-user support
- ✅ Authentication & permissions
- ✅ Remote access
- ✅ Production-ready
- ⚠️ Requires server setup

**Embedded Mode:**
- ✅ No server needed
- ✅ Simple setup
- ✅ Perfect for prototyping
- ⚠️ Single-user only
- ⚠️ No authentication

### File Paths

All file paths in Nexus:
- Start with `/` (absolute paths)
- Use forward slashes `/` (even on Windows)
- Are case-sensitive
- Support Unicode characters

### Workspaces

Workspaces provide isolation:
```python
# Files in different workspaces are isolated
nx.write("/workspace/file.txt", b"data")  # User workspace
nx.write("/shared/file.txt", b"data")  # Shared workspace
nx.write("/private/file.txt", b"data")  # Private workspace
```

---

## What's Next?

Now that you've mastered basic file operations, explore more advanced features:

### 🔍 Recommended Next Steps

1. **[Document Q&A System](document-qa.md)** (10 min)
   Learn semantic search and LLM-powered document reading

2. **[AI Agent Memory](ai-agent-memory.md)** (15 min)
   Add persistent memory to your agents

3. **[Team Collaboration](team-collaboration.md)** (20 min)
   Multi-user access with permissions

### 📚 Related Concepts

- [What is Nexus?](../concepts/what-is-nexus.md) - Understand the architecture
- [Server Setup](../getting-started/server-setup.md) - Advanced server configuration
- [Permissions](../concepts/rebac-explained.md) - Fine-grained access control

### 🔧 Advanced Topics

- [Versioning & Time Travel](../api/versioning.md) - Track file history
- [Metadata & Tags](../api/metadata.md) - Organize files with metadata
- [Batch Operations](../api/file-operations.md#batch-operations) - Optimize performance

---

## Complete Code Reference

### Python SDK

```python
import nexus

# Connect (using environment variables)
nx = nexus.connect()  # Reads NEXUS_URL and NEXUS_API_KEY

# Or connect with explicit config
nx = nexus.connect(config={"url": "http://localhost:2026", "api_key": "your-key"})

# Write
nx.write("/path/to/file.txt", b"content")

# Read
content = nx.read("/path/to/file.txt")

# List
files = nx.list_files("/path", recursive=True)

# Copy
nx.copy("/source.txt", "/dest.txt")

# Move
nx.move("/old.txt", "/new.txt")

# Delete
nx.delete("/file.txt")
nx.rmdir("/directory", recursive=True)

# Check existence
exists = nx.exists("/file.txt")

# Get file info
info = nx.stat("/file.txt")
```

### CLI Commands

```bash
# Server
nexus serve --host 0.0.0.0 --port 2026

# Admin
nexus admin create-user <username>
nexus admin create-user-key <username>
nexus admin list-users

# Files
nexus write <path> <content>
nexus cat <path>
nexus ls <path>
nexus cp <source> <dest>
nexus mv <source> <dest>
nexus rm <path>
nexus rmdir <path>
```

---

## Summary

🎉 **You've completed the Simple File Storage tutorial!**

**What you learned:**
- ✅ Start Nexus server with authentication
- ✅ Create users and API keys
- ✅ Write, read, list, copy, move, and delete files
- ✅ Handle errors gracefully
- ✅ Use both Python SDK and CLI

**Time to build:** You're now ready to integrate Nexus into your applications!

---

**Next:** [Document Q&A System →](document-qa.md)

**Questions?** Check our [Troubleshooting Guide](../getting-started/troubleshooting-101.md) or [GitHub Discussions](https://github.com/nexi-lab/nexus/discussions)
