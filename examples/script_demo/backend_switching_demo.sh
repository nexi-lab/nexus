#!/bin/bash
# Backend Switching Demo
#
# Shows how to easily switch between backends using environment variables
# instead of repeating flags on every command.

set -e

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo "======================================================================"
echo "Backend Switching Demo - Environment Variables"
echo "======================================================================"

# Setup test directories
LOCAL_DIR=$(mktemp -d)/local-data
GCS_META_DIR=$(mktemp -d)/gcs-metadata
mkdir -p "$LOCAL_DIR" "$GCS_META_DIR"

echo -e "\n${BLUE}Created temporary directories:${NC}"
echo "  Local: $LOCAL_DIR"
echo "  GCS metadata: $GCS_META_DIR"

# Initialize local backend
nexus init "$LOCAL_DIR" > /dev/null 2>&1

# Helper functions to switch backends
setup_local() {
    echo -e "\n${BLUE}→ Switching to LOCAL backend${NC}"
    export NEXUS_BACKEND=local
    export NEXUS_DATA_DIR="$LOCAL_DIR"
    unset NEXUS_GCS_BUCKET_NAME
    unset NEXUS_GCS_PROJECT_ID
}

setup_gcs() {
    echo -e "\n${BLUE}→ Switching to GCS backend${NC}"
    export NEXUS_BACKEND=gcs
    export NEXUS_GCS_BUCKET_NAME="${GCS_BUCKET_NAME:-nexi-hub}"
    export NEXUS_GCS_PROJECT_ID="${GCS_PROJECT_ID:-nexi-lab-888}"
    export NEXUS_DATA_DIR="$GCS_META_DIR"
}

# Demo: Using local backend
echo -e "\n======================================================================"
echo "PART 1: Local Backend Operations"
echo "======================================================================"

setup_local

echo -e "\n${YELLOW}Writing to local backend:${NC}"
echo "  Command: nexus write /local-file.txt \"Stored locally\""
echo "Local content" | nexus write /local-file.txt --input - 2>/dev/null
echo -e "${GREEN}✓ Written${NC}"

echo -e "\n${YELLOW}Reading from local backend:${NC}"
echo "  Command: nexus cat /local-file.txt"
echo "  Content: $(nexus cat /local-file.txt 2>/dev/null)"

echo -e "\n${YELLOW}Listing local backend:${NC}"
echo "  Command: nexus ls /"
nexus ls / 2>/dev/null

# Demo: Switching to GCS (if available)
echo -e "\n======================================================================"
echo "PART 2: GCS Backend Operations (if credentials available)"
echo "======================================================================"

setup_gcs

if echo "test" | nexus write /test-gcs.txt --input - 2>&1 | grep -q "Wrote"; then
    echo -e "${GREEN}✓ GCS credentials available${NC}"

    echo -e "\n${YELLOW}Writing to GCS backend:${NC}"
    echo "  Command: nexus write /gcs-file.txt \"Stored in GCS\""
    echo "GCS content" | nexus write /gcs-file.txt --input - 2>/dev/null
    echo -e "${GREEN}✓ Written to GCS${NC}"

    echo -e "\n${YELLOW}Reading from GCS backend:${NC}"
    echo "  Command: nexus cat /gcs-file.txt"
    echo "  Content: $(nexus cat /gcs-file.txt 2>/dev/null)"

    echo -e "\n${YELLOW}Listing GCS backend:${NC}"
    echo "  Command: nexus ls /"
    nexus ls / 2>/dev/null

    # Cleanup test file
    nexus rm /test-gcs.txt --force 2>/dev/null || true
    nexus rm /gcs-file.txt --force 2>/dev/null || true
else
    echo -e "${YELLOW}⚠ GCS credentials not available${NC}"
    echo "  To enable: gcloud auth application-default login"
    echo -e "  Skipping GCS operations"
fi

# Demo: Switching back
echo -e "\n======================================================================"
echo "PART 3: Switching Between Backends"
echo "======================================================================"

echo -e "\n${YELLOW}Demonstration of easy backend switching:${NC}"

echo -e "\n1. Switch to local backend:"
echo "   ${BLUE}setup_local${NC}"
setup_local
echo "   → Using: $NEXUS_BACKEND backend at $NEXUS_DATA_DIR"

echo -e "\n2. Perform local operations:"
echo "   ${BLUE}nexus ls /${NC}"
nexus ls / 2>/dev/null | head -3

echo -e "\n3. Switch to GCS backend:"
echo "   ${BLUE}setup_gcs${NC}"
setup_gcs
echo "   → Using: $NEXUS_BACKEND backend"
echo "   → Bucket: $NEXUS_GCS_BUCKET_NAME"

# Summary
echo -e "\n======================================================================"
echo "Summary"
echo "======================================================================"

echo -e "\n${GREEN}✨ Benefits of Environment Variables:${NC}"
echo "  1. Clean commands: 'nexus cat /file.txt' instead of 5 flags"
echo "  2. Easy switching: Call setup_local() or setup_gcs()"
echo "  3. Less errors: Configure once, use everywhere"
echo "  4. Realistic: How users actually use Nexus"

echo -e "\n${BLUE}📝 Helper Functions Pattern:${NC}"
echo "  setup_local() {"
echo "    export NEXUS_BACKEND=local"
echo "    export NEXUS_DATA_DIR=./local-data"
echo "  }"
echo ""
echo "  setup_gcs() {"
echo "    export NEXUS_BACKEND=gcs"
echo "    export NEXUS_GCS_BUCKET_NAME=nexi-hub"
echo "    export NEXUS_GCS_PROJECT_ID=nexi-lab-888"
echo "  }"

echo -e "\n${BLUE}💡 Pro Tip:${NC}"
echo "  Add these functions to your ~/.bashrc or ~/.zshrc"
echo "  Then use them in any terminal session!"

# Cleanup
echo -e "\n${BLUE}Cleaning up...${NC}"
rm -rf "$(dirname "$LOCAL_DIR")"
rm -rf "$(dirname "$GCS_META_DIR")"
echo -e "${GREEN}✓ Done!${NC}"
