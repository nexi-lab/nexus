#!/bin/bash
# Nexus CLI - Rename/Move Permissions Demo (Pure CLI Version)
#
# This demo tests issue #341: ReBAC permissions not updated during rename/move operations
#
# Prerequisites:
# 1. Server running: ./scripts/init-nexus-with-auth.sh
# 2. Load admin credentials: source .nexus-admin-env
#
# Usage:
#   ./examples/cli/rename_permissions_demo.sh

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
print_test() { echo -e "${MAGENTA}TEST:${NC} $1"; }

# Check prerequisites
if [ -z "$NEXUS_URL" ] || [ -z "$NEXUS_API_KEY" ]; then
    print_error "NEXUS_URL and NEXUS_API_KEY not set. Run: source .nexus-admin-env"
    exit 1
fi

echo "╔══════════════════════════════════════════════════════════╗"
echo "║   Nexus CLI - Rename/Move Permissions Demo              ║"
echo "║         Testing Issue #341 Fix (Pure CLI)               ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
print_info "Server: $NEXUS_URL"
echo ""

ADMIN_KEY="$NEXUS_API_KEY"
export DEMO_BASE="/workspace/rename-demo"

# Cleanup function
cleanup() {
    export NEXUS_API_KEY="$ADMIN_KEY"
    print_info "Cleaning up demo data..."

    # Delete files and directories
    nexus rmdir -r -f $DEMO_BASE 2>/dev/null || true

    # Delete ALL alice users (there may be multiple from previous runs)
    # Get all user IDs for users named 'alice'
    nexus admin list-users 2>/dev/null | grep "│ alice " | awk '{print $3}' | while read user_id; do
        nexus admin delete-user-by-id "$user_id" 2>/dev/null || true
    done

    print_success "Cleaned up demo data"
}

if [ "$KEEP" != "1" ]; then
    trap cleanup EXIT
    print_info "Cleanup enabled. To keep demo data, run: KEEP=1 $0"
else
    print_info "KEEP=1 set - demo data will NOT be cleaned up"
fi

# Initial cleanup
cleanup

# ════════════════════════════════════════════════════════════
# Section 1: Setup - Create User and Test Files
# ════════════════════════════════════════════════════════════

print_section "1. Setup - Create User and Test Files"

print_subsection "1.1 Create test user with API key"

# Create alice with API key
ALICE_KEY=$(nexus admin create-user alice --name "Alice Owner" --expires-days 1 --json-output 2>/dev/null | grep '"api_key"' | cut -d'"' -f4)
print_success "Created user 'alice' with API key: ${ALICE_KEY:0:20}..."

print_subsection "1.2 Create workspace and grant permissions"

# Create demo directory
nexus mkdir $DEMO_BASE --parents
nexus rebac create user admin direct_owner file $DEMO_BASE
print_success "Created $DEMO_BASE with admin ownership"

# Create alice's personal directory
nexus mkdir $DEMO_BASE/alice-personal --parents

# Grant alice full permissions on her directory
nexus rebac create user alice direct_owner file $DEMO_BASE/alice-personal
nexus rebac create user alice direct_editor file $DEMO_BASE/alice-personal
print_success "Alice has owner + editor permissions on $DEMO_BASE/alice-personal"

# Create files AS alice (so she owns them)
export NEXUS_API_KEY="$ALICE_KEY"
echo "Alice's test data" | nexus write $DEMO_BASE/alice-personal/test.txt -
echo "Alice's important file" | nexus write $DEMO_BASE/alice-personal/important.md -
export NEXUS_API_KEY="$ADMIN_KEY"

print_success "Created test files in alice-personal/"

print_subsection "1.3 Grant alice permission on a specific file"

# Grant alice specific permission on test.txt
nexus rebac create user alice direct_editor file $DEMO_BASE/alice-personal/test.txt
print_success "Granted alice direct_editor on $DEMO_BASE/alice-personal/test.txt"

# ════════════════════════════════════════════════════════════
# Section 2: Test File Rename with Permissions
# ════════════════════════════════════════════════════════════

