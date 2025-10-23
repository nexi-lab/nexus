#!/bin/bash
# Workspace Versioning Demo - Time-Travel for Agent Workspaces
#
# This script demonstrates workspace snapshot and restore functionality
# for time-travel debugging and rollback capabilities.

set -e  # Exit on error

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}  Nexus Workspace Versioning Demo${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""

# Configuration
AGENT_ID="demo-agent"
TENANT_ID="demo-tenant"
WORKSPACE_DIR="/tmp/nexus-workspace-demo"
DATA_DIR="$WORKSPACE_DIR/nexus-data"

# Clean up from previous runs
echo -e "${YELLOW}Cleaning up previous demo data...${NC}"
rm -rf "$WORKSPACE_DIR"
mkdir -p "$WORKSPACE_DIR"

# If using PostgreSQL, clean up old snapshots for this demo agent
if [ ! -z "$NEXUS_DATABASE_URL" ]; then
    echo -e "${YELLOW}Detected PostgreSQL, cleaning up old demo snapshots...${NC}"
    # Delete old snapshots for demo agent
    psql "$NEXUS_DATABASE_URL" -c "DELETE FROM workspace_snapshots WHERE tenant_id='demo-tenant' AND agent_id='demo-agent';" 2>/dev/null || true
fi

echo -e "${GREEN}✓ Setup complete${NC}"
echo ""

# Initialize Nexus
echo -e "${BLUE}Step 1: Initializing Nexus${NC}"
nexus init "$WORKSPACE_DIR"
echo -e "${GREEN}✓ Nexus initialized${NC}"
echo ""

# Create initial workspace state
echo -e "${BLUE}Step 2: Creating initial workspace files${NC}"
nexus write --data-dir="$DATA_DIR" "/workspace/$TENANT_ID/$AGENT_ID/README.md" "# My Project

This is the initial version of my project.
"

nexus write --data-dir="$DATA_DIR" "/workspace/$TENANT_ID/$AGENT_ID/config.json" '{
  "name": "demo-project",
  "version": "1.0.0",
  "debug": false
}'

nexus write --data-dir="$DATA_DIR" "/workspace/$TENANT_ID/$AGENT_ID/data/users.json" '[
  {"id": 1, "name": "Alice"},
  {"id": 2, "name": "Bob"}
]'

echo -e "${GREEN}✓ Created 3 files${NC}"
echo ""

# Create snapshot 1
echo -e "${BLUE}Step 3: Creating Snapshot #1 (Initial State)${NC}"
nexus workspace snapshot \
  --data-dir="$DATA_DIR" \
  --agent="$AGENT_ID" \
  --tenant="$TENANT_ID" \
  --description="Initial project setup" \
  --tag="v1.0" \
  --tag="stable"

echo -e "${GREEN}✓ Snapshot #1 created${NC}"
echo ""

# Make some changes
echo -e "${BLUE}Step 4: Making changes to workspace${NC}"

# Update config
nexus write --data-dir="$DATA_DIR" "/workspace/$TENANT_ID/$AGENT_ID/config.json" '{
  "name": "demo-project",
  "version": "1.1.0",
  "debug": true,
  "features": ["logging", "metrics"]
}'

# Add new file
nexus write --data-dir="$DATA_DIR" "/workspace/$TENANT_ID/$AGENT_ID/data/products.json" '[
  {"id": 1, "name": "Widget", "price": 9.99},
  {"id": 2, "name": "Gadget", "price": 19.99}
]'

# Update README
nexus write --data-dir="$DATA_DIR" "/workspace/$TENANT_ID/$AGENT_ID/README.md" "# My Project

This is version 1.1 with new features!

## Features
- Logging
- Metrics
- User management
- Product catalog
"

echo -e "${GREEN}✓ Made changes: updated 2 files, added 1 new file${NC}"
echo ""

# Create snapshot 2
echo -e "${BLUE}Step 5: Creating Snapshot #2 (With Features)${NC}"
nexus workspace snapshot \
  --data-dir="$DATA_DIR" \
  --agent="$AGENT_ID" \
  --tenant="$TENANT_ID" \
  --description="Added logging and metrics features" \
  --tag="v1.1" \
  --tag="development"

echo -e "${GREEN}✓ Snapshot #2 created${NC}"
echo ""

