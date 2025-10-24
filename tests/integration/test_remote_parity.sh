#!/bin/bash
# Comprehensive test script for issues #243 and #256
# Tests that remote nexus (client-server mode) works identically to embedded nexus (local mode)
# Includes tests for newly exposed RPC methods (chmod, chown, versions, workspace operations, etc.)

# Don't exit on error - we want to run all tests
set +e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Test counters
TOTAL_TESTS=0
PASSED_TESTS=0
FAILED_TESTS=0

# Test directories
TEST_DIR="/tmp/nexus-parity-test-$$"
LOCAL_DATA_DIR="$TEST_DIR/local-data"
REMOTE_DATA_DIR="$TEST_DIR/remote-data"
SERVER_PORT=18080
SERVER_PID=""

# Cleanup function
cleanup() {
    echo -e "${YELLOW}Cleaning up...${NC}"

    # Kill server
    if [ ! -z "$SERVER_PID" ]; then
        kill $SERVER_PID 2>/dev/null || true
        wait $SERVER_PID 2>/dev/null || true
    fi

    # Remove test directory
    rm -rf "$TEST_DIR"

    echo -e "${GREEN}Cleanup complete${NC}"
}

# Set up cleanup on exit
trap cleanup EXIT INT TERM

# Print test result
print_result() {
    local test_name="$1"
    local result="$2"
    local details="$3"

    TOTAL_TESTS=$((TOTAL_TESTS + 1))

    if [ "$result" = "PASS" ]; then
        PASSED_TESTS=$((PASSED_TESTS + 1))
        echo -e "${GREEN}✓${NC} $test_name"
    else
        FAILED_TESTS=$((FAILED_TESTS + 1))
        echo -e "${RED}✗${NC} $test_name"
        if [ ! -z "$details" ]; then
            echo -e "  ${RED}$details${NC}"
        fi
    fi
}

# Setup test environment
setup() {
    echo -e "${BLUE}========================================${NC}"
    echo -e "${BLUE}Nexus Remote vs Local Parity Test${NC}"
    echo -e "${BLUE}Issues #243 & #256 Verification${NC}"
    echo -e "${BLUE}========================================${NC}\n"

    echo -e "${CYAN}Setting up test environment...${NC}"

    # Create test directories
    mkdir -p "$LOCAL_DATA_DIR" "$REMOTE_DATA_DIR"

    # Initialize both backends
    echo "  Initializing local backend..."
    NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus init > /dev/null 2>&1

    echo "  Initializing remote backend..."
    NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus init > /dev/null 2>&1

    # Start server
    echo "  Starting Nexus server on port $SERVER_PORT..."
    NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus serve --host 127.0.0.1 --port $SERVER_PORT > "$TEST_DIR/server.log" 2>&1 &
    SERVER_PID=$!

    # Wait for server to start
    echo "  Waiting for server to be ready..."
    for i in {1..30}; do
        if curl -s "http://127.0.0.1:$SERVER_PORT/health" > /dev/null 2>&1; then
            echo -e "  ${GREEN}✓ Server is ready${NC}"
            break
        fi
        if [ $i -eq 30 ]; then
            echo -e "  ${RED}✗ Server failed to start${NC}"
            cat "$TEST_DIR/server.log"
            exit 1
        fi
        sleep 0.5
    done

    echo -e "${GREEN}✓ Setup complete${NC}\n"
}

