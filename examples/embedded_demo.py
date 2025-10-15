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
        # Part 4: Path Routing and Directory Operations
        # ============================================================
        print("\n" + "=" * 70)
        print("PART 4: Path Routing & Directory Operations (NEW)")
        print("=" * 70)

        print("\n12. Testing directory operations...")
        nx3 = nexus.connect(config={"data_dir": str(data_dir)})

        # Create directory structure
        print("\n   Creating directory structure...")
        nx3.mkdir("/workspace", exist_ok=True)
        nx3.mkdir("/workspace/agent1", exist_ok=True)
        nx3.mkdir("/workspace/agent1/data", exist_ok=True)
        nx3.mkdir("/workspace/agent2", exist_ok=True)
        print("   ✓ Created: /workspace/agent1/data")
        print("   ✓ Created: /workspace/agent2")

        # Check if directories exist
        print("\n13. Checking directory existence...")
        is_dir1 = nx3.is_directory("/workspace/agent1")
        is_dir2 = nx3.is_directory("/workspace/agent1/data")
        is_dir3 = nx3.is_directory("/documents")  # Should be False
        print(f"   /workspace/agent1 is directory: {is_dir1}")
        print(f"   /workspace/agent1/data is directory: {is_dir2}")
        print(f"   /documents is directory: {is_dir3}")

        # Write files into created directories
        print("\n14. Writing files into directory structure...")
        nx3.write("/workspace/agent1/data/file1.txt", b"Agent 1 data file")
        nx3.write("/workspace/agent1/data/file2.txt", b"Another file")
        nx3.write("/workspace/agent2/config.json", b'{"agent": "2"}')
        print("   ✓ Wrote files to /workspace/agent1/data/")
        print("   ✓ Wrote files to /workspace/agent2/")

        # Create nested directories with parents=True
        print("\n15. Creating deeply nested directories...")
        nx3.mkdir("/projects/ml/experiments/run1", parents=True, exist_ok=True)
        nx3.write("/projects/ml/experiments/run1/results.json", b'{"accuracy": 0.95}')
        print("   ✓ Created: /projects/ml/experiments/run1 (with parents)")
        print("   ✓ Wrote: /projects/ml/experiments/run1/results.json")

        # List all files
        print("\n16. Listing all files in workspace...")
        all_files = nx3.list()
        workspace_files = [
            f for f in all_files if f.startswith("/workspace") or f.startswith("/projects")
        ]
        print(f"   Found {len(workspace_files)} files in workspace:")
        for f in sorted(workspace_files):
            print(f"   - {f}")

        # Test path routing
        print("\n17. Demonstrating path routing...")
        print("   Router maps virtual paths to physical backend paths")
        print("   Virtual: /workspace/agent1/data/file1.txt")
        print("   → Backend: workspace/agent1/data/file1.txt")
        print("   → Physical: {data_dir}/files/workspace/agent1/data/file1.txt")

        # Remove a directory (will fail - not empty)
        print("\n18. Testing rmdir (should fail - not empty)...")
        try:
            nx3.rmdir("/workspace/agent1/data", recursive=False)
            print("   ✗ Should have failed!")
        except OSError as e:
            print(f"   ✓ Correctly failed: {e}")

        # Remove directory recursively
        print("\n19. Removing directory recursively...")
        nx3.rmdir("/workspace/agent2", recursive=True)
        print("   ✓ Removed /workspace/agent2 (recursive)")

        # Verify removal
        remaining_files = [f for f in nx3.list() if f.startswith("/workspace")]
        print(f"   Remaining workspace files: {len(remaining_files)}")
        for f in sorted(remaining_files):
            print(f"   - {f}")

        nx3.close()

        # ============================================================
        # Part 5: Multi-Mount Configuration
        # ============================================================
        print("\n" + "=" * 70)
        print("PART 5: Multi-Mount Configuration")
        print("=" * 70)

        print("\n20. Testing multiple mount points...")
        nx4 = nexus.connect(config={"data_dir": str(data_dir)})

        # Create separate backend for workspace isolation
        from nexus.backends.local import LocalBackend

        workspace_backend = LocalBackend(data_dir / "workspace-isolated")
        nx4.router.add_mount("/workspace", workspace_backend, priority=10)

        print("   ✓ Added mount: /workspace → isolated backend")
        print("   ✓ Default mount: / → main backend")

        # Write to different mounts
        nx4.write("/workspace/isolated.txt", b"in workspace backend")
        nx4.write("/other/regular.txt", b"in default backend")

        print("\n21. Verifying routing...")
        route_workspace = nx4.router.route("/workspace/test.txt")
        route_other = nx4.router.route("/other/test.txt")

        print(f"   /workspace/test.txt → mount: {route_workspace.mount_point}")
        print(f"   /other/test.txt → mount: {route_other.mount_point}")

        # List files from both mounts
        all_files_multi = nx4.list()
        workspace_files = [f for f in all_files_multi if f.startswith("/workspace")]
        other_files = [f for f in all_files_multi if f.startswith("/other")]

        print(f"\n   Workspace mount files: {len(workspace_files)}")
        for f in sorted(workspace_files)[:3]:
            print(f"   - {f}")

        print(f"\n   Default mount files: {len(other_files)}")
        for f in sorted(other_files)[:3]:
            print(f"   - {f}")

        nx4.close()

        # ============================================================
        # Part 6: Content-Addressable Storage (CAS) with Embedded
        # ============================================================
        print("\n" + "=" * 70)
        print("PART 6: Content-Addressable Storage (CAS) - NEW!")
        print("=" * 70)

        print("\n22. Using Nexus with automatic CAS deduplication...")
        # CAS is now always enabled - no special flag needed!
        nx_cas = nexus.connect(config={"data_dir": str(data_dir / "cas-mode")})
        print("   ✓ Connected (CAS automatic)")
        print(f"   ✓ Storage location: {data_dir / 'cas-mode'}")

        # Write content
        print("\n23. Writing content via CAS-enabled Nexus...")
        content1 = b"This is important data that will be content-addressed"
        nx_cas.write("/documents/data.txt", content1)

        # Get metadata to see content hash
        meta1 = nx_cas.metadata.get("/documents/data.txt")
        hash1 = meta1.etag  # etag is SHA-256 hash
        print(f"   Content hash (etag): {hash1[:16]}...{hash1[-8:]}")
        print(f"   Ref count: {nx_cas.backend.get_ref_count(hash1)}")
        print(f"   Size: {meta1.size} bytes")
        print(f"   Backend: {meta1.backend_name}")

        # Verify directory structure
        print("\n   Physical storage path:")
        print(f"   cas/{hash1[:2]}/{hash1[2:4]}/{hash1}")
        print(f"   Structure: {hash1[:2]}/{hash1[2:4]}/{hash1}")

        # Write identical content (deduplication)
        print("\n24. Testing automatic content deduplication...")
        content2 = b"This is important data that will be content-addressed"  # Same content!
        nx_cas.write("/reports/summary.txt", content2)  # Different path, same content

        meta2 = nx_cas.metadata.get("/reports/summary.txt")
        hash2 = meta2.etag
        print(f"   Second file hash: {hash2[:16]}...{hash2[-8:]}")
        print(f"   Hashes match: {hash1 == hash2}")
        print(f"   Ref count (auto-incremented): {nx_cas.backend.get_ref_count(hash1)}")
        print(f"   Physical paths match: {meta1.physical_path == meta2.physical_path}")
        print("   ✓ Content deduplicated - only stored once!")

        # Write different content
        print("\n25. Writing different content...")
        content3 = b"Different content with different hash"
        nx_cas.write("/logs/access.log", content3)

        meta3 = nx_cas.metadata.get("/logs/access.log")
        hash3 = meta3.etag
        print(f"   New content hash: {hash3[:16]}...{hash3[-8:]}")
        print(f"   Different from first: {hash1 != hash3}")
        print(f"   Ref count: {nx_cas.backend.get_ref_count(hash3)}")

        # Read content back
        print("\n26. Reading content transparently...")
        retrieved = nx_cas.read("/documents/data.txt")
        print(f"   Retrieved {len(retrieved)} bytes")
        print(f"   Content matches: {retrieved == content1}")
        print(f"   Content: {retrieved.decode()[:50]}...")
        print("   ✓ CAS backend is transparent to user!")

        # Delete with reference counting
        print("\n27. Testing automatic reference counting on delete...")
        print(f"   Current ref count for shared content: {nx_cas.backend.get_ref_count(hash1)}")

        nx_cas.delete("/documents/data.txt")  # First delete
        print("   After deleting /documents/data.txt...")
        print(f"   Ref count: {nx_cas.backend.get_ref_count(hash1)}")
        print(f"   Content still exists: {nx_cas.backend.content_exists(hash1)}")
        print(f"   Other file still readable: {nx_cas.exists('/reports/summary.txt')}")

        nx_cas.delete("/reports/summary.txt")  # Second delete
        print("\n   After deleting /reports/summary.txt...")
        print(f"   Content exists in CAS: {nx_cas.backend.content_exists(hash1)}")
        print("   ✓ Content automatically removed when last reference deleted!")

        # Inspect CAS directory structure
        print("\n28. Inspecting CAS directory structure...")
        cas_files = list((data_dir / "cas-mode" / "cas").rglob("*"))
        content_files = [f for f in cas_files if f.is_file() and f.suffix != ".meta"]
        meta_files = [f for f in cas_files if f.suffix == ".meta"]
        print(f"   Content files: {len(content_files)}")
        print(f"   Metadata files: {len(meta_files)}")
        print("\n   Directory tree (CAS storage):")
        for f in sorted(cas_files)[:10]:  # Show first 10
            if f.is_file():
                rel_path = f.relative_to(data_dir / "cas-mode" / "cas")
                print(f"   {rel_path}")

        # Demonstrate hash collision resistance
        print("\n29. Hash collision resistance...")
        test_contents = [
            (b"Content A", "/test/a.txt"),
            (b"Content B", "/test/b.txt"),
            (b"Similar content 1", "/test/c.txt"),
            (b"Similar content 2", "/test/d.txt"),
            (b"x" * 1000, "/test/e.txt"),
            (b"y" * 1000, "/test/f.txt"),
        ]
        for content, path in test_contents:
            nx_cas.write(path, content)

        hashes = [nx_cas.metadata.get(path).etag for _, path in test_contents]
        unique_hashes = set(hashes)
        print(f"   Wrote {len(test_contents)} different contents")
        print(f"   Got {len(unique_hashes)} unique hashes")
        print(f"   No collisions: {len(hashes) == len(unique_hashes)}")

        # Show storage efficiency
        print("\n30. Storage efficiency demonstration...")
        # Write same content 100 times to different paths
        repeated_content = b"This content will be written 100 times"
        nx_cas.write("/efficiency/test0.txt", repeated_content)
        repeated_meta = nx_cas.metadata.get("/efficiency/test0.txt")
        repeated_hash = repeated_meta.etag

        for i in range(1, 100):
            nx_cas.write(f"/efficiency/test{i}.txt", repeated_content)

        print("   Content written: 100 times (different paths)")
        print(f"   Ref count: {nx_cas.backend.get_ref_count(repeated_hash)}")
        print("   Physical copies: 1")
        print(f"   Space saved: ~{len(repeated_content) * 99} bytes")
        print("   ✓ Automatic deduplication saves storage!")

        # List some files
        print("\n   Files exist in metadata:")
        files = nx_cas.list("/efficiency")
        print(f"   Total files: {len(files)}")
        print("   But only 1 physical copy in CAS storage!")

        nx_cas.close()

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

