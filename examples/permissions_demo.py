"""Demo of UNIX-style file permissions and ACL support in Nexus.

This example demonstrates:
1. Setting file owners, groups, and permissions
2. Checking permissions
3. Using Access Control Lists (ACLs)
4. Permission inheritance

Run with:
    python examples/permissions_demo.py
"""

import nexus
from nexus.core.acl import ACL, ACLManager, ACLPermission
from nexus.core.permissions import FileMode, FilePermissions, PermissionChecker


def demo_basic_permissions():
    """Demo basic UNIX-style permissions."""
    print("\n" + "=" * 60)
    print("1. BASIC UNIX-STYLE PERMISSIONS")
    print("=" * 60)

    # Connect to Nexus
    nx = nexus.connect()

    # Write a file
    nx.write("/workspace/project/code.py", b"print('Hello World')")
    print("✓ Created file: /workspace/project/code.py")

    # Get file metadata
    meta = nx.metadata.get("/workspace/project/code.py")

    # Set owner and permissions
    meta.owner = "alice"
    meta.group = "developers"
    meta.mode = 0o644  # rw-r--r--
    nx.metadata.put(meta)

    print("✓ Set permissions:")
    mode = FileMode(meta.mode)
    print(f"  Owner: {meta.owner}")
    print(f"  Group: {meta.group}")
    print(f"  Mode:  {oct(meta.mode)} ({mode})")

    nx.close()


def demo_permission_checking():
    """Demo permission checking."""
    print("\n" + "=" * 60)
    print("2. PERMISSION CHECKING")
    print("=" * 60)

    # Create file permissions
    perms = FilePermissions(
        owner="alice",
        group="developers",
        mode=FileMode(0o644),  # rw-r--r--
    )

    print(f"File permissions: {perms.mode} (owner=alice, group=developers)")
    print()

    # Check various users
    test_cases = [
        ("alice", [], "owner"),
        ("bob", ["developers"], "developer"),
        ("charlie", ["admins"], "outsider"),
    ]

    for user, groups, role in test_cases:
        can_read = perms.can_read(user, groups)
        can_write = perms.can_write(user, groups)
        can_execute = perms.can_execute(user, groups)

        print(f"{user} ({role}):")
        print(f"  Read:    {'✓' if can_read else '✗'}")
        print(f"  Write:   {'✓' if can_write else '✗'}")
        print(f"  Execute: {'✓' if can_execute else '✗'}")
        print()


def demo_mode_formats():
    """Demo different mode format conversions."""
    print("\n" + "=" * 60)
    print("3. MODE FORMAT CONVERSIONS")
    print("=" * 60)

    from nexus.core.permissions import parse_mode

    # Demonstrate different formats
    formats = [
        ("Octal", "755"),
        ("Octal with 0o prefix", "0o644"),
        ("Symbolic", "rwxr-xr-x"),
        ("Symbolic", "rw-r--r--"),
    ]

    for desc, mode_str in formats:
        mode_int = parse_mode(mode_str)
        mode_obj = FileMode(mode_int)
        print(f"{desc:25} {mode_str:15} → {oct(mode_int):8} → {mode_obj}")


def demo_acl_basics():
    """Demo ACL basics."""
    print("\n" + "=" * 60)
    print("4. ACCESS CONTROL LISTS (ACL)")
    print("=" * 60)

    # Create an empty ACL
    acl = ACL.empty()
    manager = ACLManager()

    print("Creating ACL for /workspace/secret.txt\n")

    # Grant permissions to specific users
    manager.grant_user(acl, "alice", read=True, write=True)
    print("✓ Granted alice: read, write")

    manager.grant_user(acl, "bob", read=True)
    print("✓ Granted bob: read")

    manager.grant_group(acl, "developers", read=True, execute=True)
    print("✓ Granted developers group: read, execute")

    # Deny access to specific user
    manager.deny_user(acl, "charlie")
    print("✓ Denied charlie: all access")

    print("\nACL Entries:")
    for entry_str in acl.to_strings():
        print(f"  {entry_str}")

    print("\nPermission Checks:")

    # Test permission checks
    checks = [
        ("alice", [], ACLPermission.READ),
        ("alice", [], ACLPermission.WRITE),
        ("bob", [], ACLPermission.WRITE),
        ("dave", ["developers"], ACLPermission.READ),
        ("charlie", [], ACLPermission.READ),
    ]

    for user, groups, perm in checks:
        result = acl.check_permission(user, groups, perm)
        status = "✓ Allowed" if result is True else "✗ Denied" if result is False else "→ No Match"
        groups_str = f"groups={groups}" if groups else ""
        print(f"  {user:10} {groups_str:20} {perm.value:8} : {status}")


