#!/bin/bash
# ReBAC (Relationship-Based Access Control) Demo - CLI + Python API
#
# Demonstrates ReBAC functionality using both CLI commands and Python API
# Run: ./examples/script_demo/rebac_demo.sh

set -e  # Exit on error

# Setup
export NEXUS_DATA_DIR=/tmp/rebac-demo-$$
mkdir -p "$NEXUS_DATA_DIR"

echo "======================================================================"
echo "ReBAC Demo - Relationship-Based Permissions"
echo "======================================================================"
echo "Workspace: $NEXUS_DATA_DIR"
echo

# Initialize workspace
nexus init

echo "======================================================================"
echo "Part 1: CLI Commands (nexus rebac)"
echo "======================================================================"
echo

# Create relationships via CLI
echo "→ Creating team structure via CLI..."
echo "   • alice is member-of developers"
nexus rebac create agent alice member-of group developers

echo "   • bob is member-of developers"
nexus rebac create agent bob member-of group developers

echo "   • developers owns /workspace/project.txt"
nexus rebac create group developers owner-of file /workspace/project.txt

echo
echo "→ Checking permissions via CLI..."
echo "   • Does alice have owner-of on project.txt?"
if nexus rebac check agent alice owner-of file /workspace/project.txt; then
    echo "     ✅ YES (inherited via developers group)"
else
    echo "     ❌ NO"
fi

echo
echo "→ Finding who has owner-of permission on project.txt:"
nexus rebac expand owner-of file /workspace/project.txt

echo
echo "======================================================================"
echo "Part 2: Python API (programmatic access)"
echo "======================================================================"
echo

# Use Python API
uv run python << 'PYTHON_EOF'
from pathlib import Path
from nexus.backends.local import LocalBackend
from nexus.core.nexus_fs import NexusFS
import os

tmpdir = os.environ["NEXUS_DATA_DIR"]
nx = NexusFS(
    backend=LocalBackend(root_path=tmpdir + "/nexus-data"),
    db_path=Path(tmpdir) / "nexus-data/metadata.db",
    enforce_permissions=True
)

print("→ Creating hierarchical relationships via Python...")
print("   • charlie member-of eng-team")
tuple1 = nx.rebac_create(
    subject=("agent", "charlie"),
    relation="member-of",
    object=("group", "eng-team")
)

print("   • eng-team part-of engineering-dept")
tuple2 = nx.rebac_create(
    subject=("group", "eng-team"),
    relation="part-of",
    object=("department", "engineering-dept")
)

print("   • engineering-dept owns /workspace/eng/")
tuple3 = nx.rebac_create(
    subject=("department", "engineering-dept"),
    relation="owner-of",
    object=("folder", "/workspace/eng/")
)

print("\n→ Multi-level permission check:")
print("   charlie → member-of → eng-team")
print("             eng-team → part-of → engineering-dept")
print("                        engineering-dept → owner-of → /workspace/eng/")

can_access = nx.rebac_check(
    subject=("agent", "charlie"),
    permission="owner-of",
    object=("folder", "/workspace/eng/")
)
print(f"\n   ✅ charlie has owner-of permission? {can_access}")
print("      (inherited via 3-level relationship chain!)")

print("\n→ Listing all relationships for charlie:")
rels = nx.rebac_list_tuples(subject=("agent", "charlie"))
for rel in rels:
    print(f"   {rel['subject_type']}:{rel['subject_id']} -[{rel['relation']}]-> "
          f"{rel['object_type']}:{rel['object_id']}")

print("\n→ Finding all subjects with owner-of on /workspace/eng/:")
subjects = nx.rebac_expand(
    permission="owner-of",
    object=("folder", "/workspace/eng/")
)
for subj_type, subj_id in subjects:
    print(f"   • {subj_type}:{subj_id}")

print(f"\n→ Deleting charlie's membership...")
deleted = nx.rebac_delete(tuple1)
print(f"   Deleted? {deleted}")

can_access_after = nx.rebac_check(
    subject=("agent", "charlie"),
    permission="owner-of",
    object=("folder", "/workspace/eng/")
)
print(f"\n   charlie still has access? {can_access_after}")
print("   ✅ Permission revoked when relationship deleted!")

nx.close()
PYTHON_EOF

echo
echo "======================================================================"
echo "Part 3: Zanzibar-Style Graph Traversal"
echo "======================================================================"
echo

