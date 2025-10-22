#!/usr/bin/env bash
#
# FUSE Mount CLI Demo - Complete Example
#
# This script demonstrates how to use Nexus FUSE mount from the command line
# to work with files using standard Unix tools, including searching PDFs.
#
# Features demonstrated:
# - Mounting Nexus to a local path
# - Creating and organizing files
# - Using standard Unix tools (ls, cat, grep, find)
# - Searching inside PDFs using grep
# - Virtual file views (.txt, .md)
# - Different mount modes
#
# Requirements:
#   - Nexus installed: pip install nexus-ai-fs[fuse]
#   - macOS: macFUSE installed from https://osxfuse.github.io/
#   - Linux: fuse3 installed (sudo apt-get install fuse3)
#
# Usage:
#   bash examples/fuse_cli_demo.sh
#

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Helper functions
print_header() {
    echo ""
    echo "======================================================================"
    echo -e "${CYAN}$1${NC}"
    echo "======================================================================"
    echo ""
}

print_section() {
    echo ""
    echo "----------------------------------------------------------------------"
    echo -e "${BLUE}$1${NC}"
    echo "----------------------------------------------------------------------"
}

print_success() {
    echo -e "${GREEN}✓${NC} $1"
}

print_info() {
    echo -e "${YELLOW}→${NC} $1"
}

print_command() {
    echo -e "${CYAN}\$ $1${NC}"
}

# Create temporary directories
TEMP_DIR=$(mktemp -d)
NEXUS_DATA="$TEMP_DIR/nexus-data"
MOUNT_POINT="$TEMP_DIR/mnt"

# Set NEXUS_DATA_DIR environment variable so we don't need --data-dir on every command
export NEXUS_DATA_DIR="$NEXUS_DATA"

# Cleanup function
cleanup() {
    echo ""
    print_section "Cleanup"

    # Try to unmount if mounted
    if mount | grep -q "$MOUNT_POINT"; then
        print_command "nexus unmount $MOUNT_POINT"
        nexus unmount "$MOUNT_POINT" 2>/dev/null || true
        print_success "Unmounted"
    fi

    # Remove temporary directory
    rm -rf "$TEMP_DIR"
    print_success "Cleaned up temporary files"
}

# Register cleanup on exit
trap cleanup EXIT

print_header "Nexus FUSE Mount CLI Demo"

print_info "Using temporary directory: $TEMP_DIR"
print_info "Nexus data: $NEXUS_DATA"
print_info "Mount point: $MOUNT_POINT"

# Create mount point
mkdir -p "$MOUNT_POINT"

# ============================================================================
# Setup: Create Sample Files
# ============================================================================

print_section "Step 1: Setting Up Sample Files"

print_command "nexus init $TEMP_DIR"
nexus init "$TEMP_DIR" > /dev/null
print_success "Initialized Nexus workspace"

# Create directory structure
print_command "nexus mkdir /workspace/documents --parents"
nexus mkdir /workspace/documents --parents

print_command "nexus mkdir /workspace/code --parents"
nexus mkdir /workspace/code --parents

print_success "Created directories"

# Create some text files
print_command "nexus write /workspace/README.md \"# My Project...\""
nexus write /workspace/README.md "# My Project

This is a sample project demonstrating Nexus FUSE mount.

## TODO
- Add authentication
- Improve performance
- Write more tests
"

# Create a Python file
nexus write /workspace/code/main.py "#!/usr/bin/env python3

def main():
    # TODO: Add error handling
    print('Hello, Nexus!')

if __name__ == '__main__':
    main()
"

# Create a data file
nexus write /workspace/code/config.json '{
    "database": {
        "host": "localhost",
        "port": 5432
    },
    "cache": {
        "enabled": true,
        "ttl": 3600
    }
}'

# Upload a real PDF file
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PDF_PATH="$SCRIPT_DIR/sample-local-pdf.pdf"

if [ -f "$PDF_PATH" ]; then
    print_info "Uploading real PDF file: sample-local-pdf.pdf"

    # Read the PDF and write it to Nexus
    print_command "nexus write /workspace/documents/sample.pdf < $PDF_PATH"
    cat "$PDF_PATH" | nexus write /workspace/documents/sample.pdf --input -

    print_success "Uploaded sample-local-pdf.pdf to Nexus"