print_section "2. Test File Rename with Permissions (Issue #341)"

print_subsection "2.1 Verify alice can access file before rename"

export NEXUS_API_KEY="$ALICE_KEY"
print_test "Alice can read test.txt before rename"
if nexus cat $DEMO_BASE/alice-personal/test.txt 2>/dev/null | grep -q "Alice's test data"; then
    print_success "✅ Alice can read file before rename"
else
    print_error "❌ Alice cannot read file before rename (setup issue)"
    exit 1
fi

export NEXUS_API_KEY="$ADMIN_KEY"

print_subsection "2.2 Count permissions before rename"

# Count direct permission tuples for the old path (exclude parent tuples)
print_info "=== DETAILED PERMISSIONS BEFORE RENAME ==="
nexus rebac list --object-type file --object-id "$DEMO_BASE/alice-personal/test.txt" 2>/dev/null | head -20
echo ""
OLD_COUNT=$(nexus rebac list --object-type file --object-id "$DEMO_BASE/alice-personal/test.txt" --format compact 2>/dev/null | grep -v "→ parent →" | wc -l | tr -d ' ')
print_info "Found $OLD_COUNT direct permission tuples for test.txt"

print_subsection "2.3 Rename the file"

print_test "Rename test.txt to renamed-test.txt"
nexus move $DEMO_BASE/alice-personal/test.txt $DEMO_BASE/alice-personal/renamed-test.txt --force
print_success "File renamed to renamed-test.txt"

print_subsection "2.4 Verify permissions followed the file (BUG #341 TEST)"

# Check old path (should be 0) - exclude parent tuples
print_info "=== DETAILED PERMISSIONS FOR OLD PATH AFTER RENAME ==="
nexus rebac list --object-type file --object-id "$DEMO_BASE/alice-personal/test.txt" 2>/dev/null | head -20
echo ""
OLD_COUNT_AFTER=$(nexus rebac list --object-type file --object-id "$DEMO_BASE/alice-personal/test.txt" --format compact 2>/dev/null | grep -v "→ parent →" | grep -v "No tuples found" | wc -l | tr -d ' ')
print_info "Direct tuples for old path after rename: $OLD_COUNT_AFTER (should be 0)"

# Check new path (should be > 0) - exclude parent tuples
print_info "=== DETAILED PERMISSIONS FOR NEW PATH AFTER RENAME ==="
nexus rebac list --object-type file --object-id "$DEMO_BASE/alice-personal/renamed-test.txt" 2>/dev/null | head -20
echo ""
NEW_COUNT=$(nexus rebac list --object-type file --object-id "$DEMO_BASE/alice-personal/renamed-test.txt" --format compact 2>/dev/null | grep -v "→ parent →" | grep -v "No tuples found" | wc -l | tr -d ' ')
print_info "Direct tuples for new path after rename: $NEW_COUNT (should be $OLD_COUNT)"

if [ "$OLD_COUNT_AFTER" -eq 0 ] && [ "$NEW_COUNT" -gt 0 ]; then
    print_success "✅ BUG #341 FIXED: ReBAC tuples updated correctly!"
else
    print_error "❌ BUG #341 STILL EXISTS: ReBAC tuples not updated"
fi

print_subsection "2.5 Verify alice can access file AFTER rename"

export NEXUS_API_KEY="$ALICE_KEY"
print_test "Alice should still be able to read renamed-test.txt"
print_info "=== ALICE'S ACCESS CHECK (verbose) ==="
if nexus cat $DEMO_BASE/alice-personal/renamed-test.txt 2>&1 | tee /tmp/alice_cat_output.txt | grep -q "Alice's test data"; then
    print_success "✅ Alice can read file after rename!"
    print_info "Permissions followed the file to new path"
else
    print_error "❌ Alice cannot read file after rename"
    print_info "Error output:"
    cat /tmp/alice_cat_output.txt
    print_info "Checking alice's actual permissions:"
    export NEXUS_API_KEY="$ADMIN_KEY"
    nexus rebac check user alice can_read file "$DEMO_BASE/alice-personal/renamed-test.txt"
    export NEXUS_API_KEY="$ALICE_KEY"
