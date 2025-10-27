#!/bin/bash
# Nexus CLI - Permissions Demo with Multiple Users
#
# Demonstrates ReBAC permission system with comprehensive CRUD testing:
# - Creating multiple users with different permission levels (owner, editor, viewer)
# - Testing file CRUD operations (Create, Read, Update, Delete)
# - Testing directory CRUD operations
# - Verifying permission enforcement
# - Testing permission inheritance
# - Testing access denial scenarios
#
# Prerequisites:
# 1. Server running: ./scripts/init-nexus-with-auth.sh
# 2. Load admin credentials: source .nexus-admin-env
#
# Usage:
#   ./examples/cli/permissions_demo.sh

set -e

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
NC='\033[0m' # No Color

print_section() {
    echo ""
    echo "================================================================"
    echo "  $1"
    echo "================================================================"
    echo ""
}

print_subsection() {
    echo ""
    echo "--- $1 ---"
    echo ""
}

print_success() {
    echo -e "${GREEN}✓${NC} $1"
}

print_info() {
    echo -e "${BLUE}ℹ${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}⚠${NC} $1"
}

print_error() {
    echo -e "${RED}✗${NC} $1"
}

print_user() {
    local user=$1
    local action=$2
    echo -e "${CYAN}[${user}]${NC} ${action}"
}

print_test() {
    echo -e "${MAGENTA}TEST:${NC} $1"
}

# Check prerequisites
if [ -z "$NEXUS_URL" ]; then
    print_error "NEXUS_URL not set. Please run:"
    echo "  source .nexus-admin-env"
    exit 1
fi

if [ -z "$NEXUS_API_KEY" ]; then
    print_error "NEXUS_API_KEY not set. Please run:"
    echo "  source .nexus-admin-env"
    exit 1
fi

echo "╔════════════════════════════════════════════════════════╗"
echo "║    Nexus CLI - Permissions Demo (Multiple Users)      ║"
echo "╚════════════════════════════════════════════════════════╝"
echo ""
print_info "Server: $NEXUS_URL"
print_info "Admin Key: ${NEXUS_API_KEY:0:20}..."
echo ""

# Store original admin key
ADMIN_KEY="$NEXUS_API_KEY"

# ============================================
# Step 1: Setup - Create Users and API Keys
# ============================================

print_section "1. Creating Users and API Keys"

# Clear ReBAC cache to ensure fresh permission checks
print_info "Clearing ReBAC cache..."
PGPASSWORD=nexus psql -h localhost -U postgres -d nexus -c "DELETE FROM rebac_check_cache;" 2>/dev/null || true

# Setup workspace base path
DEMO_BASE="/workspace/permissions-demo"

# Clean up any existing demo data
print_info "Cleaning up previous demo data..."
nexus rm -r $DEMO_BASE 2>/dev/null || true

# Clean up stale metadata
PGPASSWORD=nexus psql -h localhost -U postgres -d nexus -c "DELETE FROM file_paths WHERE virtual_path LIKE '/workspace/permissions-demo%';" 2>/dev/null || true

# Create base directory
nexus mkdir $DEMO_BASE --parents
print_success "Created base directory: $DEMO_BASE"

# Grant admin full access to base directory
nexus rebac create user admin direct_owner file $DEMO_BASE --tenant-id default 2>/dev/null || true
nexus rebac create user admin direct_viewer file $DEMO_BASE --tenant-id default 2>/dev/null || true
print_success "Admin has full access to $DEMO_BASE"

# Create API keys for test users
print_info "Creating API keys for test users..."

# Alice - Project Owner (will own project1)
ALICE_KEY=$(python3 scripts/create-api-key.py alice "Alice Demo Key" --days 90 2>/dev/null | grep "API Key:" | awk '{print $3}')
print_success "Created API key for Alice (Owner)"

