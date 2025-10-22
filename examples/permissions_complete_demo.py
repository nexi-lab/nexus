#!/usr/bin/env python3
"""Complete Multi-Layer Permission System Demo for Nexus.

This demo showcases all three layers of permission enforcement:
1. ReBAC (Relationship-Based Access Control) - Graph-based permissions
2. ACL (Access Control Lists) - Explicit allow/deny rules
3. UNIX Permissions - Traditional owner/group/mode

Run this demo to see how the multi-layer security works in practice.
"""

import tempfile
from pathlib import Path

from nexus import connect
from nexus.core.permissions import OperationContext


def demo_unix_permissions():
    """Demo traditional UNIX permissions (owner/group/mode)."""
    print("\n" + "=" * 70)
    print("DEMO 1: UNIX Permissions (owner/group/mode)")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmpdir:
        nx = connect(
            config={
                "backend": "local",
                "data_dir": str(Path(tmpdir) / "data"),
                "db_path": str(Path(tmpdir) / "metadata.db"),
                "agent_id": "alice",
                "enforce_permissions": True,  # Enable enforcement
            }
        )

        # Create contexts for different users
        alice_ctx = OperationContext(user="alice", groups=["developers"])
        bob_ctx = OperationContext(user="bob", groups=["developers"])
        charlie_ctx = OperationContext(user="charlie", groups=["guests"])

        print("\n1. Alice creates a file (owner=alice, mode=0o644)")
        nx.write("/workspace/alice_file.txt", b"Alice's private data", context=alice_ctx)

        # Set permissions: owner can read+write, group/others can only read
        nx.chmod("/workspace/alice_file.txt", 0o644, context=alice_ctx)

        print("   File permissions: rw-r--r-- (0o644)")

        # Alice can read her own file
        print("\n2. Alice reads her file ✓")
        content = nx.read("/workspace/alice_file.txt", context=alice_ctx)
        print(f"   Content: {content.decode()}")

        # Bob (same group) can read but not write
        print("\n3. Bob (same group) reads Alice's file ✓")
        content = nx.read("/workspace/alice_file.txt", context=bob_ctx)
        print(f"   Content: {content.decode()}")

        print("\n4. Bob tries to write to Alice's file ✗")
        try:
            nx.write("/workspace/alice_file.txt", b"Bob trying to modify", context=bob_ctx)
            print("   ERROR: Bob should not be able to write!")
        except PermissionError as e:
            print(f"   ✓ Denied: {e}")

        # Charlie (different group) can read but not write
        print("\n5. Charlie (other user) reads Alice's file ✓")
        content = nx.read("/workspace/alice_file.txt", context=charlie_ctx)
        print(f"   Content: {content.decode()}")

        # Change to owner-only mode
        print("\n6. Alice changes permissions to 0o600 (owner-only)")
        nx.chmod("/workspace/alice_file.txt", 0o600, context=alice_ctx)
        print("   File permissions: rw------- (0o600)")

        print("\n7. Bob tries to read now ✗")
        try:
            nx.read("/workspace/alice_file.txt", context=bob_ctx)
            print("   ERROR: Bob should not be able to read!")
        except PermissionError as e:
            print(f"   ✓ Denied: {e}")

        print("\n8. Charlie tries to read now ✗")
        try:
            nx.read("/workspace/alice_file.txt", context=charlie_ctx)
            print("   ERROR: Charlie should not be able to read!")
        except PermissionError as e:
            print(f"   ✓ Denied: {e}")

        nx.close()
        print("\n✓ UNIX Permissions demo complete!")


