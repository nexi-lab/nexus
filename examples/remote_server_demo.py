#!/usr/bin/env python3
"""Remote Nexus Server Demo

This example demonstrates how to:
1. Start a Nexus RPC server
2. Connect to it from a remote client
3. Use the remote client exactly like a local filesystem
4. Mount the remote filesystem with FUSE

The server exposes all NexusFileSystem operations over HTTP using JSON-RPC.
The client implements the same NexusFilesystem interface, making remote
access transparent.

Requirements:
    pip install nexus-ai-fs
    pip install nexus-ai-fs[fuse]  # For FUSE mount demo

Usage:
    # Terminal 1: Start the server
    python examples/remote_server_demo.py server

    # Terminal 2: Run the client
    python examples/remote_server_demo.py client

    # Or use the CLI
    nexus serve --api-key mysecret
"""

import sys
import time
from pathlib import Path

import nexus
from nexus.remote import RemoteNexusFS


def run_server() -> None:
    """Start the Nexus RPC server."""
    print("=" * 70)
    print("Starting Nexus RPC Server")
    print("=" * 70)
    print()

    # Create local Nexus instance
    print("Initializing local Nexus filesystem...")
    nx = nexus.connect(config={"data_dir": "./.nexus-server-data"})

    # Add some initial data
    print("Creating initial data...")
    nx.mkdir("/workspace", exist_ok=True)
    nx.write("/workspace/welcome.txt", b"Welcome to Nexus RPC Server!")
    nx.write("/workspace/info.md", b"# Nexus Server\n\nThis is a remote Nexus filesystem.")
    print(f"Created {len(nx.list('/workspace'))} files")
    print()

    # Start RPC server
    print("Starting RPC server...")
    print("  Host: 0.0.0.0")
    print("  Port: 8080")
    print("  API Key: mysecret")
    print()
    print("Server endpoints:")
    print("  Health: http://localhost:8080/health")
    print("  RPC: http://localhost:8080/api/nfs/{method}")
    print()
    print("Press Ctrl+C to stop")
    print()

    from nexus.server import NexusRPCServer

    server = NexusRPCServer(
        nexus_fs=nx,
        host="0.0.0.0",
        port=8080,
        api_key="mysecret",
    )

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
        server.shutdown()
        print("Server stopped")


def run_client() -> None:
    """Connect to remote Nexus server and demonstrate usage."""
    print("=" * 70)
    print("Nexus Remote Client Demo")
    print("=" * 70)
    print()

    # Connect to remote server
    print("Connecting to remote Nexus server...")
    print("  URL: http://localhost:8080")
    print("  API Key: mysecret")

    nx = RemoteNexusFS(
        server_url="http://localhost:8080",
        api_key="mysecret",
    )

    print("✓ Connected!")
    print()

    # Example 1: List files
    print("-" * 70)
    print("Example 1: List Files")
    print("-" * 70)
    files = nx.list("/workspace")
    print(f"Files in /workspace: {len(files)}")
    for file in files:
        print(f"  - {file}")
    print()

    # Example 2: Read a file
    print("-" * 70)
    print("Example 2: Read File")
    print("-" * 70)
    content = nx.read("/workspace/welcome.txt")
    print("Content of welcome.txt:")
    print(f"  {content.decode('utf-8')}")
    print()

    # Example 3: Write a file
    print("-" * 70)
    print("Example 3: Write File")
    print("-" * 70)
    print("Writing /workspace/remote_file.txt...")
    nx.write("/workspace/remote_file.txt", b"This file was created by a remote client!")
    print("✓ File written")
    print()

    # Verify it exists
    print("Verifying file exists...")
    if nx.exists("/workspace/remote_file.txt"):
        print("✓ File exists on server")
        content = nx.read("/workspace/remote_file.txt")
        print(f"  Content: {content.decode('utf-8')}")
    print()

    # Example 4: Glob pattern matching
    print("-" * 70)
    print("Example 4: Glob Pattern Matching")
    print("-" * 70)
    print("Finding all .txt files...")
    txt_files = nx.glob("*.txt", "/workspace")
    print(f"Found {len(txt_files)} .txt files:")
    for file in txt_files:
        print(f"  - {file}")
    print()

    # Example 5: Directory operations
    print("-" * 70)
    print("Example 5: Directory Operations")
    print("-" * 70)
    print("Creating /workspace/subdir...")
    nx.mkdir("/workspace/subdir", exist_ok=True)
    print("✓ Directory created")

    print("Writing file in subdirectory...")
    nx.write("/workspace/subdir/nested.txt", b"File in nested directory")
    print("✓ File written")
    print()

    # Example 6: Check if path is directory
    print("-" * 70)
    print("Example 6: Check Directory")
    print("-" * 70)
    is_dir = nx.is_directory("/workspace")
    print(f"/workspace is directory: {is_dir}")
    is_dir = nx.is_directory("/workspace/welcome.txt")
    print(f"/workspace/welcome.txt is directory: {is_dir}")
    print()

    # Example 7: Search file contents
    print("-" * 70)
    print("Example 7: Search File Contents (grep)")
    print("-" * 70)
    print("Searching for 'remote' in all files...")
    results = nx.grep("remote", "/workspace", ignore_case=True)
    print(f"Found {len(results)} matches:")
    for match in results[:5]:  # Show first 5
        print(f"  {match['file']}:{match['line']} - {match['content'][:50]}")
    print()

    # Close connection
    nx.close()
    print("=" * 70)
    print("Demo Complete!")
    print("=" * 70)
    print()
    print("Key Takeaways:")
    print("  1. ✓ RemoteNexusFS implements the same NexusFilesystem interface")
    print("  2. ✓ All operations work exactly like local filesystem")
    print("  3. ✓ Can be used with FUSE mount for seamless access")
    print("  4. ✓ Simple API key authentication")
    print()


def run_fuse_demo() -> None:
    """Demonstrate mounting remote Nexus with FUSE."""
    print("=" * 70)
    print("Nexus Remote FUSE Mount Demo")
    print("=" * 70)
    print()

    try:
        from nexus.fuse import mount_nexus
    except ImportError:
        print("Error: FUSE support not available")
        print("Install with: pip install nexus-ai-fs[fuse]")
        return

    # Connect to remote server
    print("Connecting to remote Nexus server...")
    nx = RemoteNexusFS(
        server_url="http://localhost:8080",
        api_key="mysecret",
    )
    print("✓ Connected!")
    print()

    # Create mount point
    mount_point = Path("./mnt-remote")
    mount_point.mkdir(exist_ok=True)

    print(f"Mounting remote Nexus to {mount_point}...")
    print("Press Ctrl+C to unmount")
    print()

    # Mount filesystem
    fuse = mount_nexus(
        nx,
        str(mount_point),
        foreground=True,  # Run in foreground so we can see output
    )

    try:
        fuse.wait()
    except KeyboardInterrupt:
        print("\nUnmounting...")
        fuse.unmount()
        print("✓ Unmounted")

    nx.close()


def main() -> None:
    """Main entry point."""
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python examples/remote_server_demo.py server  # Start server")
        print("  python examples/remote_server_demo.py client  # Run client demo")
        print("  python examples/remote_server_demo.py fuse    # Mount with FUSE")
        sys.exit(1)

    mode = sys.argv[1].lower()

    if mode == "server":
        run_server()
    elif mode == "client":
        # Wait a moment for server to be ready
        time.sleep(1)
        run_client()
    elif mode == "fuse":
        run_fuse_demo()
    else:
        print(f"Unknown mode: {mode}")
        print("Valid modes: server, client, fuse")
        sys.exit(1)


if __name__ == "__main__":
    main()
