#!/bin/bash
# Operation Log Demo - Undo & Audit Trail
#
# This script demonstrates the operation logging system:
# - Automatic logging of all operations
# - Query operation history with filters
# - Undo last operation
# - Audit trail for compliance

set -e  # Exit on error

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "======================================================================"
echo "Nexus Operation Log Demo - Undo & Audit Trail"
echo "======================================================================"

# Create temporary workspace
DEMO_DIR=$(mktemp -d)
export NEXUS_DATA_DIR="$DEMO_DIR/nexus-data"

echo -e "\n📁 Data directory: $NEXUS_DATA_DIR"

# Initialize
echo -e "\n${BLUE}1. Initializing Nexus workspace...${NC}"
nexus init "$NEXUS_DATA_DIR"
echo -e "${GREEN}   ✓ Initialized${NC}"

# ============================================================
# Part 1: Automatic Operation Logging
# ============================================================
echo -e "\n======================================================================"
echo "PART 1: Automatic Operation Logging"
echo "======================================================================"

echo -e "\n${BLUE}2. Performing filesystem operations (logged automatically)...${NC}"

# Write files
echo "Version 1 content" | nexus write /workspace/version1.txt --input -
echo -e "${GREEN}   ✓ Wrote version1.txt${NC}"

echo "Version 2 content" | nexus write /workspace/version2.txt --input -
echo -e "${GREEN}   ✓ Wrote version2.txt${NC}"

echo "Important data" | nexus write /workspace/data.txt --input -
echo -e "${GREEN}   ✓ Wrote data.txt${NC}"

# Update file (logs previous version)
echo "Version 1 UPDATED" | nexus write /workspace/version1.txt --input -
echo -e "${GREEN}   ✓ Updated version1.txt (previous version logged)${NC}"

# Rename file
nexus mv /workspace/version2.txt /workspace/renamed.txt
echo -e "${GREEN}   ✓ Renamed version2.txt to renamed.txt${NC}"

# Delete file
nexus rm /workspace/data.txt --force
echo -e "${GREEN}   ✓ Deleted data.txt (content snapshot saved)${NC}"

# ============================================================
# Part 2: Query Operation History
# ============================================================
echo -e "\n======================================================================"
echo "PART 2: Query Operation History"
echo "======================================================================"

echo -e "\n${BLUE}3. Viewing operation log...${NC}"
echo ""
nexus ops log --limit 10

echo -e "\n${BLUE}4. Filtering operations by type...${NC}"
echo -e "\n${YELLOW}Write operations:${NC}"
nexus ops log --type write --limit 5

echo -e "\n${YELLOW}Delete operations:${NC}"
nexus ops log --type delete --limit 5

echo -e "\n${YELLOW}Rename operations:${NC}"
nexus ops log --type rename --limit 5

# ============================================================
# Part 3: Undo Operations
# ============================================================
echo -e "\n======================================================================"
echo "PART 3: Undo Operations"
echo "======================================================================"

echo -e "\n${BLUE}5. Demonstrating undo capability...${NC}"
echo -e "${YELLOW}   Current files:${NC}"
nexus ls /workspace --long

echo -e "\n${YELLOW}   Undoing last operation...${NC}"
nexus undo --yes

echo -e "\n${YELLOW}   Files after undo:${NC}"
nexus ls /workspace --long

# ============================================================
# Part 4: Audit Trail
# ============================================================
echo -e "\n======================================================================"
echo "PART 4: Audit Trail"
echo "======================================================================"

echo -e "\n${BLUE}6. Viewing audit trail for specific path...${NC}"
echo -e "${YELLOW}   Operation history for /workspace/version1.txt:${NC}"
nexus ops log --path /workspace/version1.txt --limit 10

# ============================================================
# Part 5: Key Features
# ============================================================
echo -e "\n======================================================================"
echo "PART 5: Key Features"
echo "======================================================================"

echo -e "\n${GREEN}✨ Operation Log Features:${NC}"
echo "   • Automatic logging of all operations (write, delete, rename)"
echo "   • CAS-backed snapshots (zero storage overhead)"
echo "   • Undo capability for any operation"
echo "   • Filter by agent, type, path, time, status"
echo "   • Complete audit trail for compliance"
echo "   • Query API for operation history"

echo -e "\n${BLUE}📊 Usage Examples:${NC}"
echo "   # View recent operations"
echo "   nexus ops log --limit 20"
echo ""
echo "   # Filter by type"
echo "   nexus ops log --type write"
echo ""
echo "   # Filter by agent"
echo "   nexus ops log --agent my-agent"
echo ""
echo "   # Filter by path"
echo "   nexus ops log --path /workspace/data.txt"
echo ""
echo "   # Undo last operation"
echo "   nexus undo"
echo ""
echo "   # Undo last operation by specific agent"
echo "   nexus undo --agent my-agent --yes"

# Cleanup
echo -e "\n${BLUE}Cleaning up...${NC}"
rm -rf "$DEMO_DIR"

echo ""
echo "======================================================================"
echo "Demo Complete!"
echo "======================================================================"
