# File Operations Example

This example demonstrates all file manipulation operations in Nexus: write, read, copy, move, and delete with metadata tracking and optimistic concurrency control.

## 🎯 What You'll Learn

- Write content to files (inline, stdin, from files)
- Read files with and without metadata
- Copy files with automatic deduplication
- Move and rename files
- Delete files safely
- Use optimistic concurrency control (create-only, conditional updates)
- Handle binary files
- Work with permissions

## 🚀 Quick Start

=== "Python SDK"

    ```python
    import nexus

    # Connect (embedded mode - no auth)
    nx = nexus.connect(config={"data_dir": "./nexus-data"})

    # Write a file
    nx.write("/workspace/hello.txt", b"Hello, Nexus!")

    # Read it back
    content = nx.read("/workspace/hello.txt")
    print(content.decode())  # "Hello, Nexus!"

    # Copy file
    nx.copy("/workspace/hello.txt", "/workspace/hello-backup.txt")

    # Move/rename
    nx.move("/workspace/hello-backup.txt", "/workspace/archived/hello.txt")

    # Delete
    nx.delete("/workspace/archived/hello.txt")
    ```

=== "CLI"

    ```bash
    # Write files
    nexus write /workspace/hello.txt "Hello, Nexus!"
    echo "Content from stdin" | nexus write /workspace/file.txt --input -

    # Read files
    nexus cat /workspace/hello.txt
    nexus cat /workspace/hello.txt --metadata

    # Copy files
    nexus cp /workspace/hello.txt /workspace/hello-backup.txt

    # Move files
    nexus move /workspace/hello-backup.txt /workspace/archived/hello.txt --force

    # Delete files
    nexus rm /workspace/archived/hello.txt --force
    ```

=== "Server Mode"

    ```bash
    # Start server
    nexus serve --host 0.0.0.0 --port 8080

    # Set credentials
    export NEXUS_URL=http://localhost:8080
    export NEXUS_API_KEY=your-key

    # Same commands work remotely
    nexus write /workspace/hello.txt "Hello from server!"
    nexus cat /workspace/hello.txt
    ```

## 📝 Write Operations

=== "Basic Write"

    ```python
    import nexus

    nx = nexus.connect(config={"data_dir": "./nexus-data"})

    # Write inline content
    result = nx.write("/workspace/hello.txt", b"Hello, Nexus!")
    print(f"Wrote {result['size']} bytes, version {result['version']}")
    ```

=== "Write from File"

    ```python
    import nexus

    nx = nexus.connect(config={"data_dir": "./nexus-data"})

    # Read local file and write to Nexus
    with open("local_file.txt", "rb") as f:
        content = f.read()

    nx.write("/workspace/uploaded.txt", content)
    ```

=== "Write JSON"

    ```python
    import nexus
    import json

    nx = nexus.connect(config={"data_dir": "./nexus-data"})

    # Write JSON configuration
    config = {
        "app": "nexus-demo",
        "version": "1.0",
        "settings": {"debug": True, "max_retries": 3}
    }

    nx.write("/workspace/config.json", json.dumps(config).encode())
    ```

=== "Write Binary"

    ```python
    import nexus

    nx = nexus.connect(config={"data_dir": "./nexus-data"})

    # Write binary data (images, models, etc.)
    with open("model.pkl", "rb") as f:
        model_data = f.read()

    nx.write("/models/model.pkl", model_data)
    ```

## 📖 Read Operations

=== "Basic Read"

    ```python
    import nexus

    nx = nexus.connect(config={"data_dir": "./nexus-data"})

    # Read file content
    content = nx.read("/workspace/hello.txt")
    print(content.decode('utf-8'))
    ```

=== "Read with Metadata"

    ```python
    import nexus

    nx = nexus.connect(config={"data_dir": "./nexus-data"})

    # Read with full metadata
    result = nx.read("/workspace/hello.txt", return_metadata=True)

    print(f"Content: {result['content'].decode()}")
    print(f"ETag: {result['etag']}")
    print(f"Version: {result['version']}")
    print(f"Size: {result['size']} bytes")
    print(f"Modified: {result['modified_at']}")
    ```

=== "Stream Large Files"

    ```python
    import nexus

    nx = nexus.connect(config={"data_dir": "./nexus-data"})

    # Stream large file in chunks
    with open("output.bin", "wb") as f:
        for chunk in nx.stream("/models/large-model.pkl"):
            f.write(chunk)
    ```

## 📋 Copy Operations

=== "Simple Copy"

    ```python
    import nexus

    nx = nexus.connect(config={"data_dir": "./nexus-data"})

    # Copy file
    nx.copy("/workspace/source.txt", "/workspace/backup.txt")

    # Nexus uses content-addressable storage
    # Identical content is stored only once (automatic deduplication)
    ```

=== "Cross-Directory Copy"

    ```python
    import nexus

    nx = nexus.connect(config={"data_dir": "./nexus-data"})

    # Create destination directory
    nx.mkdir("/workspace/backups", parents=True)

    # Copy to different directory
    nx.copy("/workspace/config.json", "/workspace/backups/config.json")
    ```

## 🔀 Move Operations

