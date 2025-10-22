#!/usr/bin/env bash
#
# Nexus CLI Demo (No FUSE Required)
#
# This demonstrates the same features as fuse_cli_demo.sh but using
# pure Nexus CLI commands - works perfectly on macOS without macFUSE!
#
# Usage:
#   bash examples/nexus_cli_demo_no_fuse.sh

set -e

# Colors
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
NC='\033[0m'

print_header() {
    echo ""
    echo "======================================================================"
    echo -e "${CYAN}$1${NC}"
    echo "======================================================================"
}

print_section() {
    echo ""
    echo -e "${CYAN}→ $1${NC}"
}

print_success() {
    echo -e "${GREEN}✓${NC} $1"
}

# Create temp directory
TEMP_DIR=$(mktemp -d)
export NEXUS_DATA_DIR="$TEMP_DIR/nexus-data"

cleanup() {
    rm -rf "$TEMP_DIR"
    echo "✓ Cleaned up"
}
trap cleanup EXIT

print_header "Nexus CLI Demo (macOS-Friendly - No FUSE!)"

print_section "Step 1: Initialize workspace"
nexus init "$TEMP_DIR" > /dev/null
print_success "Initialized Nexus"

print_section "Step 2: Create files and directories"
nexus mkdir /workspace/documents --parents
nexus mkdir /workspace/code --parents

nexus write /workspace/README.md "# My Project

This is a sample project using Nexus.

## TODO
- Add authentication
- Improve performance
"

nexus write /workspace/code/main.py "#!/usr/bin/env python3

def main():
    # TODO: Add error handling
    print('Hello, Nexus!')

if __name__ == '__main__':
    main()
"

print_success "Created sample files"

print_section "Step 3: List files"
echo "$ nexus ls / --recursive"
nexus ls / --recursive

print_section "Step 4: Read files"
echo "$ nexus cat /workspace/README.md"
nexus cat /workspace/README.md

print_section "Step 5: Search with nexus grep"
echo "$ nexus grep 'TODO' --file-pattern '**/*.py' --file-pattern '**/*.md'"
nexus grep 'TODO' --file-pattern '**/*.py' --file-pattern '**/*.md'

print_section "Step 6: File permissions"
echo "$ nexus chmod 755 /workspace/code/main.py"
nexus chmod 755 /workspace/code/main.py

echo "$ nexus ls /workspace/code --long"
nexus ls /workspace/code --long

print_section "Step 7: Access Control Lists (ACLs)"
nexus write /workspace/sensitive.txt "Confidential data"
nexus setfacl "user:bob:r--" /workspace/sensitive.txt
nexus setfacl "user:carol:rw-" /workspace/sensitive.txt
nexus setfacl "deny:user:eve:---" /workspace/sensitive.txt

echo "$ nexus getfacl /workspace/sensitive.txt"
nexus getfacl /workspace/sensitive.txt

print_section "Step 8: Export data"
echo "$ nexus export /tmp/backup.jsonl"
nexus export /tmp/backup.jsonl
print_success "Exported to /tmp/backup.jsonl"

print_header "Demo Complete!"

echo ""
echo "Key Takeaways:"
echo "  ✓ All features work without FUSE mount"
echo "  ✓ Perfect for macOS without macFUSE"
echo "  ✓ Great for scripting and automation"
echo "  ✓ Use 'nexus grep' for searching files"
echo "  ✓ Use 'nexus cat/ls/chmod' for file ops"
echo ""
echo "💡 Want Unix tools integration? Use E2B:"
echo "   bash examples/e2b/fuse_cli_demo_e2b.sh"
echo ""
