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
print_command "nexus mkdir /workspace/documents --data-dir $NEXUS_DATA"
nexus mkdir /workspace/documents --data-dir "$NEXUS_DATA"

print_command "nexus mkdir /workspace/code --data-dir $NEXUS_DATA"
nexus mkdir /workspace/code --data-dir "$NEXUS_DATA"

print_success "Created directories"

# Create some text files
print_command "nexus write /workspace/README.md \"# My Project...\" --data-dir $NEXUS_DATA"
nexus write /workspace/README.md "# My Project

This is a sample project demonstrating Nexus FUSE mount.

## TODO
- Add authentication
- Improve performance
- Write more tests
" --data-dir "$NEXUS_DATA"

# Create a Python file
nexus write /workspace/code/main.py "#!/usr/bin/env python3

def main():
    # TODO: Add error handling
    print('Hello, Nexus!')

if __name__ == '__main__':
    main()
" --data-dir "$NEXUS_DATA"

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
}' --data-dir "$NEXUS_DATA"

# Upload a real PDF file
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PDF_PATH="$SCRIPT_DIR/sample-local-pdf.pdf"

if [ -f "$PDF_PATH" ]; then
    print_info "Uploading real PDF file: sample-local-pdf.pdf"

    # Read the PDF and write it to Nexus
    print_command "nexus write /workspace/documents/sample.pdf < $PDF_PATH --data-dir $NEXUS_DATA"
    cat "$PDF_PATH" | nexus write /workspace/documents/sample.pdf --input - --data-dir "$NEXUS_DATA"

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
" --data-dir "$NEXUS_DATA"
fi

print_success "Created sample files"

# List files to confirm
print_command "nexus ls / --recursive --data-dir $NEXUS_DATA"
nexus ls / --recursive --data-dir "$NEXUS_DATA"

# ============================================================================
# Example 1: Mount and Basic Operations
# ============================================================================

print_section "Step 2: Mounting Nexus Filesystem"

print_info "We'll mount in auto-parse mode so grep works directly on PDFs!"
echo ""

print_command "nexus mount $MOUNT_POINT --data-dir $NEXUS_DATA --auto-parse --daemon"
nexus mount "$MOUNT_POINT" --data-dir "$NEXUS_DATA" --auto-parse --daemon

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

# Verify it exists in Nexus
print_command "nexus cat /workspace/new_file.txt --data-dir $NEXUS_DATA"
nexus cat /workspace/new_file.txt --data-dir "$NEXUS_DATA"
echo ""

print_success "File successfully written via FUSE mount!"

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
# Summary
# ============================================================================

print_header "Demo Complete!"

echo "Key Takeaways:"
echo ""
echo "  1. ✓ Mount Nexus like any other filesystem"
echo "  2. ✓ Use ALL standard Unix tools: ls, cat, grep, find, vim, etc."
echo "  3. ✓ grep works DIRECTLY on PDFs (with --auto-parse) 🔥"
echo "  4. ✓ Choose your mode: auto-parse OR explicit .txt views"
echo "  5. ✓ Access raw files via .raw/ directory"
echo "  6. ✓ Write operations work seamlessly"
echo "  7. ✓ Perfect for scripts and automation"
echo "  8. ✓ Works with PDFs, Excel, Word, PowerPoint, and more!"
echo ""

print_info "To try this yourself:"
echo ""
echo "  # Auto-parse mode (grep PDFs directly):"
echo "  $ nexus mount /mnt/nexus --auto-parse"
echo "  $ grep 'pattern' /mnt/nexus/**/*.pdf"
echo ""
echo "  # Explicit views (binary + text compatibility):"
echo "  $ nexus mount /mnt/nexus"
echo "  $ grep 'pattern' /mnt/nexus/**/*.pdf.txt"
echo ""
echo "  # Unmount when done:"
echo "  $ nexus unmount /mnt/nexus"
echo ""

print_success "Demo finished successfully!"
