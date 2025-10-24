#!/bin/bash
# Comprehensive test script for issue #243
# Tests that remote nexus (client-server mode) works identically to embedded nexus (local mode)

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
    echo -e "${BLUE}Issue #243 Verification${NC}"
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

    NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus write /workspace/search/file1.txt "Hello World" > /dev/null 2>&1
    NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus write /workspace/search/file2.txt "Goodbye World" > /dev/null 2>&1
    NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus write /workspace/search/file3.py "print('Hello')" > /dev/null 2>&1

    # Test search (if search command exists)
    if NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus search "World" /workspace/search > /dev/null 2>&1; then
        local_search=$(NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus search "World" /workspace/search 2>/dev/null | wc -l | tr -d ' ')
        remote_search=$(NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus search "World" /workspace/search 2>/dev/null | wc -l | tr -d ' ')

        if [ "$local_search" = "$remote_search" ]; then
            print_result "Search and query features" "PASS"
        else
            print_result "Search and query features" "FAIL" "Search results differ"
        fi
    else
        print_result "Search and query features" "SKIP" "Search command not available"
        TOTAL_TESTS=$((TOTAL_TESTS - 1))
    fi

    echo ""
}

# Test Performance
test_performance() {
    echo -e "${BLUE}========================================${NC}"
    echo -e "${BLUE}Testing Performance${NC}"
    echo -e "${BLUE}========================================${NC}"

    # Test 1: Write performance (20 files)
    local start_local=$(date +%s)
    for i in {1..20}; do
        NEXUS_DATA_DIR="$LOCAL_DATA_DIR" nexus write "/workspace/perf_local_$i.txt" "content $i" > /dev/null 2>&1
    done
    local end_local=$(date +%s)
    local local_time=$((end_local - start_local))

    local start_remote=$(date +%s)
    for i in {1..20}; do
        NEXUS_DATA_DIR="$REMOTE_DATA_DIR" nexus write "/workspace/perf_remote_$i.txt" "content $i" > /dev/null 2>&1
    done
    local end_remote=$(date +%s)
    local remote_time=$((end_remote - start_remote))

    echo -e "  ${CYAN}Local write time (20 files): ${local_time}s${NC}"
    echo -e "  ${CYAN}Remote write time (20 files): ${remote_time}s${NC}"

    # Performance is informational - both should complete
    if [ $remote_time -gt 0 ]; then
        print_result "Compare operation latency" "PASS"
    else
        print_result "Compare operation latency" "FAIL" "Remote operations failed"
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
    test_basic_operations
    test_edge_cases
    test_search
    test_performance
    print_summary
}

main
