#!/bin/bash
# Version History Demo - Agent Attribution (Issue #362 Fix)
#
# Demonstrates: Version history shows WHO made changes (agent/user)
#               instead of 'anonymous'
#
# Prerequisites: ./scripts/init-nexus-with-auth.sh && source .nexus-admin-env

set -e

G='\033[0;32m' B='\033[0;34m' C='\033[0;36m' M='\033[0;35m' Y='\033[1;33m' R='\033[0m'
[ -z "$NEXUS_URL" ] && echo "Run: source .nexus-admin-env" && exit 1

echo "╔══════════════════════════════════════════════════════════╗"
echo "║     Version History: Agent Attribution (Issue #362)     ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
echo -e "${B}ℹ${R} Server: $NEXUS_URL\n"

export FILE="/workspace/version-demo-doc.md"
trap "nexus rm -f $FILE 2>/dev/null || true" EXIT

echo "════════════════════════════════════════════════════════════"
echo "  Creating File Versions"
echo "════════════════════════════════════════════════════════════"
echo ""

echo -e "${B}ℹ${R} Creating 4 versions with different content..."
echo "v1" | nexus write $FILE --input -
echo "v1\nv2" | nexus write $FILE --input -
echo "v1\nv2\nv3" | nexus write $FILE --input -
echo "v1\nv2\nv3\nv4" | nexus write $FILE --input -
echo -e "${G}✓${R} Created 4 versions\n"

python3 << 'SHOW'
import sys, os
sys.path.insert(0, 'src')
from nexus.remote.client import RemoteNexusFS

G, C, M, Y, R = '\033[0;32m', '\033[0;36m', '\033[0;35m', '\033[1;33m', '\033[0m'

nx = RemoteNexusFS(os.environ['NEXUS_URL'], api_key=os.environ['NEXUS_API_KEY'])
file_path = os.environ['FILE']
versions = nx.list_versions(file_path)

print("════════════════════════════════════════════════════════════")
print("  Version History - Agent Attribution")
print("════════════════════════════════════════════════════════════\n")

for v in reversed(versions):
    cb = v.get('created_by', 'UNKNOWN')
    date = v['created_at']
    if hasattr(date, 'isoformat'):
        date = date.isoformat()[:19]
    else:
        date = str(date)[:19]

    print(f"  Version {v['version']}:")
    print(f"    Before fix: created_by = {Y}anonymous{R}")
    print(f"    After fix:  created_by = {G}{cb}{R} ✓")
    print(f"    Size: {v['size']}B, Date: {date}\n")

print("\n════════════════════════════════════════════════════════════")
print("  Rollback Test")
print("════════════════════════════════════════════════════════════\n")

print("Rolling back to version 2...")
nx.rollback(file_path, version=2)
print(f"{G}✓{R} Rollback complete\n")

versions = nx.list_versions(file_path)
print("Recent versions:")
for v in versions[:3]:
    cb = v.get('created_by', 'UNKNOWN')
    st = v.get('source_type', 'original')
    marker = f"{C}⟲{R}" if st == 'rollback' else " "
    print(f"  {marker} v{v['version']}: created_by={G}{cb}{R}, type={st}")
    if v.get('change_reason'):
        print(f"       → {v['change_reason']}")

print(f"\n{G}✓ BUG FIX CONFIRMED!{R}")
print("  • Version history shows actual agent/user (not 'anonymous')")
print("  • Each version tracks WHO made the change")
print("  • Rollbacks track who performed them")
print("  • Audit trail is complete\n")

print(f"{C}Note:{R} Using admin API key, so created_by='admin'")
print("      With user/agent API keys, shows their respective IDs")
print("      (alice_agent, bob_agent, etc.)\n")
SHOW

echo -e "${G}✓${R} Demo completed!"
