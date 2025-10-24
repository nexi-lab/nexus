"""ReBAC (Relationship-Based Access Control) Demo - Python API

Demonstrates the new ReBAC Python API for graph-based permissions:
- Create relationship tuples
- Check permissions via graph traversal
- Find all subjects with permission (expand)
- List and delete relationships
- Hierarchical permission inheritance

Run: NEXUS_DATA_DIR=/tmp/rebac-demo python examples/py_demo/rebac_demo.py
"""

import os
import tempfile
from datetime import UTC, datetime, timedelta
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
    print("ReBAC Demo - Relationship-Based Access Control")
    print("=" * 70)

    # Scenario 1: Team membership and ownership
    print("\n" + "=" * 70)
    print("Scenario 1: Team Membership & File Ownership")
    print("=" * 70)

    print("\n→ Creating team structure...")
    print("   • alice is member-of developers")
    tuple1 = nx.rebac_create(
        subject=("agent", "alice"), relation="member-of", object=("group", "developers")
    )

    print("   • bob is member-of developers")

    print("   • developers group owns /workspace/project.txt")

    print("\n→ Checking permissions via graph traversal...")
    alice_can_own = nx.rebac_check(
        subject=("agent", "alice"),
        permission="owner-of",
        object=("file", "/workspace/project.txt"),
    )
    print(f"   • alice can own project.txt? {alice_can_own} (via developers group)")

    bob_can_own = nx.rebac_check(
        subject=("agent", "bob"),
        permission="owner-of",
        object=("file", "/workspace/project.txt"),
    )
    print(f"   • bob can own project.txt? {bob_can_own} (via developers group)")

    # Show how permission is inherited
    print("\n✅ Permission Inheritance:")
    print("   alice → member-of → developers → owner-of → project.txt")
    print("   Therefore: alice inherits owner-of permission!")

    # Scenario 2: Finding all subjects with permission
    print("\n" + "=" * 70)
    print("Scenario 2: Expand - Who Has Access?")
    print("=" * 70)

    print("\n→ Creating more relationships...")
    nx.rebac_create(
        subject=("agent", "charlie"),
        relation="viewer-of",
        object=("file", "/workspace/project.txt"),
    )

    print("\n→ Finding all subjects with 'owner-of' permission on project.txt...")
    subjects = nx.rebac_expand(permission="owner-of", object=("file", "/workspace/project.txt"))

    print(f"\n✅ Found {len(subjects)} subject(s) with owner-of permission:")
    for subj_type, subj_id in subjects:
        print(f"   • {subj_type}:{subj_id}")

    print("\n→ Finding all subjects with 'viewer-of' permission on project.txt...")
    subjects = nx.rebac_expand(permission="viewer-of", object=("file", "/workspace/project.txt"))

    print(f"\n✅ Found {len(subjects)} subject(s) with viewer-of permission:")
    for subj_type, subj_id in subjects:
        print(f"   • {subj_type}:{subj_id}")

    # Scenario 3: Hierarchical relationships
    print("\n" + "=" * 70)
    print("Scenario 3: Hierarchical Permissions (Folders → Files)")
    print("=" * 70)

    print("\n→ Creating folder hierarchy...")
    print("   • developers owns /workspace/ (parent folder)")
    nx.rebac_create(
        subject=("group", "developers"),
        relation="owner-of",
        object=("folder", "/workspace/"),
    )

    print("   • /workspace/code.py is child of /workspace/")
    nx.rebac_create(
        subject=("folder", "/workspace/"),
        relation="parent-of",
        object=("file", "/workspace/code.py"),
    )

    print("\n→ Checking inherited permissions...")
    alice_folder_owner = nx.rebac_check(
        subject=("agent", "alice"),
        permission="owner-of",
        object=("folder", "/workspace/"),
    )
    print(f"   • alice owns /workspace/? {alice_folder_owner}")

    print("\n✅ Hierarchical Permission Flow:")
    print("   alice → member-of → developers")
    print("   developers → owner-of → /workspace/ (folder)")
    print("   /workspace/ → parent-of → /workspace/code.py")
    print("   Result: alice indirectly controls code.py via folder ownership")

    # Scenario 4: Temporary access with expiration
    print("\n" + "=" * 70)
    print("Scenario 4: Temporary Access (Time-Limited)")
    print("=" * 70)

    expires_in_1_hour = datetime.now(UTC) + timedelta(hours=1)
    print("\n→ Granting contractor temporary viewer access (expires in 1 hour)")
    temp_tuple = nx.rebac_create(
        subject=("agent", "contractor"),
        relation="viewer-of",
        object=("file", "/workspace/sensitive.txt"),
        expires_at=expires_in_1_hour,
    )

    print(f"   • Created temporary relationship (ID: {temp_tuple[:8]}...)")
    print(f"   • Expires at: {expires_in_1_hour.strftime('%Y-%m-%d %H:%M:%S')} UTC")

    can_view_now = nx.rebac_check(
        subject=("agent", "contractor"),
        permission="viewer-of",
        object=("file", "/workspace/sensitive.txt"),
    )
    print(f"   • contractor can view now? {can_view_now}")

    print("\n✅ After expiration time, permission will automatically be denied")

    # Scenario 5: Listing and deleting relationships
    print("\n" + "=" * 70)
    print("Scenario 5: List & Delete Relationships")
    print("=" * 70)

    print("\n→ Listing all relationships for alice:")
    alice_rels = nx.rebac_list_tuples(subject=("agent", "alice"))
    print(f"   • Found {len(alice_rels)} relationship(s):")
    for rel in alice_rels:
        print(
            f"     {rel['subject_type']}:{rel['subject_id']} -[{rel['relation']}]-> "
            f"{rel['object_type']}:{rel['object_id']}"
        )

    print("\n→ Listing all 'owner-of' relationships:")
    owner_rels = nx.rebac_list_tuples(relation="owner-of")
    print(f"   • Found {len(owner_rels)} relationship(s):")
    for rel in owner_rels:
        print(
            f"     {rel['subject_type']}:{rel['subject_id']} -[{rel['relation']}]-> "
            f"{rel['object_type']}:{rel['object_id']}"
        )

    print(f"\n→ Deleting alice's team membership (tuple ID: {tuple1[:8]}...)...")
    deleted = nx.rebac_delete(tuple1)
    print(f"   • Deleted? {deleted}")

    print("\n→ Checking alice's permission after membership deleted...")
    alice_still_owner = nx.rebac_check(
        subject=("agent", "alice"),
        permission="owner-of",
        object=("file", "/workspace/project.txt"),
    )
    print(f"   • alice can still own project.txt? {alice_still_owner}")
    print("   ✅ Permission revoked when relationship deleted!")

    # Scenario 6: Complex organization hierarchy
    print("\n" + "=" * 70)
    print("Scenario 6: Complex Organization (Multi-Level)")
    print("=" * 70)

    print("\n→ Building org hierarchy...")
    print("   • alice member-of eng-team")
    nx.rebac_create(subject=("agent", "alice"), relation="member-of", object=("group", "eng-team"))

    print("   • eng-team part-of engineering-dept")
    nx.rebac_create(
        subject=("group", "eng-team"),
        relation="part-of",
        object=("department", "engineering-dept"),
    )

    print("   • engineering-dept owns /workspace/eng/")
    nx.rebac_create(
        subject=("department", "engineering-dept"),
        relation="owner-of",
        object=("folder", "/workspace/eng/"),
    )

    print("\n✅ Multi-Level Permission Chain:")
    print("   alice → member-of → eng-team")
    print("           eng-team → part-of → engineering-dept")
    print("                      engineering-dept → owner-of → /workspace/eng/")
    print("\n   Result: alice gets access via 3-level relationship chain!")

    # Summary
    print("\n" + "=" * 70)
    print("Summary - ReBAC vs ACL vs UNIX")
    print("=" * 70)
    print("""
Permission System Comparison:

UNIX Permissions (owner/group/mode):
✓ Simple and fast
✓ Good for basic access control
✗ Static - no dynamic relationships

ACL (Access Control Lists):
✓ Per-user/group granular control
✓ Explicit deny rules
✗ Manual - must grant per file

ReBAC (Relationship-Based):
✓ Dynamic permission inheritance
✓ Automatic via graph relationships
✓ Scales to complex org structures
✓ Supports hierarchies and groups
✗ More complex to set up

Use Cases for ReBAC:
• Team-based access (member-of group)
• Hierarchical folders (parent-of relationships)
• Organization structures (dept owns resources)
• Temporary access (time-limited permissions)
• Dynamic sharing (add to group = auto access)
• Multi-level inheritance (teams within departments)
""")

    nx.close()
    print("=" * 70)
    print("✅ Demo Complete!")
    print("=" * 70)


if __name__ == "__main__":
    main()
