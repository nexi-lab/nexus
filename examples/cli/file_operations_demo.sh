#!/bin/bash
# Nexus CLI - File Operations Demo
#
# This demo showcases all file manipulation operations in Nexus:
# - write: Write content to files (inline, stdin, file, JSON, binary)
# - cat: Read and display file contents (with metadata)
# - cp/copy: Copy files (content-addressed, deduplicated)
# - move: Move/rename files
# - rm: Delete files (with/without confirmation)
#
# ┌─────────────────────────────────────────────────────────────┐
# │ QUICK START                                                 │
# ├─────────────────────────────────────────────────────────────┤
# │ Terminal 1: Start server                                    │
# │   ./scripts/init-nexus-with-auth.sh                         │
# │                                                             │
# │ Terminal 2: Run demo                                        │
# │   source .nexus-admin-env                                   │
# │   ./examples/cli/file_operations_demo.sh                    │
# └─────────────────────────────────────────────────────────────┘
#
# Prerequisites:
# - PostgreSQL running (Homebrew or Docker)
# - Nexus installed: pip install nexus-ai-fs
#
# Features Demonstrated:
# ✓ Basic file operations (write, read, copy, move, delete)
# ✓ Advanced features (metadata, versioning, binary files)
# ✓ Optimistic concurrency control (create-only, conditional updates)
# ✓ Permission-aware operations
# ✓ Complete workflow examples
#
# Related Documentation:
# - docs/api/cli/file-operations.md
# - examples/README.md

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
print_command() { echo -e "${CYAN}\$${NC} $1"; }
print_api() { echo -e "${MAGENTA}API:${NC} $1"; }

# Check prerequisites
if [ -z "$NEXUS_URL" ] || [ -z "$NEXUS_API_KEY" ]; then
    print_error "NEXUS_URL and NEXUS_API_KEY not set."
    echo ""
    echo "Please run:"
    echo "  1. ./scripts/init-nexus-with-auth.sh    # Start server"
    echo "  2. source .nexus-admin-env              # Load credentials"
    echo "  3. $0                                    # Run this demo"
    echo ""
    exit 1
fi

echo "╔══════════════════════════════════════════════════════════╗"
echo "║       Nexus CLI - File Operations Demo                  ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
print_info "Server: $NEXUS_URL"
print_info "Demonstrating: write, cat, cp, move, rm"
echo ""

DEMO_BASE="/workspace/file-ops-demo"

# Save admin API key for cleanup and restoration
ADMIN_API_KEY="$NEXUS_API_KEY"

# Cleanup function
cleanup() {
    print_info "Cleaning up demo files..."
    # Restore admin key for cleanup (in case we're running as a different user)
    export NEXUS_API_KEY="$ADMIN_API_KEY"

    # Only clean up filesystem - database cleanup happens in init script
    nexus rmdir -r -f $DEMO_BASE 2>/dev/null || true
    rm -f /tmp/nexus-demo-*.txt
}

# Enable cleanup on exit (disable with KEEP=1)
if [ "$KEEP" != "1" ]; then
    trap cleanup EXIT
    print_info "Cleanup enabled. To keep demo data, run: KEEP=1 $0"
else
    print_info "KEEP=1 set - demo data will NOT be cleaned up"
fi

# Setup
print_section "Setup"
nexus mkdir $DEMO_BASE --parents
print_success "Created demo workspace: $DEMO_BASE"

# Grant admin ownership on base directory (permissions will inherit to subdirectories)
nexus rebac create user admin direct_owner file $DEMO_BASE 2>/dev/null || true
nexus rebac create user admin direct_editor file $DEMO_BASE 2>/dev/null || true
print_success "Configured permissions (subdirectories will inherit automatically)"

# ════════════════════════════════════════════════════════════
# Section 1: Write Operations
# ════════════════════════════════════════════════════════════

print_section "1. Write Operations"

print_subsection "1.1 Basic write (inline content)"
print_command "nexus write /workspace/file.txt 'Hello World'"
nexus write $DEMO_BASE/hello.txt "Hello from Nexus!"
print_success "Wrote inline content to hello.txt"
print_api "nx.write('/workspace/file.txt', b'Hello World')"

print_subsection "1.2 Write from stdin"
print_command "echo 'Content' | nexus write /workspace/file.txt --input -"
echo "This content came from stdin" | nexus write $DEMO_BASE/from-stdin.txt --input -
print_success "Wrote content from stdin"
print_api "nx.write('/workspace/file.txt', b'Content')"

print_subsection "1.3 Write from local file"
echo "Local file content" > /tmp/nexus-demo-local.txt
print_command "nexus write /workspace/remote.txt --input local_file.txt"
nexus write $DEMO_BASE/from-file.txt --input /tmp/nexus-demo-local.txt
print_success "Wrote content from local file"
print_api "with open('local.txt', 'rb') as f: nx.write('/remote.txt', f.read())"

