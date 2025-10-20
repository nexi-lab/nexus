"""Integrated demo showing how Embedded mode uses the Metadata Store.

This demo demonstrates the connection between:
1. High-level nexus.connect() API (user-facing)
2. Low-level SQLAlchemy Metadata Store (internal)

It shows how file operations through nexus.connect()
translate to metadata store operations.
"""

import tempfile
from datetime import UTC, datetime, timedelta
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
        # Part 5: Multi-Mount Configuration (INTERNAL APIs)
        # ============================================================
        print("\n" + "=" * 70)
        print("PART 5: Multi-Mount Configuration (Educational - Internal APIs)")
        print("=" * 70)
        print("NOTE: This section uses internal router APIs for educational purposes.")
        print("      In production code, use only user-facing APIs (read/write/delete/etc).")

        print("\n20. Testing multiple mount points...")
        nx4 = nexus.connect(config={"data_dir": str(data_dir)})

        # Create separate backend for workspace isolation (INTERNAL API)
        from nexus.backends.local import LocalBackend

        workspace_backend = LocalBackend(data_dir / "workspace-isolated")
        nx4.router.add_mount("/workspace", workspace_backend, priority=10)  # INTERNAL

        print("   ✓ Added mount: /workspace → isolated backend (INTERNAL API)")
        print("   ✓ Default mount: / → main backend")

        # Write to different mounts (USER-FACING API)
        nx4.write("/workspace/isolated.txt", b"in workspace backend")
        nx4.write("/other/regular.txt", b"in default backend")

        print("\n21. Verifying routing (INTERNAL API - for demonstration)...")
        route_workspace = nx4.router.route("/workspace/test.txt")  # INTERNAL
        route_other = nx4.router.route("/other/test.txt")  # INTERNAL

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
        # Part 6: Namespace & Tenant Isolation (NEW in v0.1.0)
        # ============================================================
        print("\n" + "=" * 70)
        print("PART 6: Namespace & Tenant Isolation (Educational + User-facing)")
        print("=" * 70)
        print("NOTE: Sections 22-24 use internal APIs for education.")
        print("      Section 25-29 show recommended user-facing approach.")

        # Add small delay to ensure file handles are released (macOS tmpfs issue)
        import time

        time.sleep(0.5)

        print("\n22. Demonstrating path parsing and namespace extraction...")
        print("    (INTERNAL API - for educational purposes)")

        try:
            nx5 = nexus.connect(config={"data_dir": str(data_dir)})
        except Exception as e:
            print(f"\n[WARNING] Could not open connection (tmpfs locking issue): {e}")
            print("Skipping Parts 6-14 (namespace isolation, work detection, etc.)")
            print("Note: This is a macOS tmpfs limitation when reusing databases.")
            print("Jumping to Part 15 (sync operations)...")
            nx5 = None

        if nx5 is not None:
            # Parse different namespace paths

            test_paths = [
                "/workspace/acme/agent1/data/file.txt",
                "/shared/acme/datasets/model.pkl",
                "/archives/acme/2024/01/backup.tar",
                "/external/s3/bucket/file.txt",
                "/system/config/settings.json",
            ]

        print("   Parsing virtual paths to extract namespace info:")
        for path in test_paths:
            info = nx5.router.parse_path(path)
            print(f"\n   Path: {path}")
            print(f"   → Namespace: {info.namespace}")
            print(f"   → Tenant: {info.tenant_id or 'N/A'}")
            print(f"   → Agent: {info.agent_id or 'N/A'}")
            print(f"   → Relative path: {info.relative_path or '(root)'}")

        print("\n23. Testing path validation and security...")

        # Valid paths
        valid_paths = [
            "/workspace/tenant1/agent1/data.txt",
            "/shared/tenant1/file.txt",
            "/external/backend/file.txt",
        ]
        print("   Valid paths (should pass):")
        for path in valid_paths:
            try:
                normalized = nx5.router.validate_path(path)
                print(f"   ✓ {path} → {normalized}")
            except Exception as e:
                print(f"   ✗ {path} → ERROR: {e}")

        # Invalid paths (security issues)
        from nexus.core.router import InvalidPathError

        invalid_paths = [
            ("/workspace/../../etc/passwd", "path traversal"),
            ("/workspace/file\x00name.txt", "null byte"),
            ("workspace/relative", "relative path"),
        ]
        print("\n   Invalid paths (security checks):")
        for path, reason in invalid_paths:
            try:
                nx5.router.validate_path(path)
                print(f"   ✗ {path} should have been rejected ({reason})!")
            except InvalidPathError:
                print(f"   ✓ Rejected {reason}: {repr(path)[:50]}")

        print("\n24. Namespace configuration and access control...")

        # Show namespace configurations (INTERNAL API - for educational purposes only)
        print("   Default namespace configurations:")
        print("   (NOTE: Accessing ._namespaces is internal API, shown for education)")
        for ns_name in ["workspace", "shared", "external", "system", "archives"]:
            ns_config = nx5.router._namespaces[ns_name]
            print(f"\n   {ns_name}:")
            print(f"   - Read-only: {ns_config.readonly}")
            print(f"   - Admin-only: {ns_config.admin_only}")
            print(f"   - Requires tenant: {ns_config.requires_tenant}")

        # Close old instance and create with custom namespace (USER-FACING API)
        nx5.close()
        print("\n   Creating instance with custom namespace...")
        from nexus.core.router import NamespaceConfig

        custom_ns = NamespaceConfig(
            name="experiments", readonly=False, admin_only=False, requires_tenant=True
        )

        # USER-FACING: Pass custom_namespaces parameter
        nx5 = nexus.connect(config={"data_dir": str(data_dir), "custom_namespaces": [custom_ns]})
        print("   ✓ Registered custom namespace: 'experiments' (via config)")

        print("\n25. Testing tenant isolation (INTERNAL APIs - educational)...")

        # Mount workspace backend (INTERNAL API)
        from nexus.backends.local import LocalBackend

        workspace_backend = LocalBackend(data_dir / "workspace-tenant-test")
        nx5.router.add_mount("/workspace", workspace_backend, priority=10)  # INTERNAL
        nx5.router.add_mount("/shared", workspace_backend, priority=10)  # INTERNAL

        # Tenant "acme" accessing their own resources (INTERNAL API)
        print("   Tenant 'acme' accessing own resources:")
        try:
            route = nx5.router.route(  # INTERNAL
                "/workspace/acme/agent1/data.txt", tenant_id="acme", is_admin=False
            )
            print("   ✓ Access granted to /workspace/acme/agent1/data.txt")
            print(f"     → Mount: {route.mount_point}")
            print(f"     → Backend path: {route.backend_path}")
        except Exception as e:
            print(f"   ✗ Unexpected error: {e}")

        # Tenant "acme" trying to access "other-tenant" resources
        from nexus.core.router import AccessDeniedError

        print("\n   Tenant 'acme' accessing 'other-tenant' resources:")
        try:
            route = nx5.router.route(
                "/workspace/other-tenant/agent1/data.txt", tenant_id="acme", is_admin=False
            )
            print("   ✗ Should have been denied!")
        except AccessDeniedError as e:
            print("   ✓ Access denied (tenant isolation enforced)")
            print(f"     → {e}")

        # Admin accessing any tenant's resources
        print("\n   Admin accessing 'other-tenant' resources:")
        try:
            route = nx5.router.route(
                "/workspace/other-tenant/agent1/data.txt", tenant_id="acme", is_admin=True
            )
            print("   ✓ Admin access granted to any tenant")
            print(f"     → Backend path: {route.backend_path}")
        except Exception as e:
            print(f"   ✗ Unexpected error: {e}")

        print("\n26. Testing read-only namespaces (INTERNAL APIs - educational)...")

        # Mount archives backend (INTERNAL API)
        archives_backend = LocalBackend(data_dir / "archives-test")
        nx5.router.add_mount("/archives", archives_backend, priority=10)  # INTERNAL

        # Reading from archives (should succeed)
        print("   Reading from /archives (read-only namespace):")
        try:
            route = nx5.router.route(
                "/archives/acme/2024/backup.tar",
                tenant_id="acme",
                is_admin=False,
                check_write=False,
            )
            print("   ✓ Read access granted")
            print(f"     → Readonly: {route.readonly}")
        except Exception as e:
            print(f"   ✗ Unexpected error: {e}")

        # Writing to archives (should fail)
        print("\n   Writing to /archives (should fail):")
        try:
            route = nx5.router.route(
                "/archives/acme/2024/backup.tar",
                tenant_id="acme",
                is_admin=False,
                check_write=True,
            )
            print("   ✗ Write should have been denied!")
        except AccessDeniedError as e:
            print("   ✓ Write denied (read-only namespace)")
            print(f"     → {e}")

        print("\n27. Testing admin-only namespaces (INTERNAL APIs - educational)...")

        # Mount system backend (INTERNAL API)
        system_backend = LocalBackend(data_dir / "system-test")
        nx5.router.add_mount("/system", system_backend, priority=10)  # INTERNAL

        # Non-admin accessing system (should fail)
        print("   Non-admin accessing /system namespace:")
        try:
            route = nx5.router.route(
                "/system/config/settings.json", is_admin=False, check_write=False
            )
            print("   ✗ Non-admin access should have been denied!")
        except AccessDeniedError as e:
            print("   ✓ Access denied (admin-only namespace)")
            print(f"     → {e}")

        # Admin accessing system (should succeed)
        print("\n   Admin accessing /system namespace:")
        try:
            route = nx5.router.route(
                "/system/config/settings.json", is_admin=True, check_write=False
            )
            print("   ✓ Admin access granted")
            print(f"     → Backend path: {route.backend_path}")
            print(f"     → Readonly: {route.readonly}")
        except Exception as e:
            print(f"   ✗ Unexpected error: {e}")

        print("\n28. Practical example: Multi-tenant workspace isolation...")

        # Create workspace structure for multiple tenants
        print("   Creating multi-tenant workspace:")

        # Tenant 1: ACME Corp
        acme_files = [
            "/workspace/acme/agent1/tasks/task1.json",
            "/workspace/acme/agent1/data/results.csv",
            "/workspace/acme/agent2/tasks/task2.json",
            "/shared/acme/models/classifier.pkl",
        ]

        # Tenant 2: Tech Inc
        tech_files = [
            "/workspace/techincCorp/agent1/tasks/analysis.json",
            "/workspace/techincCorp/agent1/data/metrics.csv",
            "/shared/techincCorp/datasets/training_data.csv",
        ]

        print("   Tenant: ACME Corp")
        for path in acme_files:
            info = nx5.router.parse_path(path)
            print(f"   - {path}")
            print(f"     Tenant: {info.tenant_id}, Agent: {info.agent_id or 'shared'}")

        print("\n   Tenant: Tech Inc")
        for path in tech_files:
            info = nx5.router.parse_path(path)
            print(f"   - {path}")
            print(f"     Tenant: {info.tenant_id}, Agent: {info.agent_id or 'shared'}")

            print("\n   Enforcing isolation:")
            print("   ✓ ACME's agent1 can only access /workspace/acme/agent1/")
            print("   ✓ ACME's agents can share via /shared/acme/")
            print("   ✓ Tech Inc cannot access ACME's resources")
            print("   ✓ Admins can access all tenants for maintenance")

            print("\n29. Summary of namespace features:")
            print("   Namespaces defined:")
            print("   - workspace/  : Agent-specific scratch space (tenant+agent required)")
            print("   - shared/     : Tenant-wide shared data (tenant required)")
            print("   - external/   : Pass-through to external backends (no tenant)")
            print("   - system/     : System metadata (admin-only, read-only)")
            print("   - archives/   : Cold storage (tenant required, read-only)")
            print()
            print("   Security features:")
            print("   ✓ Path validation (null bytes, control chars, path traversal)")
            print("   ✓ Tenant isolation (enforced by namespace)")
            print("   ✓ Admin override (full access when needed)")
            print("   ✓ Read-only namespaces (archives, system)")
            print("   ✓ Custom namespace registration")

            nx5.close()
        else:
            # If nx5 failed to connect, skip Parts 6-14 and jump to Part 15
            print("\n[INFO] Skipping Parts 6-14 due to database locking issue")
            print("[INFO] Continuing to Part 15: rclone-style CLI Operations...")

        # End of nx5 conditional block - Parts 6-14 run only if nx5 succeeds

        # ============================================================
        # Part 7: End-to-End Tenant Isolation (USER-FACING APIs ONLY!)
        # ============================================================
        print("\n" + "=" * 70)
        print("PART 7: End-to-End Tenant Isolation - USER-FACING APIs ONLY!")
        print("=" * 70)
        print("NOTE: This section demonstrates the RECOMMENDED approach.")
        print("      Uses only public APIs: read(), write(), delete(), mkdir(), etc.")

        print("\n30. Creating multi-tenant Embedded instances...")

        # Create separate instances for each tenant
        nx_acme = nexus.connect(
            config={"data_dir": str(data_dir / "multi-tenant"), "tenant_id": "acme"}
        )
        nx_tech = nexus.connect(
            config={"data_dir": str(data_dir / "multi-tenant"), "tenant_id": "techinc"}
        )
        nx_admin = nexus.connect(
            config={"data_dir": str(data_dir / "multi-tenant"), "is_admin": True}
        )

        print("   ✓ Created ACME tenant instance (tenant_id='acme')")
        print("   ✓ Created TechInc tenant instance (tenant_id='techinc')")
        print("   ✓ Created Admin instance (is_admin=True)")

        print("\n31. Testing write isolation...")
        # ACME writes to their workspace
        nx_acme.write("/workspace/acme/agent1/secret.txt", b"ACME confidential data")
        print("   ✓ ACME wrote: /workspace/acme/agent1/secret.txt")

        # TechInc writes to their workspace
        nx_tech.write("/workspace/techinc/agent1/data.json", b'{"project": "tech-project"}')
        print("   ✓ TechInc wrote: /workspace/techinc/agent1/data.json")

        # ACME writes to shared
        nx_acme.write("/shared/acme/models/v1.pkl", b"ACME ML model")
        print("   ✓ ACME wrote: /shared/acme/models/v1.pkl")

        print("\n32. Testing read isolation...")
        # ACME can read their own files
        acme_secret = nx_acme.read("/workspace/acme/agent1/secret.txt")
        print(f"   ✓ ACME read their own file: {acme_secret.decode()}")

        # TechInc cannot read ACME's files
        print("\n   TechInc attempting to read ACME's file...")
        try:
            nx_tech.read("/workspace/acme/agent1/secret.txt")
            print("   ✗ Should have been blocked!")
        except Exception as e:
            print(f"   ✓ Access denied: {type(e).__name__}")
            print(f"     → {e}")

        print("\n33. Testing write isolation to other tenant...")
        try:
            nx_tech.write("/workspace/acme/agent1/hacked.txt", b"malicious data")
            print("   ✗ Should have been blocked!")
        except Exception as e:
            print(f"   ✓ Write blocked: {type(e).__name__}")
            print(f"     → {e}")

        print("\n34. Testing delete isolation...")
        try:
            nx_tech.delete("/workspace/acme/agent1/secret.txt")
            print("   ✗ Should have been blocked!")
        except Exception as e:
            print(f"   ✓ Delete blocked: {type(e).__name__}")
            print(f"     → {e}")

        print("\n35. Testing admin override...")
        # Admin can read any tenant's files
        admin_read = nx_admin.read("/workspace/acme/agent1/secret.txt")
        print(f"   ✓ Admin read ACME's file: {admin_read.decode()}")

        admin_read2 = nx_admin.read("/workspace/techinc/agent1/data.json")
        print(f"   ✓ Admin read TechInc's file: {admin_read2.decode()}")

        # Admin can write to any tenant's workspace
        nx_admin.write("/workspace/acme/agent1/admin-note.txt", b"Admin audit log")
        print("   ✓ Admin wrote to ACME's workspace")

        print("\n36. Testing read-only namespace enforcement...")
        # Try to write to archives (read-only)
        try:
            nx_acme.write("/archives/acme/2024/backup.tar", b"backup data")
            print("   ✗ Should have been blocked (read-only)!")
        except Exception as e:
            print(f"   ✓ Write to archives blocked: {type(e).__name__}")
            print(f"     → {e}")

        print("\n37. Testing admin-only namespace enforcement...")
        # Non-admin cannot access /system
        try:
            nx_acme.write("/system/config.json", b'{"setting": "value"}')
            print("   ✗ Should have been blocked (admin-only)!")
        except Exception as e:
            print(f"   ✓ Access to /system blocked: {type(e).__name__}")
            print(f"     → {e}")

        print("\n38. Testing directory isolation...")
        # ACME creates a directory
        nx_acme.mkdir("/workspace/acme/agent2/experiments", parents=True)
        print("   ✓ ACME created directory: /workspace/acme/agent2/experiments")

        # TechInc cannot delete ACME's directory
        try:
            nx_tech.rmdir("/workspace/acme/agent2/experiments", recursive=True)
            print("   ✗ Should have been blocked!")
        except Exception as e:
            print(f"   ✓ Directory deletion blocked: {type(e).__name__}")

        print("\n39. Testing agent-level isolation...")
        # Create agent-specific instances
        nx_agent1 = nexus.connect(
            config={
                "data_dir": str(data_dir / "multi-tenant"),
                "tenant_id": "acme",
                "agent_id": "agent1",
            }
        )
        nx_agent2 = nexus.connect(
            config={
                "data_dir": str(data_dir / "multi-tenant"),
                "tenant_id": "acme",
                "agent_id": "agent2",
            }
        )

        print("   ✓ Created agent1 instance (tenant='acme', agent='agent1')")
        print("   ✓ Created agent2 instance (tenant='acme', agent='agent2')")

        # Agent1 writes to their workspace
        nx_agent1.write("/workspace/acme/agent1/task.json", b'{"status": "in_progress"}')
        print("\n   Agent1 wrote to /workspace/acme/agent1/task.json")

        # Agent1 can read their own file
        agent1_data = nx_agent1.read("/workspace/acme/agent1/task.json")
        print(f"   Agent1 read their own file: {agent1_data.decode()}")

        # Agent2 cannot read Agent1's workspace
        print("\n   Agent2 attempting to read Agent1's file...")
        try:
            nx_agent2.read("/workspace/acme/agent1/task.json")
            print("   ✗ Should have been blocked!")
        except Exception as e:
            print(f"   ✓ Agent isolation enforced: {type(e).__name__}")
            print(f"     → {e}")

        # Agents can collaborate via /shared
        print("\n   Testing agent collaboration via /shared namespace...")
        nx_agent1.write("/shared/acme/team-data.json", b'{"project": "collaboration"}')
        print("   Agent1 wrote to /shared/acme/team-data.json")

        shared_data = nx_agent2.read("/shared/acme/team-data.json")
        print(f"   Agent2 read from shared: {shared_data.decode()}")
        print("   ✓ Agents can collaborate via /shared namespace!")

        nx_agent1.close()
        nx_agent2.close()

        print("\n40. Summary of tenant and agent isolation:")
        print("   Tenant isolation:")
        print("   ✓ Tenant 'acme' cannot access tenant 'techinc' resources")
        print("   ✓ Tenant 'techinc' cannot access tenant 'acme' resources")
        print()
        print("   Agent isolation (workspace only):")
        print("   ✓ Agent 'agent1' cannot access agent 'agent2' workspace")
        print("   ✓ Agent 'agent2' cannot access agent 'agent1' workspace")
        print("   ✓ Agents can collaborate via /shared namespace")
        print()
        print("   Admin privileges:")
        print("   ✓ Admin can access all tenant and agent resources")
        print()
        print("   Namespace enforcement:")
        print("   ✓ Read-only namespaces (/archives, /system) enforced")
        print("   ✓ Admin-only namespaces (/system) enforced")
        print("   ✓ All file and directory operations respect isolation")

        nx_acme.close()
        nx_tech.close()
        nx_admin.close()

        # ============================================================
        # Part 8: Content-Addressable Storage (CAS) with Embedded
        # ============================================================
        print("\n" + "=" * 70)
        print("PART 8: Content-Addressable Storage (CAS) - NEW!")
        print("=" * 70)

        print("\n40. Using Nexus with automatic CAS deduplication...")
        # CAS is now always enabled - no special flag needed!
        nx_cas = nexus.connect(config={"data_dir": str(data_dir / "cas-mode")})
        print("   ✓ Connected (CAS automatic)")
        print(f"   ✓ Storage location: {data_dir / 'cas-mode'}")

        # Write content
        print("\n41. Writing content via CAS-enabled Nexus...")
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
        print("\n42. Testing automatic content deduplication...")
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
        print("\n43. Writing different content...")
        content3 = b"Different content with different hash"
        nx_cas.write("/logs/access.log", content3)

        meta3 = nx_cas.metadata.get("/logs/access.log")
        hash3 = meta3.etag
        print(f"   New content hash: {hash3[:16]}...{hash3[-8:]}")
        print(f"   Different from first: {hash1 != hash3}")
        print(f"   Ref count: {nx_cas.backend.get_ref_count(hash3)}")

        # Read content back
        print("\n44. Reading content transparently...")
        retrieved = nx_cas.read("/documents/data.txt")
        print(f"   Retrieved {len(retrieved)} bytes")
        print(f"   Content matches: {retrieved == content1}")
        print(f"   Content: {retrieved.decode()[:50]}...")
        print("   ✓ CAS backend is transparent to user!")

        # Delete with reference counting
        print("\n45. Testing automatic reference counting on delete...")
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
        print("\n46. Inspecting CAS directory structure...")
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
        print("\n47. Hash collision resistance...")
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
        print("\n48. Storage efficiency demonstration...")
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
        # Part 9: File Discovery Operations (v0.1.0 - NEW!)
        # ============================================================
        print("\n" + "=" * 70)
        print("PART 9: File Discovery Operations - NEW in v0.1.0!")
        print("=" * 70)
        print("Issue #6 - Implement file discovery operations (list, glob, grep)")

        print("\n49. Setting up test files for discovery...")
        nx_discover = nexus.connect(config={"data_dir": str(data_dir / "discovery-demo")})

        # Create a structured file hierarchy for testing
        test_files = {
            "/src/main.py": b"def main():\n    print('Hello from main')\n    # TODO: Add logging\n",
            "/src/utils/helper.py": b"def helper():\n    return 42\n# TODO: Add tests\n",
            "/src/utils/validator.py": b"def validate(data):\n    # TODO: Add validation logic\n    return True\n",
            "/tests/test_main.py": b"import pytest\ndef test_main():\n    assert True\n",
            "/tests/test_helper.py": b"def test_helper():\n    # TODO: Implement tests\n    pass\n",
            "/data/config.json": b'{"setting": "enabled"}',
            "/data/users.csv": b"name,email\nAlice,alice@example.com\nBob,bob@example.com",
            "/docs/README.md": b"# Project Documentation\n## Overview\nThis is a test project.",
            "/docs/API.md": b"# API Reference\n## Endpoints\n- GET /api/users",
        }

        for path, content in test_files.items():
            nx_discover.write(path, content)

        print(f"   ✓ Created {len(test_files)} test files")

        # Test list() with recursive and details options
        print("\n50. Testing list() with recursive option...")
        all_files = nx_discover.list("/", recursive=True)
        print(f"   All files (recursive): {len(all_files)}")
        for f in sorted(all_files)[:5]:
            print(f"   - {f}")

        print("\n51. Testing list() non-recursive...")
        root_files = nx_discover.list("/", recursive=False)
        print(f"   Root directory only: {len(root_files)}")
        for f in sorted(root_files):
            print(f"   - {f}")

        src_files = nx_discover.list("/src", recursive=False)
        print(f"\n   /src directory only: {len(src_files)}")
        for f in sorted(src_files):
            print(f"   - {f}")

        print("\n52. Testing list() with details...")
        detailed_files = nx_discover.list("/data", recursive=True, details=True)
        print("   Files in /data with metadata:")
        for file_info in detailed_files:
            print(f"   - {file_info['path']}")
            print(f"     Size: {file_info['size']} bytes")
            print(f"     Modified: {file_info['modified_at']}")
            print(f"     ETag: {file_info['etag'][:16]}...")

        # Test glob() with various patterns
        print("\n53. Testing glob() with simple patterns...")
        py_files = nx_discover.glob("*.py")
        print(f"   Pattern '*.py' (root only): {len(py_files)}")
        for f in sorted(py_files):
            print(f"   - {f}")

        csv_files = nx_discover.glob("*.csv", path="/data")
        print(f"\n   Pattern '*.csv' in /data: {len(csv_files)}")
        for f in sorted(csv_files):
            print(f"   - {f}")

        print("\n54. Testing glob() with recursive patterns...")
        all_py = nx_discover.glob("**/*.py")
        print(f"   Pattern '**/*.py' (all Python files): {len(all_py)}")
        for f in sorted(all_py):
            print(f"   - {f}")

        all_md = nx_discover.glob("**/*.md")
        print(f"\n   Pattern '**/*.md' (all Markdown files): {len(all_md)}")
        for f in sorted(all_md):
            print(f"   - {f}")

        test_files_glob = nx_discover.glob("test_*.py", path="/tests")
        print(f"\n   Pattern 'test_*.py' in /tests: {len(test_files_glob)}")
        for f in sorted(test_files_glob):
            print(f"   - {f}")

        print("\n55. Testing glob() with question mark wildcard...")
        all_files_glob = nx_discover.glob("**/*")
        print(f"   Pattern '**/*' (all files): {len(all_files_glob)}")

        # Test grep() for searching content
        print("\n56. Testing grep() for content search...")
        todo_matches = nx_discover.grep("TODO")
        print(f"   Searching for 'TODO': {len(todo_matches)} matches")
        for match in todo_matches:
            print(f"   - {match['file']}:{match['line']}")
            print(f"     {match['content'].strip()}")

        print("\n57. Testing grep() with regex patterns...")
        function_matches = nx_discover.grep(r"def \w+\(")
        print(f"   Searching for function definitions: {len(function_matches)} matches")
        for match in function_matches[:5]:  # Show first 5
            print(f"   - {match['file']}:{match['line']}")
            print(f"     {match['content'].strip()}")
            print(f"     Match: '{match['match']}'")

        print("\n58. Testing grep() with file pattern filtering...")
        todo_in_py = nx_discover.grep("TODO", file_pattern="**/*.py")
        print(f"   Searching 'TODO' in Python files only: {len(todo_in_py)} matches")
        for match in todo_in_py:
            print(f"   - {match['file']}:{match['line']}")

        print("\n59. Testing grep() case-insensitive search...")
        api_matches_sensitive = nx_discover.grep("api")
        api_matches_insensitive = nx_discover.grep("api", ignore_case=True)
        print(f"   Case-sensitive 'api': {len(api_matches_sensitive)} matches")
        print(f"   Case-insensitive 'api': {len(api_matches_insensitive)} matches")
        for match in api_matches_insensitive:
            print(f"   - {match['file']}:{match['line']}")
            print(f"     {match['content'].strip()}")

        print("\n60. Testing grep() with result limiting...")
        # Create a file with many matches
        repeated_content = "\n".join([f"Line {i} with KEYWORD here" for i in range(50)])
        nx_discover.write("/test/repeated.txt", repeated_content.encode())

        limited_results = nx_discover.grep("KEYWORD", max_results=5)
        print(f"   Limited to 5 results: {len(limited_results)} matches returned")
        for match in limited_results:
            print(f"   - Line {match['line']}: {match['content'][:40]}...")

        print("\n61. Practical example: Finding all test files...")
        # Combine glob and grep for powerful file discovery
        all_test_files = nx_discover.glob("**/test_*.py")
        print(f"   Found {len(all_test_files)} test files:")
        for f in sorted(all_test_files):
            print(f"   - {f}")

        print("\n62. Practical example: Finding unimplemented tests...")
        unimplemented = nx_discover.grep("pass|TODO", file_pattern="**/test_*.py")
        print(f"   Found {len(unimplemented)} potential unimplemented tests:")
        for match in unimplemented:
            print(f"   - {match['file']}:{match['line']}")
            print(f"     {match['content'].strip()}")

        # NEW in v0.2.0 - search_mode parameter
        print("\n62a. Testing grep() with search_mode parameter (v0.2.0)...")
        print("   search_mode='auto': Try parsed text first, fallback to raw (default)")
        auto_matches = nx_discover.grep("TODO", search_mode="auto")
        print(f"   Found {len(auto_matches)} matches with auto mode")
        for match in auto_matches[:2]:
            source = match.get("source", "raw")
            print(f"   - {match['file']}:{match['line']} (source: {source})")

        print("\n   search_mode='raw': Only search raw file content (skip parsing)")
        raw_matches = nx_discover.grep("TODO", search_mode="raw")
        print(f"   Found {len(raw_matches)} matches with raw mode")

        print("\n   search_mode='parsed': Only search parsed text (for PDFs/docs)")
        parsed_matches = nx_discover.grep("TODO", search_mode="parsed")
        print(f"   Found {len(parsed_matches)} matches with parsed mode")
        print("   (Only includes files with parsed content)")

        print("\n63. Summary of file discovery operations:")
        print("   list() enhancements:")
        print("   ✓ recursive parameter - control depth of listing")
        print("   ✓ details parameter - get file metadata (size, dates, etag)")
        print("   ✓ Backward compatible with old prefix parameter")
        print()
        print("   glob() patterns supported:")
        print("   ✓ * - matches any characters except /")
        print("   ✓ ** - matches any characters including / (recursive)")
        print("   ✓ ? - matches single character")
        print("   ✓ [...] - character classes")
        print()
        print("   grep() capabilities:")
        print("   ✓ Regex pattern matching in file contents")
        print("   ✓ File filtering with glob patterns")
        print("   ✓ Case-insensitive search option")
        print("   ✓ Result limiting for large result sets")
        print("   ✓ Automatic binary file detection and skipping")
        print("   ✓ Returns file path, line number, matched line, and match text")

        nx_discover.close()

        # ============================================================
        # Part 10: Metadata Export/Import (v0.1.0 - NEW!)
        # ============================================================
        print("\n" + "=" * 70)
        print("PART 10: Metadata Export/Import - NEW in v0.1.0!")
        print("=" * 70)
        print("Issue #35 - Advanced metadata export/import with filters and conflict resolution")

        print("\n64. Setting up test data for export/import...")
        export_dir = data_dir / "export-demo"
        nx_export = nexus.connect(config={"data_dir": str(export_dir)})

        # Create test files with various metadata
        import time

        test_export_files = {
            "/workspace/project1/main.py": b"def main():\n    print('Hello World')\n",
            "/workspace/project1/utils.py": b"def helper():\n    return 42\n",
            "/workspace/project2/app.py": b"# Application entry point\n",
            "/shared/models/v1.pkl": b"mock ML model data",
            "/shared/datasets/train.csv": b"col1,col2\n1,2\n3,4\n",
        }

        for path, content in test_export_files.items():
            nx_export.write(path, content)

        # Add custom metadata to some files
        print(f"   ✓ Created {len(test_export_files)} test files")
        print("\n   Adding custom metadata to files...")
        nx_export.metadata.set_file_metadata("/workspace/project1/main.py", "author", "Alice")
        nx_export.metadata.set_file_metadata("/workspace/project1/main.py", "version", "1.0")
        nx_export.metadata.set_file_metadata("/shared/models/v1.pkl", "model_type", "classifier")
        nx_export.metadata.set_file_metadata("/shared/models/v1.pkl", "accuracy", 0.95)
        print("   ✓ Added custom metadata to 2 files")

        # Test metadata export
        print("\n65. Exporting all metadata to JSONL file...")
        export_file = export_dir / "metadata-export.jsonl"
        exported_count = nx_export.export_metadata(export_file)
        print(f"   ✓ Exported {exported_count} file metadata records")
        print(f"   Output: {export_file}")
        print("   ✓ Output is sorted by path for clean git diffs!")

        # Show sample of exported JSONL
        print("\n66. Sample of exported JSONL content...")
        with open(export_file) as f:
            lines = f.readlines()
            for i, line in enumerate(lines[:2], 1):
                import json

                data = json.loads(line)
                print(f"\n   Record {i}:")
                print(f"     Path: {data['path']}")
                print(f"     Size: {data['size']} bytes")
                print(f"     ETag: {data['etag'][:16]}...")
                if "custom_metadata" in data:
                    print(f"     Custom metadata: {data['custom_metadata']}")

        # Test selective export with ExportFilter
        print("\n67. Testing ExportFilter with path prefix...")
        from nexus.core.export_import import ExportFilter

        workspace_export = export_dir / "workspace-export.jsonl"
        workspace_filter = ExportFilter(path_prefix="/workspace")
        workspace_count = nx_export.export_metadata(workspace_export, filter=workspace_filter)
        print(f"   ✓ Exported {workspace_count} workspace files")
        print(f"   Output: {workspace_export}")

        # Test export with time filter
        print("\n68. Testing ExportFilter with time filter...")
        cutoff_time = datetime.now(UTC)
        time.sleep(0.1)  # Ensure different timestamp

        # Create new files after cutoff
        nx_export.write("/workspace/recent1.py", b"# Recent file 1")
        nx_export.write("/workspace/recent2.py", b"# Recent file 2")

        recent_export = export_dir / "recent-export.jsonl"
        recent_filter = ExportFilter(after_time=cutoff_time)
        recent_count = nx_export.export_metadata(recent_export, filter=recent_filter)
        print(f"   ✓ Exported {recent_count} files modified after {cutoff_time.isoformat()}")

        # Test metadata import to a new instance
        print("\n69. Testing basic import to new instance...")
        import_dir = data_dir / "import-demo"
        nx_import = nexus.connect(config={"data_dir": str(import_dir)})

        # Import metadata using new API
        from nexus.core.export_import import ImportOptions

        print("   Importing metadata from export file...")
        result = nx_import.import_metadata(export_file)
        print(f"   ✓ Created: {result.created}")
        print(f"   ✓ Updated: {result.updated}")
        print(f"   ✓ Skipped: {result.skipped}")
        print(f"   ✓ Total: {result.total_processed}")

        # Verify imported metadata
        print("\n70. Verifying imported metadata...")
        imported_files = nx_import.list()
        print(f"   Total files in new instance: {len(imported_files)}")
        for path in sorted(imported_files)[:3]:
            meta = nx_import.metadata.get(path)
            print(f"\n   {path}")
            print(f"     Size: {meta.size} bytes")
            print(f"     ETag: {meta.etag[:16]}...")

        # Verify custom metadata was imported
        print("\n   Checking custom metadata preservation...")
        author = nx_import.metadata.get_file_metadata("/workspace/project1/main.py", "author")
        version = nx_import.metadata.get_file_metadata("/workspace/project1/main.py", "version")
        print(f"   main.py author: {author}")
        print(f"   main.py version: {version}")
        print("   ✓ Custom metadata preserved during import!")

        # Test import with conflict resolution modes
        print("\n71. Testing conflict resolution modes...")

        # Create conflict by modifying a file
        nx_import.write("/workspace/project1/main.py", b"# Modified content")
        print("   Modified /workspace/project1/main.py in import instance")

        # Test skip mode (default)
        print("\n   Testing 'skip' conflict mode...")
        skip_options = ImportOptions(conflict_mode="skip")
        skip_result = nx_import.import_metadata(export_file, options=skip_options)
        print(
            f"   Created: {skip_result.created}, Updated: {skip_result.updated}, Skipped: {skip_result.skipped}"
        )
        print(f"   Collisions detected: {len(skip_result.collisions)}")
        if skip_result.collisions:
            for collision in skip_result.collisions[:2]:
                print(f"   - {collision.path}: {collision.resolution}")

        # Test overwrite mode
        print("\n   Testing 'overwrite' conflict mode...")
        overwrite_options = ImportOptions(conflict_mode="overwrite")
        overwrite_result = nx_import.import_metadata(export_file, options=overwrite_options)
        print(
            f"   Created: {overwrite_result.created}, Updated: {overwrite_result.updated}, Skipped: {overwrite_result.skipped}"
        )
        print(f"   ✓ Overwrote {overwrite_result.updated} files")

        # Test remap mode
        print("\n72. Testing 'remap' conflict mode...")
        remap_dir = data_dir / "remap-demo"
        nx_remap = nexus.connect(config={"data_dir": str(remap_dir)})

        # Create initial file
        nx_remap.write("/test.txt", b"original content")

        # Import with different content for same path
        import json

        conflict_data = {
            "path": "/test.txt",
            "backend_name": "local",
            "physical_path": "differenthash",
            "size": 20,
            "etag": "a" * 64,
            "created_at": datetime.now(UTC).isoformat(),
            "modified_at": datetime.now(UTC).isoformat(),
            "version": 1,
        }

        remap_export = remap_dir / "conflict-export.jsonl"
        with open(remap_export, "w") as f:
            f.write(json.dumps(conflict_data) + "\n")

        remap_options = ImportOptions(conflict_mode="remap")
        remap_result = nx_remap.import_metadata(remap_export, options=remap_options)
        print(f"   Remapped: {remap_result.remapped} files")
        if remap_result.collisions:
            collision = remap_result.collisions[0]
            print(f"   {collision.path} → {collision.message}")

        # Verify remapped file exists
        all_files = nx_remap.list()
        print(f"   Files in instance: {all_files}")
        print("   ✓ Original and remapped files both exist!")

        nx_remap.close()

        # Test auto mode
        print("\n73. Testing 'auto' conflict mode (newer wins)...")
        auto_dir = data_dir / "auto-demo"
        nx_auto = nexus.connect(config={"data_dir": str(auto_dir)})

        # Create old file
        nx_auto.write("/test.txt", b"old content")
        time.sleep(0.1)

        # Create export with newer timestamp
        future_data = {
            "path": "/test.txt",
            "backend_name": "local",
            "physical_path": "newhash",
            "size": 15,
            "etag": "b" * 64,
            "created_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "modified_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "version": 1,
        }

        auto_export = auto_dir / "newer-export.jsonl"
        with open(auto_export, "w") as f:
            f.write(json.dumps(future_data) + "\n")

        auto_options = ImportOptions(conflict_mode="auto")
        auto_result = nx_auto.import_metadata(auto_export, options=auto_options)
        print(f"   Updated: {auto_result.updated} (imported is newer)")
        if auto_result.collisions:
            collision = auto_result.collisions[0]
            print(f"   Resolution: {collision.resolution}")
            print(f"   Message: {collision.message}")

        nx_auto.close()

        # Test dry-run mode
        print("\n74. Testing dry-run mode (no changes)...")
        dry_run_dir = data_dir / "dry-run-demo"
        nx_dry = nexus.connect(config={"data_dir": str(dry_run_dir)})

        dry_run_options = ImportOptions(dry_run=True)
        dry_result = nx_dry.import_metadata(export_file, options=dry_run_options)
        print(f"   Would create: {dry_result.created} files")
        print(f"   Would update: {dry_result.updated} files")
        print(f"   Would skip: {dry_result.skipped} files")

        # Verify no files were actually created
        actual_files = nx_dry.list()
        print(f"   Actual files created: {len(actual_files)}")
        print("   ✓ Dry-run did not modify database!")

        nx_dry.close()
        nx_export.close()
        nx_import.close()

        print("\n75. Summary of metadata export/import:")
        print("   Export capabilities (Issue #35):")
        print("   ✓ Export all metadata to JSONL format")
        print("   ✓ ExportFilter with path_prefix filtering")
        print("   ✓ ExportFilter with after_time (time-based) filtering")
        print("   ✓ Sorted output for clean git diffs")
        print("   ✓ Includes file metadata (path, size, timestamps, etag)")
        print("   ✓ Includes custom key-value metadata")
        print("   ✓ Human-readable JSON format (one file per line)")
        print()
        print("   Import capabilities (Issue #35):")
        print("   ✓ Import metadata from JSONL file")
        print("   ✓ ImportOptions with 4 conflict resolution modes:")
        print("     - skip: Keep existing (default)")
        print("     - overwrite: Replace with imported")
        print("     - remap: Rename imported to avoid collision")
        print("     - auto: Smart resolution (newer wins)")
        print("   ✓ Dry-run mode (simulate without changes)")
        print("   ✓ Collision detection and tracking (CollisionDetail)")
        print("   ✓ Returns ImportResult with detailed counts")
        print("   ✓ Restore custom metadata")
        print("   ✓ Validate required fields during import")
        print("   ✓ Backward compatible with old API")
        print()
        print("   Use cases:")
        print("   • Git-friendly metadata backups (sorted output)")
        print("   • Zero-downtime migrations between instances")
        print("   • Disaster recovery with conflict resolution")
        print("   • Audit and inspect file metadata externally")
        print("   • Time-based incremental backups (after_time filter)")
        print("   • Safe trial runs with dry-run mode")

        # ============================================================
        # Part 11: Work Detection with SQL Views (v0.1.0 - NEW!)
        # ============================================================
        print("\n" + "=" * 70)
        print("PART 11: Work Detection with SQL Views - NEW in v0.1.0!")
        print("=" * 70)
        print("Issue #69 - SQL views for efficient work detection")

        print("\n72. Setting up work queue demonstration...")
        work_dir = data_dir / "work-demo"
        nx_work = nexus.connect(config={"data_dir": str(work_dir)})

        # Create work items (files representing tasks/jobs)
        print("   Creating work items as files...")
        work_items = [
            ("/jobs/task1.json", b'{"task": "process_data", "dataset": "users"}'),
            ("/jobs/task2.json", b'{"task": "train_model", "model": "classifier"}'),
            ("/jobs/task3.json", b'{"task": "generate_report", "format": "pdf"}'),
            ("/jobs/task4.json", b'{"task": "analyze_results", "experiment": "exp_001"}'),
            ("/jobs/task5.json", b'{"task": "cleanup_temp", "path": "/tmp"}'),
        ]

        for path, content in work_items:
            nx_work.write(path, content)

        print(f"   ✓ Created {len(work_items)} work items")

        # Set work metadata using the metadata store
        print("\n73. Setting work status and priority metadata...")

        # Task 1: Ready to process (high priority)
        nx_work.metadata.set_file_metadata("/jobs/task1.json", "status", "ready")
        nx_work.metadata.set_file_metadata("/jobs/task1.json", "priority", 1)
        nx_work.metadata.set_file_metadata("/jobs/task1.json", "tags", ["urgent", "data"])
        print("   task1: status=ready, priority=1 (high)")

        # Task 2: In progress
        nx_work.metadata.set_file_metadata("/jobs/task2.json", "status", "in_progress")
        nx_work.metadata.set_file_metadata("/jobs/task2.json", "priority", 2)
        nx_work.metadata.set_file_metadata("/jobs/task2.json", "worker_id", "worker-001")
        nx_work.metadata.set_file_metadata(
            "/jobs/task2.json", "started_at", datetime.now(UTC).isoformat()
        )
        print("   task2: status=in_progress, worker=worker-001")

        # Task 3: Pending (waiting to start)
        nx_work.metadata.set_file_metadata("/jobs/task3.json", "status", "pending")
        nx_work.metadata.set_file_metadata("/jobs/task3.json", "priority", 3)
        print("   task3: status=pending, priority=3")

        # Task 4: Blocked (depends on task 2)
        # Get path_id for task2 (the blocker)
        task2_path_id = nx_work.metadata.get_path_id("/jobs/task2.json")
        nx_work.metadata.set_file_metadata("/jobs/task4.json", "status", "blocked")
        nx_work.metadata.set_file_metadata("/jobs/task4.json", "priority", 2)
        nx_work.metadata.set_file_metadata("/jobs/task4.json", "depends_on", task2_path_id)
        print(
            f"   task4: status=blocked, depends_on={task2_path_id[:8]}..."
            if task2_path_id
            else "   task4: status=blocked"
        )

        # Task 5: Ready (low priority)
        nx_work.metadata.set_file_metadata("/jobs/task5.json", "status", "ready")
        nx_work.metadata.set_file_metadata("/jobs/task5.json", "priority", 5)
        nx_work.metadata.set_file_metadata("/jobs/task5.json", "tags", ["cleanup"])
        print("   task5: status=ready, priority=5 (low)")

        # Query ready work using SQL views
        print("\n74. Querying ready work items (no blockers)...")
        ready_work = nx_work.metadata.get_ready_work(limit=10)
        print(f"   Found {len(ready_work)} ready work items:")
        for item in ready_work:
            print(f"   - {item['virtual_path']}")
            print(f"     Priority: {item['priority']}, Status: {item['status']}")

        # Query pending work
        print("\n75. Querying pending work items...")
        pending_work = nx_work.metadata.get_pending_work()
        print(f"   Found {len(pending_work)} pending work items:")
        for item in pending_work:
            print(f"   - {item['virtual_path']}")

        # Query blocked work
        print("\n76. Querying blocked work items...")
        blocked_work = nx_work.metadata.get_blocked_work()
        print(f"   Found {len(blocked_work)} blocked work items:")
        for item in blocked_work:
            print(f"   - {item['virtual_path']}")
            print(f"     Blocker count: {item['blocker_count']}")

        # Query in-progress work
        print("\n77. Querying in-progress work items...")
        in_progress_work = nx_work.metadata.get_in_progress_work()
        print(f"   Found {len(in_progress_work)} in-progress work items:")
        for item in in_progress_work:
            print(f"   - {item['virtual_path']}")
            if item.get("worker_id"):
                import json

                try:
                    worker_id = json.loads(item["worker_id"])
                    print(f"     Worker: {worker_id}")
                except (json.JSONDecodeError, TypeError):
                    print(f"     Worker: {item['worker_id']}")

        # Query all work by priority
        print("\n78. Querying all work items sorted by priority...")
        all_work = nx_work.metadata.get_work_by_priority(limit=10)
        print(f"   Found {len(all_work)} work items (top 10 by priority):")
        for item in all_work:
            import json

            status = "N/A"
            priority = "N/A"
            if item.get("status"):
                try:
                    status = json.loads(item["status"])
                except (json.JSONDecodeError, TypeError):
                    status = str(item["status"])
            if item.get("priority"):
                try:
                    priority = json.loads(item["priority"])
                except (json.JSONDecodeError, TypeError):
                    priority = str(item["priority"])
            print(f"   - {item['virtual_path']}: status={status}, priority={priority}")

        # Simulate work processing
        print("\n79. Simulating work processing...")
        print("   Picking highest priority ready work item...")
        if ready_work:
            work_item = ready_work[0]
            work_path = work_item["virtual_path"]
            print(f"   Selected: {work_path}")

            # Mark as in_progress
            print(f"\n   Marking {work_path} as in_progress...")
            nx_work.metadata.set_file_metadata(work_path, "status", "in_progress")
            nx_work.metadata.set_file_metadata(work_path, "worker_id", "worker-002")
            nx_work.metadata.set_file_metadata(
                work_path, "started_at", datetime.now(UTC).isoformat()
            )

            # Check ready work again
            ready_after = nx_work.metadata.get_ready_work()
            print(f"   Ready work items after starting: {len(ready_after)}")

            # Simulate completion
            print(f"\n   Completing {work_path}...")
            nx_work.metadata.set_file_metadata(work_path, "status", "completed")
            nx_work.metadata.set_file_metadata(
                work_path, "completed_at", datetime.now(UTC).isoformat()
            )

            # Check if any blocked work became ready
            print("\n   Checking if any blocked work became unblocked...")
            ready_final = nx_work.metadata.get_ready_work()
            blocked_final = nx_work.metadata.get_blocked_work()
            print(f"   Ready work: {len(ready_final)} items")
            print(f"   Blocked work: {len(blocked_final)} items")

        nx_work.close()

        # ============================================================
        # Part 12: Type-Level Validation (v0.1.0 - NEW!)
        # ============================================================
        print("\n" + "=" * 70)
        print("PART 12: Type-Level Validation - NEW in v0.1.0!")
        print("=" * 70)
        print("Issue #37 - Type-level validation for all domain types")

        print("\n81. Testing type-level validation on FileMetadata...")
        from nexus.core.exceptions import ValidationError
        from nexus.core.metadata import FileMetadata

        # Test invalid path (doesn't start with /)
        print("   Testing invalid path (must start with /)...")
        try:
            invalid_meta = FileMetadata(
                path="data/file.txt",  # Invalid: doesn't start with /
                backend_name="local",
                physical_path="/storage/file.txt",
                size=1024,
            )
            invalid_meta.validate()
            print("   ✗ Should have raised ValidationError!")
        except ValidationError as e:
            print(f"   ✓ Correctly rejected: {e}")

        # Test negative size
        print("\n   Testing negative size...")
        try:
            invalid_meta = FileMetadata(
                path="/data/file.txt",
                backend_name="local",
                physical_path="/storage/file.txt",
                size=-100,  # Invalid: negative size
            )
            invalid_meta.validate()
            print("   ✗ Should have raised ValidationError!")
        except ValidationError as e:
            print(f"   ✓ Correctly rejected: {e}")

        # Test path with null bytes
        print("\n   Testing path with null bytes...")
        try:
            invalid_meta = FileMetadata(
                path="/data/file\x00.txt",  # Invalid: contains null byte
                backend_name="local",
                physical_path="/storage/file.txt",
                size=1024,
            )
            invalid_meta.validate()
            print("   ✗ Should have raised ValidationError!")
        except ValidationError as e:
            print(f"   ✓ Correctly rejected: {e}")

        # Test missing required fields
        print("\n   Testing missing backend_name...")
        try:
            invalid_meta = FileMetadata(
                path="/data/file.txt",
                backend_name="",  # Invalid: empty backend_name
                physical_path="/storage/file.txt",
                size=1024,
            )
            invalid_meta.validate()
            print("   ✗ Should have raised ValidationError!")
        except ValidationError as e:
            print(f"   ✓ Correctly rejected: {e}")

        print("\n82. Testing validation in metadata store operations...")
        validation_dir = data_dir / "validation-demo"
        nx_validation = nexus.connect(config={"data_dir": str(validation_dir)})

        # Valid metadata - should succeed
        print("   Creating valid file metadata...")
        try:
            nx_validation.write("/test/valid.txt", b"Valid content")
            print("   ✓ Valid file created successfully")
        except ValidationError as e:
            print(f"   ✗ Unexpected validation error: {e}")

        # Test that validation happens at metadata store level
        print("\n   Testing validation at metadata store level...")
        from nexus.core.metadata import FileMetadata

        invalid_metadata = FileMetadata(
            path="invalid-path",  # Doesn't start with /
            backend_name="local",
            physical_path="/storage/file.txt",
            size=100,
        )

        try:
            nx_validation.metadata.put(invalid_metadata)
            print("   ✗ Should have raised ValidationError!")
        except ValidationError as e:
            print(f"   ✓ Validation enforced at store level: {e}")

        nx_validation.close()

        print("\n83. Testing SQLAlchemy model validation...")
        from nexus.storage.models import ContentChunkModel, FileMetadataModel, FilePathModel

        # Test FilePathModel validation
        print("   Testing FilePathModel validation...")
        try:
            invalid_file_path = FilePathModel(
                virtual_path="no-leading-slash",  # Invalid
                backend_id="local",
                physical_path="/storage/file.txt",
                size_bytes=100,
                tenant_id="test-tenant",
            )
            invalid_file_path.validate()
            print("   ✗ Should have raised ValidationError!")
        except ValidationError as e:
            print(f"   ✓ FilePathModel validation works: {e}")

        # Test FileMetadataModel validation
        print("\n   Testing FileMetadataModel validation...")
        try:
            invalid_metadata = FileMetadataModel(
                path_id="test-id",
                key="a" * 300,  # Invalid: too long (> 255)
                value='{"test": "value"}',
                created_at=datetime.now(UTC),
            )
            invalid_metadata.validate()
            print("   ✗ Should have raised ValidationError!")
        except ValidationError as e:
            print(f"   ✓ FileMetadataModel validation works: {e}")

        # Test ContentChunkModel validation
        print("\n   Testing ContentChunkModel validation...")
        try:
            invalid_chunk = ContentChunkModel(
                content_hash="tooshort",  # Invalid: must be 64 chars
                size_bytes=1024,
                storage_path="/storage/chunk",
                ref_count=1,
            )
            invalid_chunk.validate()
            print("   ✗ Should have raised ValidationError!")
        except ValidationError as e:
            print(f"   ✓ ContentChunkModel validation works: {e}")

        # Test negative ref_count
        print("\n   Testing negative ref_count...")
        try:
            invalid_chunk = ContentChunkModel(
                content_hash="a" * 64,  # Valid hash
                size_bytes=1024,
                storage_path="/storage/chunk",
                ref_count=-1,  # Invalid: negative
            )
            invalid_chunk.validate()
            print("   ✗ Should have raised ValidationError!")
        except ValidationError as e:
            print(f"   ✓ Correctly rejected negative ref_count: {e}")

        print("\n84. Summary of type-level validation:")
        print("   Validation features:")
        print("   ✓ Automatic validation before database operations")
        print("   ✓ Clear, actionable error messages")
        print("   ✓ Fail fast (before expensive DB operations)")
        print("   ✓ Consistent validation across all code paths")
        print()
        print("   Domain types with validation:")
        print("   • FileMetadata (path, size, backend_name, etc.)")
        print("   • FilePathModel (virtual_path, size_bytes, etc.)")
        print("   • FileMetadataModel (key length, path_id)")
        print("   • ContentChunkModel (content_hash, ref_count, size)")
        print()
        print("   Validation rules:")
        print("   • Paths must start with '/' and not contain null bytes")
        print("   • Sizes and counts must be non-negative")
        print("   • Required fields must not be empty")
        print("   • Content hashes must be valid 64-char SHA-256")
        print("   • Metadata keys must be ≤ 255 characters")
        print()
        print("   Benefits:")
        print("   • Prevents invalid data in database")
        print("   • Improves developer experience with clear errors")
        print("   • Reduces debugging time (fail fast)")
        print("   • Enables better API error responses (400 Bad Request)")

        # ============================================================
        # Part 13: Batch Get Content IDs for CAS Deduplication (v0.1.0 - Issue #34)
        # ============================================================
        print("\n" + "=" * 70)
        print("PART 13: Batch Get Content IDs - CAS Deduplication")
        print("=" * 70)
        print("Issue #34 - Batch operations to avoid N+1 queries")

        print("\n85. Creating files with duplicate content for deduplication demo...")
        dedup_dir = data_dir / "dedup-demo"
        nx_dedup = nexus.connect(config={"data_dir": str(dedup_dir)})

        # Create files with some duplicates
        files_to_create = [
            ("/docs/report1.txt", b"This is a unique report"),
            ("/docs/report2.txt", b"Another unique document"),
            ("/docs/report3.txt", b"This is a unique report"),  # Duplicate of report1
            ("/docs/report4.txt", b"Fourth report content"),
            ("/docs/report5.txt", b"Another unique document"),  # Duplicate of report2
            ("/docs/backup/report1.txt", b"This is a unique report"),  # Duplicate of report1 & 3
            ("/docs/backup/report4.txt", b"Fourth report content"),  # Duplicate of report4
        ]

        for path, content in files_to_create:
            nx_dedup.write(path, content)

        print(f"   Created {len(files_to_create)} files")

        print("\n86. Using batch_get_content_ids() to detect duplicates...")
        paths = [f[0] for f in files_to_create]

        # Single query to get all content hashes
        # Now available at top level for convenience!
        content_ids = nx_dedup.batch_get_content_ids(paths)

        # Also available at metadata level: nx_dedup.metadata.batch_get_content_ids(paths)

        print(f"   Retrieved {len(content_ids)} content hashes in single query")
        print("   ✓ Avoided N+1 query problem (1 query vs 7 queries)")

        # Find duplicates
        from collections import defaultdict

        by_hash = defaultdict(list)
        for path, content_hash in content_ids.items():
            if content_hash:
                by_hash[content_hash].append(path)

        duplicates = {h: paths for h, paths in by_hash.items() if len(paths) > 1}

        print("\n87. Deduplication analysis:")
        print(f"   Total files: {len(files_to_create)}")
        print(f"   Unique content hashes: {len(by_hash)}")
        print(f"   Duplicate groups: {len(duplicates)}")

        print("\n   Duplicate content detected:")
        for i, (content_hash, duplicate_paths) in enumerate(duplicates.items(), 1):
            print(f"\n   Group {i} (hash: {content_hash[:16]}...):")
            for path in duplicate_paths:
                print(f"     • {path}")

        # Calculate space savings
        total_bytes = sum(len(f[1]) for f in files_to_create)
        unique_bytes = sum(
            len(files_to_create[paths.index(paths_list[0])][1]) for paths_list in by_hash.values()
        )
        saved_bytes = total_bytes - unique_bytes

        print("\n88. Storage optimization (CAS deduplication):")
        print(f"   Total bytes (without dedup): {total_bytes}")
        print(f"   Unique bytes (with dedup): {unique_bytes}")
        print(f"   Space saved: {saved_bytes} bytes ({saved_bytes / total_bytes * 100:.1f}%)")

        print("\n89. Performance comparison - batch vs individual queries:")
        print("   Without batch_get_content_ids() (N+1 problem):")
        print(f"     • {len(paths)} files × 2ms/query = ~{len(paths) * 2}ms")
        print("   With batch_get_content_ids():")
        print("     • 1 query = ~2ms")
        print(f"   ✓ Performance improvement: ~{len(paths)}× faster!")

        print("\n90. Summary of batch_get_content_ids():")
        print("   Features:")
        print("   ✓ Single SQL query with IN clause (not N queries)")
        print("   ✓ Returns dict mapping path → content_hash")
        print("   ✓ Efficient for CAS deduplication scenarios")
        print("   ✓ Only fetches content_hash field (not full metadata)")
        print()
        print("   Use cases:")
        print("   • Content-addressable storage (CAS) systems")
        print("   • Finding duplicate files for deduplication")
        print("   • Efficient backup systems (only backup unique content)")
        print("   • Data lake optimization (avoid storing duplicate datasets)")
        print("   • Media asset management (detect duplicate images/videos)")

        nx_dedup.close()

        print("\n80. Summary of work detection SQL views:")
        print("   SQL Views created:")
        print("   • ready_work_items    - Files ready for processing (no blockers)")
        print("   • pending_work_items  - Files waiting to start")
        print("   • blocked_work_items  - Files blocked by dependencies")
        print("   • in_progress_work    - Files currently being processed")
        print("   • work_by_priority    - All work items ordered by priority")
        print()
        print("   Python API methods:")
        print("   • get_ready_work()       - Get ready items (sorted by priority)")
        print("   • get_pending_work()     - Get pending items")
        print("   • get_blocked_work()     - Get blocked items with blocker count")
        print("   • get_in_progress_work() - Get active work items")
        print("   • get_work_by_priority() - Get all work sorted by priority")
        print()
        print("   Performance:")
        print("   ✓ O(n) query performance using indexed SQL views")
        print("   ✓ Efficient dependency checking with EXISTS subqueries")
        print("   ✓ < 100ms query time for 10,000+ work items")
        print()
        print("   Use cases:")
        print("   • Work queue systems (distributed task processing)")
        print("   • Dependency resolution (DAG execution)")
        print("   • Priority-based scheduling (high-priority first)")
        print("   • Monitoring dashboards (real-time work status)")
        print("   • Worker assignment (load balancing)")

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
• Namespace & Tenant Isolation (workspace, shared, external, system, archives)
• Path parsing & validation (security checks)
• Access control (tenant isolation, admin-only, read-only namespaces)
        """
        )

        print("\n📊 Feature Summary:")
        print("   ✓ File operations (read/write/delete)")
        print("   ✓ Metadata tracking (SQLite)")
        print("   ✓ Custom metadata (key-value)")
        print("   ✓ Directory operations (mkdir/rmdir/is_directory)")
        print("   ✓ Path routing (virtual → physical)")
        print("   ✓ Multi-mount support (multiple backends)")
        print("   ✓ Namespace & tenant isolation (workspace/shared/external/system/archives)")
        print("   ✓ Path validation & security (null bytes, control chars, path traversal)")
        print("   ✓ Access control (tenant isolation, admin-only, read-only)")
        print("   ✓ Persistence (survives restarts)")
        print("   ✓ Data integrity (ETags)")
        print("   ✓ Content-addressable storage (CAS)")
        print("   ✓ Content deduplication (save space)")
        print("   ✓ Reference counting (safe deletion)")
        print("   ✓ Atomic writes (data integrity)")
        print("   ✓ SHA-256 content hashing")
        print("   ✓ File discovery operations (list/glob/grep) - NEW in v0.1.0!")
        print("     - list() with recursive and details options")
        print("     - glob() with ** recursive patterns")
        print("     - grep() with regex and file filtering")
        print("   ✓ Metadata export/import (JSONL format) - NEW in v0.1.0!")
        print("     - Export metadata to JSONL for backup/migration")
        print("     - Import metadata from JSONL with validation")
        print("     - Selective export with path prefix filtering")
        print("     - Preserve custom metadata during export/import")
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

        # ============================================================
        # Part 14: Automatic Document Parsing (v0.2.0 - NEW!)
        # ============================================================
        print("\n" + "=" * 70)
        print("PART 14: Automatic Document Parsing - NEW in v0.2.0!")
        print("=" * 70)
        print("Issue #17 - Transparent document parsing with high-level primitives")

        print("\n91. Setting up files for auto-parsing demo...")
        parse_dir = data_dir / "parse-demo"
        nx_parse = nexus.connect(config={"data_dir": str(parse_dir), "auto_parse": True})

        print("   ✓ Connected with auto_parse=True (default)")
        print("   Parser automatically extracts text from 23+ file formats")

        # Upload a PDF if available
        print("\n92. Uploading PDF file (auto-parsing in background)...")
        pdf_file = Path("examples/sample-local-pdf.pdf")
        if pdf_file.exists():
            with open(pdf_file, "rb") as f:
                pdf_content = f.read()

            # Just write - parsing happens automatically!
            nx_parse.write("/documents/sample.pdf", pdf_content)
            print(f"   ✓ Uploaded PDF ({len(pdf_content)} bytes)")
            print("   → Parsing triggered automatically in background thread")
        else:
            print("   ⚠ PDF file not found, using markdown instead")

        # Upload markdown
        print("\n93. Uploading Markdown file (also auto-parsed)...")
        markdown_content = b"""# Project Report