def demo_acl_permissions():
    """Demo ACL (Access Control List) permissions."""
    print("\n" + "=" * 70)
    print("DEMO 2: ACL (Access Control Lists)")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmpdir:
        nx = connect(
            config={
                "backend": "local",
                "data_dir": str(Path(tmpdir) / "data"),
                "db_path": str(Path(tmpdir) / "metadata.db"),
                "agent_id": "alice",
                "enforce_permissions": True,
            }
        )

        # Create contexts
        # alice_ctx = OperationContext(user="alice", groups=["developers"])  # Unused in this demo
        bob_ctx = OperationContext(user="bob", groups=["developers"])
        charlie_ctx = OperationContext(user="charlie", groups=["designers"])
        admin_ctx = OperationContext(user="admin", groups=["admins"], is_admin=True)

        print("\n1. Alice creates a file with restrictive UNIX permissions (0o600)")
        nx.write("/workspace/project_file.txt", b"Project data", context=admin_ctx)
        nx.chmod("/workspace/project_file.txt", 0o600, context=admin_ctx)
        nx.chown("/workspace/project_file.txt", "alice", context=admin_ctx)
        print("   UNIX permissions: rw------- (owner-only)")

        # Without ACL, Bob cannot read
        print("\n2. Bob tries to read ✗")
        try:
            nx.read("/workspace/project_file.txt", context=bob_ctx)
            print("   ERROR: Bob should not be able to read (UNIX denies)!")
        except PermissionError as e:
            print(f"   ✓ UNIX Denied: {e}")

        # Add ACL entry to grant Bob read access
        print("\n3. Admin adds ACL entry: user:bob:r-- (grant Bob read access)")

        # ACL store is available via nx._permission_enforcer.acl_store if needed

        # Add ACL entry to database
        from nexus.storage.models import ACLEntryModel

        # Get path_id using the metadata store's method
        path_id = nx.metadata.get_path_id("/workspace/project_file.txt")
        with nx.metadata.SessionLocal() as session:
            acl_entry = ACLEntryModel(
                path_id=path_id,
                entry_type="user",
                identifier="bob",
                permissions="r--",
                deny=False,
                is_default=False,
            )
            session.add(acl_entry)
            session.commit()

        print("   ACL entry added: user:bob:r--")

        # Now Bob can read (ACL overrides UNIX)
        print("\n4. Bob reads the file ✓ (ACL grants access)")
        content = nx.read("/workspace/project_file.txt", context=bob_ctx)
        print(f"   Content: {content.decode()}")

        # But Bob still cannot write (ACL only granted read)
        print("\n5. Bob tries to write ✗")
        try:
            nx.write("/workspace/project_file.txt", b"Bob's update", context=bob_ctx)
            print("   ERROR: Bob should not be able to write!")
        except PermissionError as e:
            print(f"   ✓ Denied: {e}")

        # Add deny ACL for Charlie
        print("\n6. Admin adds ACL deny entry: deny:user:charlie")
        with nx.metadata.SessionLocal() as session:
            deny_entry = ACLEntryModel(
                path_id=path_id,  # Use same path_id
                entry_type="user",
                identifier="charlie",
                permissions="---",
                deny=True,
                is_default=False,
            )
            session.add(deny_entry)
            session.commit()

        print("   ACL entry added: deny:user:charlie")

        # Charlie is explicitly denied
        print("\n7. Charlie tries to read ✗ (ACL denies)")
        try:
            nx.read("/workspace/project_file.txt", context=charlie_ctx)
            print("   ERROR: Charlie should be denied!")
        except PermissionError as e:
            print(f"   ✓ ACL Denied: {e}")

        nx.close()
        print("\n✓ ACL Permissions demo complete!")