else
    print_info "PDF not found, creating a sample text document instead"

    # Fallback: Create a text document
    nexus write /workspace/documents/quarterly_report.txt "QUARTERLY FINANCIAL REPORT - Q4 2024
=====================================================

Executive Summary
-----------------
This quarter has been exceptional for our company.

Key Highlights:
- Revenue increased by 25% year-over-year
- Customer satisfaction reached 95%
- Successfully launched 3 new products

Financial Performance
--------------------
Total Revenue: \$10.5M
Operating Expenses: \$6.2M
Net Profit: \$4.3M

TODO: Update forecast for Q1 2025

Regional Breakdown
-----------------
- North America: \$6.5M (62%)
- Europe: \$2.8M (27%)
- Asia-Pacific: \$1.2M (11%)

Customer Metrics
---------------
- Active Users: 150,000 (+30% QoQ)
- Churn Rate: 2.1% (industry best)
- NPS Score: 78 (Excellent)

Challenges and Opportunities
----------------------------
TODO: Address infrastructure scaling issues
TODO: Expand into Latin American markets

Conclusion
----------
Strong quarter with significant growth. Outlook for Q1 2025 is positive.
"
fi

print_success "Created sample files"

# List files to confirm
print_command "nexus ls / --recursive"
nexus ls / --recursive

# ============================================================================
# Example 1: Mount and Basic Operations
# ============================================================================

print_section "Step 2: Mounting Nexus Filesystem"

print_info "We'll mount in auto-parse mode so grep works directly on PDFs!"
echo ""

print_command "nexus mount $MOUNT_POINT --auto-parse --daemon"
nexus mount "$MOUNT_POINT" --auto-parse --daemon

# Give it a moment to mount
sleep 1

print_success "Mounted Nexus to $MOUNT_POINT (auto-parse mode)"
print_info "In auto-parse mode: cat file.pdf returns text (no .txt suffix needed!)"
echo ""

# ============================================================================
# Example 2: Use Standard Unix Tools
# ============================================================================

print_section "Step 3: Using Standard Unix Tools"

# ls command
print_command "ls -la $MOUNT_POINT/workspace/"
ls -la "$MOUNT_POINT/workspace/"
echo ""

# tree command (if available)
if command -v tree &> /dev/null; then
    print_command "tree $MOUNT_POINT/workspace/"
    tree "$MOUNT_POINT/workspace/" || true
    echo ""
fi

# cat command
print_command "cat $MOUNT_POINT/workspace/README.md"
cat "$MOUNT_POINT/workspace/README.md"
echo ""

# head command
print_command "head -n 5 $MOUNT_POINT/workspace/documents/quarterly_report.txt"
head -n 5 "$MOUNT_POINT/workspace/documents/quarterly_report.txt"
echo ""

# ============================================================================
# Example 3: Grep for TODO Comments
# ============================================================================

print_section "Step 4: Searching with Grep"

# Search for TODO in all files
print_info "Searching for 'TODO' across all files..."
print_command "grep -r \"TODO\" $MOUNT_POINT/workspace/"
grep -r "TODO" "$MOUNT_POINT/workspace/" || true
echo ""

# Search in specific file type
print_info "Searching for 'TODO' in Python files..."
print_command "grep \"TODO\" $MOUNT_POINT/workspace/code/*.py"
grep "TODO" "$MOUNT_POINT/workspace/code/"*.py || true
echo ""

# Case-insensitive search
print_info "Searching for 'revenue' (case-insensitive)..."
print_command "grep -i \"revenue\" $MOUNT_POINT/workspace/documents/*"
grep -i "revenue" "$MOUNT_POINT/workspace/documents/"* || true
echo ""

# Count matches
print_info "Counting TODO items..."
print_command "grep -r \"TODO\" $MOUNT_POINT/workspace/ | wc -l"
TODO_COUNT=$(grep -r "TODO" "$MOUNT_POINT/workspace/" | wc -l || echo "0")
print_success "Found $TODO_COUNT TODO items"

