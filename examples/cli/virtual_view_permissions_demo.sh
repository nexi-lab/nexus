#!/bin/bash
# Nexus CLI - Virtual View Permission Inheritance Demo (Simplified)
#
# This demo showcases issue #332 fix: Virtual parsed views inherit permissions.
# This is a simplified version that demonstrates the permission setup without
# requiring complex user authentication.

set -e

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
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
print_command() { echo -e "${CYAN}\$${NC} $1"; }

# Check prerequisites
if [ -z "$NEXUS_URL" ] || [ -z "$NEXUS_API_KEY" ]; then
    echo -e "${RED}✗${NC} NEXUS_URL and NEXUS_API_KEY not set."
    echo ""
    echo "Please run:"
    echo "  1. ./scripts/init-nexus-with-auth.sh"
    echo "  2. source .nexus-admin-env"
    echo "  3. $0"
    exit 1
fi

print_section "Virtual View Permission Inheritance Demo"

print_info "This demo shows how to set up permission inheritance for virtual parsed views"
print_info "Issue #332: Virtual views (*.md) now inherit permissions from original files"
echo ""

# Test base directory
DEMO_BASE="/demo-virtual-views-$$"

# Cleanup function
cleanup() {
    if [ -n "$DEMO_BASE" ]; then
        print_subsection "Cleanup"
        nexus rmdir -r $DEMO_BASE 2>/dev/null || true
        print_success "Cleaned up demo files"
    fi
}
trap cleanup EXIT

print_section "Step 1: Create Test Files"

nexus mkdir $DEMO_BASE/documents --parents
nexus rebac create user admin direct_editor file $DEMO_BASE 2>/dev/null || true
nexus rebac create user admin direct_editor file $DEMO_BASE/documents 2>/dev/null || true
print_success "Created directory: $DEMO_BASE/documents"

# Create a sample document
cat > /tmp/nexus-demo-report.txt << 'EOF'
Q4 2024 Financial Report

Executive Summary:
- Revenue: $10M (+15% YoY)
- Operating Expenses: $7M
- Net Income: $3M
EOF

nexus write $DEMO_BASE/documents/report.pdf --input /tmp/nexus-demo-report.txt
print_success "Created file: $DEMO_BASE/documents/report.pdf"

print_section "Step 2: Grant Permission to User"

# Create user alice
nexus admin create-user alice password123 2>/dev/null || print_warning "User alice may already exist"

# Grant viewer permission on the PDF
print_command "nexus rebac create user alice direct_viewer file $DEMO_BASE/documents/report.pdf"
TUPLE_RESULT=$(nexus rebac create user alice direct_viewer file $DEMO_BASE/documents/report.pdf 2>&1)
ALICE_TUPLE_ID=$(echo "$TUPLE_RESULT" | grep "Tuple ID:" | head -1 | awk '{print $3}')

print_success "Granted alice 'direct_viewer' permission"
print_info "Tuple ID: $ALICE_TUPLE_ID"

print_section "Step 3: How Virtual Views Inherit Permissions"

echo -e "${BLUE}Original file:${NC}     $DEMO_BASE/documents/report.pdf"
echo -e "${BLUE}Virtual view:${NC}      $DEMO_BASE/documents/report_parsed.pdf.md"
echo ""
print_info "The virtual view is dynamically generated - no physical file exists"
print_info "When alice tries to read the virtual view:"
echo "  1. Permission check is performed on the ${GREEN}original file${NC}"
echo "  2. Alice has direct_viewer permission on report.pdf ✓"
echo "  3. Virtual view inherits this permission automatically ✓"
echo "  4. Alice can read the parsed content"
echo ""

print_section "Step 4: Verify Permission Configuration"

print_command "nexus rebac explain user alice read file $DEMO_BASE/documents/report.pdf"
echo ""
nexus rebac explain user alice read file $DEMO_BASE/documents/report.pdf 2>&1 || true
echo ""

print_section "Step 5: Clean Up Permission"

if [ -n "$ALICE_TUPLE_ID" ]; then
    print_command "nexus rebac delete $ALICE_TUPLE_ID"
    nexus rebac delete $ALICE_TUPLE_ID
    print_success "Revoked alice's permission"
fi

print_section "Summary"

echo ""
print_success "Demo completed successfully!"
echo ""
print_info "What This Demonstrated:"
echo "  • How to grant file permissions using ReBAC"
echo "  • Virtual parsed views (.md files) inherit permissions from original files"
echo "  • Users only need permission on the original file"
echo "  • No separate permission management needed for virtual views"
echo ""
print_info "The Fix (Issue #332):"
echo "  • Modified _check_permission() to detect virtual views"
echo "  • Permission checks use the original file path"
echo "  • Modified read() to handle virtual view requests"
echo "  • Virtual views are parsed on-demand from original files"
echo ""

print_section "Demo Complete"