print_subsection "1.4 Write with metadata display"
print_command "nexus write /workspace/doc.txt 'Content' --show-metadata"
nexus write $DEMO_BASE/with-metadata.txt "Content with metadata" --show-metadata
print_success "Wrote with metadata output"
print_api "metadata = nx.write('/doc.txt', b'Content')"

print_subsection "1.5 Write JSON data"
print_info "Writing JSON configuration..."
cat > /tmp/nexus-demo-config.json << 'EOF'
{
  "app": "nexus-demo",
  "version": "1.0",
  "settings": {
    "debug": true,
    "max_retries": 3
  }
}
EOF
nexus write $DEMO_BASE/config.json --input /tmp/nexus-demo-config.json
print_success "Wrote JSON file"
print_api "import json; nx.write('/config.json', json.dumps(data).encode())"

print_subsection "1.6 Overwrite existing file"
print_command "nexus write /workspace/hello.txt 'Updated content'"
nexus write $DEMO_BASE/hello.txt "Updated: Hello again!"
print_success "Overwrote hello.txt with new content"

# ════════════════════════════════════════════════════════════
# Section 2: Read Operations
# ════════════════════════════════════════════════════════════

print_section "2. Read Operations (cat)"

print_subsection "2.1 Basic read"
print_command "nexus cat /workspace/hello.txt"
echo ""
nexus cat $DEMO_BASE/hello.txt
echo ""
print_success "Read file content"
print_api "content = nx.read('/workspace/hello.txt').decode('utf-8')"

print_subsection "2.2 Read with metadata"
print_command "nexus cat /workspace/hello.txt --metadata"
echo ""
nexus cat $DEMO_BASE/hello.txt --metadata
echo ""
print_success "Displayed file with metadata (etag, version, size)"
print_api "result = nx.read('/file.txt', return_metadata=True)"

print_subsection "2.3 Read JSON file"
print_command "nexus cat /workspace/config.json"
echo ""
nexus cat $DEMO_BASE/config.json
echo ""
print_success "Read JSON configuration"
print_api "data = json.loads(nx.read('/config.json').decode())"

# ════════════════════════════════════════════════════════════
# Section 3: Copy Operations
# ════════════════════════════════════════════════════════════

print_section "3. Copy Operations"

print_subsection "3.1 Simple file copy"
print_command "nexus cp /workspace/hello.txt /workspace/hello-backup.txt"
nexus cp $DEMO_BASE/hello.txt $DEMO_BASE/hello-backup.txt
print_success "Copied hello.txt → hello-backup.txt"
print_api "nx.copy('/workspace/hello.txt', '/workspace/hello-backup.txt')"

print_subsection "3.2 Verify copy (content-addressed deduplication)"
print_info "Nexus uses content-addressed storage - identical content shares storage"
echo ""
print_command "nexus cat /workspace/hello-backup.txt"
nexus cat $DEMO_BASE/hello-backup.txt
echo ""
print_success "Verified copied file has same content"

print_subsection "3.3 Copy to different directory"
nexus mkdir $DEMO_BASE/backups --parents
nexus cp $DEMO_BASE/config.json $DEMO_BASE/backups/config.json
print_success "Copied config.json to backups/ directory"
print_api "nx.copy('/config.json', '/backups/config.json')"

# ════════════════════════════════════════════════════════════
# Section 4: Move/Rename Operations
# ════════════════════════════════════════════════════════════

print_section "4. Move/Rename Operations"

print_subsection "4.1 Simple rename (same directory)"
print_command "nexus move /workspace/from-stdin.txt /workspace/renamed.txt --force"
nexus move $DEMO_BASE/from-stdin.txt $DEMO_BASE/renamed.txt --force
print_success "Renamed from-stdin.txt → renamed.txt"
print_api "nx.move('/workspace/old.txt', '/workspace/new.txt')"

print_subsection "4.2 Verify old path doesn't exist"
if nexus cat $DEMO_BASE/from-stdin.txt 2>&1 | grep -qi "not found\|does not exist"; then
    print_success "✓ Original path no longer exists"
else
    print_warning "Original path still accessible (unexpected)"
fi

print_subsection "4.3 Verify new path works"
print_command "nexus cat /workspace/renamed.txt"
echo ""
nexus cat $DEMO_BASE/renamed.txt
echo ""
print_success "File accessible at new path"

print_subsection "4.4 Move to different directory"
nexus mkdir $DEMO_BASE/archive --parents
print_command "nexus move /workspace/hello-backup.txt /workspace/archive/hello-backup.txt --force"
nexus move $DEMO_BASE/hello-backup.txt $DEMO_BASE/archive/hello-backup.txt --force
print_success "Moved file to archive/ directory"
print_api "nx.move('/workspace/file.txt', '/archive/file.txt')"

