#!/usr/bin/env python3
"""FUSE Mount Demo - Python SDK Example

This example demonstrates how to use Nexus FUSE mount functionality
from Python to seamlessly work with files using standard Python tools.

Features demonstrated:
- Mounting Nexus to a local path
- Using Python's built-in file operations on mounted path
- Working with virtual file views (.txt, .md)
- Different mount modes (smart, binary, text)
- Context manager for automatic unmount

Requirements:
    pip install nexus-ai-fs[fuse]

Platform requirements:
    - macOS: Install macFUSE from https://osxfuse.github.io/
    - Linux: sudo apt-get install fuse3
"""

import os
import tempfile
import time
from pathlib import Path

import nexus
from nexus.fuse import MountMode, NexusFUSE


def main() -> None:
    """Run FUSE mount demonstration."""
    print("=" * 70)
    print("Nexus FUSE Mount Demo - Python SDK")
    print("=" * 70)
    print()

    # Create a temporary directory for Nexus data and mount point
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        nexus_data = temp_path / "nexus-data"
        mount_point = temp_path / "mnt"
        mount_point.mkdir()

        # Initialize Nexus filesystem
        print(f"📁 Initializing Nexus at {nexus_data}")
        nx = nexus.connect(config={"data_dir": str(nexus_data)})

        # Create some sample files
        print("📝 Creating sample files...")
        nx.write("/workspace/hello.txt", b"Hello, World!")
        nx.write("/workspace/code.py", b"print('Hello from Python')")
        nx.write(
            "/workspace/data.csv",
            b"Name,Age,City\nAlice,30,NYC\nBob,25,SF\nCharlie,35,LA",
        )
        nx.write(
            "/workspace/document.md",
            b"# Sample Document\n\nThis is a **markdown** document.",
        )

        # Create a binary file (simulated PDF-like content)
        nx.write(
            "/workspace/report.bin",
            b"REPORT: Q4 2024 Results\n\nRevenue increased by 25%.\nCustomer satisfaction: 95%",
        )

        print(f"✅ Created {len(nx.list('/workspace', recursive=False))} files")
        print()

        # Example 1: Basic FUSE Mount
        print("-" * 70)
        print("Example 1: Basic FUSE Mount (Smart Mode)")
        print("-" * 70)

        # Mount with context manager (auto-unmount on exit)
        print(f"🔧 Mounting Nexus to {mount_point} in smart mode...")
        fuse = NexusFUSE(nx, str(mount_point), mode=MountMode.SMART)

        # Mount in background
        fuse.mount(foreground=False)
        print(f"✅ Mounted to {mount_point}")
        print()

        # Give it a moment to stabilize
        time.sleep(0.5)

        # Now use standard Python file operations!
        print("📂 Listing files using os.listdir():")
        files = os.listdir(mount_point / "workspace")
        for file in sorted(files):
            print(f"  - {file}")
        print()

        # Read a file using standard Python
        print("📖 Reading hello.txt using standard open():")
        with open(mount_point / "workspace" / "hello.txt") as f:
            content = f.read()
            print(f"  Content: {content}")
        print()

        # Read Python code
        print("🐍 Reading code.py:")
        with open(mount_point / "workspace" / "code.py") as f:
            code = f.read()
            print(f"  {code}")
        print()

        # Clean up first mount
        fuse.unmount()
        print()

        # Example 2: Custom Cache Configuration
        print("-" * 70)
        print("Example 2: Custom Cache Configuration (Performance Tuning)")
        print("-" * 70)

        # Mount with custom cache settings and metrics enabled
        print("🔧 Mounting with custom cache configuration...")
        cache_config = {
            "attr_cache_size": 2048,  # Double the default
            "attr_cache_ttl": 120,  # Cache attributes for 2 minutes
            "content_cache_size": 200,  # Cache more files
            "parsed_cache_size": 100,  # Cache more parsed content
            "enable_metrics": True,  # Track cache performance
        }

        from nexus.fuse import mount_nexus

        fuse2 = mount_nexus(
            nx,
            str(mount_point),
            mode="smart",
            cache_config=cache_config,
            foreground=False,
        )
        print("✅ Mounted with custom cache config")
        print("  - Attribute cache: 2048 entries, 120s TTL")
        print("  - Content cache: 200 files")
        print("  - Parsed cache: 100 files")
        print("  - Metrics: Enabled")
        print()

        time.sleep(0.5)

        # Do some file operations to populate cache
        print("📊 Performing file operations to populate cache...")
        for _ in range(3):  # Repeat to generate cache hits
            files = os.listdir(mount_point / "workspace")
            with open(mount_point / "workspace" / "hello.txt") as f:
                _ = f.read()
        print("  ✅ Operations complete")
        print()

        # View cache metrics
        print("📈 Cache Performance Metrics:")
        try:
            # Access the cache manager from the FUSE operations
            cache_mgr = fuse2.fuse.operations.cache
            metrics = cache_mgr.get_metrics()

            if metrics:
                print("  Attribute cache:")
                print(f"    - Hits: {metrics['attr_hits']}")
                print(f"    - Misses: {metrics['attr_misses']}")
                print(f"    - Hit rate: {metrics['attr_hit_rate']:.1%}")
                print()
                print("  Content cache:")
                print(f"    - Hits: {metrics['content_hits']}")
                print(f"    - Misses: {metrics['content_misses']}")
                print(f"    - Hit rate: {metrics['content_hit_rate']:.1%}")
                print()
                print("  Cache sizes:")
                print(f"    - Attributes: {metrics['cache_sizes']['attr']} entries")
                print(f"    - Content: {metrics['cache_sizes']['content']} files")
                print(f"    - Parsed: {metrics['cache_sizes']['parsed']} files")
                print()
                print("  💡 Higher hit rates = better performance! Cache is working automatically.")
        except Exception as e:
            print(f"  ⚠️  Could not retrieve metrics: {e}")
        print()

        # Unmount
        fuse2.unmount()
        print("🔧 Unmounted")
        print()

        # Re-mount for remaining examples (without custom config)
        print("🔧 Re-mounting with default settings for remaining examples...")
        fuse = NexusFUSE(nx, str(mount_point), mode=MountMode.SMART)
        fuse.mount(foreground=False)
        time.sleep(0.5)
        print()

        # Example 3: Virtual File Views
        print("-" * 70)
        print("Example 3: Virtual File Views (.txt, .md)")
        print("-" * 70)

        # In smart mode, binary files get virtual .txt and .md views
        print("🔍 Looking for virtual views of report.bin:")
        workspace_files = os.listdir(mount_point / "workspace")
        virtual_views = [f for f in workspace_files if f.startswith("report.bin")]
        for view in sorted(virtual_views):
            print(f"  - {view}")
        print()

        # Read the virtual .txt view
        print("📄 Reading report.bin.txt (virtual parsed view):")
        txt_path = mount_point / "workspace" / "report.bin.txt"
        if txt_path.exists():
            with open(txt_path) as f:
                content = f.read()
                print(f"  {content[:100]}...")
        print()

        # Example 4: Using Python Tools on Mounted Files
        print("-" * 70)
        print("Example 4: Using Python Tools (pathlib, glob, etc.)")
        print("-" * 70)

        # Use pathlib
        print("🔎 Using pathlib to find all .py files:")
        workspace_path = Path(mount_point / "workspace")
        py_files = list(workspace_path.glob("*.py"))
        for py_file in py_files:
            print(f"  - {py_file.name}")
        print()

        # Use glob module
        import glob

        print("🔎 Using glob to find all .txt files (including virtual views):")
        txt_files = glob.glob(str(workspace_path / "*.txt"))
        for txt_file in sorted(txt_files):
            print(f"  - {Path(txt_file).name}")
        print()

        # Example 5: Writing Files
        print("-" * 70)
        print("Example 5: Writing Files via Mount")
        print("-" * 70)

        new_file = mount_point / "workspace" / "new_file.txt"
        print(f"✍️  Writing to {new_file.name}...")
        with open(new_file, "w") as f:
            f.write("This file was created via FUSE mount!")

        # Read it back
        with open(new_file) as f:
            content = f.read()
            print(f"  ✅ Wrote and read: {content}")
        print()

        # Verify it exists in Nexus
        if nx.exists("/workspace/new_file.txt"):
            print("  ✅ File also exists in Nexus filesystem!")
        print()

        # Example 6: Using Standard Library Tools
        print("-" * 70)
        print("Example 6: Using Standard Library (csv, json, etc.)")
        print("-" * 70)

        # Read CSV using Python's csv module
        import csv

        print("📊 Reading data.csv using Python's csv module:")
        csv_path = mount_point / "workspace" / "data.csv"
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                print(f"  {row['Name']}: {row['Age']} years old, lives in {row['City']}")
        print()

        # Example 7: Accessing Raw Binary via .raw/
        print("-" * 70)
        print("Example 7: Accessing Original Binary via .raw/")
        print("-" * 70)

        print("🗄️  Accessing raw binary file:")
        raw_path = mount_point / ".raw" / "workspace" / "report.bin"
        with open(raw_path, "rb") as f:
            raw_content = f.read()
            print(f"  Raw bytes (first 50): {raw_content[:50]}")
        print()

        # Example 8: Directory Operations
        print("-" * 70)
        print("Example 8: Directory Operations")
        print("-" * 70)

        # Create a new directory
        new_dir = mount_point / "workspace" / "subdir"
        print(f"📁 Creating directory: {new_dir.name}")
        new_dir.mkdir()
        print("  ✅ Directory created")

        # Write a file in the new directory
        subfile = new_dir / "nested.txt"
        with open(subfile, "w") as f:
            f.write("File in nested directory")
        print(f"  ✅ Created {subfile.name} in subdirectory")
        print()

        # Clean up: unmount
        print("-" * 70)
        print("🔧 Unmounting...")
        fuse.unmount()
        print("✅ Unmounted successfully")
        print()

        # Close Nexus
        nx.close()

    print("=" * 70)
    print("Demo Complete!")
    print("=" * 70)
    print()
    print("Key Takeaways:")
    print("  1. ✅ FUSE mount makes Nexus look like a regular directory")
    print("  2. ✅ Customize cache config for better performance (optional)")
    print("  3. ✅ Track cache metrics to measure performance")
    print("  4. ✅ Use standard Python file operations (open, read, write)")
    print("  5. ✅ Virtual .txt/.md views for binary files in smart mode")
    print("  6. ✅ Access raw files via .raw/ directory")
    print("  7. ✅ Works with all Python stdlib tools (csv, json, pathlib, etc.)")
    print()


if __name__ == "__main__":
    main()