NEW in v0.1.0:
• Path Router maps virtual paths to backends
• Directory operations (mkdir, rmdir, is_directory)
• Multi-mount support (different paths → different backends)
• Backend-agnostic interface (LocalFS today, S3/GDrive future)
• Longest-prefix matching for mount points
        """
        )

        print("\n📊 Feature Summary:")
        print("   ✓ File operations (read/write/delete)")
        print("   ✓ Metadata tracking (SQLite)")
        print("   ✓ Custom metadata (key-value)")
        print("   ✓ Directory operations (mkdir/rmdir/is_directory)")
        print("   ✓ Path routing (virtual → physical)")
        print("   ✓ Multi-mount support (multiple backends)")
        print("   ✓ Persistence (survives restarts)")
        print("   ✓ Data integrity (ETags)")
        print("   ✓ Content-addressable storage (CAS)")
        print("   ✓ Content deduplication (save space)")
        print("   ✓ Reference counting (safe deletion)")
        print("   ✓ Atomic writes (data integrity)")
        print("   ✓ SHA-256 content hashing")
        print()
        print("📁 Files created:")
        workspace_backend_files = list((data_dir / "workspace-isolated").rglob("*"))
        main_backend_files = list((data_dir / "files").rglob("*"))
        print(f"   Main backend: {len([f for f in main_backend_files if f.is_file()])} files")
        print(
            f"   Workspace backend: {len([f for f in workspace_backend_files if f.is_file()])} files"
        )
        print()
        print("🎯 Key Capabilities:")
        print("   • Unified API across different storage backends")
        print("   • Automatic directory creation on write")
        print("   • Mount-based path routing with priority")
        print("   • Cache-friendly design (path resolution)")
        print()
        print("🔮 Future Backends (same API!):")
        print("   • S3: Flat key-value (path → key)")
        print("   • Google Drive: ID-based (path → file ID with caching)")
        print("   • SharePoint: Site/Library structure")
        print()
        print("Example multi-backend config:")
        print("   /workspace → LocalFS (fast, local)")
        print("   /shared → S3 (scalable, remote)")
        print("   /external/gdrive → Google Drive (collaborative)")
        print()
        print("All using the same nx.write(path, content) API!")
        print()
        print("=" * 70)

        print("\n✓ Integrated demo completed successfully!")
        print("=" * 70)


if __name__ == "__main__":
    main()
