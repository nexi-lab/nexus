#!/bin/bash
# grant-admin-permissions.sh - Grant admin user permissions for demo paths
#
# This script grants the admin user write permissions for:
# - /workspace/.nexus/skills/ (agent tier)
# - /shared/skills/ (tenant tier)
# - /system/skills/ (system tier)
#
# Usage:
#   source .nexus-admin-env
#   ./scripts/grant-admin-permissions.sh

set -e

if [ -z "$NEXUS_URL" ] || [ -z "$NEXUS_API_KEY" ]; then
    echo "❌ Error: NEXUS_URL and NEXUS_API_KEY not set"
    echo "   Run: source .nexus-admin-env"
    exit 1
fi

echo "╔═══════════════════════════════════════════════════════════╗"
echo "║      Granting Admin Permissions for Demo Paths           ║"
echo "╚═══════════════════════════════════════════════════════════╗"
echo ""

# Function to grant permission
grant_permission() {
    local path="$1"
    local user="${2:-admin}"

    echo "Granting WRITE permission for: $path"

    # Create ReBAC tuple: (user, admin) owner (file, path)
    # Note: "owner" relation grants write permission (see DEFAULT_FILE_NAMESPACE)
    nexus rebac create user "$user" owner file "$path" 2>&1 || {
        echo "  ⚠️  Warning: Failed to grant permission (tuple may already exist)"
    }

    echo "  ✓ Permission granted"
}

# Grant permissions for demo paths
echo "1. Agent tier skills directory"
grant_permission "/workspace/.nexus/skills"

echo ""
echo "2. Tenant tier skills directory"
grant_permission "/shared/skills"

echo ""
echo "3. System tier skills directory"
grant_permission "/system/skills"

echo ""
echo "4. Workspace root (for testing)"
grant_permission "/workspace"

echo ""
echo "╔═══════════════════════════════════════════════════════════╗"
echo "║               Permissions Granted Successfully            ║"
echo "╚═══════════════════════════════════════════════════════════╝"
echo ""
echo "You can now run the demo:"
echo "  ./examples/cli/skills_rebac_demo.sh"
echo ""
