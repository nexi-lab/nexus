#!/usr/bin/env python3
"""
Nexus Python SDK - Advanced Usage Example

Demonstrates advanced Nexus features using a remote server:
- Remote server connection with authentication
- File operations (write, read, list, stat)
- Version history and metadata
- Search operations (grep)
- Workspace snapshots

Prerequisites:
1. Server running: ./scripts/init-nexus-with-auth.sh
2. Load credentials: source .nexus-admin-env
3. Set SERVER_URL: export SERVER_URL=$NEXUS_URL

Usage:
    python examples/python/advanced_usage_demo.py
"""

import json
import os
import sys
from contextlib import suppress
from datetime import datetime

try:
    import nexus
except ImportError:
    print("❌ nexus-ai-fs not installed. Run: pip install nexus-ai-fs")
    sys.exit(1)


def print_section(title: str):
    """Print a formatted section header"""
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}\n")


async def main():
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
    print("║       Nexus Python SDK - Remote Server Demo           ║")
    print("╚════════════════════════════════════════════════════════╝")
    print(f"\n🌐 Server: {server_url}")
    print(f"🔑 Using API Key: {api_key[:20]}...")

    # Connect to remote server
    print_section("1. Connecting to Remote Server")

    try:
        nx = nexus.connect(config={"mode": "remote", "url": server_url, "api_key": api_key})
        print(f"✅ Connected to {server_url}")
    except Exception as e:
        print(f"❌ Failed to connect: {e}")
        sys.exit(1)

    try:
        # Create workspace structure
        print_section("2. Creating Workspace Structure")

        # Create main workspace directory
        try:
            nx.mkdir("/workspace/demo-project", parents=True)
            print("✓ Created: /workspace/demo-project")
        except Exception as e:
            if "already exists" in str(e).lower():
                print("  (already exists: /workspace/demo-project)")
            else:
                raise

        # Grant admin permissions FIRST before any file operations
        print("\n🔐 Setting up permissions...")
        try:
            # Create permission tuples: admin is direct_owner AND direct_viewer
            # API uses tuples: subject=(type, id), object=(type, id)
            # Note: We grant both owner (for write) and viewer (for read) due to schema
            with suppress(Exception):  # May already exist
                nx.rebac_create(
                    subject=("user", "admin"),
                    relation="direct_owner",
                    object=("file", "/workspace/demo-project"),
                )

            with suppress(Exception):  # May already exist
                nx.rebac_create(
                    subject=("user", "admin"),
                    relation="direct_viewer",
                    object=("file", "/workspace/demo-project"),
                )

            print("✓ Granted admin full access to /workspace/demo-project")
        except Exception as e:
            # Check if permission already exists
            try:
                result = nx.rebac_check(
                    subject=("user", "admin"),
                    permission="write",
                    object=("file", "/workspace/demo-project"),
                )
                if result:
                    print("✓ Admin already has access to /workspace/demo-project")
                else:
                    print(f"⚠️  Warning: Failed to grant permissions: {e}")
            except Exception as check_error:
                print(f"⚠️  Warning: Permission setup issue: {check_error}")

        # Create subdirectories
        directories = [
            "/workspace/demo-project/data",
            "/workspace/demo-project/config",
            "/workspace/demo-project/outputs",
        ]

        for dir_path in directories:
            try:
                nx.mkdir(dir_path, parents=True)
                print(f"✓ Created: {dir_path}")
            except Exception as e:
                if "already exists" in str(e).lower():
                    print(f"  (already exists: {dir_path})")
                else:
                    raise

        # Write configuration file
        print_section("3. Writing Configuration Files")

        config_data = {
            "project_name": "Demo Project",
            "version": "1.0.0",
            "created_at": datetime.now().isoformat(),
            "settings": {
                "debug": True,
                "max_files": 1000,
                "allowed_formats": ["json", "txt", "csv"],
            },
        }

        config_path = "/workspace/demo-project/config/app.json"
        nx.sys_write(config_path, json.dumps(config_data, indent=2).encode())
        print(f"✓ Written: {config_path}")

        # Write data files
        data_files = [
            ("/workspace/demo-project/data/sample1.txt", b"Sample data file 1\nLine 2\nLine 3"),
            ("/workspace/demo-project/data/sample2.txt", b"Sample data file 2\nDifferent content"),
            (
                "/workspace/demo-project/data/users.json",
                json.dumps(
                    [
                        {"id": 1, "name": "Alice", "role": "admin"},
                        {"id": 2, "name": "Bob", "role": "user"},
                        {"id": 3, "name": "Charlie", "role": "viewer"},
                    ],
                    indent=2,
                ).encode(),
            ),
        ]

        for file_path, content in data_files:
            nx.sys_write(file_path, content)
            print(f"✓ Written: {file_path}")

        # Read files back
        print_section("4. Reading Files")

        try:
            config_content = nx.sys_read(config_path)
            config = json.loads(config_content)
            print(f"📄 Config: {config['project_name']} v{config['version']}")
            print(f"   Settings: {json.dumps(config['settings'], indent=2)}")
        except Exception as e:
            print(f"⚠️  Read operation encountered an issue: {e}")
            print("   This may be due to server configuration. Continuing with other operations...")

        # List files
        print_section("5. Listing Files")

        all_files = nx.sys_readdir("/workspace/demo-project", recursive=True)
        print(f"📂 Found {len(all_files)} files:")
        for file_path in sorted(all_files):
            print(f"   - {file_path}")

        # File operations
        print_section("6. File Operations")

        # Check if file exists
        if await nx.access(config_path):
            print(f"✓ Config file exists: {config_path}")

        # Check if directory exists
        if await nx.is_directory("/workspace/demo-project/data"):
            print("✓ Data directory exists")

        # Update the config file to create a new version
        print("\n📝 Updating config file...")
        config_data["version"] = "1.0.1"
        config_data["updated_at"] = datetime.now().isoformat()
        nx.sys_write(config_path, json.dumps(config_data, indent=2).encode())
        print("✓ Updated config to version 1.0.1")

        # Version history
        print_section("7. File Metadata")

        try:
            # Get file metadata instead of version history
            # (list_versions is not exposed via RPC server)
            file_info = nx.stat(config_path)
            print(f"📜 File metadata for {config_path}:")
            print(f"   Size: {file_info.get('size', 'N/A')} bytes")
            print(f"   Modified: {file_info.get('modified', 'N/A')}")
            print(f"   Content hash: {file_info.get('content_hash', 'N/A')[:16]}...")
        except Exception as e:
            print(f"⚠️  Metadata retrieval: {e}")

        # Permission and namespace info
        print_section("8. Server Information")

        try:
            namespaces = nx.get_top_level_mounts()
            print(f"📋 Available namespaces: {', '.join(namespaces)}")
        except Exception as e:
            print(f"⚠️  Namespace listing: {e}")

        print("\nNote: For advanced permission management, see:")
        print("  - docs/getting-started/quickstart.md")
        print("  - docs/api/permissions.md")

        # Glob pattern matching
        print_section("9. Pattern Matching")

        try:
            json_files = nx.glob("*.json", path="/workspace/demo-project/data")
            print("🔍 JSON files in /workspace/demo-project/data:")
            for file in json_files:
                print(f"   - {file}")
        except Exception as e:
            print(f"⚠️  Glob pattern matching: {e}")

        # Cleanup demo
        print_section("10. Cleanup (Optional)")

        # Ask user if they want to cleanup
        # For demo purposes, we'll skip actual deletion
        print("Demo files created in /workspace/demo-project/")
        print("To cleanup manually, run:")
        print("  nexus rm -r /workspace/demo-project --remote-url $SERVER_URL")

        print_section("✅ Demo Complete!")

        print("You've learned:")
        print("  ✓ Connect to remote Nexus server with authentication")
        print("  ✓ Create directory structures (mkdir with parents)")
        print("  ✓ Write files (text and JSON)")
        print("  ✓ List files recursively")
        print("  ✓ Check file/directory existence")
        print("  ✓ Version history management")
        print("  ✓ Pattern matching with glob")
        print("\nNext steps:")
        print("  - See docs/api/permissions.md for permission management")
        print("  - See docs/api/advanced-usage.md for advanced patterns")
        print("  - Try the CLI example: ./examples/cli/advanced_usage_demo.sh")

    finally:
        nx.close()
        print("\n🔌 Disconnected from server")


if __name__ == "__main__":
    main()
