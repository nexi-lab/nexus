#!/bin/bash
# Test script for Nexus CLI commands
# This script tests all CLI functionality end-to-end

set -e  # Exit on error

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Test workspace
TEST_WORKSPACE="/tmp/nexus-cli-test-$$"
DATA_DIR="$TEST_WORKSPACE/nexus-data"

echo -e "${BLUE}================================${NC}"
echo -e "${BLUE}Nexus CLI Test Suite${NC}"
echo -e "${BLUE}================================${NC}"
echo ""

# Cleanup function
cleanup() {
    echo -e "\n${YELLOW}Cleaning up test workspace...${NC}"
    rm -rf "$TEST_WORKSPACE"
}

# Register cleanup
trap cleanup EXIT

# Test counter
TESTS_RUN=0
TESTS_PASSED=0

# Test helper function
test_command() {
    local description="$1"
    shift
    TESTS_RUN=$((TESTS_RUN + 1))

    echo -e "${BLUE}Test $TESTS_RUN:${NC} $description"

    if "$@"; then
        echo -e "${GREEN}✓ PASSED${NC}"
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo ""
        return 0
    else
        echo -e "${RED}✗ FAILED${NC}"
        echo ""
        return 1
    fi
}

# Install nexus if not already installed
if ! command -v nexus &> /dev/null; then
    echo -e "${YELLOW}Installing Nexus CLI...${NC}"
    pip install -e . > /dev/null 2>&1
fi

echo -e "${GREEN}Starting CLI tests...${NC}\n"

# Test 1: Initialize workspace
test_command "Initialize workspace" \
    nexus init "$TEST_WORKSPACE"

# Test 2: List empty workspace
test_command "List empty workspace" \
    nexus ls /workspace --data-dir "$DATA_DIR"

# Test 3: Create directory
test_command "Create directory" \
    nexus mkdir /workspace/data --data-dir "$DATA_DIR"

# Test 4: Create nested directory with --parents
test_command "Create nested directory" \
    nexus mkdir /workspace/deep/nested/dir --parents --data-dir "$DATA_DIR"

# Test 5: Write file with string content
test_command "Write file with string content" \
    nexus write /workspace/hello.txt "Hello, Nexus!" --data-dir "$DATA_DIR"

# Test 6: Write Python file
test_command "Write Python file" \
    bash -c "echo 'def hello():\n    print(\"Hello World\")' | nexus write /workspace/code.py --input - --data-dir $DATA_DIR"

# Test 7: Write JSON file
test_command "Write JSON file" \
    bash -c "echo '{\"name\": \"test\", \"value\": 42}' | nexus write /workspace/data.json --input - --data-dir $DATA_DIR"

# Test 8: Write Markdown file
test_command "Write Markdown file" \
    bash -c "echo '# Test Document\n\n## Section 1\n\nSome content here.' | nexus write /workspace/README.md --input - --data-dir $DATA_DIR"

# Test 9: List files
test_command "List files in /workspace" \
    nexus ls /workspace --data-dir "$DATA_DIR"

# Test 10: List files recursively
test_command "List files recursively" \
    nexus ls /workspace --recursive --data-dir "$DATA_DIR"

# Test 11: List files with details
test_command "List files with details" \
    nexus ls /workspace --long --data-dir "$DATA_DIR"

# Test 12: Cat text file
test_command "Display text file" \
    nexus cat /workspace/hello.txt --data-dir "$DATA_DIR"

# Test 13: Cat Python file (with syntax highlighting)
test_command "Display Python file with syntax highlighting" \
    nexus cat /workspace/code.py --data-dir "$DATA_DIR"

# Test 14: Copy file
test_command "Copy file" \
    nexus cp /workspace/hello.txt /workspace/hello_copy.txt --data-dir "$DATA_DIR"

# Test 15: Glob - find all .txt files
test_command "Find all .txt files" \
    nexus glob "*.txt" --path /workspace --data-dir "$DATA_DIR"

# Test 16: Glob - find all files recursively
test_command "Find all files with ** pattern" \
    nexus glob "**/*" --data-dir "$DATA_DIR"

# Test 17: Glob - find Python files
test_command "Find Python files" \
    nexus glob "**/*.py" --data-dir "$DATA_DIR"

# Test 18: Grep - search for "Hello"
test_command "Grep search for 'Hello'" \
    nexus grep "Hello" --data-dir "$DATA_DIR"

# Test 19: Grep - search in Python files only
test_command "Grep in Python files only" \
    nexus grep "def" --file-pattern "**/*.py" --data-dir "$DATA_DIR"

# Test 20: Grep - case-insensitive search
test_command "Grep case-insensitive search" \
    nexus grep "HELLO" --ignore-case --data-dir "$DATA_DIR"

# Test 21: Info - show file details
test_command "Show file information" \
    nexus info /workspace/hello.txt --data-dir "$DATA_DIR"

# Test 22: Delete file
test_command "Delete file" \
    nexus rm /workspace/hello_copy.txt --force --data-dir "$DATA_DIR"

# Test 23: Verify deletion
test_command "Verify file was deleted" \
    bash -c "! nexus cat /workspace/hello_copy.txt --data-dir $DATA_DIR 2>/dev/null"

# Populate /workspace/data for rmdir test
echo -e "${BLUE}Populating /workspace/data for rmdir test...${NC}"
nexus write /workspace/data/testfile.txt "test content" --data-dir "$DATA_DIR"

# Test 24: Remove directory (should fail - not empty)
test_command "Try to remove non-empty directory" \
    bash -c "! nexus rmdir /workspace/data --force --data-dir $DATA_DIR 2>/dev/null"

# Test 25: Remove directory recursively
test_command "Remove directory recursively" \
    nexus rmdir /workspace/data --recursive --force --data-dir "$DATA_DIR"

# Test 26: Version command
test_command "Show version information" \
    nexus version --data-dir "$DATA_DIR"

# Test 27: Help command
test_command "Show help" \
    nexus --help

# Test 28: Command-specific help
test_command "Show ls command help" \
    nexus ls --help

# Test 29: Write multiple test files for advanced grep
echo -e "${BLUE}Creating test files for advanced operations...${NC}"
nexus write /workspace/test1.py "# TODO: implement feature\ndef test():\n    pass" --data-dir "$DATA_DIR"
nexus write /workspace/test2.py "def another_test():\n    # TODO: add tests\n    return 42" --data-dir "$DATA_DIR"
nexus write /workspace/test3.txt "This file has TODO items\nAnd ERROR messages" --data-dir "$DATA_DIR"

test_command "Grep with multiple matches" \
    nexus grep "TODO" --data-dir "$DATA_DIR"

# Test 30: Complex glob pattern
test_command "Complex glob with test_*.py pattern" \
    nexus glob "test*.py" --path /workspace --data-dir "$DATA_DIR"

# Summary
echo -e "${BLUE}================================${NC}"
echo -e "${BLUE}Test Summary${NC}"
echo -e "${BLUE}================================${NC}"
echo -e "Total tests run: ${TESTS_RUN}"
echo -e "${GREEN}Tests passed: ${TESTS_PASSED}${NC}"

if [ $TESTS_PASSED -eq $TESTS_RUN ]; then
    echo -e "\n${GREEN}✓ All tests passed!${NC}"
    exit 0
else
    TESTS_FAILED=$((TESTS_RUN - TESTS_PASSED))
    echo -e "${RED}Tests failed: ${TESTS_FAILED}${NC}"
    echo -e "\n${RED}✗ Some tests failed${NC}"
    exit 1
fi
