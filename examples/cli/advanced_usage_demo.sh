#!/bin/bash
# Nexus CLI - Advanced Usage Example
#
# Demonstrates advanced Nexus features using CLI with remote server:
# - Remote server operations with authentication
# - File operations (mkdir, write, read, ls, stat)
# - Search operations (grep)
# - Permission management
# - Workspace snapshots
#
# Prerequisites:
# 1. Server running: ./scripts/init-nexus-with-auth.sh
# 2. Load credentials: source .nexus-admin-env
# 3. Set SERVER_URL: export SERVER_URL=$NEXUS_URL
#
# Usage:
#   ./examples/cli/advanced_usage_demo.sh

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
echo "║         Nexus CLI - Advanced Usage Demo               ║"
echo "╚════════════════════════════════════════════════════════╝"
echo ""
print_info "Server: $NEXUS_URL (from env var)"
print_info "API Key: ${NEXUS_API_KEY:0:20}... (from env var)"
echo ""
print_info "✨ CLI automatically uses NEXUS_URL and NEXUS_API_KEY - no --remote-url flag needed!"

# 1. Create workspace structure
print_section "1. Creating Workspace Structure"

nexus mkdir /workspace/cli-demo 2>/dev/null || true
print_success "Created /workspace/cli-demo"

# Grant admin permissions FIRST before any file operations
print_info "Setting up permissions for admin user..."
# Grant both direct_owner (for write) and direct_viewer (for read)
nexus rebac create user admin direct_owner file /workspace/cli-demo 2>/dev/null || true
nexus rebac create user admin direct_viewer file /workspace/cli-demo 2>/dev/null || true

if nexus rebac check user admin write file /workspace/cli-demo 2>&1 | grep -q "GRANTED"; then
    print_success "Admin has full access to /workspace/cli-demo"
else
    print_error "Failed to grant admin permissions - demo may fail!"
    echo "Trying to continue anyway..."
fi

nexus mkdir /workspace/cli-demo/data 2>/dev/null || true
print_success "Created /workspace/cli-demo/data"

nexus mkdir /workspace/cli-demo/config 2>/dev/null || true
print_success "Created /workspace/cli-demo/config"

nexus mkdir /workspace/cli-demo/outputs 2>/dev/null || true
print_success "Created /workspace/cli-demo/outputs"

# 2. Write configuration files
print_section "2. Writing Configuration Files"

# Create a JSON config file
cat > /tmp/nexus-cli-demo-config.json <<EOF
{
  "project_name": "CLI Demo Project",
  "version": "1.0.0",
  "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "settings": {
    "debug": true,
    "max_files": 1000,
    "allowed_formats": ["json", "txt", "csv", "log"]
  }
}
EOF

nexus write /workspace/cli-demo/config/app.json /tmp/nexus-cli-demo-config.json
print_success "Written: /workspace/cli-demo/config/app.json"

# Create data files
echo "Sample data file 1
Line 2
Line 3" > /tmp/sample1.txt

nexus write /workspace/cli-demo/data/sample1.txt /tmp/sample1.txt
print_success "Written: /workspace/cli-demo/data/sample1.txt"

echo "Sample data file 2
Different content here" > /tmp/sample2.txt

nexus write /workspace/cli-demo/data/sample2.txt /tmp/sample2.txt
print_success "Written: /workspace/cli-demo/data/sample2.txt"

# Create a users JSON file
cat > /tmp/users.json <<EOF
[
  {"id": 1, "name": "Alice", "role": "admin"},
  {"id": 2, "name": "Bob", "role": "user"},
  {"id": 3, "name": "Charlie", "role": "viewer"}
]
EOF

nexus write /workspace/cli-demo/data/users.json /tmp/users.json
print_success "Written: /workspace/cli-demo/data/users.json"

# 3. Read files back
print_section "3. Reading Files"

print_info "Reading config file:"
nexus cat /workspace/cli-demo/config/app.json

echo ""
print_info "Reading sample1.txt:"
nexus cat /workspace/cli-demo/data/sample1.txt

# 4. List files
print_section "4. Listing Files"

