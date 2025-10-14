"""Integrated demo showing how Embedded mode uses the Metadata Store.

This demo demonstrates the connection between:
1. High-level nexus.connect() API (user-facing)
2. Low-level SQLAlchemy Metadata Store (internal)

It shows how file operations through nexus.connect()
translate to metadata store operations.
"""

import tempfile
from pathlib import Path

import nexus
from nexus.storage.metadata_store import SQLAlchemyMetadataStore


def main() -> None:
    """Run the integrated demo."""
    print("=" * 70)
    print("Nexus Integrated Demo: Embedded Mode + Metadata Store")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir) / "nexus-data"
        db_path = data_dir / "metadata.db"

        print(f"\n📁 Data directory: {data_dir}")
        print(f"💾 Database: {db_path}")

        # ============================================================
        # Part 1: Using High-Level User API
        # ============================================================
        print("\n" + "=" * 70)
        print("PART 1: High-Level User API (nexus.connect)")
        print("=" * 70)

        # Initialize using nexus.connect() - the recommended way
        print("\n1. Connecting to Nexus...")
        nx = nexus.connect(config={"data_dir": str(data_dir)})
        print("   ✓ Connected via nexus.connect()")
        print("   ✓ Mode auto-detected: embedded")
        print(f"   ✓ Using metadata store at: {db_path}")

        # Write files using high-level API
        print("\n2. Writing files via nexus API...")
        nx.write("/documents/report.pdf", b"PDF content here...")
        nx.write("/images/photo.jpg", b"JPEG data here...")
        nx.write("/data/config.json", b'{"setting": "enabled"}')
        print("   ✓ Wrote 3 files")

        # Read a file
        print("\n3. Reading file...")
        content = nx.read("/documents/report.pdf")
        print(f"   Content: {content.decode()}")

        # List files
        print("\n4. Listing files...")
        files = nx.list()
        print(f"   Found {len(files)} files:")
        for f in files:
            print(f"   - {f}")

        # Close connection
        nx.close()
        print("\n   ✓ Connection closed")

        # ============================================================
        # Part 2: Inspecting Low-Level Metadata Store
        # ============================================================
        print("\n" + "=" * 70)
        print("PART 2: Low-Level Metadata Store (Internal View)")
        print("=" * 70)

        # Open the same database directly
        print("\n5. Opening metadata store directly...")
        store = SQLAlchemyMetadataStore(db_path)
        print("   ✓ Connected to same database")

        # Inspect stored metadata
        print("\n6. Inspecting file metadata...")
        all_files = store.list()
        print(f"   Total files in database: {len(all_files)}")

        for file_meta in all_files:
            print(f"\n   📄 {file_meta.path}")
            print(f"      Backend: {file_meta.backend_name}")
            print(f"      Physical path: {file_meta.physical_path}")
            print(f"      Size: {file_meta.size} bytes")
            print(f"      ETag: {file_meta.etag}")
            print(f"      Version: {file_meta.version}")
            print(f"      Created: {file_meta.created_at}")
            print(f"      Modified: {file_meta.modified_at}")

        # Add custom metadata (not available in high-level API yet)
        print("\n7. Adding custom metadata (low-level feature)...")
        store.set_file_metadata("/documents/report.pdf", "author", "John Doe")
        store.set_file_metadata("/documents/report.pdf", "department", "Engineering")
        store.set_file_metadata("/documents/report.pdf", "confidential", True)
        print("   ✓ Added custom metadata")

        # Retrieve custom metadata
        author = store.get_file_metadata("/documents/report.pdf", "author")
        department = store.get_file_metadata("/documents/report.pdf", "department")
        confidential = store.get_file_metadata("/documents/report.pdf", "confidential")
        print(f"   Author: {author}")
        print(f"   Department: {department}")
        print(f"   Confidential: {confidential}")

        store.close()

        # ============================================================
        # Part 3: Re-open and Verify Persistence
        # ============================================================
        print("\n" + "=" * 70)
        print("PART 3: Persistence Test")
        print("=" * 70)

        print("\n8. Re-connecting to Nexus...")
        nx2 = nexus.connect(config={"data_dir": str(data_dir)})
        files2 = nx2.list()
        print(f"   ✓ Files still present: {len(files2)}")
        for f in files2:
            print(f"   - {f}")

        # Verify content is still readable
        print("\n9. Verifying file content...")
        content2 = nx2.read("/data/config.json")
        print(f"   Content: {content2.decode()}")
        print("   ✓ Data persisted correctly!")

        # Delete a file
        print("\n10. Deleting a file...")
        nx2.delete("/images/photo.jpg")
        remaining = nx2.list()
        print(f"   ✓ Remaining files: {len(remaining)}")
        for f in remaining:
            print(f"   - {f}")

        nx2.close()

        # Verify deletion in metadata store
        print("\n11. Verifying deletion in metadata store...")
        store2 = SQLAlchemyMetadataStore(db_path)
        final_files = store2.list()
        print(f"   Files in metadata store: {len(final_files)}")
        for file_meta in final_files:
            print(f"   - {file_meta.path}")

        # Check custom metadata still exists
        author2 = store2.get_file_metadata("/documents/report.pdf", "author")
        print(f"\n   Custom metadata preserved: author={author2}")

        store2.close()

        # ============================================================
        # Summary
        # ============================================================
        print("\n" + "=" * 70)
        print("SUMMARY: How It Works")
        print("=" * 70)
        print(
            """
┌─────────────────────────────────────────────────────────┐
│                   USER APPLICATION                      │
│                 (your Python code)                      │
└────────────────────┬────────────────────────────────────┘
                     │
                     │ import nexus
                     │ nx = nexus.connect()  ← THE RIGHT WAY
                     ▼
┌─────────────────────────────────────────────────────────┐
│              nexus.connect()                            │
│              (auto-detects mode)                        │
└────────────────────┬────────────────────────────────────┘
                     │ Returns Embedded instance
                     ▼
┌─────────────────────────────────────────────────────────┐
│              Embedded Filesystem Class                  │
│              (nexus.core.embedded)                      │
├─────────────────────────────────────────────────────────┤
│  • Path validation                                      │
│  • ETag computation                                     │
│  • Automatic metadata tracking                          │
└─────┬──────────────────────────────────┬────────────────┘
      │                                  │
      │ Store metadata                   │ Read/write data
      ▼                                  ▼
┌──────────────────────┐      ┌──────────────────────────┐
│ SQLAlchemy Metadata  │      │   Storage Backend        │
│      Store           │      │   (LocalBackend)         │
├──────────────────────┤      ├──────────────────────────┤
│ • FilePathModel      │      │ • Physical file I/O      │
│ • FileMetadataModel  │      │ • Local filesystem       │
│ • ContentChunkModel  │      │   operations             │
└──────┬───────────────┘      └──────────────────────────┘
       │
       ▼
┌──────────────────────┐
│   SQLite Database    │
│   (metadata.db)      │
└──────────────────────┘

Key Points:
• Use nexus.connect() - it auto-detects mode
• Embedded API provides simple file operations
• Metadata Store tracks all file information
• Custom metadata can be added at low level
• Both views access the same SQLite database
• Changes are immediately persisted
        """
        )

        print("\n✓ Integrated demo completed successfully!")
        print("=" * 70)


if __name__ == "__main__":
    main()
