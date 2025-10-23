#!/bin/bash
# Time-Travel Debugging Demo Script
#
# This script demonstrates Nexus's time-travel debugging capabilities
# for reading files at historical operation points.
#
# Usage: ./time_travel_demo.sh

set -e

echo "======================================================================"
echo "Time-Travel Debugging Demo - CLI Edition"
echo "======================================================================"
echo ""

# Setup temporary workspace
TEMP_DIR=$(mktemp -d)
export NEXUS_DATA_DIR="$TEMP_DIR/nexus-data"

echo "📁 Setting up temporary workspace at: $TEMP_DIR"
nexus init "$TEMP_DIR"
echo ""

# Simulate agent workflow with evolving files
echo "======================================================================"
echo "1. Creating Files with Version History"
echo "======================================================================"
echo ""

echo "📝 Version 1: Agent starts logging..."
echo "Agent started
Initializing workspace..." | nexus write /workspace/agent_log.txt --input -

echo "✓ Created version 1"
echo ""

sleep 0.1  # Small delay to ensure different timestamps

echo "📝 Version 2: Agent makes progress..."
echo "Agent started
Initializing workspace...
Fetched data from API
Processing 100 records..." | nexus write /workspace/agent_log.txt --input -

echo "✓ Created version 2"
echo ""

sleep 0.1

echo "📝 Version 3: Agent completes task..."
echo "Agent started
Initializing workspace...
Fetched data from API
Processing 100 records...
Task completed successfully!
Results saved to output.json" | nexus write /workspace/agent_log.txt --input -

echo "✓ Created version 3"
echo ""

# Create another file
echo '{"status": "success", "records": 100}' | nexus write /workspace/output.json --input -
echo "✓ Created output.json"
echo ""

# Show operation log
echo "======================================================================"
echo "2. View Operation Log"
echo "======================================================================"
echo ""

echo "All operations for /workspace/agent_log.txt:"
echo "----------------------------------------------------------------------"
nexus ops log --path /workspace/agent_log.txt --limit 10
echo ""

# Get operation IDs using a simple Python script
echo "Extracting operation IDs..."

# Create temp Python script to extract operation IDs
TEMP_SCRIPT=$(mktemp)
cat > "$TEMP_SCRIPT" <<'PYTHON'
import nexus
from nexus.storage.operation_logger import OperationLogger
nx = nexus.connect()
with nx.metadata.SessionLocal() as session:
    logger = OperationLogger(session)
    ops = logger.list_operations(path="/workspace/agent_log.txt", limit=3)
    for op in reversed(ops):
        print(op.operation_id)
nx.close()
PYTHON

# Run the script and capture operation IDs (compatible with older bash)
OP_IDS=()
while IFS= read -r line; do
    OP_IDS+=("$line")
done < <(uv run python "$TEMP_SCRIPT" 2>/dev/null)
rm "$TEMP_SCRIPT"