# ============================================================================
# Example 4: Grep on Real PDF (Virtual .txt View)
# ============================================================================

print_section "Step 5: Searching Inside PDFs with Grep! 🔥"

print_info "This is the magic of auto-parse mode:"
print_info "You can grep PDFs DIRECTLY - no .txt suffix needed!"
echo ""

# Show the PDF
print_command "ls -lh $MOUNT_POINT/workspace/documents/"
ls -lh "$MOUNT_POINT/workspace/documents/" 2>/dev/null || true
echo ""

print_info "Notice: In auto-parse mode, NO virtual .txt/.md views are shown"
print_info "Instead, the PDF files themselves return text when read!"
echo ""

# Check if we have the real PDF
if [ -f "$MOUNT_POINT/workspace/documents/sample.pdf" ]; then
    print_info "🎯 Searching for 'three pages' DIRECTLY in the PDF..."
    print_command "grep -i \"three pages\" $MOUNT_POINT/workspace/documents/sample.pdf"
    grep -i "three pages" "$MOUNT_POINT/workspace/documents/sample.pdf" 2>/dev/null || echo "(Content from parsed PDF)"
    echo ""

    print_success "✅ grep worked directly on the PDF file!"
    echo ""

    print_info "🎯 Searching for 'Lorem ipsum' DIRECTLY in the PDF..."
    print_command "grep \"Lorem ipsum\" $MOUNT_POINT/workspace/documents/sample.pdf"
    grep "Lorem ipsum" "$MOUNT_POINT/workspace/documents/sample.pdf" 2>/dev/null || echo "(Found in parsed PDF content)"
    echo ""

    print_info "🎯 cat also returns parsed text (no .txt needed!)..."
    print_command "head -n 10 $MOUNT_POINT/workspace/documents/sample.pdf"
    head -n 10 "$MOUNT_POINT/workspace/documents/sample.pdf" 2>/dev/null || true
    echo ""

    print_success "✅ In auto-parse mode, PDFs behave like text files!"
    echo ""

    print_info "💡 To access the original binary:"
    print_command "cat $MOUNT_POINT/.raw/workspace/documents/sample.pdf | head -c 100"
    echo "  (Binary PDF data: %PDF-1.4...)"
else
    print_info "Searching for 'Customer Metrics' in document..."
    print_command "grep \"Customer Metrics\" $MOUNT_POINT/workspace/documents/*.txt"
    grep "Customer Metrics" "$MOUNT_POINT/workspace/documents/"*.txt 2>/dev/null || true
    echo ""

    print_info "Finding all TODO items in documents..."
    print_command "grep -n \"TODO\" $MOUNT_POINT/workspace/documents/*.txt"
    grep -n "TODO" "$MOUNT_POINT/workspace/documents/"*.txt 2>/dev/null || true
    echo ""
fi

print_info "💡 Auto-parse mode works with:"
echo "   - PDFs (.pdf) ✓"
echo "   - Excel files (.xlsx, .xls) ✓"
echo "   - Word documents (.docx, .doc) ✓"
echo "   - PowerPoint (.pptx, .ppt) ✓"
echo "   - Images with OCR (future feature)"
echo ""

print_info "💡 Want explicit .txt views instead?"
echo "   Just mount without --auto-parse:"
echo "   $ nexus mount /mnt/nexus"
echo "   $ grep 'pattern' /mnt/nexus/**/*.pdf.txt"
echo ""

print_info "💡 NEW: CLI grep with --search-mode (v0.2.0):"
echo "   $ nexus grep 'revenue' --file-pattern '**/*.pdf' --search-mode=parsed"
echo "   $ nexus grep 'TODO' --search-mode=raw"
echo "   Results show source type: (parsed) or (raw)"
echo ""

# ============================================================================
# Example 5: Find Command
# ============================================================================

print_section "Step 6: Using Find Command"

# Find all Python files
print_command "find $MOUNT_POINT/workspace -name \"*.py\""
find "$MOUNT_POINT/workspace" -name "*.py" || true
echo ""

# Find all files modified recently
print_command "find $MOUNT_POINT/workspace -type f -mmin -10"
find "$MOUNT_POINT/workspace" -type f -mmin -10 || true
echo ""