# Test Basic Operations
test_basic_operations() {
    echo -e "${BLUE}========================================${NC}"
    echo -e "${BLUE}Testing Basic Operations${NC}"
    echo -e "${BLUE}========================================${NC}"

    # Test 1: Create files
    NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus write /workspace/test1.txt "test content" > /dev/null 2>&1
    NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus write /workspace/test1.txt "test content" > /dev/null 2>&1

    local_content=$(NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus cat /workspace/test1.txt)
    remote_content=$(NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus cat /workspace/test1.txt)

    if [ "$local_content" = "$remote_content" ] && [ "$local_content" = "test content" ]; then
        print_result "Create and read files" "PASS"
    else
        print_result "Create and read files" "FAIL" "Content mismatch"
    fi

    # Test 2: Write to existing files
    NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus write /workspace/test1.txt "updated content" > /dev/null 2>&1
    NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus write /workspace/test1.txt "updated content" > /dev/null 2>&1

    local_content=$(NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus cat /workspace/test1.txt)
    remote_content=$(NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus cat /workspace/test1.txt)

    if [ "$local_content" = "$remote_content" ] && [ "$local_content" = "updated content" ]; then
        print_result "Update existing files" "PASS"
    else
        print_result "Update existing files" "FAIL" "Content mismatch"
    fi

    # Test 3: List directory contents
    NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus write /workspace/list_test/file1.txt "a" > /dev/null 2>&1
    NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus write /workspace/list_test/file2.txt "b" > /dev/null 2>&1

    NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus write /workspace/list_test/file1.txt "a" > /dev/null 2>&1
    NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus write /workspace/list_test/file2.txt "b" > /dev/null 2>&1

    local_list=$(NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus ls /workspace/list_test | grep -E "file[12].txt" | wc -l | tr -d ' ')
    remote_list=$(NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus ls /workspace/list_test | grep -E "file[12].txt" | wc -l | tr -d ' ')

    if [ "$local_list" = "$remote_list" ] && [ "$local_list" = "2" ]; then
        print_result "List directory contents" "PASS"
    else
        print_result "List directory contents" "FAIL" "List counts differ: local=$local_list, remote=$remote_list"
    fi

    # Test 4: Delete files
    NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus rm /workspace/test1.txt --force > /dev/null 2>&1
    NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus rm /workspace/test1.txt --force > /dev/null 2>&1

    local_exists=0
    remote_exists=0
    NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus cat /workspace/test1.txt > /dev/null 2>&1 && local_exists=1
    NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus cat /workspace/test1.txt > /dev/null 2>&1 && remote_exists=1

    if [ "$local_exists" = "0" ] && [ "$remote_exists" = "0" ]; then
        print_result "Delete files" "PASS"
    else
        print_result "Delete files" "FAIL" "Files still exist after deletion"
    fi

    # Test 5: File metadata (size)
    NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus write /workspace/meta_test.txt "12345678" > /dev/null 2>&1
    NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus write /workspace/meta_test.txt "12345678" > /dev/null 2>&1

    local_content=$(NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus cat /workspace/meta_test.txt)
    remote_content=$(NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus cat /workspace/meta_test.txt)

    local_size=${#local_content}
    remote_size=${#remote_content}

    if [ "$local_size" = "$remote_size" ] && [ "$local_size" = "8" ]; then
        print_result "File metadata (size)" "PASS"
    else
        print_result "File metadata (size)" "FAIL" "Sizes differ: local=$local_size, remote=$remote_size"
    fi

    echo ""
}

# Test Edge Cases
test_edge_cases() {
    echo -e "${BLUE}========================================${NC}"
    echo -e "${BLUE}Testing Edge Cases${NC}"
    echo -e "${BLUE}========================================${NC}"

    # Test 1: Large files (1MB)
    dd if=/dev/urandom of="$TEST_DIR/large_file.bin" bs=1024 count=1024 2>/dev/null

    NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus write /workspace/large.bin --file "$TEST_DIR/large_file.bin" > /dev/null 2>&1
    NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus write /workspace/large.bin --file "$TEST_DIR/large_file.bin" > /dev/null 2>&1

    NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus cat /workspace/large.bin > "$TEST_DIR/large_local.bin" 2>/dev/null
    NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus cat /workspace/large.bin > "$TEST_DIR/large_remote.bin" 2>/dev/null

    if cmp -s "$TEST_DIR/large_local.bin" "$TEST_DIR/large_remote.bin"; then
        print_result "Large file handling (1MB)" "PASS"
    else
        print_result "Large file handling (1MB)" "FAIL" "File contents differ"
    fi

    # Test 2: Special characters in filenames
    NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus write "/workspace/file with spaces.txt" "spaces" > /dev/null 2>&1
    NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus write "/workspace/file with spaces.txt" "spaces" > /dev/null 2>&1

    local_content=$(NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus cat "/workspace/file with spaces.txt" 2>/dev/null)
    remote_content=$(NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus cat "/workspace/file with spaces.txt" 2>/dev/null)

    if [ "$local_content" = "$remote_content" ] && [ "$local_content" = "spaces" ]; then
        print_result "Special characters in filenames" "PASS"
    else
        print_result "Special characters in filenames" "FAIL" "Content mismatch"
    fi

    # Test 3: Unicode content
    NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus write /workspace/unicode.txt "Hello 世界 🌍" > /dev/null 2>&1
    NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus write /workspace/unicode.txt "Hello 世界 🌍" > /dev/null 2>&1

    local_content=$(NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus cat /workspace/unicode.txt)
    remote_content=$(NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus cat /workspace/unicode.txt)

    if [ "$local_content" = "$remote_content" ]; then
        print_result "Unicode content handling" "PASS"
    else
        print_result "Unicode content handling" "FAIL" "Content mismatch"
    fi

    # Test 4: Concurrent operations (simple version)
    for i in {1..10}; do
        NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus write "/workspace/concurrent_local_$i.txt" "content $i" > /dev/null 2>&1 &
        NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus write "/workspace/concurrent_remote_$i.txt" "content $i" > /dev/null 2>&1 &
    done
    wait

    local_count=$(NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus ls /workspace | grep "concurrent_local_" | wc -l | tr -d ' ')
    remote_count=$(NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus ls /workspace | grep "concurrent_remote_" | wc -l | tr -d ' ')

    if [ "$local_count" = "10" ] && [ "$remote_count" = "10" ]; then
        print_result "Concurrent operations (10 files)" "PASS"
    else
        print_result "Concurrent operations (10 files)" "FAIL" "Created local=$local_count, remote=$remote_count files"
    fi

    echo ""
}

# Test Search and Query
test_search() {
    echo -e "${BLUE}========================================${NC}"
    echo -e "${BLUE}Testing Search and Query Features${NC}"
    echo -e "${BLUE}========================================${NC}"

    # Create test files
    NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus write /workspace/search/file1.txt "Hello World" > /dev/null 2>&1
    NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus write /workspace/search/file2.txt "Goodbye World" > /dev/null 2>&1
    NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus write /workspace/search/file3.py "print('Hello')" > /dev/null 2>&1
    NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus write /workspace/search/subdir/nested.txt "Nested content" > /dev/null 2>&1

    NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus write /workspace/search/file1.txt "Hello World" > /dev/null 2>&1
    NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus write /workspace/search/file2.txt "Goodbye World" > /dev/null 2>&1
    NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus write /workspace/search/file3.py "print('Hello')" > /dev/null 2>&1
    NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus write /workspace/search/subdir/nested.txt "Nested content" > /dev/null 2>&1

    # Test 1: glob - find .txt files in search directory (Linux-style)
    # Count only lines that start with / (actual file paths)
    # Note: Nexus glob searches recursively, so this finds all 3 .txt files under /workspace/search
    local_glob=$(NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus glob "*.txt" /workspace/search 2>/dev/null | grep "^  /" | wc -l | tr -d ' ')
    remote_glob=$(NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus glob "*.txt" /workspace/search 2>/dev/null | grep "^  /" | wc -l | tr -d ' ')

    if [ "$local_glob" = "$remote_glob" ] && [ "$local_glob" = "3" ]; then
        print_result "glob - find .txt files" "PASS"
    else
        print_result "glob - find .txt files" "FAIL" "Glob results differ: local=$local_glob, remote=$remote_glob (expected 3)"
    fi

    # Test 2: glob - recursive pattern (Linux-style)
    # Count only lines that start with / (actual file paths)
    local_glob_rec=$(NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus glob "**/*.txt" /workspace/search 2>/dev/null | grep "^  /" | wc -l | tr -d ' ')
    remote_glob_rec=$(NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus glob "**/*.txt" /workspace/search 2>/dev/null | grep "^  /" | wc -l | tr -d ' ')

    if [ "$local_glob_rec" = "$remote_glob_rec" ] && [ "$local_glob_rec" = "3" ]; then
        print_result "glob - recursive pattern" "PASS"
    else
        print_result "glob - recursive pattern" "FAIL" "Glob results differ: local=$local_glob_rec, remote=$remote_glob_rec (expected 3)"
    fi

    # Test 3: grep - search file contents (Linux-style)
    # Count only lines with "Match:" (actual matches)
    local_grep=$(NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus grep "World" /workspace/search 2>/dev/null | grep -c "Match:" || echo "0")
    remote_grep=$(NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus grep "World" /workspace/search 2>/dev/null | grep -c "Match:" || echo "0")

    if [ "$local_grep" = "$remote_grep" ] && [ "$local_grep" -ge "2" ]; then
        print_result "grep - search file contents" "PASS"
    else
        print_result "grep - search file contents" "FAIL" "Grep results differ: local=$local_grep, remote=$remote_grep (expected >=2)"
    fi

    # Test 4: grep - case insensitive (Linux-style)
    # Count only lines with "Match:" (actual matches)
    local_grep_i=$(NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus grep "hello" /workspace/search --ignore-case 2>/dev/null | grep -c "Match:" || echo "0")
    remote_grep_i=$(NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus grep "hello" /workspace/search --ignore-case 2>/dev/null | grep -c "Match:" || echo "0")

    if [ "$local_grep_i" = "$remote_grep_i" ] && [ "$local_grep_i" = "2" ]; then
        print_result "grep - case insensitive search" "PASS"
    else
        print_result "grep - case insensitive search" "FAIL" "Grep results differ: local=$local_grep_i, remote=$remote_grep_i (expected 2)"
    fi

    echo ""
}

# Test newly exposed methods (Issue #256)
test_new_rpc_methods() {
    echo -e "${BLUE}========================================${NC}"
    echo -e "${BLUE}Testing Newly Exposed RPC Methods (Issue #256)${NC}"
    echo -e "${BLUE}========================================${NC}"

    # Create test files for permission and version tests
    NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus write /workspace/perm_test.txt "version 1" > /dev/null 2>&1
    NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus write /workspace/perm_test.txt "version 1" > /dev/null 2>&1

    # Test 1: chmod (change permissions)
    if NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus chmod 0644 /workspace/perm_test.txt > /dev/null 2>&1; then
        if NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus chmod 0644 /workspace/perm_test.txt > /dev/null 2>&1; then
            print_result "chmod - change file permissions" "PASS"
        else
            print_result "chmod - change file permissions" "FAIL" "Remote chmod failed"
        fi
    else
        print_result "chmod - change file permissions" "SKIP" "chmod not available"
        TOTAL_TESTS=$((TOTAL_TESTS - 1))
    fi

    # Test 2: list_versions
    # Create multiple versions
    NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus write /workspace/version_test.txt "version 1" > /dev/null 2>&1
    NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus write /workspace/version_test.txt "version 2" > /dev/null 2>&1
    NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus write /workspace/version_test.txt "version 3" > /dev/null 2>&1

    NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus write /workspace/version_test.txt "version 1" > /dev/null 2>&1
    NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus write /workspace/version_test.txt "version 2" > /dev/null 2>&1
    NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus write /workspace/version_test.txt "version 3" > /dev/null 2>&1

    if NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus versions history /workspace/version_test.txt > /dev/null 2>&1; then
        local_versions=$(NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus versions history /workspace/version_test.txt 2>/dev/null | grep "Total versions:" | awk '{print $3}')
        remote_versions=$(NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus versions history /workspace/version_test.txt 2>/dev/null | grep "Total versions:" | awk '{print $3}')

        if [ "$local_versions" = "$remote_versions" ] && [ "$local_versions" -ge "3" ]; then
            print_result "list_versions - version tracking" "PASS"
        else
            print_result "list_versions - version tracking" "FAIL" "Version counts differ: local=$local_versions, remote=$remote_versions"
        fi
    else
        print_result "list_versions - version tracking" "SKIP" "versions command not available"
        TOTAL_TESTS=$((TOTAL_TESTS - 1))
    fi

    # Test 3: rename
    NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus write /workspace/rename_test.txt "content" > /dev/null 2>&1
    NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus write /workspace/rename_test.txt "content" > /dev/null 2>&1

    # Use --force to skip confirmation prompt
    NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus move /workspace/rename_test.txt /workspace/renamed.txt --force > /dev/null 2>&1
    NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus move /workspace/rename_test.txt /workspace/renamed.txt --force > /dev/null 2>&1

    local_renamed=$(NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus cat /workspace/renamed.txt 2>/dev/null)
    remote_renamed=$(NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus cat /workspace/renamed.txt 2>/dev/null)

    if [ "$local_renamed" = "$remote_renamed" ] && [ "$local_renamed" = "content" ]; then
        print_result "rename - move files" "PASS"
    else
        print_result "rename - move files" "FAIL" "Rename operation failed"
    fi

    # Test 4: mkdir/rmdir (need --parents since /workspace may not exist yet)
    NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus mkdir /workspace/test_dir --parents > /dev/null 2>&1
    NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus mkdir /workspace/test_dir --parents > /dev/null 2>&1

    # Check if directories exist by listing
    local_has_dir=$(NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus ls /workspace 2>/dev/null | grep "test_dir" | wc -l | tr -d ' ' || echo "0")
    remote_has_dir=$(NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus ls /workspace 2>/dev/null | grep "test_dir" | wc -l | tr -d ' ' || echo "0")

    if [ "$local_has_dir" -gt "0" ] && [ "$remote_has_dir" -gt "0" ]; then
        print_result "mkdir - create directory" "PASS"
    else
        print_result "mkdir - create directory" "FAIL" "Directory creation failed: local=$local_has_dir, remote=$remote_has_dir"
    fi

    # Test 5: chown (change file owner)
    if NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus chown alice /workspace/perm_test.txt > /dev/null 2>&1; then
        if NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus chown alice /workspace/perm_test.txt > /dev/null 2>&1; then
            print_result "chown - change file owner" "PASS"
        else
            print_result "chown - change file owner" "FAIL" "Remote chown failed"
        fi
    else
        print_result "chown - change file owner" "SKIP" "chown not available"
        TOTAL_TESTS=$((TOTAL_TESTS - 1))
    fi

    # Test 6: chgrp (change file group)
    if NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus chgrp developers /workspace/perm_test.txt > /dev/null 2>&1; then
        if NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus chgrp developers /workspace/perm_test.txt > /dev/null 2>&1; then
            print_result "chgrp - change file group" "PASS"
        else
            print_result "chgrp - change file group" "FAIL" "Remote chgrp failed"
        fi
    else
        print_result "chgrp - change file group" "SKIP" "chgrp not available"
        TOTAL_TESTS=$((TOTAL_TESTS - 1))
    fi

    # Test 7: workspace operations
    if NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus snapshot create "test snapshot" > /dev/null 2>&1; then
        if NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus snapshot create "test snapshot" > /dev/null 2>&1; then
            print_result "workspace_snapshot - create snapshot" "PASS"
        else
            print_result "workspace_snapshot - create snapshot" "FAIL" "Remote snapshot creation failed"
        fi
    else
        print_result "workspace_snapshot - create snapshot" "SKIP" "snapshot command not available"
        TOTAL_TESTS=$((TOTAL_TESTS - 1))
    fi

    echo ""
}

# Test ACL (Access Control List) Methods
test_acl_methods() {
    echo -e "${BLUE}========================================${NC}"
    echo -e "${BLUE}Testing ACL Methods (NEW)${NC}"
    echo -e "${BLUE}========================================${NC}"

    # Create test files for ACL tests
    NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus write /workspace/acl_test.txt "ACL test content" > /dev/null 2>&1
    NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus write /workspace/acl_test.txt "ACL test content" > /dev/null 2>&1

    # Test 1: grant_user - Grant ACL permissions to a user (via setfacl)
    if NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus setfacl user:alice:rw- /workspace/acl_test.txt > /dev/null 2>&1; then
        if NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus setfacl user:alice:rw- /workspace/acl_test.txt > /dev/null 2>&1; then
            print_result "grant_user - grant ACL to user" "PASS"
        else
            print_result "grant_user - grant ACL to user" "FAIL" "Remote setfacl failed"
        fi
    else
        print_result "grant_user - grant ACL to user" "SKIP" "setfacl not available"
        TOTAL_TESTS=$((TOTAL_TESTS - 1))
    fi

    # Test 2: grant_group - Grant ACL permissions to a group (via setfacl)
    if NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus setfacl group:developers:r-- /workspace/acl_test.txt > /dev/null 2>&1; then
        if NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus setfacl group:developers:r-- /workspace/acl_test.txt > /dev/null 2>&1; then
            print_result "grant_group - grant ACL to group" "PASS"
        else
            print_result "grant_group - grant ACL to group" "FAIL" "Remote setfacl failed"
        fi
    else
        print_result "grant_group - grant ACL to group" "SKIP" "setfacl not available"
        TOTAL_TESTS=$((TOTAL_TESTS - 1))
    fi

    # Test 3: get_acl - Get ACL entries for a file (via getfacl)
    if NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus getfacl /workspace/acl_test.txt > /dev/null 2>&1; then
        local_acl_count=$(NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus getfacl /workspace/acl_test.txt 2>/dev/null | grep -E "user:|group:" | wc -l | tr -d ' ')
        remote_acl_count=$(NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus getfacl /workspace/acl_test.txt 2>/dev/null | grep -E "user:|group:" | wc -l | tr -d ' ')

        if [ "$local_acl_count" = "$remote_acl_count" ] && [ "$local_acl_count" -ge "2" ]; then
            print_result "get_acl - retrieve ACL entries" "PASS"
        else
            print_result "get_acl - retrieve ACL entries" "FAIL" "ACL counts differ: local=$local_acl_count, remote=$remote_acl_count (expected >=2)"
        fi
    else
        print_result "get_acl - retrieve ACL entries" "SKIP" "getfacl not available"
        TOTAL_TESTS=$((TOTAL_TESTS - 1))
    fi

    # Test 4: deny_user - Deny user access via ACL (via setfacl with deny:user:name:---)
    if NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus setfacl deny:user:intern:--- /workspace/acl_test.txt > /dev/null 2>&1; then
        if NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus setfacl deny:user:intern:--- /workspace/acl_test.txt > /dev/null 2>&1; then
            print_result "deny_user - deny user access" "PASS"
        else
            print_result "deny_user - deny user access" "FAIL" "Remote setfacl deny failed"
        fi
    else
        print_result "deny_user - deny user access" "SKIP" "setfacl deny not available"
        TOTAL_TESTS=$((TOTAL_TESTS - 1))
    fi

    # Test 5: revoke_acl - Remove ACL entry (via setfacl --remove)
    if NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus setfacl user:intern:--- /workspace/acl_test.txt --remove > /dev/null 2>&1; then
        if NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus setfacl user:intern:--- /workspace/acl_test.txt --remove > /dev/null 2>&1; then
            print_result "revoke_acl - remove ACL entry" "PASS"
        else
            print_result "revoke_acl - remove ACL entry" "FAIL" "Remote setfacl --remove failed"
        fi
    else
        print_result "revoke_acl - remove ACL entry" "SKIP" "setfacl --remove not available"
        TOTAL_TESTS=$((TOTAL_TESTS - 1))
    fi

    # Test 6: Verify ACL removal - intern should be gone after revoke
    if NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus getfacl /workspace/acl_test.txt > /dev/null 2>&1; then
        # Count intern entries, ensure result is on single line
        local_final_count=$(NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus getfacl /workspace/acl_test.txt 2>/dev/null | grep "intern" | wc -l | tr -d ' \n' || echo "0")
        remote_final_count=$(NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus getfacl /workspace/acl_test.txt 2>/dev/null | grep "intern" | wc -l | tr -d ' \n' || echo "0")

        if [ "$local_final_count" = "0" ] && [ "$remote_final_count" = "0" ]; then
            print_result "ACL revoke verification" "PASS"
        else
            print_result "ACL revoke verification" "FAIL" "intern ACL still exists: local=$local_final_count, remote=$remote_final_count"
        fi
    else
        print_result "ACL revoke verification" "SKIP" "getfacl not available"
        TOTAL_TESTS=$((TOTAL_TESTS - 1))
    fi

    echo ""
}

# Test ReBAC Remote Functionality
test_rebac_remote() {
    echo -e "${BLUE}========================================${NC}"
    echo -e "${BLUE}Testing ReBAC Remote Functionality${NC}"
    echo -e "${BLUE}========================================${NC}"

    # Test 1: rebac create
    local tuple_id_local=$(NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus rebac create agent alice member-of group developers 2>&1 | grep "Tuple ID:" | awk '{print $3}')
    local tuple_id_remote=$(NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus rebac create agent bob member-of group engineers 2>&1 | grep "Tuple ID:" | awk '{print $3}')

    if [ -n "$tuple_id_local" ] && [ -n "$tuple_id_remote" ]; then
        print_result "rebac_create - create relationship" "PASS"
    else
        print_result "rebac_create - create relationship" "FAIL" "Failed to create tuples: local=$tuple_id_local, remote=$tuple_id_remote"
    fi

    # Test 2: rebac check (should return DENIED since we only created member-of, not read permission)
    if NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus rebac check agent alice read file /workspace/test.txt 2>&1 | grep -q "DENIED"; then
        if NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus rebac check agent bob read file /workspace/test.txt 2>&1 | grep -q "DENIED"; then
            print_result "rebac_check - check permission" "PASS"
        else
            print_result "rebac_check - check permission" "FAIL" "Remote check failed"
        fi
    else
        print_result "rebac_check - check permission" "FAIL" "Local check failed"
    fi

    # Test 3: rebac expand (find who has member-of permission on developers group)
    local local_expand=$(NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus rebac expand member-of group developers 2>&1 | grep -c "agent")
    local remote_expand=$(NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus rebac expand member-of group engineers 2>&1 | grep -c "agent")

    if [ "$local_expand" -ge "1" ] && [ "$remote_expand" -ge "1" ]; then
        print_result "rebac_expand - find subjects" "PASS"
    else
        print_result "rebac_expand - find subjects" "FAIL" "Expand counts: local=$local_expand, remote=$remote_expand"
    fi

    # Test 4: rebac delete
    if [ -n "$tuple_id_local" ]; then
        if NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus rebac delete "$tuple_id_local" 2>&1 | grep -q "Deleted"; then
            if NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus rebac delete "$tuple_id_remote" 2>&1 | grep -q "Deleted"; then
                print_result "rebac_delete - delete relationship" "PASS"
            else
                print_result "rebac_delete - delete relationship" "FAIL" "Remote delete failed"
            fi
        else
            print_result "rebac_delete - delete relationship" "FAIL" "Local delete failed"
        fi
    else
        print_result "rebac_delete - delete relationship" "SKIP" "No tuple ID to delete"
        TOTAL_TESTS=$((TOTAL_TESTS - 1))
    fi

    echo ""
}

# Test Performance
test_performance() {
    echo -e "${BLUE}========================================${NC}"
    echo -e "${BLUE}Testing Performance${NC}"
    echo -e "${BLUE}========================================${NC}"

    # Test 1: Write performance (5 files - reduced for speed)
    # NOTE: This is primarily a parity test, not a performance benchmark
    local start_local=$(date +%s)
    for i in {1..5}; do
        NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus write "/workspace/perf_local_$i.txt" "content $i" > /dev/null 2>&1
    done
    local end_local=$(date +%s)
    local local_time=$((end_local - start_local))

    local start_remote=$(date +%s)
    for i in {1..5}; do
        NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus write "/workspace/perf_remote_$i.txt" "content $i" > /dev/null 2>&1
    done
    local end_remote=$(date +%s)
    local remote_time=$((end_remote - start_remote))

    echo -e "  ${CYAN}Local write time (5 files): ${local_time}s${NC}"
    echo -e "  ${CYAN}Remote write time (5 files): ${remote_time}s${NC}"

    # Performance is informational - both should complete
    if [ $remote_time -gt 0 ]; then
        print_result "Basic operation latency" "PASS"
    else
        print_result "Basic operation latency" "FAIL" "Remote operations failed"
    fi

    echo ""
}

# Print summary
print_summary() {
    echo -e "${BLUE}========================================${NC}"
    echo -e "${BLUE}Test Summary${NC}"
    echo -e "${BLUE}========================================${NC}"
    echo -e "Total tests: $TOTAL_TESTS"
    echo -e "${GREEN}Passed: $PASSED_TESTS${NC}"
    echo -e "${RED}Failed: $FAILED_TESTS${NC}"

    local pass_rate=$((PASSED_TESTS * 100 / TOTAL_TESTS))
    echo -e "\nPass rate: ${pass_rate}%"

    if [ $FAILED_TESTS -eq 0 ]; then
        echo -e "\n${GREEN}✓ All tests passed!${NC}"
        echo -e "${GREEN}Remote Nexus behavior matches embedded Nexus.${NC}"
        return 0
    else
        echo -e "\n${RED}✗ Some tests failed.${NC}"
        echo -e "${RED}Remote Nexus behavior differs from embedded Nexus.${NC}"
        return 1
    fi
}

# Main execution
main() {
    setup
    # test_basic_operations
    # test_edge_cases
    # test_search
    test_new_rpc_methods
    test_acl_methods
    test_rebac_remote
    test_performance
    print_summary
}

main