def demo_acl_parsing():
    """Demo ACL parsing from strings."""
    print("\n" + "=" * 60)
    print("5. ACL PARSING FROM STRINGS")
    print("=" * 60)

    # Parse ACL from string format
    acl_strings = [
        "user:alice:rw-",
        "user:bob:r--",
        "group:developers:r-x",
        "deny:user:charlie:---",
    ]

    print("Parsing ACL from strings:")
    for s in acl_strings:
        print(f"  {s}")

    acl = ACL.from_strings(acl_strings)

    print("\nParsed ACL entries:")
    for entry in acl.entries:
        print(
            f"  Type: {entry.entry_type.value:10} ID: {entry.identifier or 'N/A':15} Perms: {entry.to_string()}"
        )


def demo_permission_checker():
    """Demo permission checker utility."""
    print("\n" + "=" * 60)
    print("6. PERMISSION CHECKER UTILITY")
    print("=" * 60)

    # Create a permission checker with defaults
    checker = PermissionChecker(default_owner="root", default_group="root")

    print("Default owner: root, Default group: root\n")

    # Create default permissions for a file
    file_perms = checker.create_default_permissions(owner="alice", group="developers")
    print(
        f"Default file permissions:      {file_perms.mode} (owner={file_perms.owner}, group={file_perms.group})"
    )

    # Create default permissions for a directory
    dir_perms = checker.create_default_permissions(
        owner="alice", group="developers", is_directory=True
    )
    print(
        f"Default directory permissions: {dir_perms.mode} (owner={dir_perms.owner}, group={dir_perms.group})"
    )

    # Check permissions
    print("\nPermission checks:")
    can_read = checker.check_read(file_perms, "alice", [])
    print(f"  Alice can read file: {can_read}")

    can_write = checker.check_write(file_perms, "bob", ["developers"])
    print(f"  Bob (developer) can write file: {can_write}")

    # Check with no permissions (backward compatible)
    can_read_no_perms = checker.check_read(None, "anyone", [])
    print(f"  Anyone can read file with no permissions: {can_read_no_perms}")


def demo_integrated_example():
    """Demo integrated permissions with Nexus filesystem."""
    print("\n" + "=" * 60)
    print("7. INTEGRATED EXAMPLE WITH NEXUS")
    print("=" * 60)

    nx = nexus.connect()

    # Create files with different permissions
    files = [
        ("/workspace/public/readme.txt", "alice", "users", 0o644, "Public file"),
        ("/workspace/private/secrets.txt", "alice", "admins", 0o600, "Private file"),
        ("/workspace/shared/data.csv", "bob", "developers", 0o664, "Shared file"),
    ]

    print("Creating files with permissions:\n")

    for path, owner, group, mode, desc in files:
        # Write file
        nx.write(path, f"{desc}\n".encode())

        # Set permissions
        meta = nx.metadata.get(path)
        meta.owner = owner
        meta.group = group
        meta.mode = mode
        nx.metadata.put(meta)

        mode_obj = FileMode(mode)
        print(f"  {path}")
        print(f"    Description: {desc}")
        print(f"    Owner: {owner}, Group: {group}, Mode: {mode_obj}")
        print()

    nx.close()


def main():
    """Run all permission demos."""
    print("\n╔═══════════════════════════════════════════════════════════╗")
    print("║     NEXUS FILE PERMISSIONS & ACL DEMONSTRATION            ║")
    print("╚═══════════════════════════════════════════════════════════╝")

    try:
        demo_basic_permissions()
        demo_permission_checking()
        demo_mode_formats()
        demo_acl_basics()
        demo_acl_parsing()
        demo_permission_checker()
        demo_integrated_example()

        print("\n" + "=" * 60)
        print("✓ All demos completed successfully!")
        print("=" * 60)
        print("\nNext steps:")
        print("  1. Try CLI commands: nexus chmod, chown, getfacl, setfacl")
        print("  2. View documentation: PERMISSIONS_IMPLEMENTATION.md")
        print("  3. Run tests: pytest tests/unit/test_permissions.py tests/unit/test_acl.py")
        print()

    except Exception as e:
        print(f"\n✗ Error: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    main()
