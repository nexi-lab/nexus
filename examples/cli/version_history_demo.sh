#!/bin/bash
# Version History Demo - Agent Attribution (Issue #362 Fix)
#
# Demonstrates: Version history shows WHO made changes (user vs agent)
#               instead of 'anonymous'
#
# Prerequisites: ./scripts/init-nexus-with-auth.sh && source .nexus-admin-env

set -e

G='\033[0;32m' B='\033[0;34m' C='\033[0;36m' M='\033[0;35m' Y='\033[1;33m' R='\033[0m'
[ -z "$NEXUS_URL" ] && echo "Run: source .nexus-admin-env" && exit 1

echo "╔══════════════════════════════════════════════════════════╗"
echo "║     Version History: Agent Attribution (Issue #362)     ║"
echo "║    Shows BOTH user operations AND agent operations      ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
echo -e "${B}ℹ${R} Server: $NEXUS_URL\n"

export FILE="/workspace/version-demo-doc.md"
export ADMIN_KEY="$NEXUS_API_KEY"

# Cleanup function
cleanup() {
    export NEXUS_API_KEY="$ADMIN_KEY"
    nexus rm -f $FILE 2>/dev/null || true

    # Delete agent
    python3 -c "
import sys, os
sys.path.insert(0, 'src')
from nexus.remote.client import RemoteNexusFS
nx = RemoteNexusFS('$NEXUS_URL', api_key='$ADMIN_KEY')
try:
    nx.delete_agent('demo_analyst')
except: pass
nx.close()
" 2>/dev/null || true
}

if [ "$KEEP" != "1" ]; then
    trap cleanup EXIT
else
    echo -e "${Y}⚠${R} KEEP=1 set - demo data will NOT be cleaned up\n"
fi

# Initial cleanup
cleanup 2>/dev/null || true

echo "════════════════════════════════════════════════════════════"
echo "  Setup: Create Agent"
echo "════════════════════════════════════════════════════════════"
echo ""

# Register agent for admin user
echo -e "${B}ℹ${R} Registering agent 'demo_analyst' for admin..."
python3 << 'REGISTER_AGENT'
import sys, os
sys.path.insert(0, 'src')
from nexus.remote.client import RemoteNexusFS

nexus_url = os.getenv('NEXUS_URL')
admin_key = os.getenv('ADMIN_KEY')
nx = RemoteNexusFS(nexus_url, api_key=admin_key)

# Register agent
agent = nx.register_agent(
    agent_id="demo_analyst",
    name="Demo Data Analyst",
    description="Analyzes data for version history demo"
)
print(f"✓ Registered agent: {agent['agent_id']}")
nx.close()
REGISTER_AGENT

# Create agent API key
echo -e "${B}ℹ${R} Creating API key for agent..."
AGENT_KEY=$(nexus admin create-agent-key admin demo_analyst --name "Demo Agent Key" --json-output 2>/dev/null | grep -o '"api_key": "[^"]*"' | cut -d'"' -f4)
export AGENT_KEY
echo -e "${G}✓${R} Created agent key: ${AGENT_KEY:0:20}...\n"

echo "════════════════════════════════════════════════════════════"
echo "  Creating File Versions (User vs Agent)"
echo "════════════════════════════════════════════════════════════"
echo ""

echo -e "${C}→${R} v1: Admin creates file (USER operation)"
export NEXUS_API_KEY="$ADMIN_KEY"
echo "# Document v1" | nexus write $FILE --input -

echo -e "${M}→${R} v2: Agent updates file (AGENT operation)"
export NEXUS_API_KEY="$AGENT_KEY"
echo -e "# Document v1\n\n## Agent Analysis" | nexus write $FILE --input -

echo -e "${C}→${R} v3: Admin updates (USER operation)"
export NEXUS_API_KEY="$ADMIN_KEY"
echo -e "# Document v1\n\n## Agent Analysis\n\n## Admin Review" | nexus write $FILE --input -

echo -e "${M}→${R} v4: Agent adds more (AGENT operation)"
export NEXUS_API_KEY="$AGENT_KEY"
echo -e "# Document v1\n\n## Agent Analysis\n\n## Admin Review\n\n## More Insights" | nexus write $FILE --input -

echo -e "${G}✓${R} Created 4 versions (2 user, 2 agent)\n"

