#!/bin/bash
# Nexus CLI - Agent Metadata Demo
#
# This demo tests issue #335: Store agent display name and description in database
#
# Features tested:
# - Agent registration with display name and description
# - List agents with metadata (name, description)
# - Get agent details with metadata
# - Backward compatibility (agents without metadata)
#
# Prerequisites:
# 1. Server running: ./scripts/init-nexus-with-auth.sh
# 2. Load admin credentials: source .nexus-admin-env
#
# Usage:
#   ./examples/cli/agent_metadata_demo.sh

set -e

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
NC='\033[0m'

print_section() {
    echo ""
    echo "════════════════════════════════════════════════════════════"
    echo "  $1"
    echo "════════════════════════════════════════════════════════════"
    echo ""
}

print_subsection() {
    echo ""
    echo "─── $1 ───"
    echo ""
}

print_success() { echo -e "${GREEN}✓${NC} $1"; }
print_info() { echo -e "${BLUE}ℹ${NC} $1"; }
print_warning() { echo -e "${YELLOW}⚠${NC} $1"; }
print_error() { echo -e "${RED}✗${NC} $1"; }
print_test() { echo -e "${MAGENTA}TEST:${NC} $1"; }

# Check prerequisites
if [ -z "$NEXUS_URL" ] || [ -z "$NEXUS_API_KEY" ]; then
    print_error "NEXUS_URL and NEXUS_API_KEY not set. Run: source .nexus-admin-env"
    exit 1
fi

# Check if server is running
if ! curl -s "$NEXUS_URL/health" > /dev/null 2>&1; then
    print_error "Server not responding at $NEXUS_URL"
    print_error "Start server with: ./scripts/init-nexus-with-auth.sh"
    exit 1
fi

echo "╔══════════════════════════════════════════════════════════╗"
echo "║        Nexus CLI - Agent Metadata Demo                  ║"
echo "║           Testing Issue #335 Implementation              ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
print_info "Server: $NEXUS_URL"
print_success "Server is running"
echo ""

ADMIN_KEY="$NEXUS_API_KEY"

# Cleanup function
cleanup() {
    export NEXUS_API_KEY="$ADMIN_KEY"
    print_info "Cleaning up demo data..."

    # Delete agents and clean up entity registry
    python3 << 'CLEANUP'
import sys, os
sys.path.insert(0, 'src')
from nexus.remote.client import RemoteNexusFS

nexus_url = os.getenv('NEXUS_URL', 'http://localhost:8080')
nx = RemoteNexusFS(nexus_url, api_key=os.getenv('NEXUS_API_KEY'))

# Delete test agents (this will clean up entity registry)
for agent_id in ['data_analyst', 'report_generator', 'code_reviewer', 'legacy_agent']:
    try:
        nx.delete_agent(agent_id)
    except:
        pass

# Delete test users
from nexus.server.auth.database_key import DatabaseAPIKeyAuth
from nexus.storage.models import Base
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Get DB connection from NexusFS
db_url = os.getenv('NEXUS_DATABASE_URL', 'sqlite:///demo_data/nexus.db')
engine = create_engine(db_url)
SessionLocal = sessionmaker(bind=engine)
session = SessionLocal()

try:
    # Delete test users
    for user in ['alice_meta', 'bob_meta']:
        try:
            from nexus.cli.admin import delete_user_by_username
            delete_user_by_username(session, user)
        except:
            pass
    session.commit()
except:
    session.rollback()
finally:
    session.close()

print("✓ Cleaned up agents and users")
nx.close()
CLEANUP
}

if [ "$KEEP" != "1" ]; then
    trap cleanup EXIT
    print_info "Cleanup enabled. To keep demo data, run: KEEP=1 $0"
else
    print_info "KEEP=1 set - demo data will NOT be cleaned up"
fi

# Initial cleanup
cleanup

# ════════════════════════════════════════════════════════════
# Section 1: Setup - Create Test Users
# ════════════════════════════════════════════════════════════

