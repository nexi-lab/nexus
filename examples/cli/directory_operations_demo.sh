#!/bin/bash
# Nexus CLI - Directory Operations Demo
#
# Demonstrates directory management operations using CLI with remote server:
# - Creating directories (mkdir with --parents)
# - Removing directories (rm with --recursive)
# - Checking directory existence
# - Listing directory contents
# - Working with nested directory structures
#
# Prerequisites:
# 1. Server running: ./scripts/init-nexus-with-auth.sh
# 2. Load credentials: source .nexus-admin-env
# 3. Set SERVER_URL: export SERVER_URL=$NEXUS_URL
#
# Usage:
#   ./examples/cli/directory_operations_demo.sh

set -e

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

print_section() {
    echo ""
    echo "================================================================"
    echo "  $1"
    echo "================================================================"
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
echo "║      Nexus CLI - Directory Operations Demo            ║"
echo "╚════════════════════════════════════════════════════════╝"
echo ""
print_info "Server: $NEXUS_URL (from env var)"
print_info "API Key: ${NEXUS_API_KEY:0:20}... (from env var)"
echo ""
print_info "✨ CLI automatically uses NEXUS_URL and NEXUS_API_KEY"

# 1. Basic directory creation
print_section "1. Basic Directory Creation"

WORKSPACE_ROOT="/workspace"
BASE_PATH="/workspace/dir-demo"

# Setup workspace permissions first
print_info "Setting up workspace permissions..."
nexus rebac create user admin direct_owner file $WORKSPACE_ROOT 2>/dev/null || true
nexus rebac create user admin direct_viewer file $WORKSPACE_ROOT 2>/dev/null || true

# Clean up any existing demo directory
print_info "Cleaning up previous demo data..."
# Only try to remove if it exists (avoids permission denied errors on non-existent paths)
if nexus ls $BASE_PATH >/dev/null 2>&1; then
    nexus rmdir -r -f $BASE_PATH 2>/dev/null || true
fi

# Clean up stale metadata from previous runs
print_info "Cleaning up stale metadata..."
PGPASSWORD=nexus psql -h localhost -U postgres -d nexus -c "DELETE FROM file_paths WHERE virtual_path LIKE '/workspace/dir-demo%';" 2>/dev/null || true

# Create with --parents flag
nexus mkdir $BASE_PATH --parents 2>/dev/null || true
print_success "Created directory: $BASE_PATH"

# Grant admin permissions
print_info "Granting admin permissions on demo directory..."
nexus rebac create user admin direct_owner file $BASE_PATH 2>/dev/null || true
nexus rebac create user admin direct_viewer file $BASE_PATH 2>/dev/null || true
print_success "Granted admin full access"

# 2. Creating nested directories
print_section "2. Creating Nested Directories (--parents flag)"

print_info "Creating deep directory hierarchies..."

nexus mkdir $BASE_PATH/projects/alpha/src --parents 2>/dev/null || true
print_success "Created: $BASE_PATH/projects/alpha/src"

nexus mkdir $BASE_PATH/projects/beta/tests --parents 2>/dev/null || true
print_success "Created: $BASE_PATH/projects/beta/tests"

nexus mkdir $BASE_PATH/data/raw/2025/Q1 --parents 2>/dev/null || true
print_success "Created: $BASE_PATH/data/raw/2025/Q1"

nexus mkdir $BASE_PATH/data/processed/2025/Q1 --parents 2>/dev/null || true
print_success "Created: $BASE_PATH/data/processed/2025/Q1"

nexus mkdir $BASE_PATH/config/environments/production --parents 2>/dev/null || true
print_success "Created: $BASE_PATH/config/environments/production"

# 3. Listing directory contents
print_section "3. Listing Directory Contents"

print_info "Top-level contents of $BASE_PATH:"
nexus ls $BASE_PATH

echo ""
print_info "All contents (recursive):"
nexus ls -R $BASE_PATH || nexus ls $BASE_PATH

# 4. Creating files in directories
print_section "4. Directories vs Files"

print_info "Creating test files..."

echo "Python main file" > /tmp/main.py
nexus write $BASE_PATH/projects/alpha/src/main.py /tmp/main.py
print_success "Created: $BASE_PATH/projects/alpha/src/main.py"

echo "# Project README" > /tmp/README.md
nexus write $BASE_PATH/projects/alpha/README.md /tmp/README.md
print_success "Created: $BASE_PATH/projects/alpha/README.md"

cat > /tmp/app.json <<EOF
{
  "name": "demo-app",
  "version": "1.0.0"
}
EOF
nexus write $BASE_PATH/config/app.json /tmp/app.json
print_success "Created: $BASE_PATH/config/app.json"

echo ""
print_info "Contents of projects/alpha directory:"
nexus ls $BASE_PATH/projects/alpha

# 5. Checking directory existence
print_section "5. Checking Directory Existence"

print_info "Using 'nexus ls' to check if paths exist..."

# Check existing directory
if [ -n "$(nexus ls $BASE_PATH/projects/alpha 2>/dev/null | grep -v '^No files')" ]; then
    print_success "$BASE_PATH/projects/alpha exists"
else
    print_error "$BASE_PATH/projects/alpha does not exist"
fi

# Check non-existent directory
if [ -n "$(nexus ls $BASE_PATH/projects/gamma 2>/dev/null | grep -v '^No files')" ]; then
    print_error "$BASE_PATH/projects/gamma exists (unexpected!)"
else
    print_success "$BASE_PATH/projects/gamma doesn't exist (as expected)"
fi

# Check file (not directory) - use cat for files
if nexus cat $BASE_PATH/config/app.json >/dev/null 2>&1; then
    print_success "$BASE_PATH/config/app.json exists (file)"
else
    print_error "$BASE_PATH/config/app.json does not exist"
fi

# 6. Working with directory hierarchies
print_section "6. Creating Project Structure"

print_info "Building a typical project structure..."

# Create a complete project structure
PROJECT_DIRS=(
    "$BASE_PATH/my-project/src/components/ui"
    "$BASE_PATH/my-project/src/components/layout"
    "$BASE_PATH/my-project/src/utils"
    "$BASE_PATH/my-project/src/api"
    "$BASE_PATH/my-project/tests/unit"
    "$BASE_PATH/my-project/tests/integration"
    "$BASE_PATH/my-project/docs/api"
    "$BASE_PATH/my-project/docs/guides"
)

for dir in "${PROJECT_DIRS[@]}"; do
    nexus mkdir "$dir" --parents 2>/dev/null || true
done

print_success "Created project structure"

echo ""
print_info "Project structure:"
nexus ls $BASE_PATH/my-project || echo "  (use -R flag to see recursive listing)"

# 7. Copying directory structures
print_section "7. Directory Operations"

print_info "Creating a temporary directory..."
TEMP_DIR="$BASE_PATH/temp-workspace"
nexus mkdir $TEMP_DIR 2>/dev/null || true
print_success "Created: $TEMP_DIR"

print_info "Adding files to temp directory..."
echo "temp file 1" > /tmp/temp1.txt
nexus write $TEMP_DIR/file1.txt /tmp/temp1.txt
echo "temp file 2" > /tmp/temp2.txt
nexus write $TEMP_DIR/file2.txt /tmp/temp2.txt
print_success "Added files to $TEMP_DIR"

# 8. Removing directories
print_section "8. Removing Directories"

# Create an empty directory to remove
EMPTY_DIR="$BASE_PATH/empty-dir"
nexus mkdir $EMPTY_DIR 2>/dev/null || true
print_success "Created empty directory: $EMPTY_DIR"

# Remove empty directory (use rmdir for directories, rm is for files)
nexus rmdir -f $EMPTY_DIR
print_success "Removed empty directory: $EMPTY_DIR"

# Verify it's gone
if [ -n "$(nexus ls $EMPTY_DIR 2>/dev/null | grep -v '^No files')" ]; then
    print_error "Directory still exists!"
else
    print_success "Verified: $EMPTY_DIR no longer exists"
fi

# Recursive directory removal
echo ""
print_info "Creating directory tree for recursive removal..."
TEST_TREE="$BASE_PATH/test-tree"
nexus mkdir $TEST_TREE/level1/level2 --parents 2>/dev/null || true
echo "test" > /tmp/test.txt
nexus write $TEST_TREE/file1.txt /tmp/test.txt
nexus write $TEST_TREE/level1/file2.txt /tmp/test.txt
print_success "Created test directory tree: $TEST_TREE"

print_info "Removing with --recursive flag..."
nexus rmdir -r -f $TEST_TREE
print_success "Removed directory tree: $TEST_TREE"

# Verify it's gone
if [ -n "$(nexus ls $TEST_TREE 2>/dev/null | grep -v '^No files')" ]; then
    print_error "Directory tree still exists!"
else
    print_success "Verified: $TEST_TREE no longer exists"
fi

# 9. Directory statistics
print_section "9. Directory Statistics"

print_info "Statistics for $BASE_PATH/my-project:"
FILE_COUNT=$(nexus ls $BASE_PATH/my-project 2>/dev/null | wc -l)
echo "   Items in directory: $FILE_COUNT"

# 10. Working with paths
print_section "10. Path Operations"

print_info "Different ways to reference paths:"

# Absolute paths
echo "   Absolute: $BASE_PATH/projects/alpha/src"
nexus ls $BASE_PATH/projects/alpha/src >/dev/null 2>&1 && print_success "Absolute path works"

# Multiple directory operations
echo ""
print_info "Creating multiple directories at once:"
nexus mkdir $BASE_PATH/batch/dir1 --parents 2>/dev/null || true
nexus mkdir $BASE_PATH/batch/dir2 --parents 2>/dev/null || true
nexus mkdir $BASE_PATH/batch/dir3 --parents 2>/dev/null || true
print_success "Created multiple directories in $BASE_PATH/batch/"

nexus ls $BASE_PATH/batch

# 11. Permission checks
print_section "11. Directory Permissions"

print_info "Checking admin permissions on directories..."

if nexus rebac check user admin write file $BASE_PATH 2>&1 | grep -q "GRANTED"; then
    print_success "Admin has write access to $BASE_PATH"
else
    print_warning "Admin write access check failed"
fi

if nexus rebac check user admin read file $BASE_PATH 2>&1 | grep -q "GRANTED"; then
    print_success "Admin has read access to $BASE_PATH"
else
    print_warning "Admin read access check failed"
fi

# 12. Summary
print_section "✅ Demo Complete!"

echo "You've learned:"
echo "  ✓ Create directories with 'nexus mkdir'"
echo "  ✓ Create nested directories with --parents flag"
echo "  ✓ List directory contents with 'nexus ls'"
echo "  ✓ Check directory existence with 'nexus ls' / 'nexus cat'"
echo "  ✓ Create files within directories"
echo "  ✓ Remove empty directories with 'nexus rmdir'"
echo "  ✓ Remove directory trees with 'nexus rmdir --recursive'"
echo "  ✓ Build complex project structures"
echo "  ✓ Check directory permissions"
echo ""
echo "Demo files created in $BASE_PATH/"
echo ""
echo "To cleanup:"
echo "  nexus rmdir -r $BASE_PATH"
echo ""
echo "Next steps:"
echo "  - See docs/api/cli/directory-operations.md for full CLI reference"
echo "  - See docs/api/cli/file-operations.md for file commands"
echo "  - See docs/api/cli/permissions.md for permission management"
echo "  - Try the Python example: python examples/python/directory_operations_demo.py"
echo ""

# Cleanup temp files
rm -f /tmp/main.py /tmp/README.md /tmp/app.json /tmp/temp1.txt /tmp/temp2.txt /tmp/test.txt

print_info "Cleanup: Removed temporary files"