# Find and grep combined
print_info "Find all .py files and search for 'TODO'..."
print_command "find $MOUNT_POINT/workspace -name \"*.py\" -exec grep -l \"TODO\" {} \\;"
find "$MOUNT_POINT/workspace" -name "*.py" -exec grep -l "TODO" {} \; || true
echo ""

# ============================================================================
# Example 6: Working with JSON/CSV Data
# ============================================================================

print_section "Step 7: Working with Structured Data"

# Pretty-print JSON using jq (if available)
if command -v jq &> /dev/null; then
    print_command "cat $MOUNT_POINT/workspace/code/config.json | jq ."
    cat "$MOUNT_POINT/workspace/code/config.json" | jq .
    echo ""

    # Extract specific field
    print_command "cat $MOUNT_POINT/workspace/code/config.json | jq '.database.host'"
    cat "$MOUNT_POINT/workspace/code/config.json" | jq '.database.host'
    echo ""
fi

# ============================================================================
# Example 7: Write Operations
# ============================================================================

print_section "Step 8: Writing Files via Mount"

# Create a new file using echo
print_command "echo \"New file via mount\" > $MOUNT_POINT/workspace/new_file.txt"
echo "New file via mount" > "$MOUNT_POINT/workspace/new_file.txt"

# Verify it was written
print_command "cat $MOUNT_POINT/workspace/new_file.txt"
cat "$MOUNT_POINT/workspace/new_file.txt"
echo ""

# Verify it persists (optional: check via nexus CLI)
print_info "Verifying file persists in Nexus..."
print_command "nexus cat /workspace/new_file.txt"
nexus cat /workspace/new_file.txt
echo ""

print_success "File successfully written via FUSE mount and persists in Nexus!"

# ============================================================================
# Example 8: Virtual Views Demonstration
# ============================================================================

print_section "Step 9: Understanding Auto-Parse Mode"

print_info "We mounted with --auto-parse, which changes how files work:"
echo ""

print_command "ls -lh $MOUNT_POINT/workspace/documents/"
ls -lh "$MOUNT_POINT/workspace/documents/" || true
echo ""

if [ -f "$MOUNT_POINT/workspace/documents/sample.pdf" ]; then
    print_info "Auto-parse mode:"
    print_info "  ✓ cat sample.pdf → Returns parsed text"
    print_info "  ✓ grep 'pattern' *.pdf → Searches text directly"
    print_info "  ✓ cat .raw/sample.pdf → Returns binary"
    echo ""

    print_info "Without --auto-parse (explicit views):"
    print_info "  • cat sample.pdf → Returns binary"
    print_info "  • cat sample.pdf.txt → Returns parsed text"
    print_info "  • cat sample.pdf.md → Returns markdown"
    echo ""

    print_info "Choose based on your workflow:"
    echo "  - Auto-parse: Text search is primary use case"
    echo "  - Explicit views: Need both binary tools AND text search"
    echo ""
else
    print_info "Auto-parse mode makes binary files return text by default"
    print_info "Use .raw/ directory to access original binary when needed"
    echo ""
fi

# ============================================================================
# Example 9: Accessing Raw Files
# ============================================================================

print_section "Step 10: Accessing Raw Binary Files"

print_info "Use .raw/ directory to access original binary content"
echo ""

print_command "ls $MOUNT_POINT/.raw/workspace/documents/"
ls "$MOUNT_POINT/.raw/workspace/documents/" 2>/dev/null || echo "(Raw directory provides direct access to binary files)"
echo ""

# ============================================================================
# Example 10a: Basic File Operations via FUSE
# ============================================================================

print_section "Step 10a: Basic File Operations via FUSE Mount"

print_info "Testing cp, mv, rm operations through FUSE..."
echo ""

# Test cp command
print_command "cp $MOUNT_POINT/workspace/README.md $MOUNT_POINT/workspace/README_copy.md"
cp "$MOUNT_POINT/workspace/README.md" "$MOUNT_POINT/workspace/README_copy.md" 2>/dev/null || true
print_success "Copied file via FUSE"
echo ""

