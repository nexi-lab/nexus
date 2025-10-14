"""Demo of Nexus Embedded mode Phase 2 implementation."""

import tempfile
from pathlib import Path

import nexus

# Create embedded filesystem
with tempfile.TemporaryDirectory() as tmpdir:
    print("=== Nexus Embedded Mode Demo ===\n")

    # Initialize using nexus.connect() - auto-detects mode from config
    data_dir = Path(tmpdir) / "nexus-data"
    nx = nexus.connect(config={"data_dir": str(data_dir)})
    print(f"Initialized Nexus at: {data_dir}")

    # Write files
    print("\n1. Writing files...")
    nx.write("/documents/readme.txt", b"Hello, Nexus!")
    nx.write("/data/config.json", b'{"setting": "enabled"}')
    nx.write("/logs/app.log", b"Application started")
    print("   ✓ Created 3 files")

    # Read a file
    print("\n2. Reading a file...")
    content = nx.read("/documents/readme.txt")
    print(f"   Content: {content.decode()}")

    # List all files
    print("\n3. Listing all files...")
    files = nx.list()
    for file in files:
        print(f"   - {file}")

    # List with prefix
    print("\n4. Listing files in /data...")
    data_files = nx.list(prefix="/data")
    for file in data_files:
        print(f"   - {file}")

    # Check existence
    print("\n5. Checking file existence...")
    print(f"   /documents/readme.txt exists: {nx.exists('/documents/readme.txt')}")
    print(f"   /missing.txt exists: {nx.exists('/missing.txt')}")

    # Update a file
    print("\n6. Updating a file...")
    nx.write("/documents/readme.txt", b"Updated content!")
    updated_content = nx.read("/documents/readme.txt")
    print(f"   New content: {updated_content.decode()}")

    # Delete a file
    print("\n7. Deleting a file...")
    nx.delete("/logs/app.log")
    print(f"   /logs/app.log exists: {nx.exists('/logs/app.log')}")

    # Show remaining files
    print("\n8. Remaining files:")
    remaining = nx.list()
    for file in remaining:
        print(f"   - {file}")

    # Close
    nx.close()
    print("\n✓ Demo completed successfully!")