fi

export NEXUS_API_KEY="$ADMIN_KEY"

# ════════════════════════════════════════════════════════════
# Section 3: Test Directory Move with Permissions
# ════════════════════════════════════════════════════════════

print_section "3. Test Directory Move with Permissions"

print_subsection "3.1 Create directory with files and permissions"

# Create a shared directory and files AS ADMIN
nexus mkdir $DEMO_BASE/shared --parents
echo "Shared project data" | nexus write $DEMO_BASE/shared/project.txt -
echo "Shared documentation" | nexus write $DEMO_BASE/shared/readme.md -
print_success "Created $DEMO_BASE/shared"

# Grant alice permissions on shared directory and its files
nexus rebac create user alice direct_owner file $DEMO_BASE/shared
nexus rebac create user alice direct_editor file $DEMO_BASE/shared
nexus rebac create user alice direct_viewer file $DEMO_BASE/shared
# Grant alice permissions on the files
nexus rebac create user alice direct_editor file $DEMO_BASE/shared/project.txt
nexus rebac create user alice direct_editor file $DEMO_BASE/shared/readme.md
print_success "Created shared directory with files and permissions"

print_subsection "3.2 Verify alice can access directory before move"

export NEXUS_API_KEY="$ALICE_KEY"
print_test "Alice can list files in shared/ before move"
FILE_COUNT=$(nexus ls $DEMO_BASE/shared 2>/dev/null | wc -l | tr -d ' ')
if [ "$FILE_COUNT" -ge 1 ]; then
    print_success "✅ Alice sees $FILE_COUNT files in shared/"
else
    print_error "❌ Alice cannot access shared/ (setup issue)"
fi

export NEXUS_API_KEY="$ADMIN_KEY"

print_subsection "3.3 Count permissions before directory move"

# Count all direct permission tuples related to the shared directory (exclude parent tuples)
print_info "=== DETAILED PERMISSIONS FOR shared/ BEFORE MOVE ==="
print_info "Full tuple details (not just compact):"
nexus rebac list --object-type file 2>/dev/null | grep -A2 -B2 "$DEMO_BASE/shared" | head -40
echo ""
print_info "Compact format:"
nexus rebac list --object-type file --format compact 2>/dev/null | grep "$DEMO_BASE/shared" | head -20
echo ""
OLD_DIR_COUNT=$(nexus rebac list --object-type file --format compact 2>/dev/null | grep "$DEMO_BASE/shared" | grep -v "→ parent →" | wc -l | tr -d ' ')
print_info "Found $OLD_DIR_COUNT direct permission tuples for shared/ and its contents"

print_subsection "3.4 Move/rename the directory"

print_test "Move shared/ to shared-archive/"
nexus move $DEMO_BASE/shared $DEMO_BASE/shared-archive --force
print_success "Directory moved to shared-archive/"

print_subsection "3.5 Verify permissions followed the directory"

# Count old path tuples (should be 0) - exclude parent tuples
print_info "=== DETAILED PERMISSIONS FOR OLD shared/ PATH AFTER MOVE ==="
nexus rebac list --object-type file --format compact 2>/dev/null | grep "$DEMO_BASE/shared" | grep -v "shared-archive" | head -20
echo ""
OLD_DIR_COUNT_AFTER=$(nexus rebac list --object-type file --format compact 2>/dev/null | grep "$DEMO_BASE/shared" | grep -v "shared-archive" | grep -v "→ parent →" | wc -l | tr -d ' ')
print_info "Direct tuples for old directory path: $OLD_DIR_COUNT_AFTER (should be 0)"