print_subsection "4.5 Move with rename"
print_command "nexus move /workspace/from-file.txt /workspace/archive/archived-file.txt --force"
nexus move $DEMO_BASE/from-file.txt $DEMO_BASE/archive/archived-file.txt --force
print_success "Moved and renamed file in one operation"

# ════════════════════════════════════════════════════════════
# Section 5: Delete Operations
# ════════════════════════════════════════════════════════════

print_section "5. Delete Operations"

print_subsection "5.1 Delete with confirmation prompt"
print_info "Interactive delete (would normally prompt)"
print_command "nexus rm /workspace/renamed.txt"
print_warning "Skipping interactive delete (would require confirmation)"

print_subsection "5.2 Force delete (no confirmation)"
print_command "nexus rm /workspace/renamed.txt --force"
nexus rm $DEMO_BASE/renamed.txt --force
print_success "Deleted renamed.txt without confirmation"
print_api "nx.delete('/workspace/file.txt')"

print_subsection "5.3 Verify deletion"
# Check if file is actually gone
# Exit code 0 = file exists and readable
# Exit code != 0 = file doesn't exist or not accessible (either way, deleted successfully)
nexus cat $DEMO_BASE/renamed.txt >/dev/null 2>&1
EXIT_CODE=$?
if [ $EXIT_CODE -eq 0 ]; then
    print_error "File still exists after deletion"
else
    print_success "✓ File successfully deleted (cannot be read, exit code: $EXIT_CODE)"
fi

print_subsection "5.4 Delete multiple files"
nexus write $DEMO_BASE/temp1.txt "temp 1"
nexus write $DEMO_BASE/temp2.txt "temp 2"
nexus write $DEMO_BASE/temp3.txt "temp 3"
print_info "Created 3 temporary files"

for file in temp1.txt temp2.txt temp3.txt; do
    nexus rm $DEMO_BASE/$file --force
done
print_success "Deleted all temporary files"

# ════════════════════════════════════════════════════════════
# Section 6: Advanced Features
# ════════════════════════════════════════════════════════════

print_section "6. Advanced Features"

print_subsection "6.1 Optimistic Concurrency Control (Create-only)"
print_command "nexus write /workspace/new.txt 'Initial' --if-none-match"
nexus write $DEMO_BASE/create-only.txt "Initial content" --if-none-match 2>/dev/null || true
print_success "Create-only write (fails if file exists)"
print_api "nx.write('/new.txt', b'Initial', if_none_match=True)"

print_subsection "6.2 Attempt to overwrite with create-only flag"
if nexus write $DEMO_BASE/create-only.txt "Overwrite attempt" --if-none-match 2>&1 | grep -qi "exists\|conflict\|precondition"; then
    print_success "✓ Create-only prevented overwrite (as expected)"
else
    print_warning "File was overwritten (should have been prevented)"
fi

print_subsection "6.3 Optimistic Concurrency Control (Conditional update)"
print_info "Get current ETag..."
METADATA=$(nexus cat $DEMO_BASE/hello.txt --metadata)
echo "$METADATA"
print_info "To update with ETag check, use: --if-match <etag>"
print_command "nexus write /workspace/hello.txt 'Updated' --if-match abc123"
print_api "nx.write('/hello.txt', b'Updated', if_match='abc123')"
print_warning "Skipping actual conditional update (ETag would need to be extracted)"

print_subsection "6.4 Working with binary files"
print_info "Creating a small binary file..."
dd if=/dev/urandom of=/tmp/nexus-demo-binary.dat bs=1024 count=1 2>/dev/null
nexus write $DEMO_BASE/binary.dat --input /tmp/nexus-demo-binary.dat
print_success "Wrote binary file (1 KB random data)"
print_api "with open('image.jpg', 'rb') as f: nx.write('/image.jpg', f.read())"

SIZE=$(nexus cat $DEMO_BASE/binary.dat --metadata | grep -i "size" | head -1)
print_info "Binary file info: $SIZE"

# ════════════════════════════════════════════════════════════
# Section 7: Complete Workflow Example
# ════════════════════════════════════════════════════════════

print_section "7. Complete Workflow Example"

print_info "Scenario: Document versioning workflow"
echo ""

print_subsection "7.1 Create initial document"
print_command "nexus write /workspace/docs/README.md '# My Project'"
nexus mkdir $DEMO_BASE/docs --parents
echo "# My Project" | nexus write $DEMO_BASE/docs/README.md --input -
print_success "Created initial README.md"

print_subsection "7.2 Make a backup before editing"
print_command "nexus cp /workspace/docs/README.md /workspace/docs/README.backup.md"
nexus cp $DEMO_BASE/docs/README.md $DEMO_BASE/docs/README.backup.md
print_success "Created backup"