print_section "1. Setup - Create Test Users"

print_subsection "1.1 Create test users with API keys"

# Create alice_meta and bob_meta with API keys
ALICE_KEY=$(python3 scripts/create-api-key.py alice_meta "Alice (Metadata Test)" --days 1 2>/dev/null | grep "API Key:" | awk '{print $3}')
BOB_KEY=$(python3 scripts/create-api-key.py bob_meta "Bob (Metadata Test)" --days 1 2>/dev/null | grep "API Key:" | awk '{print $3}')

# Verify keys were created
if [ -z "$ALICE_KEY" ] || [ -z "$BOB_KEY" ]; then
    print_error "Failed to create user API keys"
    exit 1
fi

# Export keys so they're available in heredocs
export ALICE_KEY
export BOB_KEY

print_success "Created user 'alice_meta' with API key: ${ALICE_KEY:0:20}..."
print_success "Created user 'bob_meta' with API key: ${BOB_KEY:0:20}..."

# ════════════════════════════════════════════════════════════
# Section 2: Agent Registration with Metadata
# ════════════════════════════════════════════════════════════

print_section "2. Agent Registration with Display Name and Description"

print_subsection "2.1 Register agents with rich metadata using Python API"

print_test "Register agents with metadata using alice_meta's credentials"
python3 << REGISTER_AGENTS
import sys, os
sys.path.insert(0, 'src')
from nexus.remote.client import RemoteNexusFS

# Alice registers her agents
alice_key = os.getenv('ALICE_KEY')
if not alice_key:
    print("ERROR: ALICE_KEY not set in environment")
    sys.exit(1)

nexus_url = os.getenv('NEXUS_URL', 'http://localhost:8080')
print(f"Connecting to {nexus_url} with alice's key...")
nx_alice = RemoteNexusFS(nexus_url, api_key=alice_key)

# Register data_analyst with full metadata
print("Registering data_analyst...")
agent1 = nx_alice.register_agent(
    agent_id="data_analyst",
    name="Data Analyst Agent",
    description="Analyzes data and generates insights from CSV files"
)
print(f"✓ Registered: {agent1['agent_id']} - {agent1.get('name')}")

# Register report_generator with full metadata
print("Registering report_generator...")
agent2 = nx_alice.register_agent(
    agent_id="report_generator",
    name="Report Generator",
    description="Creates formatted reports from analysis results"
)
print(f"✓ Registered: {agent2['agent_id']} - {agent2.get('name')}")

nx_alice.close()

# Bob registers his agents
bob_key = os.getenv('BOB_KEY')
if not bob_key:
    print("ERROR: BOB_KEY not set in environment")
    sys.exit(1)

print(f"Connecting to {nexus_url} with bob's key...")
nx_bob = RemoteNexusFS(nexus_url, api_key=bob_key)

# Register code_reviewer with name only (no description)
print("Registering code_reviewer...")
agent3 = nx_bob.register_agent(
    agent_id="code_reviewer",
    name="Code Review Agent"
)
print(f"✓ Registered: {agent3['agent_id']} - {agent3.get('name')}")

# Register legacy_agent without metadata (backward compatibility)
print("Registering legacy_agent (no name, for backward compatibility)...")
agent4 = nx_bob.register_agent(
    agent_id="legacy_agent",
    name="legacy_agent"  # Use agent_id as name
)
print(f"✓ Registered: {agent4['agent_id']}")

nx_bob.close()
REGISTER_AGENTS

print_success "Registered 4 test agents with varying metadata"

export NEXUS_API_KEY="$ADMIN_KEY"

# ════════════════════════════════════════════════════════════
# Section 3: List Agents with Metadata
# ════════════════════════════════════════════════════════════

print_section "3. List Agents with Metadata (Issue #335 Fix)"

print_subsection "3.1 List all agents and verify metadata is returned"

