#!/usr/bin/env python3
"""
Nexus Python SDK - Directory Operations Demo

Demonstrates directory management operations using a remote server:
- Creating directories (mkdir with parents)
- Removing directories (rmdir, recursive deletion)
- Checking directory existence (is_directory)
- Listing directory contents
- Directory permissions
- Working with nested directory structures

Prerequisites:
1. Server running: ./scripts/init-nexus-with-auth.sh
2. Load credentials: source .nexus-admin-env
3. Set SERVER_URL: export SERVER_URL=$NEXUS_URL

Usage:
    python examples/python/directory_operations_demo.py
"""

import os
import sys
from contextlib import suppress

try:
    from nexus.remote.client import RemoteNexusFS
except ImportError:
    print("❌ nexus-ai-fs not installed. Run: pip install nexus-ai-fs")
    sys.exit(1)


def print_section(title: str):
    """Print a formatted section header"""
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}\n")


def main():
    # Get server URL and API key from environment
    server_url = os.environ.get("SERVER_URL") or os.environ.get("NEXUS_URL")
    api_key = os.environ.get("NEXUS_API_KEY")

    if not server_url:
        print("❌ SERVER_URL not set. Please run:")
        print("   source .nexus-admin-env")
        print("   export SERVER_URL=$NEXUS_URL")
        sys.exit(1)

    if not api_key:
        print("❌ NEXUS_API_KEY not set. Please run:")
        print("   source .nexus-admin-env")
        sys.exit(1)

    print("╔════════════════════════════════════════════════════════╗")
    print("║    Nexus Python SDK - Directory Operations Demo       ║")
    print("╚════════════════════════════════════════════════════════╝")
    print(f"\n🌐 Server: {server_url}")
    print(f"🔑 Using API Key: {api_key[:20]}...")

    # Connect to remote server
    print_section("1. Connecting to Remote Server")

    try:
        nx = RemoteNexusFS(server_url=server_url, api_key=api_key)
        print(f"✅ Connected to {server_url}")
    except Exception as e:
        print(f"❌ Failed to connect: {e}")
        sys.exit(1)

    try:
        # Basic directory creation
        print_section("2. Basic Directory Creation")

        # Ensure /workspace exists and has permissions
        workspace_root = "/workspace"
        base_path = "/workspace/dir-demo"

        # Setup /workspace permissions if needed
        print("🔐 Setting up workspace permissions...")
        with suppress(Exception):
            nx.rebac_create(
                subject=("user", "admin"),
                relation="direct_owner",
                object=("file", workspace_root),
            )
        with suppress(Exception):
            nx.rebac_create(
                subject=("user", "admin"),
                relation="direct_viewer",
                object=("file", workspace_root),
            )

        # Clean up any existing demo directory first
        with suppress(Exception):
            nx.rmdir(base_path, recursive=True)

        # Create demo directory with parents flag
        try:
            nx.mkdir(base_path, parents=True)
            print(f"✓ Created directory: {base_path}")
        except Exception as e:
            if "already exists" in str(e).lower():
                print(f"  (already exists: {base_path})")
            else:
                raise

        # Grant admin permissions on demo directory
        with suppress(Exception):
            nx.rebac_create(
                subject=("user", "admin"),
                relation="direct_owner",
                object=("file", base_path),
            )
        with suppress(Exception):
            nx.rebac_create(
                subject=("user", "admin"),
                relation="direct_viewer",
                object=("file", base_path),
            )
        print("✓ Granted admin full access")

        # Create nested directories with parents=True
        print_section("3. Creating Nested Directories (parents=True)")

        nested_paths = [
            f"{base_path}/projects/alpha/src",
            f"{base_path}/projects/beta/tests",
            f"{base_path}/data/raw/2025/Q1",
            f"{base_path}/data/processed/2025/Q1",
            f"{base_path}/config/environments/production",
        ]

        for path in nested_paths:
            try:
                nx.mkdir(path, parents=True)
                print(f"✓ Created: {path}")
            except Exception as e:
                if "already exists" in str(e).lower():
                    print(f"  (already exists: {path})")
                else:
                    raise

        # Check directory existence
        print_section("4. Checking Directory Existence")

        print("Using exists() and is_directory() to check paths:")

        test_paths = [
            f"{base_path}/projects/alpha",
            f"{base_path}/projects/gamma",
            f"{base_path}/data",
            f"{base_path}/nonexistent",
        ]

        for path in test_paths:
            exists = nx.exists(path)
            is_dir = nx.is_directory(path) if exists else False
            if exists:
                path_type = "directory" if is_dir else "file"
                print(f"✓ {path} - exists ({path_type})")
            else:
                print(f"✗ {path} - doesn't exist")

        # List directory contents
        print_section("5. Listing Directory Contents")

        # Non-recursive listing
        print(f"📂 Contents of {base_path}:")
        contents = nx.list(base_path, recursive=False)
        for item in sorted(contents):
            is_dir = nx.is_directory(item)
            icon = "📁" if is_dir else "📄"
            print(f"   {icon} {item}")

        # Recursive listing
        print(f"\n📂 All items in {base_path} (recursive):")
        all_items = nx.list(base_path, recursive=True)
        for item in sorted(all_items):
            is_dir = nx.is_directory(item)
            icon = "📁" if is_dir else "📄"
            depth = item.count("/") - base_path.count("/")
            indent = "   " + "  " * depth
            print(f"{indent}{icon} {os.path.basename(item)}")

        # Create some files to demonstrate directories vs files
        print_section("6. Directories vs Files")

        test_files = [
            f"{base_path}/projects/alpha/src/main.py",
            f"{base_path}/projects/alpha/README.md",
            f"{base_path}/config/app.json",
        ]

        print("Creating test files...")
        for file_path in test_files:
            nx.write(file_path, b"# Sample content")
            print(f"✓ Created file: {file_path}")

        print("\nChecking path types:")
        check_paths = [
            f"{base_path}/projects/alpha/src",  # directory
            f"{base_path}/projects/alpha/src/main.py",  # file
            f"{base_path}/config",  # directory
            f"{base_path}/config/app.json",  # file
        ]

        for path in check_paths:
            is_dir = nx.is_directory(path)
            path_type = "directory" if is_dir else "file"
            icon = "📁" if is_dir else "📄"
            print(f"   {icon} {path} → {path_type}")

        # exist_ok parameter
        print_section("7. Using exist_ok Parameter")

        existing_dir = f"{base_path}/config"

        print(f"Attempting to create existing directory: {existing_dir}")
        try:
            nx.mkdir(existing_dir, exist_ok=False)
            print("✗ Should have raised FileExistsError")
        except Exception as e:
            print(f"✓ Correctly raised error: {e.__class__.__name__}")

        print("\nWith exist_ok=True:")
        try:
            nx.mkdir(existing_dir, exist_ok=True)
            print("✓ No error raised for existing directory")
        except Exception as e:
            print(f"✗ Unexpected error: {e}")

        # Directory removal
        print_section("8. Removing Directories")

        # Create a temporary directory to remove
        temp_dir = f"{base_path}/temp"
        nx.mkdir(temp_dir)
        print(f"✓ Created temp directory: {temp_dir}")

        # Remove empty directory
        nx.rmdir(temp_dir)
        print(f"✓ Removed empty directory: {temp_dir}")

        # Verify it's gone
        if not nx.is_directory(temp_dir):
            print(f"✓ Verified: {temp_dir} no longer exists")

        # Recursive directory removal
        print("\n📦 Recursive directory removal:")

        # Create a directory with content
        test_tree = f"{base_path}/test-tree"
        nx.mkdir(f"{test_tree}/level1/level2", parents=True)
        nx.write(f"{test_tree}/file1.txt", b"content")
        nx.write(f"{test_tree}/level1/file2.txt", b"content")
        print(f"✓ Created test directory tree: {test_tree}")

        # Try to remove non-empty directory without recursive
        print("\nAttempting to remove non-empty directory without recursive=True:")
        try:
            nx.rmdir(test_tree, recursive=False)
            print("✗ Should have raised error")
        except Exception as e:
            print(f"✓ Correctly raised error: {e.__class__.__name__}")

        # Remove with recursive=True
        print("\nRemoving with recursive=True:")
        nx.rmdir(test_tree, recursive=True)
        print(f"✓ Removed directory tree: {test_tree}")

        # Permission checks
        print_section("9. Directory Permissions")

        print("For permission management with specific users, use:")
        print("  - nexus rebac create user <user> <relation> file <path>")
        print("  - nexus rebac check user <user> <permission> file <path>")
        print("")
        print("Example:")
        print(f"  nexus rebac create user alice direct_owner file {base_path}")
        print(f"  nexus rebac check user alice write file {base_path}")

        # Working with deep hierarchies
        print_section("10. Working with Deep Directory Hierarchies")

        # Create a typical project structure
        project_structure = [
            f"{base_path}/my-project/src/components",
            f"{base_path}/my-project/src/utils",
            f"{base_path}/my-project/tests/unit",
            f"{base_path}/my-project/docs",
        ]

        print("Creating project structure:")
        for path in project_structure:
            try:
                nx.mkdir(path, parents=True, exist_ok=True)
                print(f"  📁 {path}")
            except Exception as e:
                print(f"  ⚠️  {path}: {e}")

        # Get directory statistics
        print_section("11. Directory Statistics")

        stats_path = f"{base_path}/my-project"
        try:
            all_items = nx.list(stats_path, recursive=True)
            print(f"Statistics for {stats_path}:")
            print(f"   Total items: {len(all_items)}")
            print("\nItems:")
            for item in sorted(all_items):
                print(f"   - {item}")
        except Exception as e:
            print(f"⚠️  Could not get statistics: {e}")

        # Summary
        print_section("✅ Demo Complete!")

        print("You've learned:")
        print("  ✓ Create directories with mkdir()")
        print("  ✓ Create nested directories with parents=True")
        print("  ✓ Check directory existence with exists() and is_directory()")
        print("  ✓ List directory contents (recursive and non-recursive)")
        print("  ✓ Distinguish between directories and files")
        print("  ✓ Handle existing directories with exist_ok")
        print("  ✓ Remove empty directories with rmdir()")
        print("  ✓ Remove directory trees with recursive=True")
        print("  ✓ Work with deep directory hierarchies")
        print("  ✓ Understand permission requirements for directories")
        print("")
        print(f"Demo files created in {base_path}/")
        print("")
        print("To cleanup:")
        print(f"  nexus rm -r {base_path}")
        print("")
        print("Next steps:")
        print("  - See docs/api/directory-operations.md for full API reference")
        print("  - See docs/api/file-operations.md for file management")
        print("  - See docs/api/permissions.md for permission management")
        print("")

    finally:
        nx.close()
        print("🔌 Disconnected from server")


if __name__ == "__main__":
    main()
