"""ACL (Access Control List) Demo - Python API

Demonstrates the new ACL Python API for fine-grained file permissions:
- Grant per-user permissions
- Grant per-group permissions
- Explicit deny rules
- View and revoke ACL entries

Run: NEXUS_DATA_DIR=/tmp/acl-demo python examples/py_demo/acl_demo.py
"""

import os
import tempfile
from pathlib import Path

from nexus.backends.local import LocalBackend
from nexus.core.nexus_fs import NexusFS


def main():
    # Setup temporary workspace
    tmpdir = os.environ.get("NEXUS_DATA_DIR", tempfile.mkdtemp())
    print(f"Demo workspace: {tmpdir}\n")

    # Initialize NexusFS
    nx = NexusFS(
        backend=LocalBackend(root_path=tmpdir),
        db_path=Path(tmpdir) / "metadata.db",
        enforce_permissions=True,
    )

    print("=" * 70)
    print("ACL Demo - Fine-Grained Access Control")
    print("=" * 70)

    # Create test files
    print("\n📝 Creating test files...")
    nx.write("/workspace/public-doc.txt", b"This is a public document")
    nx.write("/workspace/team-doc.txt", b"This is a team document")
    nx.write("/workspace/secret.txt", b"This is secret data")
    print("✅ Created 3 test files")

    # Scenario 1: Grant individual user permissions
    print("\n" + "=" * 70)
    print("Scenario 1: Grant User Permissions")
    print("=" * 70)

    print("\n→ Granting alice read+write access to public-doc.txt")
    nx.grant_user("/workspace/public-doc.txt", user="alice", permissions="rw-")

    print("→ Granting bob read-only access to public-doc.txt")
    nx.grant_user("/workspace/public-doc.txt", user="bob", permissions="r--")

    acl = nx.get_acl("/workspace/public-doc.txt")
    print(f"\n✅ ACL for public-doc.txt ({len(acl)} entries):")
    for entry in acl:
        perms = entry["permissions"]
        print(f"   {entry['entry_type']:5} {entry['identifier']:15} {perms}")

    # Scenario 2: Grant group permissions
    print("\n" + "=" * 70)
    print("Scenario 2: Grant Group Permissions")
    print("=" * 70)

    print("\n→ Granting 'developers' group rw- access to team-doc.txt")
    nx.grant_group("/workspace/team-doc.txt", group="developers", permissions="rw-")

    print("→ Granting 'viewers' group r-- access to team-doc.txt")
    nx.grant_group("/workspace/team-doc.txt", group="viewers", permissions="r--")

    acl = nx.get_acl("/workspace/team-doc.txt")
    print(f"\n✅ ACL for team-doc.txt ({len(acl)} entries):")
    for entry in acl:
        perms = entry["permissions"]
        print(f"   {entry['entry_type']:5} {entry['identifier']:15} {perms}")

    # Scenario 3: Explicit deny (takes precedence)
    print("\n" + "=" * 70)
    print("Scenario 3: Explicit Deny (Highest Priority)")
    print("=" * 70)

    print("\n→ First, grant intern read access to secret.txt")
    nx.grant_user("/workspace/secret.txt", user="intern", permissions="r--")

    print("→ Now, explicitly DENY intern access (overrides previous grant)")
    nx.deny_user("/workspace/secret.txt", user="intern")

    acl = nx.get_acl("/workspace/secret.txt")
    print(f"\n✅ ACL for secret.txt ({len(acl)} entries):")
    for entry in acl:
        deny_marker = " [DENY - blocks all access]" if entry["deny"] else ""
        perms = entry["permissions"]
        print(f"   {entry['entry_type']:5} {entry['identifier']:15} {perms}{deny_marker}")

    # Scenario 4: Revoke permissions
    print("\n" + "=" * 70)
    print("Scenario 4: Revoke Permissions")
    print("=" * 70)

    print("\n→ Current ACL for public-doc.txt:")
    acl = nx.get_acl("/workspace/public-doc.txt")
    for entry in acl:
        print(f"   {entry['entry_type']:5} {entry['identifier']:15} {entry['permissions']}")

    print("\n→ Revoking bob's access...")
    nx.revoke_acl("/workspace/public-doc.txt", entry_type="user", identifier="bob")

    print("\n✅ ACL after revoke:")
    acl = nx.get_acl("/workspace/public-doc.txt")
    for entry in acl:
        print(f"   {entry['entry_type']:5} {entry['identifier']:15} {entry['permissions']}")

    # Scenario 5: Mixed user and group permissions
    print("\n" + "=" * 70)
    print("Scenario 5: Complex ACL (Users + Groups + Deny)")
    print("=" * 70)

    nx.write("/workspace/project.txt", b"Project data")
    print("\n→ Setting up complex ACL on project.txt:")

    print("   • Grant 'team-lead' group full access (rwx)")
    nx.grant_group("/workspace/project.txt", group="team-lead", permissions="rwx")

    print("   • Grant 'developers' group read+write (rw-)")
    nx.grant_group("/workspace/project.txt", group="developers", permissions="rw-")

    print("   • Grant alice special execute permission (--x)")
    nx.grant_user("/workspace/project.txt", user="alice", permissions="--x")

    print("   • DENY contractor access")
    nx.deny_user("/workspace/project.txt", user="contractor")

    acl = nx.get_acl("/workspace/project.txt")
    print(f"\n✅ Final ACL for project.txt ({len(acl)} entries):")
    for entry in acl:
        deny_marker = " [DENY]" if entry["deny"] else ""
        perms = entry["permissions"]
        print(f"   {entry['entry_type']:5} {entry['identifier']:15} {perms}{deny_marker}")

    # Summary
    print("\n" + "=" * 70)
    print("Summary - ACL Permission Checking Order")
    print("=" * 70)
    print("""
When checking access, Nexus uses this order:
1. DENY entries (explicit denies take precedence)
2. ALLOW entries (grants from ACL)
3. UNIX permissions (owner/group/other mode bits)

ACL entries complement UNIX permissions by allowing:
✓ Per-user granular control
✓ Per-group granular control
✓ Explicit deny rules
✓ Multiple users/groups with different permissions

Use Cases:
• Share file with specific users without changing UNIX perms
• Temporarily grant access to external contractors
• Block specific users while allowing group access
• Mix team and individual permissions flexibly
""")

    nx.close()
    print("=" * 70)
    print("✅ Demo Complete!")
    print("=" * 70)


if __name__ == "__main__":
    main()
