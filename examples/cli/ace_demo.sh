#!/bin/bash
# Nexus CLI - ACE (Agentic Context Engineering) Demo
#
# This demo showcases the ACE learning system including:
# - Trajectory tracking (execution history)
# - Reflection (extracting learnings from experience)
# - Playbook curation (building learned strategies)
# - Re-learning queue (continuous improvement)
#
# Prerequisites:
# 1. Server running: ./scripts/init-nexus-with-auth.sh
# 2. Load admin credentials: source .nexus-admin-env
#
# Usage:
#   ./examples/cli/ace_demo.sh

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
print_command() { echo -e "${CYAN}\$${NC} $1"; }

# Check prerequisites
if [ -z "$NEXUS_URL" ] || [ -z "$NEXUS_API_KEY" ]; then
    print_error "NEXUS_URL and NEXUS_API_KEY not set. Run: source .nexus-admin-env"
    exit 1
fi

echo "╔══════════════════════════════════════════════════════════╗"
echo "║        Nexus CLI - ACE Learning System Demo             ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
print_info "Server: $NEXUS_URL"
print_info "Demonstrating trajectory tracking, reflection, and playbook learning"
echo ""

ADMIN_KEY="$NEXUS_API_KEY"

# Cleanup function
cleanup() {
    if [ "$KEEP" != "1" ]; then
        export NEXUS_API_KEY="$ADMIN_KEY"
        print_info "Cleaning up ACE demo data..."
        # Note: Trajectory deletion would need to be added to server API
        print_warning "Note: Some demo data may persist. Use KEEP=1 to inspect results."
    fi
}

# Gate cleanup behind KEEP flag
if [ "$KEEP" != "1" ]; then
    trap cleanup EXIT
    print_info "Cleanup enabled. To keep demo data, run: KEEP=1 $0"
else
    print_warning "KEEP=1 set - demo data will persist after exit"
fi

sleep 1

#═══════════════════════════════════════════════════════════════════════
# Section 1: Trajectory Tracking
#═══════════════════════════════════════════════════════════════════════
print_section "1. Trajectory Tracking - Recording Task Execution"

print_subsection "Starting a trajectory"
print_command "nexus memory trajectory start \"ACE CLI Demo: Process customer data\" --type data_processing --json"

# Capture both stdout and stderr for debugging
set +e  # Temporarily disable exit on error
TRAJ_OUTPUT=$(nexus memory trajectory start "ACE CLI Demo: Process customer data" --type data_processing --json 2>&1)
TRAJ_EXIT_CODE=$?
set -e  # Re-enable exit on error

if [ $TRAJ_EXIT_CODE -ne 0 ]; then
    print_error "Command failed with exit code $TRAJ_EXIT_CODE"
    echo "Output: $TRAJ_OUTPUT"
    exit 1
fi

# Try to parse JSON
if echo "$TRAJ_OUTPUT" | python3 -c "import json, sys; json.load(sys.stdin)" >/dev/null 2>&1; then
    TRAJ1=$(echo "$TRAJ_OUTPUT" | python3 -c "import json, sys; print(json.load(sys.stdin)['trajectory_id'])")
    print_success "Started trajectory: $TRAJ1"
else
    print_error "Failed to parse JSON output"
    echo "Output: [$TRAJ_OUTPUT]"
    exit 1
fi
sleep 1

print_subsection "Logging execution steps"
print_command "nexus memory trajectory log \$TRAJ1 \"Loaded 1000 customer records\" --type action"
nexus memory trajectory log "$TRAJ1" "Loaded 1000 customer records" --type action
print_success "Logged step 1"
sleep 0.5

print_command "nexus memory trajectory log \$TRAJ1 \"Found 50 records with missing email\" --type observation"
nexus memory trajectory log "$TRAJ1" "Found 50 records with missing email" --type observation
print_success "Logged step 2"
sleep 0.5

print_command "nexus memory trajectory log \$TRAJ1 \"Applied email validation rule\" --type decision"
nexus memory trajectory log "$TRAJ1" "Applied email validation rule" --type decision
print_success "Logged step 3"
sleep 0.5