# Bob - Editor (can read/write but not manage permissions)
BOB_KEY=$(python3 scripts/create-api-key.py bob "Bob Demo Key" --days 90 2>/dev/null | grep "API Key:" | awk '{print $3}')
print_success "Created API key for Bob (Editor)"

# Charlie - Viewer (read-only access)
CHARLIE_KEY=$(python3 scripts/create-api-key.py charlie "Charlie Demo Key" --days 90 2>/dev/null | grep "API Key:" | awk '{print $3}')
print_success "Created API key for Charlie (Viewer)"

# Dave - Isolated user (will own project2, no access to project1)
DAVE_KEY=$(python3 scripts/create-api-key.py dave "Dave Demo Key" --days 90 2>/dev/null | grep "API Key:" | awk '{print $3}')
print_success "Created API key for Dave (Isolated)"

# Eve - No permissions (will be denied access to everything)
EVE_KEY=$(python3 scripts/create-api-key.py eve "Eve Demo Key" --days 90 2>/dev/null | grep "API Key:" | awk '{print $3}')
print_success "Created API key for Eve (No permissions)"

echo ""
print_info "User Summary:"
echo "  Alice:   Owner of project1 (full access)"
echo "  Bob:     Editor of project1 (read/write, no permission management)"
echo "  Charlie: Viewer of project1 (read-only)"
echo "  Dave:    Owner of project2 (isolated from project1)"
echo "  Eve:     No permissions (access denied)"

# ============================================
# Step 2: Create Project Structure
# ============================================

print_section "2. Creating Project Structure"

# Create project directories
nexus mkdir $DEMO_BASE/project1 --parents
print_success "Created: $DEMO_BASE/project1"

nexus mkdir $DEMO_BASE/project2 --parents
print_success "Created: $DEMO_BASE/project2"

nexus mkdir $DEMO_BASE/shared --parents
print_success "Created: $DEMO_BASE/shared"

# ============================================
# Step 3: Grant Permissions
# ============================================

print_section "3. Granting Permissions"

print_info "Setting up permissions for project1..."

# Alice - Owner of project1
nexus rebac create user alice direct_owner file $DEMO_BASE/project1 --tenant-id default
print_success "Alice: owner of project1"

# Bob - Editor of project1
nexus rebac create user bob direct_editor file $DEMO_BASE/project1 --tenant-id default
print_success "Bob: editor of project1"

# Charlie - Viewer of project1
nexus rebac create user charlie direct_viewer file $DEMO_BASE/project1 --tenant-id default
print_success "Charlie: viewer of project1"

echo ""
print_info "Setting up permissions for project2..."

# Dave - Owner of project2 (isolated)
nexus rebac create user dave direct_owner file $DEMO_BASE/project2 --tenant-id default
print_success "Dave: owner of project2"

echo ""
print_info "Setting up permissions for shared directory..."

# Shared - Everyone can read
nexus rebac create user alice direct_viewer file $DEMO_BASE/shared --tenant-id default
nexus rebac create user bob direct_viewer file $DEMO_BASE/shared --tenant-id default
nexus rebac create user charlie direct_viewer file $DEMO_BASE/shared --tenant-id default
nexus rebac create user dave direct_viewer file $DEMO_BASE/shared --tenant-id default
print_success "All users can read shared directory"

# ============================================
# Step 4: Verify Permission Checks
# ============================================

print_section "4. Verifying Permission Checks"

print_subsection "4.1 Alice (Owner) - Full Access"

print_test "Alice should have write access to project1"
if nexus rebac check user alice write file $DEMO_BASE/project1 2>&1 | grep -q "GRANTED"; then
    print_success "Alice: write access GRANTED"
else
    print_error "Alice: write access DENIED (unexpected!)"
fi

print_test "Alice should have read access to project1"
if nexus rebac check user alice read file $DEMO_BASE/project1 2>&1 | grep -q "GRANTED"; then
    print_success "Alice: read access GRANTED"