# Make breaking changes
echo -e "${BLUE}Step 6: Making breaking changes${NC}"

# Delete a file
nexus rm --data-dir="$DATA_DIR" "/workspace/$TENANT_ID/$AGENT_ID/data/users.json" --force

# Break the config
nexus write --data-dir="$DATA_DIR" "/workspace/$TENANT_ID/$AGENT_ID/config.json" '{
  "name": "demo-project-BROKEN",
  "version": "2.0.0-alpha",
  "debug": true,
  "experimental": true,
  "DANGER": "This config is broken!"
}'

echo -e "${RED}✗ Oh no! Made breaking changes${NC}"
echo ""

# Create snapshot 3
echo -e "${BLUE}Step 7: Creating Snapshot #3 (Broken State)${NC}"
nexus workspace snapshot \
  --data-dir="$DATA_DIR" \
  --agent="$AGENT_ID" \
  --tenant="$TENANT_ID" \
  --description="Experimental changes (broken)" \
  --tag="v2.0-alpha" \
  --tag="broken"

echo -e "${GREEN}✓ Snapshot #3 created${NC}"
echo ""

# View snapshot history
echo -e "${BLUE}Step 8: Viewing Snapshot History${NC}"
nexus workspace log \
  --data-dir="$DATA_DIR" \
  --agent="$AGENT_ID" \
  --tenant="$TENANT_ID" \
  --limit=10

echo ""

# Compare snapshots
echo -e "${BLUE}Step 9: Comparing Snapshots #1 and #2${NC}"
nexus workspace diff \
  --data-dir="$DATA_DIR" \
  --agent="$AGENT_ID" \
  --tenant="$TENANT_ID" \
  --snapshot1=1 \
  --snapshot2=2

echo ""

echo -e "${BLUE}Step 10: Comparing Snapshots #2 and #3${NC}"
nexus workspace diff \
  --data-dir="$DATA_DIR" \
  --agent="$AGENT_ID" \
  --tenant="$TENANT_ID" \
  --snapshot1=2 \
  --snapshot2=3

echo ""

# Time-travel: restore to good state
echo -e "${BLUE}Step 11: Time-Travel - Restoring to Snapshot #2${NC}"
echo -e "${YELLOW}Restoring workspace to the last working state...${NC}"

nexus workspace restore \
  --data-dir="$DATA_DIR" \
  --agent="$AGENT_ID" \
  --tenant="$TENANT_ID" \
  --snapshot=2 \
  --yes

echo -e "${GREEN}✓ Workspace restored to Snapshot #2${NC}"
echo ""

# Verify restoration
echo -e "${BLUE}Step 12: Verifying Restoration${NC}"
echo "Config file contents:"
nexus cat --data-dir="$DATA_DIR" "/workspace/$TENANT_ID/$AGENT_ID/config.json"
echo ""

echo "Users file metadata (restored):"
if nexus info --data-dir="$DATA_DIR" "/workspace/$TENANT_ID/$AGENT_ID/data/users.json" 2>/dev/null | grep -q "Size:"; then
    echo -e "${GREEN}✓ Users file metadata restored successfully${NC}"
    echo "  (Note: Content may need to be re-read from original source if CAS was GC'd)"
else
    echo -e "${RED}✗ Users file not found${NC}"
fi
echo ""

# List current workspace
echo -e "${BLUE}Step 13: Current Workspace State${NC}"
nexus ls --data-dir="$DATA_DIR" "/workspace/$TENANT_ID/$AGENT_ID" --long --recursive

echo ""
echo -e "${BLUE}========================================${NC}"
echo -e "${GREEN}Demo Complete!${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""
echo "Key Takeaways:"
echo "  • Created 3 snapshots tracking workspace evolution"
echo "  • Compared different versions to see what changed"
echo "  • Restored workspace to previous working state"
echo "  • All content deduplicated using CAS (no storage waste!)"
echo ""
echo "Data location: $WORKSPACE_DIR"
echo ""
echo "Try these commands yourself:"
echo "  nexus workspace log --agent=$AGENT_ID --tenant=$TENANT_ID --data-dir=$DATA_DIR"
echo "  nexus workspace diff --agent=$AGENT_ID --tenant=$TENANT_ID --snapshot1=1 --snapshot2=3 --data-dir=$DATA_DIR"
echo ""
