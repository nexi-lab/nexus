#!/bin/bash
# Skill Seekers Plugin CLI Demo
# Demonstrates how to use the nexus-plugin-skill-seekers plugin via CLI

set -e  # Exit on error

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Test workspace
TEST_WORKSPACE="/tmp/nexus-skill-seekers-demo-$$"
DATA_DIR="$TEST_WORKSPACE/nexus-data"

# Set NEXUS_DATA_DIR environment variable
export NEXUS_DATA_DIR="$DATA_DIR"

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}Nexus Skill Seekers Plugin CLI Demo${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""

# Cleanup function
cleanup() {
    echo -e "\n${YELLOW}Cleaning up test workspace...${NC}"
    rm -rf "$TEST_WORKSPACE"
}

# Register cleanup
trap cleanup EXIT

# Check if plugin is installed
if ! python -c "import nexus_skill_seekers" 2>/dev/null; then
    echo -e "${YELLOW}Installing Skill Seekers plugin...${NC}"
    pip install -e ./nexus-plugin-skill-seekers > /dev/null 2>&1
fi

echo -e "${GREEN}Starting Skill Seekers plugin demo...${NC}\n"

# ============================================================
# Part 1: Setup and Initialization
# ============================================================
echo -e "${BLUE}Part 1: Setup and Initialization${NC}"
echo ""

# Initialize workspace
echo -e "${GREEN}1. Initialize workspace${NC}"
nexus init "$TEST_WORKSPACE"
echo ""

# Verify plugin is loaded
echo -e "${GREEN}2. List installed plugins${NC}"
nexus plugins list
echo ""

# View plugin info
echo -e "${GREEN}3. View Skill Seekers plugin info${NC}"
nexus plugins info skill-seekers
echo ""

# ============================================================
# Part 2: Generate Skills from Documentation
# ============================================================
echo -e "\n${BLUE}Part 2: Generate Skills from Documentation${NC}"
echo ""

# Generate a skill from React documentation
echo -e "${GREEN}4. Generate skill from React documentation${NC}"
echo "   URL: https://react.dev/"
echo "   Note: This will scrape the documentation and create a SKILL.md file"
echo ""
nexus skill-seekers generate https://react.dev/ --name react-basics --tier agent
echo ""

# Generate a skill from FastAPI documentation with custom tier
echo -e "${GREEN}5. Generate skill from FastAPI documentation (tenant tier)${NC}"
echo "   URL: https://fastapi.tiangolo.com/"
echo ""
nexus skill-seekers generate https://fastapi.tiangolo.com/ --name fastapi-guide --tier tenant
echo ""

# Generate skill with auto-generated name
echo -e "${GREEN}6. Generate skill with auto-generated name${NC}"
echo "   URL: https://docs.python.org/3/library/asyncio.html"
echo ""
nexus skill-seekers generate https://docs.python.org/3/library/asyncio.html
echo ""

# ============================================================
# Part 3: List Generated Skills
# ============================================================
echo -e "\n${BLUE}Part 3: List Generated Skills${NC}"
echo ""

echo -e "${GREEN}7. List all generated skills${NC}"
nexus skill-seekers list
echo ""

# ============================================================
# Part 4: Import Existing SKILL.md Files
# ============================================================
echo -e "\n${BLUE}Part 4: Import Existing SKILL.md Files${NC}"
echo ""

# Create a sample SKILL.md file for import
SAMPLE_SKILL="$TEST_WORKSPACE/sample-skill.md"
cat > "$SAMPLE_SKILL" << 'EOF'
---
name: custom-api-skill
version: 1.0.0
description: Custom API integration skill
author: Demo Team
created: 2025-01-15T10:00:00Z
tier: agent
---

# Custom API Skill

## Overview

This is a custom skill for API integration patterns.

## Features

- RESTful API design
- GraphQL integration
- WebSocket communication
- Authentication strategies

## Usage

Use this skill when working with API integrations in your projects.

## Keywords

api, rest, graphql, websocket, authentication
EOF

echo -e "${GREEN}8. Import existing SKILL.md file${NC}"
echo "   File: $SAMPLE_SKILL"
nexus skill-seekers import "$SAMPLE_SKILL" --tier agent
echo ""

# Import with custom name
echo -e "${GREEN}9. Import with custom name${NC}"
nexus skill-seekers import "$SAMPLE_SKILL" --name custom-api-v2 --tier agent
echo ""

# ============================================================
# Part 5: Batch Generation
# ============================================================
echo -e "\n${BLUE}Part 5: Batch Generation from URL List${NC}"
echo ""

# Create URLs file for batch processing
URLS_FILE="$TEST_WORKSPACE/urls.txt"
cat > "$URLS_FILE" << 'EOF'
# Documentation URLs for batch processing
# Format: url name
https://docs.djangoproject.com/ django-framework
https://vuejs.org/guide/ vue-guide
https://docs.pytest.org/ pytest-testing
EOF

echo -e "${GREEN}10. Create URLs file for batch processing${NC}"
echo "   File: $URLS_FILE"
cat "$URLS_FILE"
echo ""

echo -e "${GREEN}11. Batch generate skills from URLs file${NC}"
nexus skill-seekers batch "$URLS_FILE" --tier tenant
echo ""

# ============================================================
# Part 6: Verify Skills in Nexus Filesystem
# ============================================================
echo -e "\n${BLUE}Part 6: Verify Skills in Nexus Filesystem${NC}"
echo ""

echo -e "${GREEN}12. List agent tier skills${NC}"
nexus ls /workspace/.nexus/skills/
echo ""

echo -e "${GREEN}13. List tenant tier skills${NC}"
nexus ls /shared/skills/
echo ""

echo -e "${GREEN}14. View a generated skill file${NC}"
# Find first .md file in agent skills
FIRST_SKILL=$(nexus ls /workspace/.nexus/skills/ | head -1)
if [ -n "$FIRST_SKILL" ]; then
    echo "   Viewing: /workspace/.nexus/skills/$FIRST_SKILL"
    nexus cat "/workspace/.nexus/skills/$FIRST_SKILL" | head -30
fi
echo ""

# ============================================================
# Part 7: Integration with Nexus Skills System
# ============================================================
echo -e "\n${BLUE}Part 7: Integration with Nexus Skills System${NC}"
echo ""

echo -e "${GREEN}15. List all skills using Nexus skills command${NC}"
nexus skills list
echo ""

echo -e "${GREEN}16. Search for generated skills${NC}"
nexus skills search "api"
echo ""

echo -e "${GREEN}17. Get info about a generated skill${NC}"
if [ -n "$FIRST_SKILL" ]; then
    SKILL_NAME=$(basename "$FIRST_SKILL" .md)
    echo "   Skill: $SKILL_NAME"
    nexus skills info "$SKILL_NAME" 2>/dev/null || echo "   (Skill info may not be available for auto-generated skills)"
fi
echo ""

# ============================================================
# Part 8: Advanced Configuration
# ============================================================
echo -e "\n${BLUE}Part 8: Advanced Configuration${NC}"
echo ""

echo -e "${GREEN}18. Configuration Options${NC}"
echo "   Config file: ~/.nexus/plugins/skill-seekers/config.yaml"
echo ""
echo "   Example configuration:"
cat << 'EOF'
   ---
   # Default tier for imported skills
   default_tier: agent

   # Default output directory
   output_dir: /tmp/nexus-skills

   # OpenAI API key for skill generation (optional)
   openai_api_key: sk-...
EOF
echo ""

# ============================================================
# Summary
# ============================================================
echo -e "\n${BLUE}========================================${NC}"
echo -e "${BLUE}Demo Summary${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""
echo -e "${GREEN}Commands demonstrated:${NC}"
echo "  • nexus plugins list               - List installed plugins"
echo "  • nexus plugins info skill-seekers - View plugin details"
echo "  • nexus skill-seekers generate     - Generate skill from URL"
echo "  • nexus skill-seekers import       - Import existing SKILL.md"
echo "  • nexus skill-seekers batch        - Batch generate from URLs file"
echo "  • nexus skill-seekers list         - List generated skills"
echo ""
echo -e "${GREEN}Key Features:${NC}"
echo "  ✓ Automatic documentation scraping"
echo "  ✓ SKILL.md generation with metadata"
echo "  ✓ Multi-tier support (agent, tenant, system)"
echo "  ✓ Batch processing from URLs file"
echo "  ✓ Import existing skill files"
echo "  ✓ Integration with Nexus skills system"
echo ""
echo -e "${GREEN}Next Steps:${NC}"
echo "  • Configure OpenAI API key for enhanced skill generation"
echo "  • Create custom URL lists for your documentation needs"
echo "  • Use generated skills in your AI workflows"
echo "  • Share skills across teams using tenant tier"
echo ""
echo -e "${GREEN}Demo complete!${NC}"