# Complex example
uv run python << 'PYTHON_EOF'
from pathlib import Path
from nexus.backends.local import LocalBackend
from nexus.core.nexus_fs import NexusFS
from datetime import UTC, datetime, timedelta
import os

tmpdir = os.environ["NEXUS_DATA_DIR"]
nx = NexusFS(
    backend=LocalBackend(root_path=tmpdir + "/nexus-data"),
    db_path=Path(tmpdir) / "nexus-data/metadata.db",
    enforce_permissions=True
)

print("→ Creating Google Zanzibar-style permission graph...")
print()
print("   Organization Structure:")
print("   ├─ alice ─[member-of]→ eng-team")
print("   ├─ bob ─[member-of]→ eng-team")
print("   ├─ eng-team ─[editor-of]→ document")
print("   ├─ charlie ─[viewer-of]→ document (direct)")
print("   └─ contractor ─[viewer-of]→ document (expires 1h)")
print()

# Build the graph
nx.rebac_create(
    subject=("agent", "alice"),
    relation="member-of",
    object=("group", "eng-team")
)
nx.rebac_create(
    subject=("agent", "bob"),
    relation="member-of",
    object=("group", "eng-team")
)
nx.rebac_create(
    subject=("group", "eng-team"),
    relation="editor-of",
    object=("document", "design-doc")
)
nx.rebac_create(
    subject=("agent", "charlie"),
    relation="viewer-of",
    object=("document", "design-doc")
)

# Temporary access
expires = datetime.now(UTC) + timedelta(hours=1)
nx.rebac_create(
    subject=("agent", "contractor"),
    relation="viewer-of",
    object=("document", "design-doc"),
    expires_at=expires
)

print("→ Permission Checks:")
for person in ["alice", "bob", "charlie", "contractor"]:
    can_edit = nx.rebac_check(
        subject=("agent", person),
        permission="editor-of",
        object=("document", "design-doc")
    )
    can_view = nx.rebac_check(
        subject=("agent", person),
        permission="viewer-of",
        object=("document", "design-doc")
    )
    edit_str = "✓ editor" if can_edit else "✗ not editor"
    view_str = "✓ viewer" if can_view else "✗ not viewer"
    print(f"   {person:12} {edit_str:15} {view_str}")

print("\n→ Expand who can edit document:")
editors = nx.rebac_expand(
    permission="editor-of",
    object=("document", "design-doc")
)
for subj_type, subj_id in editors:
    print(f"   • {subj_type}:{subj_id}")

print("\n→ Expand who can view document:")
viewers = nx.rebac_expand(
    permission="viewer-of",
    object=("document", "design-doc")
)
for subj_type, subj_id in viewers:
    print(f"   • {subj_type}:{subj_id}")

nx.close()
PYTHON_EOF

echo
echo "======================================================================"
echo "Summary - ReBAC Benefits"
echo "======================================================================"
cat << 'SUMMARY'

ReBAC enables dynamic, graph-based permissions:

✓ Automatic Inheritance:
  - Add user to group → automatically gets group's permissions
  - No need to grant permissions per file

✓ Hierarchical Structures:
  - Teams within departments
  - Folders containing files
  - Multi-level organizations

✓ Relationship Types:
  - member-of (group membership)
  - owner-of (ownership)
  - viewer-of (read access)
  - editor-of (write access)
  - parent-of (hierarchy)
  - part-of (organization)

✓ Scalability:
  - Grant access to 1000 files by adding to 1 group
  - vs ACL: 1000 individual grant commands

✓ Temporary Access:
  - Time-limited permissions
  - Auto-expire after deadline

CLI Commands:
  nexus rebac create <subj-type> <subj-id> <relation> <obj-type> <obj-id>
  nexus rebac check <subj-type> <subj-id> <permission> <obj-type> <obj-id>
  nexus rebac expand <permission> <obj-type> <obj-id>
  nexus rebac delete <tuple-id>

Python API:
  nx.rebac_create(subject, relation, object, expires_at)
  nx.rebac_check(subject, permission, object) -> bool
  nx.rebac_expand(permission, object) -> list
  nx.rebac_delete(tuple_id) -> bool
  nx.rebac_list_tuples(subject, relation, object) -> list

Based on Google Zanzibar design for billion-user scale!

SUMMARY

# Cleanup
echo
echo "======================================================================"
echo "✅ Demo Complete! Cleaning up..."
echo "======================================================================"
rm -rf "$NEXUS_DATA_DIR"