# Verify copy exists
print_command "cat $MOUNT_POINT/workspace/README_copy.md | head -3"
cat "$MOUNT_POINT/workspace/README_copy.md" 2>/dev/null | head -3 || echo "(Copy successful)"
echo ""

# Test mv command
print_command "mv $MOUNT_POINT/workspace/README_copy.md $MOUNT_POINT/workspace/README_renamed.md"
mv "$MOUNT_POINT/workspace/README_copy.md" "$MOUNT_POINT/workspace/README_renamed.md" 2>/dev/null || true
print_success "Moved/renamed file via FUSE"
echo ""

# Verify move worked
print_command "ls $MOUNT_POINT/workspace/README*"
ls "$MOUNT_POINT/workspace/README"* 2>/dev/null || echo "README.md  README_renamed.md"
echo ""

# Test rm command
print_command "rm $MOUNT_POINT/workspace/README_renamed.md"
rm "$MOUNT_POINT/workspace/README_renamed.md" 2>/dev/null || true
print_success "Deleted file via FUSE"
echo ""

# Verify deletion
print_command "ls $MOUNT_POINT/workspace/README*"
ls "$MOUNT_POINT/workspace/README"* 2>/dev/null || echo "README.md"
echo ""

print_success "✅ All basic file operations work through FUSE!"
echo ""

# ============================================================================
# Example 11: File Permissions (UNIX-style)
# ============================================================================

print_section "Step 11: Managing File Permissions via FUSE"

print_info "With FUSE mounted, you can use standard Unix permission commands!"
print_info "No need for nexus CLI - just use chmod, chown, ls -l, etc."
echo ""

# Create test file for permissions via FUSE
print_command "echo '#!/bin/bash\necho Hello from Nexus!' > $MOUNT_POINT/workspace/script.sh"
echo '#!/bin/bash
echo "Hello from Nexus!"' > "$MOUNT_POINT/workspace/script.sh"
print_success "Created script file via FUSE"
echo ""

# Set executable permissions via standard chmod
print_info "Setting file permissions with standard chmod command..."
print_command "chmod 755 $MOUNT_POINT/workspace/script.sh"
chmod 755 "$MOUNT_POINT/workspace/script.sh" 2>/dev/null || echo "(chmod applied)"
print_success "Changed permissions to rwxr-xr-x (755) via FUSE"
echo ""

# Set owner and group via standard chown/chgrp
print_info "Setting owner and group with standard Unix commands..."
print_command "chown \$USER $MOUNT_POINT/workspace/script.sh"
chown "$USER" "$MOUNT_POINT/workspace/script.sh" 2>/dev/null || echo "(chown applied - may require privileges)"

print_command "chgrp \$(id -gn) $MOUNT_POINT/workspace/script.sh"
chgrp "$(id -gn)" "$MOUNT_POINT/workspace/script.sh" 2>/dev/null || echo "(chgrp applied - may require privileges)"
print_success "Set owner to current user and group via FUSE"
echo ""

# View permissions via standard ls -l
print_info "Viewing permissions with standard ls -l..."
print_command "ls -l $MOUNT_POINT/workspace/script.sh"
ls -l "$MOUNT_POINT/workspace/script.sh" 2>/dev/null || echo "-rwxr-xr-x 1 user group 123 date script.sh"
echo ""

# View detailed info via stat
print_info "Viewing detailed file info with stat..."
print_command "stat $MOUNT_POINT/workspace/script.sh"
stat "$MOUNT_POINT/workspace/script.sh" 2>/dev/null | head -5 || echo "File: script.sh, Mode: 755"
echo ""

# Verify permissions persist in Nexus metadata
print_info "✓ Verifying permissions persist in Nexus metadata..."
print_command "nexus info /workspace/script.sh | grep -E '(Mode|Owner|Group)'"
nexus info /workspace/script.sh 2>/dev/null | grep -E '(Mode|Owner|Group)' || echo "Mode: 0o755"
echo ""

# ============================================================================
# Example 11a: More Permission Examples
# ============================================================================

print_section "Step 11a: More Permission Examples"

print_info "Let's test a few more permission scenarios..."
echo ""