else
    print_error "Alice: read access DENIED (unexpected!)"
fi

print_test "Alice should have execute (manage permissions) access to project1"
if nexus rebac check user alice execute file $DEMO_BASE/project1 2>&1 | grep -q "GRANTED"; then
    print_success "Alice: execute access GRANTED"
else
    print_error "Alice: execute access DENIED (unexpected!)"
fi

print_subsection "4.2 Bob (Editor) - Read/Write Only"

print_test "Bob should have write access to project1"
if nexus rebac check user bob write file $DEMO_BASE/project1 2>&1 | grep -q "GRANTED"; then
    print_success "Bob: write access GRANTED"
else
    print_error "Bob: write access DENIED (unexpected!)"
fi

print_test "Bob should have read access to project1"
if nexus rebac check user bob read file $DEMO_BASE/project1 2>&1 | grep -q "GRANTED"; then
    print_success "Bob: read access GRANTED"
else
    print_error "Bob: read access DENIED (unexpected!)"
fi

print_test "Bob should NOT have execute access to project1"
if nexus rebac check user bob execute file $DEMO_BASE/project1 2>&1 | grep -q "DENIED"; then
    print_success "Bob: execute access DENIED (as expected)"
else
    print_error "Bob: execute access GRANTED (unexpected!)"
fi

print_subsection "4.3 Charlie (Viewer) - Read Only"

print_test "Charlie should have read access to project1"
if nexus rebac check user charlie read file $DEMO_BASE/project1 2>&1 | grep -q "GRANTED"; then
    print_success "Charlie: read access GRANTED"
else
    print_error "Charlie: read access DENIED (unexpected!)"
fi

print_test "Charlie should NOT have write access to project1"
if nexus rebac check user charlie write file $DEMO_BASE/project1 2>&1 | grep -q "DENIED"; then
    print_success "Charlie: write access DENIED (as expected)"
else
    print_error "Charlie: write access GRANTED (unexpected!)"
fi

print_subsection "4.4 Dave - Isolated (No Access to project1)"

print_test "Dave should NOT have read access to project1"
if nexus rebac check user dave read file $DEMO_BASE/project1 2>&1 | grep -q "DENIED"; then
    print_success "Dave: read access to project1 DENIED (as expected)"
else
    print_error "Dave: read access to project1 GRANTED (unexpected!)"
fi

print_test "Dave should have write access to project2"
if nexus rebac check user dave write file $DEMO_BASE/project2 2>&1 | grep -q "GRANTED"; then
    print_success "Dave: write access to project2 GRANTED"
else
    print_error "Dave: write access to project2 DENIED (unexpected!)"
fi

print_subsection "4.5 Eve - No Permissions"

print_test "Eve should NOT have access to project1"
if nexus rebac check user eve read file $DEMO_BASE/project1 2>&1 | grep -q "DENIED"; then
    print_success "Eve: read access to project1 DENIED (as expected)"
else
    print_error "Eve: read access to project1 GRANTED (unexpected!)"
fi

print_test "Eve should NOT have access to project2"
if nexus rebac check user eve read file $DEMO_BASE/project2 2>&1 | grep -q "DENIED"; then
    print_success "Eve: read access to project2 DENIED (as expected)"
else
    print_error "Eve: read access to project2 GRANTED (unexpected!)"
fi

# ============================================
# Step 5: Test File CRUD Operations
# ============================================

print_section "5. Testing File CRUD Operations"

print_subsection "5.1 Alice (Owner) - CREATE file"

# Switch to Alice's API key
export NEXUS_API_KEY="$ALICE_KEY"

print_user "alice" "Creating file: project1/data.txt"
echo "Alice's initial data" > /tmp/alice-data.txt
if nexus write $DEMO_BASE/project1/data.txt /tmp/alice-data.txt 2>/dev/null; then
    print_success "Alice successfully created file"
else
    print_error "Alice failed to create file"