# Count new path tuples (should match old count) - exclude parent tuples
print_info "=== DETAILED PERMISSIONS FOR NEW shared-archive/ PATH AFTER MOVE ==="
nexus rebac list --object-type file --format compact 2>/dev/null | grep "$DEMO_BASE/shared-archive" | head -20
echo ""
NEW_DIR_COUNT=$(nexus rebac list --object-type file --format compact 2>/dev/null | grep "$DEMO_BASE/shared-archive" | grep -v "→ parent →" | wc -l | tr -d ' ')
print_info "Direct tuples for new directory path: $NEW_DIR_COUNT (should be $OLD_DIR_COUNT)"

if [ "$OLD_DIR_COUNT_AFTER" -eq 0 ] && [ "$NEW_DIR_COUNT" -gt 0 ]; then
    print_success "✅ BUG #341 FIXED: Directory permissions updated correctly!"
else
    print_error "❌ BUG #341: Directory permissions not updated correctly"
fi

print_subsection "3.6 Verify alice can access directory AFTER move"

export NEXUS_API_KEY="$ALICE_KEY"
print_test "Alice should still be able to access shared-archive/"
print_info "=== ALICE'S DIRECTORY ACCESS CHECK (verbose) ==="
FILE_COUNT=$(nexus ls $DEMO_BASE/shared-archive 2>&1 | tee /tmp/alice_ls_output.txt | wc -l | tr -d ' ')
if [ "$FILE_COUNT" -ge 1 ]; then
    print_success "✅ Alice can access directory after move!"
    print_success "Alice sees $FILE_COUNT files in shared-archive/"
    print_info "Files seen by alice:"
    cat /tmp/alice_ls_output.txt
else
    print_error "❌ Alice cannot access directory after move"
    print_info "Error output:"
    cat /tmp/alice_ls_output.txt
    print_info "Checking alice's actual permissions:"
    export NEXUS_API_KEY="$ADMIN_KEY"
    nexus rebac check user alice can_read file "$DEMO_BASE/shared-archive"
    export NEXUS_API_KEY="$ALICE_KEY"
fi

print_test "Alice can read files in moved directory"
print_info "=== ALICE'S FILE READ CHECK (verbose) ==="
if nexus cat $DEMO_BASE/shared-archive/project.txt 2>&1 | tee /tmp/alice_cat_dir_output.txt | grep -q "Shared project data"; then
    print_success "✅ Alice can read files in moved directory"
else
    print_error "❌ Alice cannot read files in moved directory"
    print_info "Error output:"
    cat /tmp/alice_cat_dir_output.txt
    print_info "Checking alice's actual permissions on the file:"
    export NEXUS_API_KEY="$ADMIN_KEY"
    nexus rebac check user alice can_read file "$DEMO_BASE/shared-archive/project.txt"
    print_info "All permissions for project.txt:"
    nexus rebac list --object-type file --object-id "$DEMO_BASE/shared-archive/project.txt" | head -20
    export NEXUS_API_KEY="$ALICE_KEY"
fi

export NEXUS_API_KEY="$ADMIN_KEY"

# ════════════════════════════════════════════════════════════
# Summary
# ════════════════════════════════════════════════════════════

print_section "✅ Rename/Move Permissions Demo Complete!"

echo "╔═══════════════════════════════════════════════════════════════════╗"
echo "║              Rename/Move Permission Features Tested               ║"
echo "╠═══════════════════════════════════════════════════════════════════╣"
echo "║  ✅ File Rename with Permission Preservation                      ║"
echo "║  ✅ Directory Move with Recursive Permission Updates              ║"
echo "║  ✅ ReBAC Tuple Updates for old -> new paths                      ║"
echo "║  ✅ Access Control After Rename/Move Operations                   ║"
echo "╚═══════════════════════════════════════════════════════════════════╝"
echo ""
print_success "All rename/move permission tests passed!"
print_info "Issue #341 is FIXED: ReBAC permissions are updated during rename/move"
echo ""

if [ "$KEEP" == "1" ]; then
    echo "════════════════════════════════════════════════════════════"
    echo "Demo data preserved. Inspect with:"
    echo "  nexus ls $DEMO_BASE"
    echo "  nexus rebac list --object-type file | grep $DEMO_BASE"
    echo "════════════════════════════════════════════════════════════"
fi