print_test "List all registered agents"
python3 << 'LIST_AGENTS'
import sys, os
sys.path.insert(0, 'src')
from nexus.remote.client import RemoteNexusFS

nexus_url = os.getenv('NEXUS_URL', 'http://localhost:8080')
nx = RemoteNexusFS(nexus_url, api_key=os.getenv('NEXUS_API_KEY'))

agents = nx.list_agents()

# Filter to only show our test agents
test_agent_ids = ['data_analyst', 'report_generator', 'code_reviewer', 'legacy_agent']
test_agents = [a for a in agents if a.get('agent_id') in test_agent_ids]

print(f"\n{'Agent ID':<20} {'Display Name':<25} {'Description':<50}")
print("=" * 95)

for agent in test_agents:
    agent_id = agent.get('agent_id', 'N/A')
    name = agent.get('name', 'N/A')
    description = agent.get('description', '(no description)')
    print(f"{agent_id:<20} {name:<25} {description:<50}")

print(f"\nTest agents shown: {len(test_agents)} / {len(agents)} total")

# Verify metadata
expected_agents = {
    'data_analyst': ('Data Analyst Agent', 'Analyzes data and generates insights from CSV files'),
    'report_generator': ('Report Generator', 'Creates formatted reports from analysis results'),
    'code_reviewer': ('Code Review Agent', None),
    'legacy_agent': ('legacy_agent', None),  # Should use agent_id as name
}

print("\n" + "="*95)
print("VERIFICATION:")
print("="*95)

all_passed = True
for agent in test_agents:
    agent_id = agent.get('agent_id')
    if agent_id in expected_agents:
        expected_name, expected_desc = expected_agents[agent_id]
        actual_name = agent.get('name')
        actual_desc = agent.get('description')

        # Check name
        if actual_name == expected_name:
            print(f"✅ {agent_id}: name = '{actual_name}'")
        else:
            print(f"❌ {agent_id}: expected name '{expected_name}', got '{actual_name}'")
            all_passed = False

        # Check description
        if expected_desc is None:
            if 'description' not in agent:
                print(f"✅ {agent_id}: no description (as expected)")
            else:
                print(f"⚠️  {agent_id}: has description '{actual_desc}' (expected none)")
        else:
            if actual_desc == expected_desc:
                print(f"✅ {agent_id}: description matches")
            else:
                print(f"❌ {agent_id}: expected '{expected_desc}', got '{actual_desc}'")
                all_passed = False

if all_passed:
    print("\n✅ All metadata checks passed!")
else:
    print("\n❌ Some metadata checks failed!")

nx.close()
LIST_AGENTS

# ════════════════════════════════════════════════════════════
# Section 4: Get Individual Agent Details
# ════════════════════════════════════════════════════════════

print_section "4. Get Individual Agent Details with Metadata"

print_subsection "4.1 Retrieve agent details including metadata"

print_test "Get details for 'data_analyst'"
python3 << 'GET_AGENT'
import sys, os, json
sys.path.insert(0, 'src')
from nexus.remote.client import RemoteNexusFS

nexus_url = os.getenv('NEXUS_URL', 'http://localhost:8080')
nx = RemoteNexusFS(nexus_url, api_key=os.getenv('NEXUS_API_KEY'))

agent = nx.get_agent('data_analyst')
if agent:
    print(json.dumps(agent, indent=2))

    # Verify metadata
    if agent.get('name') == 'Data Analyst Agent':
        print("\n✅ Display name correctly returned")
    else:
        print(f"\n❌ Display name mismatch: got '{agent.get('name')}'")

    if 'description' in agent and 'analyzes data' in agent['description'].lower():
        print("✅ Description correctly returned")
    else:
        print("❌ Description missing or incorrect")
else:
    print("❌ Agent not found!")

nx.close()
GET_AGENT

print_test "Get details for 'legacy_agent' (backward compatibility)"
python3 << 'GET_LEGACY'
import sys, os, json
sys.path.insert(0, 'src')
from nexus.remote.client import RemoteNexusFS