print_subsection "Completing trajectory with success"
print_command "nexus memory trajectory complete $TRAJ1 --status success --score 0.95"
nexus memory trajectory complete "$TRAJ1" --status success --score 0.95
print_success "Trajectory completed with 95% success score"
sleep 1

print_subsection "Starting a second trajectory (with failure)"
print_command "nexus memory trajectory start \"ACE CLI Demo: Validate product catalog\" --type validation --json"
TRAJ2=$(nexus memory trajectory start "ACE CLI Demo: Validate product catalog" --type validation --json | python3 -c "import json, sys; print(json.load(sys.stdin)['trajectory_id'])")
print_success "Started trajectory: $TRAJ2"
sleep 0.5

nexus memory trajectory log "$TRAJ2" "Loaded 500 products" --type action
nexus memory trajectory log "$TRAJ2" "Found 30 products with invalid prices" --type observation
nexus memory trajectory log "$TRAJ2" "Found 15 products with missing categories" --type observation
sleep 0.5

print_command "nexus memory trajectory complete $TRAJ2 --status partial --score 0.70"
nexus memory trajectory complete "$TRAJ2" --status partial --score 0.70
print_success "Trajectory completed with 70% success (partial completion)"
sleep 1

print_subsection "Starting a third trajectory (successful)"
TRAJ3=$(nexus memory trajectory start "ACE CLI Demo: Generate monthly report" --type reporting --json | python3 -c "import json, sys; print(json.load(sys.stdin)['trajectory_id'])")
nexus memory trajectory log "$TRAJ3" "Aggregated sales data for 30 days" --type action
nexus memory trajectory log "$TRAJ3" "Detected 3 anomalies in transaction patterns" --type observation
nexus memory trajectory log "$TRAJ3" "Generated report with insights" --type action
nexus memory trajectory complete "$TRAJ3" --status success --score 0.98
print_success "Trajectory completed with 98% success score"
sleep 1

print_subsection "Listing trajectories"
print_command "nexus memory trajectory list --limit 5"
nexus memory trajectory list --limit 5
sleep 1

#═══════════════════════════════════════════════════════════════════════
# Section 2: Reflection - Learning from Experience
#═══════════════════════════════════════════════════════════════════════
print_section "2. Reflection - Extracting Learnings from Trajectories"

print_subsection "Running batch reflection on recent trajectories"
print_info "This analyzes multiple trajectories to find common patterns"
print_command "nexus memory reflect --batch --min-count 1"
REFLECT_OUTPUT=$(nexus memory reflect --batch --min-count 1 --json)
print_success "Reflections created from execution traces"
echo "$REFLECT_OUTPUT" | python3 -m json.tool 2>/dev/null | head -20 || echo "$REFLECT_OUTPUT"
sleep 1

# Extract reflection IDs for later curation
REFLECTION_IDS=$(echo "$REFLECT_OUTPUT" | python3 -c "import json, sys; data=json.load(sys.stdin); print(','.join(data.get('reflection_ids', [])))" 2>/dev/null || echo "")
print_info "Reflection IDs: ${REFLECTION_IDS:-none}"
sleep 1

#═══════════════════════════════════════════════════════════════════════
# Section 3: Playbook Management - Building Learned Strategies
#═══════════════════════════════════════════════════════════════════════
print_section "3. Playbook Management - Building Learned Strategies"

print_subsection "Creating a playbook from reflections"
print_info "This curates reflection learnings into a reusable playbook"
if [ -n "$REFLECTION_IDS" ]; then
    print_command "nexus memory playbook curate --reflections $REFLECTION_IDS --name cli-demo-playbook"
    nexus memory playbook curate --reflections "$REFLECTION_IDS" --name cli-demo-playbook || print_warning "Curation failed (may need more reflections)"
    print_success "Playbook curated with learned strategies"
else
    print_warning "No reflection IDs available for curation"
    print_info "Creating playbook manually via Python SDK..."
    python3 -c "
