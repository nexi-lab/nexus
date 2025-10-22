#!/bin/bash
# Test CLI OCC (Optimistic Concurrency Control) features

set -e  # Exit on error

# Setup test directory
export TESTDIR="/tmp/nexus-cli-occ-test-$$"
export NEXUS_DATA_DIR="$TESTDIR"

echo "========================================================================"
echo "CLI Optimistic Concurrency Control Tests"
echo "========================================================================"
echo "Data directory: $TESTDIR"
echo

# Clean up on exit
cleanup() {
    rm -rf "$TESTDIR"
}
trap cleanup EXIT

# Test 1: Write with metadata
echo "Test 1: Write file with --show-metadata"
uv run nexus write /test.txt "Initial content" --show-metadata 2>&1 | grep -v RuntimeWarning | grep -v ffmpeg
echo

# Test 2: Read with metadata
echo "Test 2: Read file with --metadata"
uv run nexus cat /test.txt --metadata 2>&1 | grep -v RuntimeWarning | grep -v ffmpeg
echo

# Test 3: Create-only mode (success)
echo "Test 3: Create new file with --if-none-match (should succeed)"
uv run nexus write /new.txt "New file" --if-none-match --show-metadata 2>&1 | grep -v RuntimeWarning | grep -v ffmpeg
echo

# Test 4: Create-only mode (fail on existing)
echo "Test 4: Try to create existing file with --if-none-match (should fail)"
if uv run nexus write /new.txt "Duplicate" --if-none-match 2>&1 | grep -E "already exists|File already exists" > /dev/null; then
    echo "✓ Create-only mode correctly rejected existing file"
else
    echo "✗ Test failed!"
    exit 1
fi
echo

# Test 5: Get ETag for conditional write
echo "Test 5: Get current ETag from CLI"
ETAG=$(uv run nexus cat /test.txt --metadata 2>&1 | grep "ETag:" | awk '{print $2}')
echo "Current ETag: $ETAG"
echo

# Test 6: Conditional write with correct ETag
echo "Test 6: Update with correct --if-match (should succeed)"
uv run nexus write /test.txt "Updated content" --if-match "$ETAG" --show-metadata 2>&1 | grep -v RuntimeWarning | grep -v ffmpeg
echo

# Test 7: Conditional write with stale ETag
echo "Test 7: Try to update with stale --if-match (should fail)"
if uv run nexus write /test.txt "This should fail" --if-match "$ETAG" 2>&1 | grep "Conflict detected" > /dev/null; then
    echo "✓ Conflict detected as expected (ETag mismatch)"
else
    echo "✗ Test failed! Conflict should have been detected"
    exit 1
fi
echo

# Test 8: Force overwrite
echo "Test 8: Force overwrite with --force"
uv run nexus write /test.txt "Force overwritten" --force --show-metadata 2>&1 | grep -v RuntimeWarning | grep -v ffmpeg
echo

# Test 9: Verify final content
echo "Test 9: Verify final content"
uv run nexus cat /test.txt 2>&1 | grep -v RuntimeWarning | grep -v ffmpeg
echo

echo "========================================================================"
echo "✓ All CLI OCC tests passed!"
echo "========================================================================"
echo
echo "Available CLI options for optimistic concurrency control:"
echo "  nexus write --if-match <etag>     # Conditional write (fail if modified)"
echo "  nexus write --if-none-match       # Create-only (fail if exists)"
echo "  nexus write --force               # Force overwrite (skip version check)"
echo "  nexus write --show-metadata       # Show etag/version after write"
echo "  nexus cat --metadata              # Show file metadata (etag, version)"