print_info "All files in /workspace/cli-demo:"
nexus ls /workspace/cli-demo

echo ""
print_info "Data files only:"
nexus ls /workspace/cli-demo/data

# 5. File metadata
print_section "5. File Metadata"

print_info "File info for config file:"
nexus info /workspace/cli-demo/config/app.json || true

# Update the config file to create a new version
cat > /tmp/nexus-cli-demo-config-v2.json <<EOF
{
  "project_name": "CLI Demo Project",
  "version": "1.0.1",
  "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "updated_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "settings": {
    "debug": false,
    "max_files": 2000,
    "allowed_formats": ["json", "txt", "csv", "log", "md"]
  }
}
EOF

nexus write /workspace/cli-demo/config/app.json /tmp/nexus-cli-demo-config-v2.json
print_success "Updated config to version 1.0.1"

# 6. Search operations
print_section "6. Searching Files (grep)"

print_info "Searching for 'admin' in JSON files:"
nexus grep '"role".*"admin"' /workspace/cli-demo --pattern "*.json" || print_warning "Grep may not be available"

# 7. Permission management
print_section "7. Permission Management"

print_info "Checking admin permissions..."
nexus rebac check user admin write file /workspace/cli-demo || true
nexus rebac check user admin read file /workspace/cli-demo || true

echo ""
print_info "Listing ReBAC namespaces:"
nexus rebac namespace-list || true

echo ""
print_info "Note: For more on permissions, see docs/getting-started/quickstart.md"
print_info "Example commands:"
echo "  # Create user permissions"
echo "  nexus rebac create user alice direct_owner file /workspace/cli-demo"
echo ""
echo "  # Check permissions"
echo "  nexus rebac check user alice write file /workspace/cli-demo"

# 8. Workspace operations
print_section "8. Workspace Operations"

print_info "Registering workspace..."
nexus workspace register /workspace/cli-demo \
  --name "CLI Demo Workspace" \
  --description "Demo workspace for CLI examples" \
  || print_warning "Workspace registration may not be available"

print_info "Creating workspace snapshot..."
nexus workspace snapshot /workspace/cli-demo \
  --description "CLI demo initial state" \
  --tag demo \
  --tag v1.0.1 \
  || print_warning "Workspace snapshot may not be available"

print_info "Listing workspace snapshots..."
nexus workspace log /workspace/cli-demo || print_warning "Workspace snapshot log may not be available"

# 9. File existence checks
print_section "9. File Operations"

print_info "Checking if config file exists..."
if nexus info /workspace/cli-demo/config/app.json >/dev/null 2>&1; then
    print_success "Config file exists"
else
    print_error "Config file does not exist"
fi

# Copy operation
print_info "Copying sample file..."
nexus cp /workspace/cli-demo/data/sample1.txt /workspace/cli-demo/data/sample1-backup.txt && print_success "File copied successfully" || print_warning "Copy failed - may need additional permissions"

# 10. Summary
print_section "✅ Demo Complete!"

echo "You've learned:"
echo "  ✓ Connect to remote Nexus server with authentication"
echo "  ✓ Create directory structures (mkdir)"
echo "  ✓ Write and read files (write, cat)"
echo "  ✓ List files (ls)"
echo "  ✓ Get file metadata (info)"
echo "  ✓ Search file contents (grep)"
echo "  ✓ Manage permissions (rebac commands)"
echo "  ✓ Create workspace snapshots"
echo ""
echo "Demo files created in /workspace/cli-demo/"
echo ""
echo "To cleanup:"
echo "  nexus rm -r /workspace/cli-demo"
echo ""
echo "Next steps:"
echo "  - See docs/api/cli-reference.md for all CLI commands"
echo "  - See docs/api/permissions.md for permission management"
echo "  - Try the Python example: python examples/python/server_demo.py"
echo ""

# Cleanup temp files
rm -f /tmp/nexus-cli-demo-config.json /tmp/nexus-cli-demo-config-v2.json
rm -f /tmp/sample1.txt /tmp/sample2.txt /tmp/users.json

print_info "Cleanup: Removed temporary files"