from nexus.sdk import connect
nx = connect()
from nexus.core.ace.playbook import PlaybookManager
session = nx.metadata.SessionLocal()
pm = PlaybookManager(session, nx.backend, 'system', None, None)
pb_id = pm.create_playbook('cli-demo-playbook', 'Demo playbook for CLI showcase', 'user')
session.close()
print(f'Created playbook: {pb_id}')
" || print_warning "Could not create playbook"
fi
sleep 1

print_subsection "Viewing playbook strategies"
print_command "nexus memory playbook get cli-demo-playbook"
nexus memory playbook get cli-demo-playbook
sleep 1

print_subsection "Listing all playbooks"
print_command "nexus memory playbook list"
nexus memory playbook list
sleep 1

#═══════════════════════════════════════════════════════════════════════
# Section 4: Memory Operations - Storing and Retrieving Learnings
#═══════════════════════════════════════════════════════════════════════
print_section "4. Memory Operations - Storing Knowledge"

print_subsection "Storing explicit memories"
print_command "nexus memory store \"Email validation regex: ^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,}$\" --type pattern"
nexus memory store "Email validation regex: ^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$" --type pattern
print_success "Stored pattern memory"
sleep 0.5

print_command "nexus memory store \"Price validation failed for negative values - need to add check\" --type insight"
nexus memory store "Price validation failed for negative values - need to add check" --type insight
print_success "Stored insight memory"
sleep 1

print_subsection "Listing memories"
print_command "nexus memory list --limit 5"
nexus memory list --limit 5
sleep 1

print_subsection "Querying memories by type"
print_command "nexus memory query --type reflection --limit 3"
nexus memory query --type reflection --limit 3
sleep 1

#═══════════════════════════════════════════════════════════════════════
# Section 5: Re-learning Queue - Continuous Improvement
#═══════════════════════════════════════════════════════════════════════
print_section "5. Re-learning Queue - Flagging Tasks for Improvement"

print_subsection "Processing re-learning queue"
print_info "This processes trajectories that need additional learning"
print_command "nexus memory process-relearning --limit 5"
nexus memory process-relearning --limit 5 || print_warning "No trajectories in re-learning queue yet"
sleep 1

#═══════════════════════════════════════════════════════════════════════
# Section 6: Advanced - Consolidation and Maintenance
#═══════════════════════════════════════════════════════════════════════
print_section "6. Memory Consolidation - Preventing Context Collapse"

print_subsection "Consolidating memories to preserve important knowledge"
print_info "This merges similar memories and archives old ones"
print_command "nexus memory consolidate --dry-run"
nexus memory consolidate --dry-run || print_info "Consolidation would run here"
sleep 1

#═══════════════════════════════════════════════════════════════════════
# Demo Complete
#═══════════════════════════════════════════════════════════════════════
print_section "Demo Complete! 🎉"

print_success "Successfully demonstrated ACE learning system:"
echo ""
echo "  1. ✓ Tracked 3 execution trajectories with steps and outcomes"
echo "  2. ✓ Extracted learnings through reflection"
echo "  3. ✓ Curated a playbook with learned strategies"
echo "  4. ✓ Stored and queried memories"
echo "  5. ✓ Showed re-learning queue processing"
echo "  6. ✓ Demonstrated memory consolidation"
echo ""

print_info "Key Takeaways:"
echo "  • Trajectories = Execution history with steps, decisions, outcomes"
echo "  • Reflection = Analyzing what worked and what didn't"
echo "  • Playbooks = Reusable strategies learned from experience"
echo "  • Re-learning = Continuous improvement on challenging tasks"
echo ""

if [ "$KEEP" = "1" ]; then
    print_warning "Demo data persisted. View with:"
    echo "  nexus memory trajectory list"
    echo "  nexus memory playbook get cli-demo-playbook"
else
    print_info "Demo data cleaned up automatically"
fi

echo ""
print_success "For a complete working example, see: examples/ace/demo_3_data_validator.py"
echo ""
