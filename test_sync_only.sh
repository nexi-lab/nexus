#!/bin/bash
# Isolated test for sync operation (Test 70-72)

set -e

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

# Test workspace
TEST_WORKSPACE="/tmp/nexus-sync-only-test-$$"
DATA_DIR="$TEST_WORKSPACE/nexus-data"
export NEXUS_DATA_DIR="$DATA_DIR"

echo -e "${BLUE}Setting up test workspace...${NC}"
mkdir -p "$TEST_WORKSPACE"

# Cleanup on exit
trap "rm -rf '$TEST_WORKSPACE'" EXIT

# Create sync test directories
SYNC_TEST_DIR="$TEST_WORKSPACE/sync-test"
mkdir -p "$SYNC_TEST_DIR/source"
mkdir -p "$SYNC_TEST_DIR/dest"

# Create test files
echo "File 1 content" > "$SYNC_TEST_DIR/source/file1.txt"
echo "File 2 content" > "$SYNC_TEST_DIR/source/file2.txt"
mkdir -p "$SYNC_TEST_DIR/source/subdir"
echo "File 3 content" > "$SYNC_TEST_DIR/source/subdir/file3.txt"

echo -e "${BLUE}Initializing Nexus workspace...${NC}"
uv run nexus init "$TEST_WORKSPACE"

echo -e "${BLUE}Test 70: Sync dry-run${NC}"
uv run nexus sync "$SYNC_TEST_DIR/source/" /sync-dest/ --dry-run
echo -e "${GREEN}✓ PASSED${NC}"

echo -e "${BLUE}Test 71: Actual sync${NC}"
uv run nexus sync "$SYNC_TEST_DIR/source/" /sync-dest/
echo -e "${GREEN}✓ PASSED${NC}"

echo -e "${BLUE}Test 72: Verify sync created files${NC}"
if uv run nexus cat /sync-dest/file1.txt; then
    echo -e "${GREEN}✓ PASSED${NC}"
else
    echo -e "${RED}✗ FAILED${NC}"
    echo "Debugging info:"
    echo "Files in /sync-dest:"
    uv run nexus ls /sync-dest --long || echo "ls failed"
    echo "CAS directory contents:"
    find "$DATA_DIR/workspace/cas" -type f | head -20 || echo "find failed"
    exit 1
fi

echo -e "${BLUE}Test 73: Re-sync (should skip identical)${NC}"
uv run nexus sync "$SYNC_TEST_DIR/source/" /sync-dest/
echo -e "${GREEN}✓ PASSED${NC}"

echo -e "${GREEN}All sync tests passed!${NC}"
