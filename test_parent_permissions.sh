#!/bin/bash
# Test parent permission inheritance

set -e

source .nexus-admin-env

echo "=== Testing Parent Permission Inheritance ==="
echo ""

# Clean up any existing test data
echo "1. Cleaning up test data..."
nexus rmdir -r -f /workspace/.nexus/skills 2>/dev/null || true
echo "   Done"
echo ""

# Grant admin permission on parent directory
echo "2. Granting admin owner permission on /workspace/.nexus/skills..."
./scripts/grant-admin-permissions.sh > /dev/null 2>&1
echo "   Done"
echo ""

# Create directory
echo "3. Creating directory /workspace/.nexus/skills/test-skill..."
nexus mkdir -p /workspace/.nexus/skills/test-skill
echo "   Done"
echo ""

# Try to write file
echo "4. Writing file to /workspace/.nexus/skills/test-skill/SKILL.md..."
echo "test content" | nexus write /workspace/.nexus/skills/test-skill/SKILL.md --input -
echo "   Done"
echo ""

echo "=== Test Completed Successfully ==="