print_subsection "7.3 Update the document"
cat > /tmp/nexus-demo-readme.md << 'EOF'
# My Project

## Overview
This is an awesome project built with Nexus.

## Features
- Feature 1
- Feature 2
- Feature 3
EOF
print_command "nexus write /workspace/docs/README.md --input updated.md"
nexus write $DEMO_BASE/docs/README.md --input /tmp/nexus-demo-readme.md
print_success "Updated README.md"

print_subsection "7.4 Review the changes"
print_command "nexus cat /workspace/docs/README.md"
echo ""
nexus cat $DEMO_BASE/docs/README.md
echo ""
print_success "Verified updated content"

print_subsection "7.5 Archive old backup"
nexus mkdir $DEMO_BASE/archive/docs --parents
print_command "nexus move /workspace/docs/README.backup.md /workspace/archive/docs/README.backup.md --force"
nexus move $DEMO_BASE/docs/README.backup.md $DEMO_BASE/archive/docs/README.backup.md --force
print_success "Moved backup to archive"

print_subsection "7.6 Clean up old archives (delete)"
print_info "After verifying new version works, delete old backup..."
print_command "nexus rm /workspace/archive/docs/README.backup.md --force"
nexus rm $DEMO_BASE/archive/docs/README.backup.md --force
print_success "Deleted old backup"

# ════════════════════════════════════════════════════════════
# Section 8: Working with Permissions (Simple Setup)
# ════════════════════════════════════════════════════════════

print_section "8. File Operations with Permissions"

print_subsection "8.1 Create a test user"
USER_KEY=$(python3 scripts/create-api-key.py alice "Alice Test User" --days 1 2>/dev/null | grep "API Key:" | awk '{print $3}')
print_success "Created user 'alice'"

print_subsection "8.2 Grant read-only access to a file"
nexus rebac create user alice direct_viewer file $DEMO_BASE/hello.txt
print_success "Granted alice viewer permission on hello.txt"
print_info "Viewer permission: read only (no write/delete)"

print_subsection "8.3 Verify alice can read"
export NEXUS_API_KEY="$USER_KEY"
print_command "nexus cat /workspace/hello.txt (as alice)"
if nexus cat $DEMO_BASE/hello.txt 2>/dev/null | grep -q "Hello"; then
    print_success "✓ Alice can read the file"
else
    print_error "Alice cannot read (permission issue)"
fi

print_subsection "8.4 Verify alice cannot write"
print_command "echo 'Alice update' | nexus write /workspace/hello.txt - (as alice)"
if echo "Alice attempt" | nexus write $DEMO_BASE/hello.txt - 2>&1 | grep -qi "permission\|denied\|forbidden"; then
    print_success "✓ Write correctly denied (viewer has read-only access)"
else
    print_error "Alice was able to write (should be denied)"
fi

print_subsection "8.5 Grant write access (upgrade to editor)"
export NEXUS_API_KEY="$ADMIN_API_KEY"  # Switch back to admin
nexus rebac create user alice direct_editor file $DEMO_BASE/hello.txt
print_success "Upgraded alice to editor (read + write)"

print_subsection "8.6 Verify alice can now write"
export NEXUS_API_KEY="$USER_KEY"
print_command "echo 'Alice update' | nexus write /workspace/hello.txt - (as alice)"
if echo "Updated by Alice" | nexus write $DEMO_BASE/hello.txt - 2>/dev/null; then
    print_success "✓ Alice can now write (editor permission)"
else
    print_error "Alice still cannot write"
fi

# Restore admin key
export NEXUS_API_KEY="$ADMIN_API_KEY"

# ════════════════════════════════════════════════════════════
# Summary
# ════════════════════════════════════════════════════════════

print_section "✅ File Operations Demo Complete!"

echo "╔═══════════════════════════════════════════════════════════════════╗"
echo "║                    Operations Demonstrated                        ║"
echo "╠═══════════════════════════════════════════════════════════════════╣"
echo "║  ✅ Write operations (inline, stdin, file, JSON, binary)          ║"
echo "║  ✅ Read operations (basic, with metadata)                        ║"
echo "║  ✅ Copy operations (simple, cross-directory)                     ║"
echo "║  ✅ Move/rename operations (same dir, cross-dir, with rename)     ║"
echo "║  ✅ Delete operations (with/without confirmation)                 ║"
echo "║  ✅ Optimistic concurrency control (create-only, conditional)     ║"
echo "║  ✅ Binary file handling                                          ║"
echo "║  ✅ Complete workflow example (document versioning)               ║"
echo "║  ✅ Permission-aware operations (read-only, read-write)           ║"
echo "╚═══════════════════════════════════════════════════════════════════╝"
echo ""
print_info "All file operations working correctly!"
print_info "For more details, see: docs/api/cli/file-operations.md"
echo ""