if [ ${#OP_IDS[@]} -eq 3 ]; then
    OP_V1="${OP_IDS[0]}"  # Oldest
    OP_V2="${OP_IDS[1]}"
    OP_V3="${OP_IDS[2]}"  # Most recent

    echo "✓ Found operation IDs:"
    echo "  Version 1: ${OP_V1:0:8}..."
    echo "  Version 2: ${OP_V2:0:8}..."
    echo "  Version 3: ${OP_V3:0:8}..."
    echo ""

    # Time-travel read
    echo "======================================================================"
    echo "3. Time-Travel - Read File at Historical Points"
    echo "======================================================================"
    echo ""

    echo "🕐 Reading at Version 1 (Initial state):"
    echo "----------------------------------------------------------------------"
    nexus cat /workspace/agent_log.txt --at-operation "$OP_V1" 2>&1 | grep -v "RuntimeWarning" || echo "(Content at v1)"
    echo "----------------------------------------------------------------------"
    echo ""

    echo "🕑 Reading at Version 2 (Progress update):"
    echo "----------------------------------------------------------------------"
    nexus cat /workspace/agent_log.txt --at-operation "$OP_V2" 2>&1 | grep -v "RuntimeWarning" || echo "(Content at v2)"
    echo "----------------------------------------------------------------------"
    echo ""

    echo "🕒 Reading at Version 3 (Current/final state):"
    echo "----------------------------------------------------------------------"
    nexus cat /workspace/agent_log.txt --at-operation "$OP_V3" 2>&1 | grep -v "RuntimeWarning" || echo "(Content at v3)"
    echo "----------------------------------------------------------------------"
    echo ""

    # Operation diff
    echo "======================================================================"
    echo "4. Operation Diff - Compare Versions"
    echo "======================================================================"
    echo ""

    echo "📊 Metadata Diff: Version 1 → Version 2"
    echo "----------------------------------------------------------------------"
    nexus ops diff /workspace/agent_log.txt "$OP_V1" "$OP_V2"
    echo ""

    echo "📊 Content Diff: Version 1 → Version 2 (with --show-content)"
    echo "----------------------------------------------------------------------"
    nexus ops diff /workspace/agent_log.txt "$OP_V1" "$OP_V2" --show-content
    echo ""

    echo "📊 Content Diff: Version 2 → Version 3 (with --show-content)"
    echo "----------------------------------------------------------------------"
    nexus ops diff /workspace/agent_log.txt "$OP_V2" "$OP_V3" --show-content
    echo ""

    # Directory listing at historical point
    echo "======================================================================"
    echo "5. Directory Time-Travel"
    echo "======================================================================"
    echo ""

    echo "📁 Directory listing at Version 1 (only agent_log.txt exists):"
    echo "----------------------------------------------------------------------"
    nexus ls /workspace --at-operation "$OP_V1"
    echo ""

    echo "📁 Directory listing at Version 3 (both files exist):"
    echo "----------------------------------------------------------------------"
    nexus ls /workspace --at-operation "$OP_V3"
    echo ""

    # Detailed listing
    echo "📁 Detailed directory listing at Version 3:"
    echo "----------------------------------------------------------------------"
    nexus ls /workspace --at-operation "$OP_V3" -l
    echo ""
else
    echo "⚠️  Could not extract operation IDs. Showing current state only."
    echo ""
    echo "Current file content:"
    nexus cat /workspace/agent_log.txt
    echo ""
fi

# Summary
echo "======================================================================"
echo "Summary - Time-Travel Commands"
echo "======================================================================"
echo ""
echo "Available Commands:"
echo ""
echo "  nexus cat <path> --at-operation <op_id>"
echo "    Read file content at a historical operation point"
echo ""
echo "  nexus ls <path> --at-operation <op_id> [-l]"
echo "    List directory contents at a historical point"
echo ""
echo "  nexus ops diff <path> <op1> <op2> [--show-content]"
echo "    Compare file states between two operations"
echo "    Use --show-content for line-by-line unified diff"
echo ""
echo "  nexus ops log [--path <path>] [--agent <agent>]"
echo "    View operation history with filters"
echo ""
echo "  nexus undo [--agent <agent>] [--yes]"
echo "    Undo the last operation (with confirmation)"
echo ""
echo "======================================================================"
echo "Use Cases"
echo "======================================================================"
echo ""
echo "1. Debug Agent Behavior:"
echo "   - See what a file looked like 10 operations ago"
echo "   - Track when an agent changed a specific value"
echo "   - Understand the sequence of agent actions"
echo ""
echo "2. Post-Mortem Analysis:"
echo "   - Non-destructive exploration of past states"
echo "   - Compare states at different points in time"
echo "   - Visualize how files evolved during a workflow"
echo ""
echo "3. Concurrent Agent Debugging:"
echo "   - See what files existed when each agent ran"
echo "   - Track which agent modified which files"
echo "   - Understand agent interaction patterns"
echo ""

# Cleanup
echo "======================================================================"
echo "Cleanup"
echo "======================================================================"
echo ""
echo "Temporary workspace: $TEMP_DIR"
echo "To keep this workspace, export: export NEXUS_DATA_DIR=\"$TEMP_DIR/nexus-data\""
echo "To delete, run: rm -rf $TEMP_DIR"
echo ""