def demo_rebac_permissions():
    """Demo ReBAC (Relationship-Based Access Control) permissions."""
    print("\n" + "=" * 70)
    print("DEMO 3: ReBAC (Relationship-Based Access Control)")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmpdir:
        # NOTE: ReBAC currently requires SQLite (not PostgreSQL)
        # Explicitly set db_path to avoid inheriting NEXUS_DATABASE_URL
        import os

        old_db_url = os.environ.pop("NEXUS_DATABASE_URL", None)

        try:
            nx = connect(
                config={
                    "backend": "local",
                    "data_dir": str(Path(tmpdir) / "data"),
                    "db_path": str(Path(tmpdir) / "metadata.db"),
                    "agent_id": "alice",
                    "enforce_permissions": True,
                }
            )

            # Create contexts
            # alice_ctx = OperationContext(user="alice", groups=["developers"])  # Unused in this demo
            bob_ctx = OperationContext(user="bob", groups=["developers"])
            admin_ctx = OperationContext(user="admin", groups=["admins"], is_admin=True)

            print("\n1. Alice creates a file with restrictive permissions")
            nx.write("/workspace/team_doc.txt", b"Team document", context=admin_ctx)
            nx.chmod("/workspace/team_doc.txt", 0o600, context=admin_ctx)
            nx.chown("/workspace/team_doc.txt", "alice", context=admin_ctx)

            # Get file metadata for ReBAC
            path_id = nx.metadata.get_path_id("/workspace/team_doc.txt")
            file_id = str(path_id)

            # Setup ReBAC relationships
            rebac_manager = nx._rebac_manager

            # Create ReBAC tables (required first time)
            import sqlite3

            conn = sqlite3.connect(str(Path(tmpdir) / "metadata.db"))
            cursor = conn.cursor()
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

            print("\n2. Setup ReBAC relationships:")
            print("   - alice is 'owner-of' team_doc")
            print("   - bob is 'member-of' engineering_team")
            print("   - team_doc is 'owned-by' engineering_team")

            # Create relationships
            rebac_manager.rebac_write(
                subject=("agent", "alice"), relation="owner-of", object=("file", file_id)
            )

            rebac_manager.rebac_write(
                subject=("agent", "bob"),
                relation="member-of",
                object=("group", "engineering_team"),
            )

            rebac_manager.rebac_write(
                subject=("group", "engineering_team"),
                relation="owner-of",
                object=("file", file_id),
            )

            print("   ✓ ReBAC relationships created")

            # Bob cannot read via UNIX permissions
            print("\n3. Bob tries to read (UNIX denies) ✗")
            try:
                nx.read("/workspace/team_doc.txt", context=bob_ctx)
                print("   ERROR: Bob should not be able to read (UNIX denies)!")
            except PermissionError as e:
                print(f"   ✓ UNIX Denied: {e}")

            # Note: ReBAC currently checks if relationship exists, but the full
            # indirect relationship traversal would need namespace config updates
            print("\n4. Checking direct ReBAC relationship:")
            has_direct_perm = rebac_manager.rebac_check(
                subject=("agent", "bob"),
                permission="member-of",
                object=("group", "engineering_team"),
            )
            print(f"   Bob is member-of engineering_team: {has_direct_perm}")

            # For full ReBAC integration, the permission would be granted via the graph
            print("\nNOTE: Full ReBAC graph traversal requires namespace configuration")
            print("      to define permission expansion rules (e.g., member -> reader).")
            print("      See src/nexus/core/rebac.py for DEFAULT_FILE_NAMESPACE config.")

            nx.close()
            print("\n✓ ReBAC Permissions demo complete!")
        finally:
            # Restore PostgreSQL env var if it was set
            if old_db_url:
                os.environ["NEXUS_DATABASE_URL"] = old_db_url


def demo_multi_layer():
    """Demo how all three layers work together."""
    print("\n" + "=" * 70)
    print("DEMO 4: Multi-Layer Security (ReBAC → ACL → UNIX)")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmpdir:
        nx = connect(
            config={
                "backend": "local",
                "data_dir": str(Path(tmpdir) / "data"),
                "db_path": str(Path(tmpdir) / "metadata.db"),
                "agent_id": "system",
                "is_admin": True,  # System needs admin to access /system namespace
                "enforce_permissions": True,
            }
        )

        print("\nPermission check order:")
        print("1. Admin/System bypass? → Skip all checks")
        print("2. ReBAC grants? → Allow")
        print("3. ACL denies? → Deny")
        print("4. ACL allows? → Allow")
        print("5. UNIX permissions → Final decision")

        # Create test scenarios
        admin_ctx = OperationContext(user="admin", groups=[], is_admin=True)
        system_ctx = OperationContext(user="system", groups=[], is_system=True)
        alice_ctx = OperationContext(user="alice", groups=["developers"])

        print("\n1. System user creates a file")
        nx.write("/shared/config.txt", b"Shared config", context=system_ctx)

        print("\n2. System bypass - can access anything ✓")
        content = nx.read("/shared/config.txt", context=system_ctx)
        print(f"   Content: {content.decode()}")

        print("\n3. Admin bypass - can access anything ✓")
        content = nx.read("/shared/config.txt", context=admin_ctx)
        print(f"   Content: {content.decode()}")

        print("\n4. Regular user denied by UNIX permissions ✗")
        # Set restrictive permissions
        nx.chmod("/shared/config.txt", 0o600, context=admin_ctx)
        try:
            nx.read("/shared/config.txt", context=alice_ctx)
            print("   ERROR: Alice should be denied!")
        except PermissionError as e:
            print(f"   ✓ Denied: {e}")

        print("\n5. List files - filtered by permissions")
        nx.write("/shared/public.txt", b"Public data", context=system_ctx)
        nx.chmod("/shared/public.txt", 0o644, context=admin_ctx)  # Readable by all

        files = nx.list("/shared", recursive=False, context=alice_ctx)
        print(f"   Alice can see {len(files)} file(s): {files}")
        print("   (config.txt is filtered out due to lack of permissions)")

        nx.close()
        print("\n✓ Multi-Layer Security demo complete!")