=== "Rename in Same Directory"

    ```python
    import nexus

    nx = nexus.connect(config={"data_dir": "./nexus-data"})

    # Rename file
    nx.move("/workspace/old-name.txt", "/workspace/new-name.txt")
    ```

=== "Move to Different Directory"

    ```python
    import nexus

    nx = nexus.connect(config={"data_dir": "./nexus-data"})

    # Move file to archive
    nx.mkdir("/workspace/archive", parents=True)
    nx.move("/workspace/temp.txt", "/workspace/archive/temp.txt")
    ```

=== "Move with Rename"

    ```python
    import nexus

    nx = nexus.connect(config={"data_dir": "./nexus-data"})

    # Move and rename in one operation
    nx.move(
        "/workspace/draft.txt",
        "/workspace/published/final.txt"
    )
    ```

## 🗑️ Delete Operations

=== "Delete Single File"

    ```python
    import nexus

    nx = nexus.connect(config={"data_dir": "./nexus-data"})

    # Delete file (soft delete - can be recovered)
    nx.delete("/workspace/temp.txt")
    ```

=== "Delete Multiple Files"

    ```python
    import nexus

    nx = nexus.connect(config={"data_dir": "./nexus-data"})

    # Delete multiple files
    files_to_delete = [
        "/workspace/temp1.txt",
        "/workspace/temp2.txt",
        "/workspace/temp3.txt"
    ]

    for file in files_to_delete:
        nx.delete(file)
    ```

## 🔒 Optimistic Concurrency Control

=== "Create-Only Mode"

    ```python
    import nexus

    nx = nexus.connect(config={"data_dir": "./nexus-data"})

    try:
        # Write only if file doesn't exist
        nx.write("/workspace/new.txt", b"Initial content", if_none_match=True)
        print("File created")
    except FileExistsError:
        print("File already exists - write prevented")
    ```

=== "Conditional Update"

    ```python
    import nexus

    nx = nexus.connect(config={"data_dir": "./nexus-data"})

    # Read current version
    result = nx.read("/workspace/data.json", return_metadata=True)
    current_etag = result['etag']

    # Modify content
    new_content = modify_data(result['content'])

    try:
        # Write only if ETag matches (no concurrent modifications)
        nx.write(
            "/workspace/data.json",
            new_content,
            if_match=current_etag
        )
        print("Update successful")
    except nexus.ConflictError:
        print("File was modified by another process!")
    ```

## 🎬 Complete Workflow Example

=== "Document Versioning"

    ```python
    import nexus

    nx = nexus.connect(config={"data_dir": "./nexus-data"})

    # 1. Create initial document
    nx.mkdir("/workspace/docs", parents=True)
    nx.write("/workspace/docs/README.md", b"# My Project")

    # 2. Create backup before editing
    nx.copy("/workspace/docs/README.md", "/workspace/docs/README.backup.md")

    # 3. Update the document
    updated_content = b"""# My Project

    ## Overview
    This is an awesome project built with Nexus.

    ## Features
    - Feature 1
    - Feature 2
    """
    nx.write("/workspace/docs/README.md", updated_content)

    # 4. Archive old backup
    nx.mkdir("/workspace/archive/docs", parents=True)
    nx.move(
        "/workspace/docs/README.backup.md",
        "/workspace/archive/docs/README.backup.md"
    )

    # 5. Clean up after verification
    nx.delete("/workspace/archive/docs/README.backup.md")
    ```

## 🔐 Permission-Aware Operations

=== "Setup Permissions"

    ```python
    import nexus

    nx = nexus.connect(remote_url="http://localhost:8080", api_key="admin-key")

    # Grant user read-only access
    nx.rebac_create("user", "alice", "direct_viewer", "file", "/workspace/hello.txt")

    # Alice can read
    alice_nx = nexus.connect(remote_url="http://localhost:8080", api_key="alice-key")
    content = alice_nx.read("/workspace/hello.txt")  # ✓ Works

    # Alice cannot write
    try:
        alice_nx.write("/workspace/hello.txt", b"Update")  # ✗ Denied
    except nexus.PermissionError:
        print("Write denied - viewer has read-only access")

    # Upgrade to editor
    nx.rebac_create("user", "alice", "direct_editor", "file", "/workspace/hello.txt")

    # Now Alice can write
    alice_nx.write("/workspace/hello.txt", b"Updated by Alice")  # ✓ Works
    ```

## 🏃 Run the Full Demo

Try the complete interactive demo script:

```bash
# Start server
./scripts/init-nexus-with-auth.sh

# In another terminal
source .nexus-admin-env
./examples/cli/file_operations_demo.sh
```

The demo covers:
- ✅ Write operations (inline, stdin, file, JSON, binary)
- ✅ Read operations (basic, with metadata)
- ✅ Copy operations (simple, cross-directory)
- ✅ Move/rename operations
- ✅ Delete operations
- ✅ Optimistic concurrency control
- ✅ Permission-aware operations

## 📚 What's Next?

- **[Directory Operations](directory-operations.md)** - Learn hierarchical permission inheritance
- **[Permissions Example](permissions.md)** - Master ReBAC access control
- **[API Reference](../api/file-operations.md)** - Complete API documentation