# Reset to admin
export NEXUS_API_KEY="$ADMIN_KEY"

python3 << 'SHOW_VERSIONS'
import sys, os
sys.path.insert(0, 'src')
from nexus.remote.client import RemoteNexusFS

G, C, M, Y, R = '\033[0;32m', '\033[0;36m', '\033[0;35m', '\033[1;33m', '\033[0m'

nx = RemoteNexusFS(os.environ['NEXUS_URL'], api_key=os.environ['NEXUS_API_KEY'])
file_path = os.environ['FILE']
versions = nx.list_versions(file_path)

print("════════════════════════════════════════════════════════════")
print("  Version History - User vs Agent Operations")
print("════════════════════════════════════════════════════════════\n")

print(f"{'Ver':<6} {'Actor':<40} {'Type':<15} {'Size':<8}")
print("="*75)

for v in reversed(versions):
    cb = v.get('created_by', 'UNKNOWN')
    ver = f"v{v['version']}"
    size = f"{v['size']}B"

    # Determine operation type
    if 'agent:' in cb and 'user:' in cb:
        op_type = f"{M}Agent Op{R}"
        actor = f"{M}{cb}{R}"
    elif 'user:' in cb and 'agent:' not in cb:
        op_type = f"{C}User Op{R}"
        actor = f"{C}{cb}{R}"
    else:
        op_type = "Unknown"
        actor = f"{Y}{cb}{R}"

    print(f"{ver:<6} {actor:<49} {op_type:<24} {size}")

print("\n" + "="*75)
print(f"\n{G}✓ Issue #362 FIXED - Shows BOTH User AND Agent:{R}\n")

# Count user vs agent operations
user_ops = sum(1 for v in versions if 'user:' in v.get('created_by', '') and 'agent:' not in v.get('created_by', ''))
agent_ops = sum(1 for v in versions if 'agent:' in v.get('created_by', ''))

print(f"  {C}User operations:{R} {user_ops} versions show 'user:admin'")
print(f"  {M}Agent operations:{R} {agent_ops} versions show 'user:admin,agent:demo_analyst'")
print(f"\n  {G}Before fix:{R} All showed {Y}'anonymous'{R} - no way to distinguish!")
print(f"  {G}After fix:{R}  Clear visibility into WHO did WHAT\n")

print("════════════════════════════════════════════════════════════")
print("  Rollback Test (Admin performs rollback)")
print("════════════════════════════════════════════════════════════\n")

print("Rolling back to version 2 (agent operation)...")
nx.rollback(file_path, version=2)
print(f"{G}✓{R} Rollback complete\n")

versions = nx.list_versions(file_path)
print("Updated version history (showing latest 3):\n")

for v in versions[:3]:
    cb = v.get('created_by', 'UNKNOWN')
    st = v.get('source_type', 'original')
    ver = v['version']

    marker = f"{C}⟲{R}" if st == 'rollback' else " "

    # Color code by type
    if 'agent:' in cb and 'user:' in cb:
        cb_color = f"{M}{cb}{R}"
        type_label = "Agent Op"
    elif 'user:' in cb:
        cb_color = f"{C}{cb}{R}"
        type_label = "User Op"
    else:
        cb_color = f"{Y}{cb}{R}"
        type_label = "Unknown"

    print(f"  {marker} v{ver}: {cb_color} ({type_label})")
    print(f"      Source: {st}")
    if v.get('change_reason'):
        print(f"      Reason: {v['change_reason']}")
    print()

print("="*75)
print(f"\n{G}✅ ISSUE #362 COMPLETELY FIXED!{R}\n")
print("  ✅ Version history distinguishes USER vs AGENT operations")
print(f"  ✅ User ops show: {C}'user:admin'{R}")
print(f"  ✅ Agent ops show: {M}'user:admin,agent:demo_analyst'{R}")
print("  ✅ Rollbacks track who performed them")
print("  ✅ Complete audit trail - NO MORE 'anonymous'!\n")

print(f"{C}Technical Implementation:{R}")
print("  • Enhanced _get_created_by() method (src/nexus/core/nexus_fs.py:430-464)")
print("  • Extracts BOTH user AND agent from OperationContext")
print("  • Returns combined format when both are available")
print("  • Applied to write operations AND rollbacks\n")

nx.close()
SHOW_VERSIONS

echo -e "${G}✓${R} Demo completed!"