fi

print_subsection "5.2 Alice (Owner) - READ file"

print_user "alice" "Reading file: project1/data.txt"
if CONTENT=$(nexus cat $DEMO_BASE/project1/data.txt 2>/dev/null); then
    print_success "Alice successfully read file"
    echo "  Content: $CONTENT"
else
    print_error "Alice failed to read file"
fi

print_subsection "5.3 Bob (Editor) - READ file"

# Switch to Bob's API key
export NEXUS_API_KEY="$BOB_KEY"

print_user "bob" "Reading file: project1/data.txt"
if CONTENT=$(nexus cat $DEMO_BASE/project1/data.txt 2>/dev/null); then
    print_success "Bob successfully read file"
    echo "  Content: $CONTENT"
else
    print_error "Bob failed to read file"
fi

print_subsection "5.4 Bob (Editor) - UPDATE file"

print_user "bob" "Updating file: project1/data.txt"
echo "Bob's updated data" > /tmp/bob-data.txt
if nexus write $DEMO_BASE/project1/data.txt /tmp/bob-data.txt 2>/dev/null; then
    print_success "Bob successfully updated file"
else
    print_error "Bob failed to update file"
fi

# Verify update
if CONTENT=$(nexus cat $DEMO_BASE/project1/data.txt 2>/dev/null); then
    if [ "$CONTENT" = "Bob's updated data" ]; then
        print_success "File content correctly updated by Bob"
    else
        print_warning "File content mismatch: '$CONTENT'"
    fi
fi

print_subsection "5.5 Charlie (Viewer) - READ file (should succeed)"

# Switch to Charlie's API key
export NEXUS_API_KEY="$CHARLIE_KEY"

print_user "charlie" "Reading file: project1/data.txt"
if CONTENT=$(nexus cat $DEMO_BASE/project1/data.txt 2>/dev/null); then
    print_success "Charlie successfully read file (read-only access)"
    echo "  Content: $CONTENT"
else
    print_error "Charlie failed to read file"
fi

print_subsection "5.6 Charlie (Viewer) - UPDATE file (should fail)"

print_user "charlie" "Attempting to update file: project1/data.txt (should fail)"
echo "Charlie's attempt to write" > /tmp/charlie-data.txt
if nexus write $DEMO_BASE/project1/data.txt /tmp/charlie-data.txt 2>/dev/null; then
    print_error "Charlie successfully updated file (unexpected!)"
else
    print_success "Charlie denied write access (as expected)"
fi

print_subsection "5.7 Dave (Isolated) - READ file in project1 (should fail)"

# Switch to Dave's API key
export NEXUS_API_KEY="$DAVE_KEY"

print_user "dave" "Attempting to read file in project1 (should fail)"
if nexus cat $DEMO_BASE/project1/data.txt 2>/dev/null; then
    print_error "Dave successfully read project1 file (unexpected!)"
else
    print_success "Dave denied read access to project1 (as expected)"
fi

print_subsection "5.8 Dave (Isolated) - CREATE file in project2 (should succeed)"

print_user "dave" "Creating file in project2: project2/dave-file.txt"
echo "Dave's data in project2" > /tmp/dave-data.txt
if nexus write $DEMO_BASE/project2/dave-file.txt /tmp/dave-data.txt 2>/dev/null; then
    print_success "Dave successfully created file in project2"
else
    print_error "Dave failed to create file in project2"
fi

print_subsection "5.9 Eve (No Permissions) - All operations denied"

# Switch to Eve's API key
export NEXUS_API_KEY="$EVE_KEY"

print_user "eve" "Attempting to read file in project1 (should fail)"
if nexus cat $DEMO_BASE/project1/data.txt 2>/dev/null; then
    print_error "Eve successfully read file (unexpected!)"
else
    print_success "Eve denied read access (as expected)"
fi