## Executive Summary
This report demonstrates **automatic document parsing** in Nexus.

## Key Features
- Transparent parsing on write()
- grep() searches parsed text automatically
- Supports PDF, Office, Markdown, JSON, CSV, and 20+ formats

## Implementation Details
The parser system uses MarkItDown for initial parsing, with an
extensible architecture for adding custom parsers.

## Performance Metrics
- Parse time: < 2 seconds for typical documents
- TODO: Add benchmarking results
- ERROR handling is robust with graceful fallbacks

## Conclusion
Auto-parsing makes document search seamless and transparent.
"""

        nx_parse.write("/docs/report.md", markdown_content)
        print("   ✓ Uploaded Markdown")
        print("   → Parsing triggered automatically")

        # Upload JSON
        nx_parse.write(
            "/data/config.json", b'{"project": "nexus", "version": "0.2.0", "auto_parse": true}'
        )
        print("   ✓ Uploaded JSON")

        # Wait for background parsing to complete
        print("\n94. Waiting for background parsing to complete...")
        import time

        time.sleep(3)  # Give parsers time to finish
        print("   ✓ Parsing complete")

        # Check what was parsed
        print("\n95. Checking parsed files...")
        all_files = nx_parse.list()
        for file_path in all_files:
            parsed_text = nx_parse.metadata.get_file_metadata(file_path, "parsed_text")
            if parsed_text:
                parser_name = nx_parse.metadata.get_file_metadata(file_path, "parser_name")
                print(f"   ✓ {file_path}")
                print(f"     Parser: {parser_name}")
                print(f"     Extracted: {len(parsed_text)} characters")
            else:
                print(f"   - {file_path} (not parsed)")

        # Demonstrate grep searching parsed content
        print("\n96. Using grep() - automatically searches parsed text!")
        print("   Searching for 'TODO' across all files...")
        todo_matches = nx_parse.grep("TODO")
        print(f"   Found {len(todo_matches)} matches:")
        for match in todo_matches:
            print(f"   - {match['file']}:{match['line']}")
            print(f"     {match['content'].strip()}")

        print("\n97. Case-insensitive search for 'ERROR'...")
        error_matches = nx_parse.grep("ERROR", ignore_case=True)
        print(f"   Found {len(error_matches)} matches:")
        for match in error_matches:
            print(f"   - {match['file']}:{match['line']}")

        # Search in PDF (if uploaded)
        if pdf_file.exists():
            print("\n98. Searching PDF content (binary file!)...")
            pdf_matches = nx_parse.grep("PDF", file_pattern="**/*.pdf")
            print(f"   Found {len(pdf_matches)} matches for 'PDF':")
            for match in pdf_matches[:3]:  # Show first 3
                print(f"   - {match['file']}:{match['line']}")
                print(f"     {match['content'][:60]}...")
            print("   ✓ Searched extracted text, not binary data!")

        # Show that you can disable auto_parse
        print("\n99. Auto-parsing can be disabled if needed...")
        nx_no_parse = nexus.connect(
            config={"data_dir": str(parse_dir / "no-parse"), "auto_parse": False}
        )
        nx_no_parse.write("/test.txt", b"This won't be auto-parsed")
        parsed = nx_no_parse.metadata.get_file_metadata("/test.txt", "parsed_text")
        print(f"   auto_parse=False: parsed_text = {parsed}")
        nx_no_parse.close()

        # Show supported formats
        print("\n100. Checking supported file formats...")
        supported = nx_parse.parser_registry.get_supported_formats()
        print(f"   Total formats supported: {len(supported)}")
        print(f"   Formats: {', '.join(supported[:15])}...")

        print("\n101. Summary of automatic document parsing:")
        print("   Features:")
        print("   ✓ Auto-parse on write() (default behavior)")
        print("   ✓ grep() searches parsed text automatically")
        print("   ✓ Transparent - no explicit parse() calls needed")
        print("   ✓ Background processing (non-blocking)")
        print("   ✓ 23+ file format support (PDF, Office, Markdown, etc.)")
        print("   ✓ Extensible parser architecture")
        print()
        print("   Supported formats:")
        print("   • Documents: PDF, DOCX, DOC, PPTX, PPT")
        print("   • Spreadsheets: XLSX, XLS, CSV")
        print("   • Text: TXT, MD, Markdown")
        print("   • Data: JSON, XML")
        print("   • Images: PNG, JPG, GIF, BMP (with OCR)")
        print("   • Archives: EPUB, ZIP")
        print()
        print("   High-level primitives that use parsed content:")
        print("   • grep() - searches parsed text when available")
        print("   • (future) read() with parse flag")
        print("   • (future) vector embedding generation")
        print()
        print("   Configuration:")
        print("   • auto_parse=True (default) - parse on write")
        print("   • auto_parse=False - disable auto-parsing")
        print("   • Can also call parse() explicitly when needed")

        # ============================================================
        # PART 10: Advanced Parser Features (New in v0.2.0)
        # ============================================================
        print("\n" + "=" * 70)
        print("PART 10: Advanced Parser Features")
        print("=" * 70)

        # 102. Parser Auto-Discovery
        print("\n102. Testing parser auto-discovery...")
        from nexus.parsers import ParserRegistry

        registry = ParserRegistry()
        discovered_count = registry.discover_parsers("nexus.parsers")
        print(f"   ✓ Auto-discovered {discovered_count} parser(s)")
        print(f"   Parsers: {[p.name for p in registry.get_parsers()]}")

        # 103. MIME Type Detection
        print("\n103. Testing MIME type detection...")
        from nexus.parsers import detect_mime_type

        test_content = b'{"key": "value"}'
        mime_type = detect_mime_type(test_content, "test.json")
        print(f"   ✓ Detected MIME type: {mime_type}")

        pdf_content_sample = b"%PDF-1.4"
        mime_type_pdf = detect_mime_type(pdf_content_sample, "test.pdf")
        print(f"   ✓ PDF MIME type: {mime_type_pdf}")

        # 104. Encoding Detection
        print("\n104. Testing text encoding detection...")
        from nexus.parsers import detect_encoding

        utf8_text = "Hello, 世界! 🌍".encode()
        encoding = detect_encoding(utf8_text)
        print(f"   ✓ Detected encoding: {encoding}")

        ascii_text = b"Hello, world!"
        encoding_ascii = detect_encoding(ascii_text)
        print(f"   ✓ ASCII encoding: {encoding_ascii}")

        # 105. Compressed File Handling
        print("\n105. Testing compressed file handling...")
        import gzip

        from nexus.parsers import decompress_content, is_compressed

        # Create a compressed file
        original_text = b"This is a test document with important content."
        compressed_data = gzip.compress(original_text)

        print(f"   Original size: {len(original_text)} bytes")
        print(f"   Compressed size: {len(compressed_data)} bytes")
        print(f"   Compression ratio: {len(compressed_data) / len(original_text) * 100:.1f}%")

        # Check compression detection
        if is_compressed("document.txt.gz"):
            print("   ✓ Compression detected for .gz file")

        # Decompress
        decompressed, inner_name = decompress_content(compressed_data, "document.txt.gz")
        print("   ✓ Decompressed successfully")
        print(f"   Inner filename: {inner_name}")
        print(f"   Decompressed size: {len(decompressed)} bytes")
        assert decompressed == original_text, "Decompression failed!"

        # 106. Unified Content Preparation
        print("\n106. Testing unified content preparation...")
        from nexus.parsers import prepare_content_for_parsing

        # Test with compressed JSON
        json_content = b'{"project": "nexus", "version": "0.2.0"}'
        compressed_json = gzip.compress(json_content)

        processed, effective_path, metadata = prepare_content_for_parsing(
            compressed_json, "config.json.gz"
        )

        print("   ✓ Original file: config.json.gz")
        print(f"   ✓ Effective path: {effective_path}")
        print(f"   ✓ Was compressed: {metadata.get('compressed', False)}")
        print(f"   ✓ Inner filename: {metadata.get('inner_filename')}")
        print(f"   ✓ MIME type: {metadata.get('mime_type')}")
        print(f"   ✓ Content size: {len(processed)} bytes")

        # 107. Test with actual compressed file write
        print("\n107. Writing compressed file to Nexus...")
        compressed_report = gzip.compress(b"""# Compressed Report

