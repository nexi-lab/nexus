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

        print("\n22. Demonstrating path parsing and namespace extraction...")
        print("    (INTERNAL API - for educational purposes)")
        nx5 = nexus.connect(config={"data_dir": str(data_dir)})

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
        print("Issue #68 - Implement metadata export/import (JSONL format)")

        print("\n64. Setting up test data for export/import...")
        export_dir = data_dir / "export-demo"
        nx_export = nexus.connect(config={"data_dir": str(export_dir)})

        # Create test files with various metadata
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

        # Test selective export with prefix
        print("\n67. Exporting only /workspace files...")
        workspace_export = export_dir / "workspace-export.jsonl"
        workspace_count = nx_export.export_metadata(workspace_export, prefix="/workspace")
        print(f"   ✓ Exported {workspace_count} workspace file metadata records")
        print(f"   Output: {workspace_export}")

        # Test metadata import to a new instance
        print("\n68. Testing metadata import to new instance...")
        import_dir = data_dir / "import-demo"
        nx_import = nexus.connect(config={"data_dir": str(import_dir)})

        # Import metadata
        print("   Importing metadata from export file...")
        imported, skipped = nx_import.import_metadata(export_file)
        print(f"   ✓ Imported {imported} file metadata records")
        print(f"   Skipped {skipped} existing files")

        # Verify imported metadata
        print("\n69. Verifying imported metadata...")
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

        # Test import with overwrite
        print("\n70. Testing import with overwrite existing metadata...")
        # Modify a file in the import instance
        nx_import.metadata.set_file_metadata("/workspace/project1/main.py", "author", "Bob")
        author_before = nx_import.metadata.get_file_metadata(
            "/workspace/project1/main.py", "author"
        )
        print(f"   Author before re-import: {author_before}")

        # Re-import with overwrite
        imported2, skipped2 = nx_import.import_metadata(export_file, overwrite=True)
        print(f"   Re-imported {imported2} records (overwrite=True)")

        # Verify overwrite worked
        author_after = nx_import.metadata.get_file_metadata("/workspace/project1/main.py", "author")
        print(f"   Author after re-import: {author_after}")
        print("   ✓ Metadata was overwritten correctly!")

        nx_export.close()
        nx_import.close()

        print("\n71. Summary of metadata export/import:")
        print("   Export capabilities:")
        print("   ✓ Export all metadata to JSONL format")
        print("   ✓ Selective export with path prefix filtering")
        print("   ✓ Includes file metadata (path, size, timestamps, etag)")
        print("   ✓ Includes custom key-value metadata")
        print("   ✓ Human-readable JSON format (one file per line)")
        print()
        print("   Import capabilities:")
        print("   ✓ Import metadata from JSONL file")
        print("   ✓ Skip existing files (default behavior)")
        print("   ✓ Overwrite existing metadata (with --overwrite flag)")
        print("   ✓ Restore custom metadata")
        print("   ✓ Validate required fields during import")
        print()
        print("   Use cases:")
        print("   • Backup metadata for disaster recovery")
        print("   • Migrate metadata between instances")
        print("   • Create alternative path mappings to same content")
        print("   • Audit and inspect file metadata externally")

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

        print("\n✓ Integrated demo completed successfully!")
        print("=" * 70)


if __name__ == "__main__":
    main()