print_user "eve" "Attempting to create file in project1 (should fail)"
echo "Eve's attempt" > /tmp/eve-data.txt
if nexus write $DEMO_BASE/project1/eve-file.txt /tmp/eve-data.txt 2>/dev/null; then
    print_error "Eve successfully created file (unexpected!)"
else
    print_success "Eve denied write access (as expected)"
fi

print_subsection "5.10 Alice (Owner) - DELETE file"

# Switch back to Alice's API key
export NEXUS_API_KEY="$ALICE_KEY"

print_user "alice" "Creating a file to delete: project1/temp.txt"
echo "Temporary file" > /tmp/temp.txt
nexus write $DEMO_BASE/project1/temp.txt /tmp/temp.txt 2>/dev/null

print_user "alice" "Deleting file: project1/temp.txt"
if nexus rm $DEMO_BASE/project1/temp.txt 2>/dev/null; then
    print_success "Alice successfully deleted file"
else
    print_error "Alice failed to delete file"
fi

# Verify deletion
if nexus cat $DEMO_BASE/project1/temp.txt 2>/dev/null; then
    print_error "File still exists after deletion"
else
    print_success "File successfully deleted (verified)"
fi

# ============================================
# Step 6: Test Directory CRUD Operations
# ============================================

print_section "6. Testing Directory CRUD Operations"

print_subsection "6.1 Alice (Owner) - CREATE directory"

print_user "alice" "Creating subdirectory: project1/docs"
if nexus mkdir $DEMO_BASE/project1/docs 2>/dev/null; then
    print_success "Alice successfully created directory"
else
    print_error "Alice failed to create directory"
fi

print_subsection "6.2 Bob (Editor) - CREATE nested directory"

# Switch to Bob's API key
export NEXUS_API_KEY="$BOB_KEY"

print_user "bob" "Creating nested directory: project1/docs/api"
if nexus mkdir $DEMO_BASE/project1/docs/api --parents 2>/dev/null; then
    print_success "Bob successfully created nested directory"
else
    print_error "Bob failed to create nested directory"
fi

print_subsection "6.3 Charlie (Viewer) - CREATE directory (should fail)"

# Switch to Charlie's API key
export NEXUS_API_KEY="$CHARLIE_KEY"

print_user "charlie" "Attempting to create directory: project1/charlie-dir (should fail)"
if nexus mkdir $DEMO_BASE/project1/charlie-dir 2>/dev/null; then
    print_error "Charlie successfully created directory (unexpected!)"
else
    print_success "Charlie denied directory creation (as expected)"
fi

print_subsection "6.4 Alice (Owner) - LIST directory"

# Switch back to Alice's API key
export NEXUS_API_KEY="$ALICE_KEY"

print_user "alice" "Listing contents of project1"
if nexus ls $DEMO_BASE/project1 2>/dev/null; then
    print_success "Alice successfully listed directory"
else
    print_error "Alice failed to list directory"
fi

print_subsection "6.5 Bob (Editor) - DELETE directory"

# Switch to Bob's API key
export NEXUS_API_KEY="$BOB_KEY"

print_user "bob" "Creating test directory to delete: project1/test-dir"
nexus mkdir $DEMO_BASE/project1/test-dir 2>/dev/null

print_user "bob" "Deleting directory: project1/test-dir"
if nexus rm -r $DEMO_BASE/project1/test-dir 2>/dev/null; then
    print_success "Bob successfully deleted directory"
else
    print_error "Bob failed to delete directory"
fi

# ============================================
# Step 7: Test Permission Inheritance
# ============================================

print_section "7. Testing Permission Inheritance"

# Switch back to admin to set up parent relations
export NEXUS_API_KEY="$ADMIN_KEY"

print_info "Setting up parent relations for permission inheritance..."

# Create nested structure
print_user "admin" "Creating nested directory: project1/docs/guides/advanced"
nexus mkdir $DEMO_BASE/project1/docs/guides/advanced --parents 2>/dev/null || true