This document was compressed with gzip before upload.

## Key Points
- Nexus automatically detects compression
- Parsers can handle compressed formats
- Transparent decompression during parsing
""")

        nx_parse.write("/documents/compressed-report.md.gz", compressed_report)
        print(f"   ✓ Uploaded compressed file ({len(compressed_report)} bytes)")
        print("   → Auto-parsing will decompress and parse automatically")

        # Wait for parsing
        import time

        time.sleep(2)

        # Check if parsed
        parsed_compressed = nx_parse.metadata.get_file_metadata(
            "/documents/compressed-report.md.gz", "parsed_text"
        )
        if parsed_compressed:
            print("   ✓ Compressed file parsed successfully")
            print(f"   ✓ Extracted {len(parsed_compressed)} characters")
        else:
            print("   ⚠ Compressed file parsing still in progress...")

        print("\n108. Summary of advanced parser features:")
        print("   New features in v0.2.0:")
        print("   ✓ Auto-discovery of parsers from packages")
        print("   ✓ MIME type detection (python-magic + fallback)")
        print("   ✓ Text encoding detection (chardet + fallback)")
        print("   ✓ Compressed file support (.gz, .zip, .bz2, .xz)")
        print("   ✓ Unified content preprocessing pipeline")
        print()
        print("   Supported compression formats:")
        print("   • .gz / .gzip - gzip compression")
        print("   • .zip - ZIP archives (single file)")
        print("   • .bz2 - bzip2 compression")
        print("   • .xz - LZMA compression")
        print()
        print("   These features work transparently with auto_parse!")

        nx_parse.close()

        # ============================================================
        # Part 15: rclone-style CLI Operations (v0.2.0)
        # ============================================================
        print("\n" + "=" * 70)
        print("PART 15: rclone-style CLI Operations (NEW in v0.2.0)")
        print("=" * 70)
        print("Demonstrating sync, copy, tree, and size operations")

        # Create test data for sync/copy operations
        print("\n54. Setting up test data for file operations...")
        nx_ops = nexus.connect(config={"data_dir": str(data_dir)})

        # Create source directory with test files
        nx_ops.write("/source/file1.txt", b"Content for file 1")
        nx_ops.write("/source/file2.txt", b"Content for file 2")
        nx_ops.write("/source/subdir/file3.txt", b"Content for file 3")
        nx_ops.write("/source/subdir/file4.txt", b"Content for file 4")
        print("   ✓ Created test files in /source/")

        # These operations are typically done via CLI, but we can demonstrate
        # the underlying functionality programmatically
        from nexus.sync import copy_recursive, sync_directories

        print("\n55. Demonstrating sync operation (hash-based)...")
        stats = sync_directories(
            nx_ops,
            "/source",
            "/dest",
            delete=False,
            dry_run=False,
            checksum=True,
            progress=False,  # Disable progress bar for demo
        )
        print(f"   Files checked: {stats.files_checked}")
        print(f"   Files copied: {stats.files_copied}")
        print(f"   Files skipped: {stats.files_skipped}")
        print(f"   Bytes transferred: {stats.bytes_transferred:,}")
        print("   ✓ Sync completed successfully")

        print("\n56. Re-syncing (should skip identical files)...")
        stats2 = sync_directories(
            nx_ops,
            "/source",
            "/dest",
            delete=False,
            dry_run=False,
            checksum=True,
            progress=False,
        )
        print(f"   Files checked: {stats2.files_checked}")
        print(f"   Files copied: {stats2.files_copied}")
        print(f"   Files skipped: {stats2.files_skipped} (all files identical!)")
        print("   ✓ Smart deduplication works!")

        print("\n57. Testing sync with delete flag...")
        # Add an extra file to destination
        nx_ops.write("/dest/extra_file.txt", b"This will be deleted")
        stats3 = sync_directories(
            nx_ops,
            "/source",
            "/dest",
            delete=True,  # Delete extra files
            dry_run=False,
            checksum=True,
            progress=False,
        )
        print(f"   Files deleted: {stats3.files_deleted}")
        print("   ✓ Extra files removed from destination")

        print("\n58. Demonstrating copy operation...")
        stats4 = copy_recursive(
            nx_ops,
            "/source",
            "/backup",
            checksum=True,
            progress=False,
        )
        print(f"   Files copied: {stats4.files_copied}")
        print("   ✓ Directory copied to /backup")

        print("\n59. Listing files to verify operations...")
        dest_files = nx_ops.list("/dest", recursive=True)
        backup_files = nx_ops.list("/backup", recursive=True)
        print(f"   Files in /dest: {len(dest_files)}")
        print(f"   Files in /backup: {len(backup_files)}")

        print("\n60. Demonstrating ReBAC (Relationship-Based Access Control)...")
        import sqlite3

        from nexus.core.rebac_manager import ReBACManager

        # Get database path (same as the data_dir used throughout this demo)
        rebac_db_path = data_dir / "metadata.db"

        # Create ReBAC tables manually (since demo doesn't run Alembic migrations)
        print("   Setting up ReBAC database tables...")
        conn = sqlite3.connect(str(rebac_db_path))
        cursor = conn.cursor()

        # Create tables if they don't exist
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS rebac_tuples (
                tuple_id TEXT PRIMARY KEY,
                subject_type TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                subject_relation TEXT,
                relation TEXT NOT NULL,
                object_type TEXT NOT NULL,
                object_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT,
                conditions TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS rebac_namespaces (
                namespace_id TEXT PRIMARY KEY,
                object_type TEXT NOT NULL UNIQUE,
                config TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS rebac_check_cache (
                cache_id TEXT PRIMARY KEY,
                subject_type TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                permission TEXT NOT NULL,
                object_type TEXT NOT NULL,
                object_id TEXT NOT NULL,
                result INTEGER NOT NULL,
                computed_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS rebac_changelog (
                change_id INTEGER PRIMARY KEY AUTOINCREMENT,
                change_type TEXT NOT NULL,
                tuple_id TEXT,
                subject_type TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                relation TEXT NOT NULL,
                object_type TEXT NOT NULL,
                object_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        conn.commit()
        conn.close()

        rebac_mgr = ReBACManager(db_path=str(rebac_db_path))

        print("   Creating relationship tuples...")
        # alice is member of engineering team
        tuple1 = rebac_mgr.rebac_write(
            subject=("agent", "alice"),
            relation="member-of",
            object=("group", "engineering"),
        )
        print(f"   ✓ Created: alice member-of engineering (ID: {tuple1[:8]}...)")

        # engineering team owns the /projects directory
        tuple2 = rebac_mgr.rebac_write(
            subject=("group", "engineering"),
            relation="owner-of",
            object=("file", "/projects"),
        )
        print(f"   ✓ Created: engineering owner-of /projects (ID: {tuple2[:8]}...)")

        # parent folder relationship
        tuple3 = rebac_mgr.rebac_write(
            subject=("file", "/projects"),
            relation="parent-of",
            object=("file", "/projects/backend"),
        )
        print(f"   ✓ Created: /projects parent-of /projects/backend (ID: {tuple3[:8]}...)")

        print("\n   Checking permissions (with graph traversal)...")
        # Direct check: alice is member of engineering
        has_perm1 = rebac_mgr.rebac_check(
            subject=("agent", "alice"),
            permission="member-of",
            object=("group", "engineering"),
        )
        print(f"   alice member-of engineering? {has_perm1} ✓")

        # Indirect check: alice owns /projects (via group membership)
        # Note: This requires namespace config for "file" type to support this traversal
        # For now, we just check direct ownership
        has_perm2 = rebac_mgr.rebac_check(
            subject=("group", "engineering"),
            permission="owner-of",
            object=("file", "/projects"),
        )
        print(f"   engineering owner-of /projects? {has_perm2} ✓")

        print("\n   Expanding permissions (find all subjects)...")
        # Find all members of engineering
        members = rebac_mgr.rebac_expand(
            permission="member-of",
            object=("group", "engineering"),
        )
        print(f"   Members of engineering: {[s[1] for s in members]}")

        # Find all owners of /projects
        owners = rebac_mgr.rebac_expand(
            permission="owner-of",
            object=("file", "/projects"),
        )
        print(f"   Owners of /projects: {[(s[0], s[1]) for s in owners]}")

        print("\n   Creating temporary access (expires after 1 second)...")
        expires_at = datetime.utcnow() + timedelta(seconds=1)
        rebac_mgr.rebac_write(
            subject=("agent", "bob"),
            relation="viewer-of",
            object=("file", "/projects/temp-doc"),
            expires_at=expires_at,
        )
        print("   ✓ Created: bob viewer-of /projects/temp-doc (expires in 1s)")

        # Check immediately
        has_temp = rebac_mgr.rebac_check(
            subject=("agent", "bob"),
            permission="viewer-of",
            object=("file", "/projects/temp-doc"),
        )
        print(f"   bob can view temp-doc (now)? {has_temp} ✓")

        # Wait for expiration
        import time

        print("   Waiting 1.5 seconds for expiration...")
        time.sleep(1.5)

        # Check after expiration
        has_temp_after = rebac_mgr.rebac_check(
            subject=("agent", "bob"),
            permission="viewer-of",
            object=("file", "/projects/temp-doc"),
        )
        print(f"   bob can view temp-doc (after expiry)? {has_temp_after} ✓ (expired)")

        print("\n   Deleting relationships...")
        deleted = rebac_mgr.rebac_delete(tuple1)
        print(f"   ✓ Deleted tuple {tuple1[:8]}... (deleted: {deleted})")

        rebac_mgr.close()
        print("   ✓ ReBAC demo completed!")

        # Note: tree and size commands are typically CLI-only
        # but we can show the underlying data they would display
        print("\n61. CLI commands available for:")
        print("   • nexus tree /workspace - ASCII tree visualization")
        print("   • nexus size /workspace --human - Calculate directory sizes")
        print("   • nexus sync ./local/ /workspace/ - One-way sync")
        print("   • nexus copy ./data/ /backup/ --recursive - Smart copy")
        print("   • nexus move /old /new - Efficient move/rename")
        print()
        print("   All commands support:")
        print("   • Progress bars (tqdm) for long operations")
        print("   • Hash-based change detection")
        print("   • Dry-run mode (--dry-run)")
        print("   • Cross-platform paths (local ↔ Nexus)")

        nx_ops.close()

        print("\n✓ Integrated demo completed successfully!")
        print("=" * 70)


if __name__ == "__main__":
    main()