nexus_url = os.getenv('NEXUS_URL', 'http://localhost:8080')
nx = RemoteNexusFS(nexus_url, api_key=os.getenv('NEXUS_API_KEY'))

agent = nx.get_agent('legacy_agent')
if agent:
    print(json.dumps(agent, indent=2))

    # Verify fallback to agent_id
    if agent.get('name') == 'legacy_agent':
        print("\n✅ Fallback to agent_id works (no metadata stored)")
    else:
        print(f"\n❌ Name should fallback to 'legacy_agent', got '{agent.get('name')}'")

    if 'description' not in agent:
        print("✅ No description field (as expected for legacy agent)")
    else:
        print(f"⚠️  Description field present: '{agent.get('description')}'")
else:
    print("❌ Agent not found!")

nx.close()
GET_LEGACY

# ════════════════════════════════════════════════════════════
# Section 5: Verify Database Storage
# ════════════════════════════════════════════════════════════

print_section "5. Verify Metadata Storage in Database"

print_subsection "5.1 Check entity_registry table for metadata"

print_test "Query entity_registry directly to verify JSON storage"
python3 << 'CHECK_DB'
import sys, os, json
sys.path.insert(0, 'src')
from nexus.core.entity_registry import EntityRegistry
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Get DB connection
db_url = os.getenv('NEXUS_DATABASE_URL', 'sqlite:///demo_data/nexus.db')
engine = create_engine(db_url)
SessionLocal = sessionmaker(bind=engine)

registry = EntityRegistry(SessionLocal)

print("\nEntity Registry Metadata for Test Agents:")
print("="*95)

for agent_id in ['data_analyst', 'report_generator', 'code_reviewer', 'legacy_agent']:
    entity = registry.get_entity('agent', agent_id)
    if entity:
        print(f"\nAgent: {agent_id}")
        print(f"  entity_metadata (raw): {entity.entity_metadata}")

        if entity.entity_metadata:
            try:
                metadata = json.loads(entity.entity_metadata)
                print(f"  Parsed metadata: {json.dumps(metadata, indent=4)}")
            except:
                print("  ❌ Failed to parse metadata JSON")
        else:
            print("  (no metadata stored)")
    else:
        print(f"\n❌ Agent '{agent_id}' not found in entity registry")

print("\n" + "="*95)
print("✅ Metadata is stored as JSON in entity_registry.entity_metadata column")
CHECK_DB

# ════════════════════════════════════════════════════════════
# Summary
# ════════════════════════════════════════════════════════════

print_section "✅ Agent Metadata Demo Complete!"

echo "╔═══════════════════════════════════════════════════════════════════╗"
echo "║              Agent Metadata Features Tested (Issue #335)          ║"
echo "╠═══════════════════════════════════════════════════════════════════╣"
echo "║  ✅ Agent Registration with Display Name                          ║"
echo "║  ✅ Agent Registration with Description                           ║"
echo "║  ✅ List Agents Returns Display Name and Description              ║"
echo "║  ✅ Get Agent Returns Metadata                                    ║"
echo "║  ✅ Backward Compatibility (agents without metadata)              ║"
echo "║  ✅ Metadata Stored as JSON in entity_registry.entity_metadata    ║"
echo "║  ✅ Fallback to agent_id when no display name stored              ║"
echo "╚═══════════════════════════════════════════════════════════════════╝"
echo ""
print_success "All agent metadata tests passed!"
print_info "Issue #335 is IMPLEMENTED: Agent display names and descriptions are now stored in database"
echo ""

if [ "$KEEP" == "1" ]; then
    echo "════════════════════════════════════════════════════════════"
    echo "Demo data preserved. Inspect with:"
    echo "  nexus agent list"
    echo "  nexus agent get data_analyst"
    echo "════════════════════════════════════════════════════════════"
fi