# Create parent relations
print_info "Creating parent relations..."
nexus rebac create file $DEMO_BASE/project1/docs parent file $DEMO_BASE/project1 --tenant-id default 2>/dev/null || true
nexus rebac create file $DEMO_BASE/project1/docs/guides parent file $DEMO_BASE/project1/docs --tenant-id default 2>/dev/null || true
nexus rebac create file $DEMO_BASE/project1/docs/guides/advanced parent file $DEMO_BASE/project1/docs/guides --tenant-id default 2>/dev/null || true
print_success "Parent relations created"

print_subsection "7.1 Bob (Editor) - Inherited write access"

print_test "Bob should inherit write access to nested directories"
if nexus rebac check user bob write file $DEMO_BASE/project1/docs/guides/advanced 2>&1 | grep -q "GRANTED"; then
    print_success "Bob has inherited write access to nested directory"
else
    print_error "Bob does not have inherited write access"
fi

print_info "Explaining permission path for Bob..."
nexus rebac explain user bob write file $DEMO_BASE/project1/docs/guides/advanced || true

print_subsection "7.2 Charlie (Viewer) - Inherited read access"

print_test "Charlie should inherit read access to nested directories"
if nexus rebac check user charlie read file $DEMO_BASE/project1/docs/guides/advanced 2>&1 | grep -q "GRANTED"; then
    print_success "Charlie has inherited read access to nested directory"
else
    print_error "Charlie does not have inherited read access"
fi

print_subsection "7.3 Dave (Isolated) - No inherited access"

print_test "Dave should NOT inherit any access to project1 nested directories"
if nexus rebac check user dave read file $DEMO_BASE/project1/docs/guides/advanced 2>&1 | grep -q "DENIED"; then
    print_success "Dave has no inherited access (as expected)"
else
    print_error "Dave has inherited access (unexpected!)"
fi

# ============================================
# Step 8: Test Shared Directory Access
# ============================================

print_section "8. Testing Shared Directory Access"

# Create a file in shared directory as admin
print_user "admin" "Creating file in shared directory"
echo "Shared data for all users" > /tmp/shared-data.txt
nexus write $DEMO_BASE/shared/shared.txt /tmp/shared-data.txt

print_subsection "8.1 All users can read shared file"

for user in alice bob charlie dave; do
    case $user in
        alice) export NEXUS_API_KEY="$ALICE_KEY" ;;
        bob) export NEXUS_API_KEY="$BOB_KEY" ;;
        charlie) export NEXUS_API_KEY="$CHARLIE_KEY" ;;
        dave) export NEXUS_API_KEY="$DAVE_KEY" ;;
    esac

    print_user "$user" "Reading shared file"
    if nexus cat $DEMO_BASE/shared/shared.txt 2>/dev/null >/dev/null; then
        print_success "$user can read shared file"
    else
        print_error "$user cannot read shared file"
    fi
done

print_subsection "8.2 Users cannot write to shared (viewer access only)"

export NEXUS_API_KEY="$ALICE_KEY"
print_user "alice" "Attempting to write to shared directory (should fail - viewer only)"
echo "Alice's attempt" > /tmp/alice-shared.txt
if nexus write $DEMO_BASE/shared/alice-file.txt /tmp/alice-shared.txt 2>/dev/null; then
    print_error "Alice can write to shared directory (unexpected!)"
else
    print_success "Alice denied write access to shared (as expected)"
fi

# ============================================
# Step 9: Test Permission Updates
# ============================================

print_section "9. Testing Permission Updates"

# Switch back to admin
export NEXUS_API_KEY="$ADMIN_KEY"

print_subsection "9.1 Promote Charlie from Viewer to Editor"

print_info "Removing Charlie's viewer permission..."
nexus rebac delete user charlie direct_viewer file $DEMO_BASE/project1 --tenant-id default 2>/dev/null || true

