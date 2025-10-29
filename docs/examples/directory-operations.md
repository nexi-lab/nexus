# Directory Operations Example

Master directory management with hierarchical permissions and automatic inheritance in Nexus.

## 🎯 What You'll Learn

- Create directories (single and nested)
- List directory contents (recursive and non-recursive)
- Remove directories safely
- Work with hierarchical permission inheritance
- Handle implicit directories

## 🚀 Quick Start

=== "Python SDK"

    ```python
    import nexus

    nx = nexus.connect(config={"data_dir": "./nexus-data"})

    # Create nested directories
    nx.mkdir("/workspace/projects/alpha/src", parents=True)

    # List contents
    files = nx.list("/workspace/projects")
    for file in files:
        print(file.path)

    # Remove directory
    nx.rmdir("/workspace/projects/alpha", recursive=True)
    ```

=== "CLI"

    ```bash
    # Create directories
    nexus mkdir /workspace/projects/alpha/src --parents

    # List contents
    nexus ls /workspace/projects
    nexus ls /workspace/projects --recursive

    # Remove directory
    nexus rmdir /workspace/projects/alpha --recursive
    ```

## 📁 Create Directories

=== "Single Directory"

    ```python
    import nexus

    nx = nexus.connect(config={"data_dir": "./nexus-data"})

    # Create single directory
    nx.mkdir("/workspace/data")

    # Fails if parent doesn't exist
    try:
        nx.mkdir("/workspace/deep/nested/dir")
    except FileNotFoundError:
        print("Parent directory doesn't exist")
    ```

=== "Nested Directories"

    ```python
    import nexus

    nx = nexus.connect(config={"data_dir": "./nexus-data"})

    # Create nested directories (like mkdir -p)
    nx.mkdir("/workspace/projects/alpha/src", parents=True)
    nx.mkdir("/workspace/projects/beta/tests", parents=True)

    # All parent directories created automatically
    ```

=== "With Permissions"

    ```python
    import nexus

    nx = nexus.connect(remote_url="http://localhost:8080", api_key="admin-key")

    # Create directory
    nx.mkdir("/workspace/team-alpha", parents=True)

    # Grant team ownership (permissions inherit to subdirectories)
    nx.rebac_create("group", "team-alpha", "owner", "file", "/workspace/team-alpha")

    # Create subdirectory - permissions automatically inherited
    nx.mkdir("/workspace/team-alpha/projects")

    # Team members automatically have access to subdirectories!
    ```

## 📋 List Directory Contents

=== "Basic Listing"

    ```python
    import nexus

    nx = nexus.connect(config={"data_dir": "./nexus-data"})

    # List files in directory
    files = nx.list("/workspace")
    for file in files:
        print(f"{file.path} ({file.size} bytes)")
    ```

=== "Recursive Listing"

    ```python
    import nexus

    nx = nexus.connect(config={"data_dir": "./nexus-data"})

    # List all files recursively
    all_files = nx.list("/workspace", recursive=True)
    print(f"Found {len(all_files)} files total")

    # Non-recursive (only direct children)
    top_level = nx.list("/workspace", recursive=False)
    print(f"Found {len(top_level)} files in top level")
    ```

=== "With Metadata"

    ```python
    import nexus

    nx = nexus.connect(config={"data_dir": "./nexus-data"})

    # List with full metadata
    files = nx.list("/workspace")
    for file in files:
        print(f"""
        Path: {file.path}
        Size: {file.size} bytes
        Modified: {file.modified_at}
        Version: {file.version}
        ETag: {file.etag}
        """)
    ```

## 🗑️ Remove Directories

=== "Empty Directory"

    ```python
    import nexus

    nx = nexus.connect(config={"data_dir": "./nexus-data"})

    # Remove empty directory
    nx.rmdir("/workspace/empty-folder")

    # Fails if directory is not empty
    try:
        nx.rmdir("/workspace/projects")  # Has files
    except OSError:
        print("Directory not empty")
    ```

=== "Recursive Remove"

    ```python
    import nexus

    nx = nexus.connect(config={"data_dir": "./nexus-data"})

    # Remove directory and all contents (like rm -rf)
    nx.rmdir("/workspace/projects/alpha", recursive=True)

    # All files and subdirectories removed
    ```

## 🔐 Hierarchical Permission Inheritance

=== "Setup Hierarchy"

    ```python
    import nexus

    nx = nexus.connect(remote_url="http://localhost:8080", api_key="admin-key")

    # Create directory hierarchy
    nx.mkdir("/workspace/company", parents=True)
    nx.mkdir("/workspace/company/engineering", parents=True)
    nx.mkdir("/workspace/company/engineering/backend", parents=True)

    # Grant permissions at top level
    nx.rebac_create(
        "group", "all-employees",
        "viewer",
        "file", "/workspace/company"
    )

    nx.rebac_create(
        "group", "engineers",
        "editor",
        "file", "/workspace/company/engineering"
    )

    nx.rebac_create(
        "group", "backend-team",
        "owner",
        "file", "/workspace/company/engineering/backend"
    )
    ```