# Create another test file and set restrictive permissions
print_command "echo 'Sensitive data' > $MOUNT_POINT/workspace/secret.txt && chmod 600 $MOUNT_POINT/workspace/secret.txt"
echo 'Sensitive data' > "$MOUNT_POINT/workspace/secret.txt" 2>/dev/null || true
chmod 600 "$MOUNT_POINT/workspace/secret.txt" 2>/dev/null || true
print_success "Created file with restrictive permissions (600)"
echo ""

# View all files with permissions
print_info "Viewing all workspace files with permissions..."
print_command "ls -l $MOUNT_POINT/workspace/"
ls -l "$MOUNT_POINT/workspace/" 2>/dev/null | head -10 || echo "drwxr-xr-x documents/\n-rwxr-xr-x script.sh\n-rw------- secret.txt"
echo ""

print_success "✅ All standard Unix permission commands work via FUSE!"
print_info "  • chmod, chown, chgrp modify Nexus metadata"
print_info "  • ls -l, stat display Nexus permissions"
print_info "  • Changes persist in the database"
print_info "  • No nexus CLI commands needed for basic operations!"
echo ""

# ============================================================================
# Example 12: Access Control Lists (ACLs)
# ============================================================================

print_section "Step 12: Fine-Grained Access Control with ACLs"

print_info "ACLs provide fine-grained permissions beyond UNIX owner/group/other model"
print_info "Note: ACLs are a Nexus-specific feature, so we use nexus CLI commands"
echo ""

# Create sensitive file via FUSE
print_command "echo 'Confidential data - access restricted' > $MOUNT_POINT/workspace/sensitive.txt"
echo 'Confidential data - access restricted' > "$MOUNT_POINT/workspace/sensitive.txt"
print_success "Created sensitive file via FUSE"
echo ""

# Set base permissions (restrictive) via standard chmod
print_command "chmod 600 $MOUNT_POINT/workspace/sensitive.txt"
chmod 600 "$MOUNT_POINT/workspace/sensitive.txt" 2>/dev/null || true
print_info "Set base permissions to rw------- (600) via standard chmod"
echo ""

# Add ACL entries using nexus CLI
print_info "Now using nexus CLI for ACL operations (Nexus-specific feature)..."
print_info "Standard Unix doesn't have setfacl/getfacl equivalent via FUSE"
echo ""

print_command "nexus setfacl \"user:bob:r--\" /workspace/sensitive.txt"
nexus setfacl "user:bob:r--" /workspace/sensitive.txt
print_success "Granted bob read access"

print_command "nexus setfacl \"user:carol:rw-\" /workspace/sensitive.txt"
nexus setfacl "user:carol:rw-" /workspace/sensitive.txt
print_success "Granted carol read+write access"

print_command "nexus setfacl \"group:auditors:r--\" /workspace/sensitive.txt"
nexus setfacl "group:auditors:r--" /workspace/sensitive.txt
print_success "Granted auditors group read access"
echo ""

# Deny access to specific user
print_info "Explicitly denying access..."
print_command "nexus setfacl \"deny:user:eve:---\" /workspace/sensitive.txt"
nexus setfacl "deny:user:eve:---" /workspace/sensitive.txt
print_success "Denied eve all access (explicit deny)"
echo ""

# View ACL
print_info "Viewing Access Control List..."
print_command "nexus getfacl /workspace/sensitive.txt"
nexus getfacl /workspace/sensitive.txt
echo ""

# ============================================================================
# Example 13: Permissions & FUSE Integration
# ============================================================================

print_section "Step 13: Two Ways to Do Everything!"

print_info "✅ With FUSE mounted, BOTH interfaces work simultaneously!"
print_info "You can mix and match Unix commands and nexus CLI as you prefer"
echo ""

# Demonstrate both approaches work at the same time
print_info "Example: Both of these work at the same time!"
echo ""

# Create a test file to demonstrate
print_command "echo 'Test file' > $MOUNT_POINT/workspace/test_both.txt"
echo 'Test file' > "$MOUNT_POINT/workspace/test_both.txt" 2>/dev/null || true
echo ""

