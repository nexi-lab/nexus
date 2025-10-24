#!/bin/bash
# ACL (Access Control List) Demo - CLI + Python API
#
# Demonstrates ACL functionality using both CLI commands and Python API
# Run: ./examples/script_demo/acl_demo.sh

set -e  # Exit on error

# Setup
export NEXUS_DATA_DIR=/tmp/acl-demo-$$
mkdir -p "$NEXUS_DATA_DIR"

echo "======================================================================"
echo "ACL Demo - Fine-Grained File Permissions"
echo "======================================================================"
echo "Workspace: $NEXUS_DATA_DIR"
echo

# Initialize workspace
nexus init

echo "======================================================================"
echo "Part 1: CLI Commands (nexus setfacl / getfacl)"
echo "======================================================================"
echo

# Create test files
echo "📝 Creating test files..."
nexus write /workspace/document.txt "Shared document content"
nexus write /workspace/secret.txt "Secret data"
echo "✅ Created files"
echo

# Set ACL via CLI
echo "→ Granting alice read+write via CLI:"
nexus setfacl user:alice:rw- /workspace/document.txt

echo "→ Granting bob read-only via CLI:"
nexus setfacl user:bob:r-- /workspace/document.txt

echo "→ Denying intern access via CLI:"
nexus setfacl deny:user:intern:--- /workspace/secret.txt

echo
echo "✅ View ACL for document.txt:"
nexus getfacl /workspace/document.txt

echo
echo "✅ View ACL for secret.txt:"
nexus getfacl /workspace/secret.txt

echo
echo "→ Removing bob's access:"
nexus setfacl user:bob:r-- /workspace/document.txt --remove

echo
echo "✅ ACL after removal:"
nexus getfacl /workspace/document.txt

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

print("→ Creating project file...")
nx.write("/workspace/project.txt", b"Project data")

print("→ Granting team-lead full access via Python:")
nx.grant_group("/workspace/project.txt", group="team-lead", permissions="rwx")

print("→ Granting developers read+write via Python:")
nx.grant_group("/workspace/project.txt", group="developers", permissions="rw-")

print("→ Granting alice execute permission via Python:")
nx.grant_user("/workspace/project.txt", user="alice", permissions="--x")

print("\n✅ ACL entries from Python API:")
acl = nx.get_acl("/workspace/project.txt")
for entry in acl:
    print(f"   {entry['entry_type']:5} {entry['identifier']:15} {entry['permissions']}")

print("\n→ Revoking alice's permission via Python:")
nx.revoke_acl("/workspace/project.txt", "user", "alice")

print("\n✅ ACL after revoke:")
acl = nx.get_acl("/workspace/project.txt")
for entry in acl:
    print(f"   {entry['entry_type']:5} {entry['identifier']:15} {entry['permissions']}")

nx.close()
PYTHON_EOF

echo
echo "======================================================================"
echo "Summary - ACL Use Cases"
echo "======================================================================"
cat << 'SUMMARY'

ACL provides fine-grained access control beyond UNIX permissions:

✓ Per-User Control:
  - Grant alice read, bob write, charlie execute
  - Different permissions for each user

✓ Per-Group Control:
  - Grant "developers" read+write
  - Grant "viewers" read-only

✓ Explicit Deny:
  - Deny specific users (takes precedence)
  - Block contractors while allowing team

✓ Flexible Sharing:
  - Share file without changing ownership
  - Temporary access grants
  - Mix user and group permissions

CLI Commands:
  nexus setfacl user:alice:rw- /file.txt   # Grant user
  nexus setfacl group:devs:r-x /file.txt   # Grant group
  nexus setfacl deny:user:bob /file.txt    # Deny user
  nexus getfacl /file.txt                  # View ACL
  nexus setfacl ... /file.txt --remove     # Remove entry

Python API:
  nx.grant_user(path, user, permissions)
  nx.grant_group(path, group, permissions)
  nx.deny_user(path, user)
  nx.revoke_acl(path, type, identifier)
  nx.get_acl(path)

SUMMARY

# Cleanup
echo
echo "======================================================================"
echo "✅ Demo Complete! Cleaning up..."
echo "======================================================================"
rm -rf "$NEXUS_DATA_DIR"