=== "Verify Inheritance"

    ```python
    import nexus

    nx = nexus.connect(remote_url="http://localhost:8080", api_key="admin-key")

    # Employee (in all-employees group) can read anywhere
    employee_can_read = nx.rebac_check(
        "user", "alice",
        "read",
        "file", "/workspace/company/engineering/backend/code.py"
    )
    print(f"Employee can read: {employee_can_read}")  # True (inherited)

    # Engineer can write to engineering/*
    engineer_can_write = nx.rebac_check(
        "user", "bob",
        "write",
        "file", "/workspace/company/engineering/doc.md"
    )
    print(f"Engineer can write: {engineer_can_write}")  # True (inherited)

    # Backend team member is owner of backend/*
    backend_can_delete = nx.rebac_check(
        "user", "charlie",
        "delete",
        "file", "/workspace/company/engineering/backend/old.py"
    )
    print(f"Backend member can delete: {backend_can_delete}")  # True
    ```

## 🎬 Complete Workflow Example

=== "Project Organization"

    ```python
    import nexus

    nx = nexus.connect(config={"data_dir": "./nexus-data"})

    # 1. Create project structure
    nx.mkdir("/workspace/my-project", parents=True)
    nx.mkdir("/workspace/my-project/src", parents=True)
    nx.mkdir("/workspace/my-project/tests", parents=True)
    nx.mkdir("/workspace/my-project/docs", parents=True)

    # 2. Add files to each directory
    nx.write("/workspace/my-project/README.md", b"# My Project")
    nx.write("/workspace/my-project/src/main.py", b"print('Hello')")
    nx.write("/workspace/my-project/tests/test_main.py", b"def test_main(): pass")

    # 3. List project structure
    files = nx.list("/workspace/my-project", recursive=True)
    for file in files:
        # Show indented tree
        depth = file.path.count('/') - 3
        indent = "  " * depth
        name = file.path.split('/')[-1]
        print(f"{indent}{name}")

    # 4. Archive old project
    nx.mkdir("/workspace/archive", parents=True)
    # Copy all files (would need to iterate)
    # Then remove original
    nx.rmdir("/workspace/my-project", recursive=True)
    ```

=== "Multi-Tenant Workspaces"

    ```python
    import nexus

    nx = nexus.connect(remote_url="http://localhost:8080", api_key="admin-key")

    tenants = ["acme-corp", "beta-inc", "gamma-ltd"]

    for tenant in tenants:
        # Create tenant workspace
        workspace_path = f"/tenants/{tenant}"
        nx.mkdir(workspace_path, parents=True)

        # Create standard subdirectories
        nx.mkdir(f"{workspace_path}/data", parents=True)
        nx.mkdir(f"{workspace_path}/models", parents=True)
        nx.mkdir(f"{workspace_path}/logs", parents=True)

        # Grant tenant admin full access
        nx.rebac_create(
            "user", f"admin@{tenant}.com",
            "owner",
            "file", workspace_path
        )

        # Permissions automatically inherited to subdirectories!

    # List all tenants
    tenant_dirs = nx.list("/tenants", recursive=False)
    print(f"Active tenants: {len(tenant_dirs)}")
    ```

## 💡 Pro Tips

=== "Implicit Directories"

    In Nexus, directories are implicit - they exist if files exist beneath them.

    ```python
    import nexus

    nx = nexus.connect(config={"data_dir": "./nexus-data"})

    # Write file with deep path
    nx.write("/workspace/a/b/c/file.txt", b"content")

    # Directories /workspace/a, /workspace/a/b, /workspace/a/b/c
    # are created automatically (implicit directories)

    # Check if directory exists (has files beneath it)
    exists = nx.metadata.is_implicit_directory("/workspace/a/b")
    print(f"Directory exists: {exists}")  # True

    # List works even though we never called mkdir
    files = nx.list("/workspace/a/b")  # Works!
    ```

=== "Permission Best Practices"

    ```python
    import nexus

    nx = nexus.connect(remote_url="http://localhost:8080", api_key="admin-key")

    # ✅ Good: Grant permissions at top level
    nx.rebac_create("group", "team", "owner", "file", "/workspace/team-folder")
    # Permissions automatically inherited to all subdirectories

    # ❌ Bad: Grant permissions file-by-file
    # nx.rebac_create("group", "team", "owner", "file", "/workspace/team-folder/file1.txt")
    # nx.rebac_create("group", "team", "owner", "file", "/workspace/team-folder/file2.txt")
    # ... tedious and error-prone!
    ```

## 🏃 Run the Full Demo

Try the complete interactive demo script:

```bash
# Start server
./scripts/init-nexus-with-auth.sh

# In another terminal
source .nexus-admin-env
./examples/cli/directory_operations_demo.sh
```

## 📚 What's Next?

- **[File Operations](file-operations.md)** - Master file manipulation
- **[Permissions Example](permissions.md)** - Learn ReBAC access control
- **[API Reference](../api/directory-operations.md)** - Complete API documentation