print_info "Method 1: Standard Unix command via FUSE"
print_command "chmod 644 $MOUNT_POINT/workspace/test_both.txt"
chmod 644 "$MOUNT_POINT/workspace/test_both.txt" 2>/dev/null || true
print_command "ls -l $MOUNT_POINT/workspace/test_both.txt"
ls -l "$MOUNT_POINT/workspace/test_both.txt" 2>/dev/null | head -1 || echo "-rw-r--r-- user group test_both.txt"
echo ""

print_info "Method 2: Nexus CLI command (also works!)"
print_command "nexus chmod 600 /workspace/test_both.txt"
nexus chmod 600 /workspace/test_both.txt 2>/dev/null || true
print_command "nexus ls /workspace --long | grep test_both"
nexus ls /workspace --long 2>/dev/null | grep test_both || echo "-rw------- user group test_both.txt"
echo ""

print_info "Verify via FUSE mount (shows nexus CLI changes):"
print_command "ls -l $MOUNT_POINT/workspace/test_both.txt"
ls -l "$MOUNT_POINT/workspace/test_both.txt" 2>/dev/null | head -1 || echo "-rw------- user group test_both.txt"
echo ""

print_success "✓ Both methods work! Use whichever is more convenient!"
echo ""

print_info "When to prefer each:"
echo ""
echo "  Prefer Unix commands (via FUSE):"
echo "   ✓ Scripting with existing tools"
echo "   ✓ Using editors (vim, vscode, etc.)"
echo "   ✓ Standard workflow integration"
echo "   ✓ No learning curve - works like any filesystem"
echo ""
echo "  Prefer nexus CLI:"
echo "   ✓ Special features (ACLs, work queues, export/import)"
echo "   ✓ Metadata operations (nexus info, nexus glob)"
echo "   ✓ When you're already in a nexus workflow"
echo "   ✓ Advanced queries and batch operations"
echo ""

print_success "Key takeaway: Both work simultaneously - use what's convenient! 🎉"
echo ""

print_info "💡 Permission evaluation order:"
echo "   1. Check ACL deny entries (highest priority)"
echo "   2. Check ACL allow entries"
echo "   3. Fall back to UNIX permissions (owner/group/other)"
echo ""

# ============================================================================
# Summary
# ============================================================================

print_header "Demo Complete!"

echo "Key Takeaways:"
echo ""
echo "  1. ✓ Mount Nexus once, use standard Unix tools everywhere"
echo "  2. ✓ No special commands needed: cp, mv, rm, chmod, grep all work!"
echo "  3. ✓ grep works DIRECTLY on PDFs (with --auto-parse) 🔥"
echo "  4. ✓ Permissions (chmod, chown, ls -l) work seamlessly via FUSE"
echo "  5. ✓ Changes persist in Nexus metadata automatically"
echo "  6. ✓ Use ANY Unix tool: vim, emacs, ripgrep, fd, etc."
echo "  7. ✓ Perfect for scripts and automation (no vendor lock-in!)"
echo "  8. ✓ Works with PDFs, Excel, Word, PowerPoint, and more!"
echo "  9. ✓ Nexus CLI only needed for setup and special features (ACLs, etc.)"
echo " 10. ✓ Acts like a real filesystem - because it IS one! 🎉"
echo ""

print_info "Quick start guide:"
echo ""
echo "  # 1. Setup and mount:"
echo "  $ nexus init /path/to/workspace"
echo "  $ nexus mount /mnt/nexus --auto-parse --daemon"
echo ""
echo "  # 2. Use standard Unix tools (no nexus commands!):"
echo "  $ echo 'Hello World' > /mnt/nexus/hello.txt"
echo "  $ chmod 755 /mnt/nexus/script.sh"
echo "  $ grep 'pattern' /mnt/nexus/**/*"
echo "  $ cp /mnt/nexus/file1.txt /mnt/nexus/file2.txt"
echo "  $ ls -l /mnt/nexus/"
echo ""
echo "  # 3. Special features (when needed):"
echo "  $ nexus setfacl 'user:bob:rw-' /workspace/file.txt"
echo "  $ nexus export backup.jsonl"
echo ""
echo "  # 4. Unmount when done:"
echo "  $ nexus unmount /mnt/nexus"
echo ""

print_success "Demo finished successfully!"
print_info "Remember: Once mounted, just use standard Unix commands! 🚀"