def demo_permission_inheritance():
    """Demo permission inheritance from parent directories."""
    print("\n" + "=" * 70)
    print("DEMO 5: Permission Inheritance")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmpdir:
        nx = connect(
            config={
                "backend": "local",
                "data_dir": str(Path(tmpdir) / "data"),
                "db_path": str(Path(tmpdir) / "metadata.db"),
                "agent_id": "alice",
                "enforce_permissions": True,
            }
        )

        admin_ctx = OperationContext(user="admin", groups=[], is_admin=True)
        bob_ctx = OperationContext(user="bob", groups=["developers"])

        print("\n1. Admin creates parent directory with specific permissions")
        nx.mkdir("/workspace/project", parents=True, context=admin_ctx)
        nx.chmod("/workspace/project", 0o755, context=admin_ctx)
        nx.chown("/workspace/project", "alice", context=admin_ctx)
        nx.chgrp("/workspace/project", "developers", context=admin_ctx)

        print("   Parent: owner=alice, group=developers, mode=0o755")

        print("\n2. Create file in directory (inherits permissions)")
        nx.write("/workspace/project/file.txt", b"Project file", context=admin_ctx)

        meta = nx.metadata.get("/workspace/project/file.txt")
        print(f"   Child: owner={meta.owner}, group={meta.group}, mode={oct(meta.mode)}")
        print("   Note: File gets 0o644 (execute bits cleared from 0o755)")

        print("\n3. Bob (in developers group) can read inherited file ✓")
        content = nx.read("/workspace/project/file.txt", context=bob_ctx)
        print(f"   Content: {content.decode()}")

        nx.close()
        print("\n✓ Permission Inheritance demo complete!")


def main():
    """Run all permission demos."""
    print("\n" + "=" * 70)
    print("NEXUS MULTI-LAYER PERMISSION SYSTEM - COMPLETE DEMO")
    print("=" * 70)
    print("\nThis demo showcases Nexus's three-layer security architecture:")
    print("  1. UNIX Permissions (owner/group/mode) - Traditional access control")
    print("  2. ACL (Access Control Lists) - Fine-grained explicit allow/deny")
    print("  3. ReBAC (Relationship-Based) - Graph-based permission inheritance")

    # Run each demo
    demo_unix_permissions()
    demo_acl_permissions()
    demo_rebac_permissions()
    demo_multi_layer()
    demo_permission_inheritance()

    print("\n" + "=" * 70)
    print("✓ ALL DEMOS COMPLETE!")
    print("=" * 70)
    print("\nKey Takeaways:")
    print("  • Permission enforcement is enabled via enforce_permissions=True")
    print("  • Three layers provide defense-in-depth security")
    print("  • Admin/System contexts bypass all checks")
    print("  • List operations automatically filter by permissions")
    print("  • Files inherit permissions from parent directories")
    print("\nFor more details, see:")
    print("  • docs/development/PERMISSION_ENFORCEMENT_GUIDE.md")
    print("  • docs/development/PERMISSIONS_IMPLEMENTATION.md")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    main()