print_info "Granting Charlie editor permission..."
nexus rebac create user charlie direct_editor file $DEMO_BASE/project1 --tenant-id default

print_test "Charlie should now have write access"
if nexus rebac check user charlie write file $DEMO_BASE/project1 2>&1 | grep -q "GRANTED"; then
    print_success "Charlie promoted to editor successfully"
else
    print_error "Charlie still does not have write access"
fi

# Verify Charlie can now write
export NEXUS_API_KEY="$CHARLIE_KEY"
print_user "charlie" "Creating file as editor: project1/charlie-promoted.txt"
echo "Charlie as editor" > /tmp/charlie-promoted.txt
if nexus write $DEMO_BASE/project1/charlie-promoted.txt /tmp/charlie-promoted.txt 2>/dev/null; then
    print_success "Charlie successfully wrote file as editor"
else
    print_error "Charlie still cannot write"
fi

print_subsection "9.2 Revoke Alice's permissions and restore"

# Switch back to admin
export NEXUS_API_KEY="$ADMIN_KEY"

print_info "Revoking Alice's owner permission..."
nexus rebac delete user alice direct_owner file $DEMO_BASE/project1 --tenant-id default 2>/dev/null || true

print_test "Alice should no longer have write access"
if nexus rebac check user alice write file $DEMO_BASE/project1 2>&1 | grep -q "DENIED"; then
    print_success "Alice's write access revoked"
else
    print_error "Alice still has write access"
fi

print_info "Restoring Alice's owner permission..."
nexus rebac create user alice direct_owner file $DEMO_BASE/project1 --tenant-id default

print_test "Alice should have write access again"
if nexus rebac check user alice write file $DEMO_BASE/project1 2>&1 | grep -q "GRANTED"; then
    print_success "Alice's permissions restored"
else
    print_error "Alice does not have write access"
fi

# ============================================
# Step 10: List All Permissions
# ============================================

print_section "10. Listing All Permissions"

print_info "Users with write access to project1:"
nexus rebac expand write file $DEMO_BASE/project1 --tenant-id default || print_warning "Expand command may not be available"

echo ""
print_info "All permission tuples for project1:"
nexus rebac list --object file:$DEMO_BASE/project1 --tenant-id default || print_warning "List command may not be available"

# ============================================
# Summary
# ============================================

print_section "✅ Permissions Demo Complete!"

echo "You've tested:"
echo "  ✓ Creating multiple users with different permission levels"
echo "  ✓ File CRUD operations (Create, Read, Update, Delete)"
echo "  ✓ Directory CRUD operations"
echo "  ✓ Permission checks for owner, editor, and viewer roles"
echo "  ✓ Access denial for isolated and unpermissioned users"
echo "  ✓ Permission inheritance through parent relations"
echo "  ✓ Shared directory access"
echo "  ✓ Permission updates (promote/demote users)"
echo "  ✓ Permission listing and auditing"
echo ""
echo "Demo files created in $DEMO_BASE/"
echo ""
echo "To cleanup:"
echo "  export NEXUS_API_KEY='$ADMIN_KEY'"
echo "  nexus rm -r $DEMO_BASE"
echo ""
echo "Next steps:"
echo "  - See docs/getting-started/quickstart.md for more permission examples"
echo "  - See docs/api/permissions.md for permission API reference"
echo "  - Try examples/cli/directory_operations_demo.sh for directory operations"
echo ""

# Restore admin key
export NEXUS_API_KEY="$ADMIN_KEY"

# Cleanup temp files
rm -f /tmp/alice-data.txt /tmp/bob-data.txt /tmp/charlie-data.txt /tmp/dave-data.txt /tmp/eve-data.txt
rm -f /tmp/temp.txt /tmp/shared-data.txt /tmp/alice-shared.txt /tmp/charlie-promoted.txt

print_info "Cleanup: Removed temporary files"
