#!/bin/bash
# Nexus Anthropic Plugin - CLI Examples
#
# This file demonstrates how to use custom skills and Anthropic skills
# through the Nexus CLI interface.

set -e  # Exit on error

echo "==================================================================="
echo "Nexus Anthropic Plugin - CLI Examples"
echo "==================================================================="

# Export API key - replace with your actual key or set via environment
# Get your API key from: https://console.anthropic.com/settings/keys
if [ -z "$ANTHROPIC_API_KEY" ]; then
    echo "Error: ANTHROPIC_API_KEY environment variable is not set"
    echo "Please set it with: export ANTHROPIC_API_KEY='your-api-key-here'"
    echo "Get your API key from: https://console.anthropic.com/settings/keys"
    exit 1
fi

# Set up test workspace
export NEXUS_DATA_DIR="./nexus-examples-data"

echo ""
echo "==================================================================="
echo "PART 1: Working with Custom Nexus Skills"
echo "==================================================================="

# Clean up any existing workspace
rm -rf ./nexus-examples-workspace 2>/dev/null || true
rm -rf $NEXUS_DATA_DIR 2>/dev/null || true

# Initialize Nexus workspace
echo "1. Initializing Nexus workspace..."
nexus init ./nexus-examples-workspace

# Create a custom skill with timestamp to avoid conflicts
TIMESTAMP=$(date +%s)
SKILL_NAME="code-analyzer-${TIMESTAMP}"

echo ""
echo "2. Creating a custom skill..."
nexus skills create $SKILL_NAME \
    --description "Analyzes code for best practices and patterns"

# List all skills
echo ""
echo "3. Listing all skills in Nexus..."
nexus skills list

# View skill details
echo ""
echo "4. Viewing skill details..."
nexus skills info $SKILL_NAME

# Fork a skill to create variations
echo ""
echo "5. Forking a skill..."
nexus skills fork $SKILL_NAME python-analyzer-${TIMESTAMP}

# Search for skills
echo ""
echo "6. Searching for skills..."
nexus skills search "code"

# View dependency tree
echo ""
echo "7. Viewing skill dependencies..."
nexus skills deps $SKILL_NAME

# Validate skill
echo ""
echo "8. Validating skill for Claude API..."
nexus skills validate $SKILL_NAME --format claude

# Export skill to zip
echo ""
echo "9. Exporting skill to .zip package..."
nexus skills export $SKILL_NAME --output ./code-analyzer.zip --format claude

echo ""
echo "==================================================================="
echo "PART 2: Anthropic Skills API Integration"
echo "==================================================================="

# Upload custom skill to Claude Skills API
echo ""
echo "10. Uploading custom skill to Claude Skills API..."
nexus anthropic upload-skill $SKILL_NAME \
    --display-title "Code Analyzer v1.0"

# List all skills in Claude API
echo ""
echo "11. Listing skills in Claude Skills API..."
nexus anthropic list-skills

# List only custom skills
echo ""
echo "12. Listing only custom skills..."
nexus anthropic list-skills --source custom

# List only Anthropic-provided skills
echo ""
echo "13. Listing Anthropic-provided skills..."
nexus anthropic list-skills --source anthropic

echo ""
echo "==================================================================="
echo "PART 3: GitHub Skills Repository Integration"
echo "==================================================================="

# Browse available GitHub skills
echo ""
echo "14. Browsing Anthropic skills from GitHub..."
nexus anthropic browse-github

# Filter by category
echo ""
echo "15. Filtering skills by category..."
nexus anthropic browse-github --category development

# Import a skill from GitHub
echo ""
echo "16. Importing a skill from GitHub..."
nexus anthropic import-github algorithmic-art --tier agent

# Import another skill
echo ""
echo "17. Importing MCP builder skill..."
nexus anthropic import-github mcp-builder --tier tenant

# Verify imported skills
echo ""
echo "18. Listing all skills (including GitHub imports)..."
nexus skills list

# View imported skill details
echo ""
echo "19. Viewing imported skill details..."
nexus skills info algorithmic-art

echo ""
echo "==================================================================="
echo "PART 4: Advanced Workflows"
echo "==================================================================="

# Fork a GitHub skill to customize it
echo ""
echo "20. Forking GitHub skill for customization..."
nexus skills fork algorithmic-art my-art-generator-${TIMESTAMP} || echo "Skill already exists, skipping fork"

# Compare original vs fork (if fork succeeded)
echo ""
echo "21. Comparing original vs customized skill..."
nexus skills diff algorithmic-art my-art-generator-${TIMESTAMP} 2>/dev/null || echo "Skipping diff (fork didn't succeed)"

# Upload customized skill to Claude API
echo ""
echo "22. Uploading customized skill to Claude API..."
nexus anthropic upload-skill my-art-generator-${TIMESTAMP} \
    --display-title "My Custom Art Generator" 2>/dev/null || echo "Skipping upload (skill doesn't exist)"

# Download a skill from Claude API (if you have one)
# Replace skill_01ABC... with actual skill ID from list-skills
echo ""
echo "23. Downloading skill from Claude API (example)..."
echo "   nexus anthropic download-skill skill_01AbCdEfGhIjKlMnOpQrStUv --tier agent"

# Delete a skill from Claude API (example, commented out for safety)
echo ""
echo "24. Deleting skill from Claude API (example - commented for safety)..."
echo "   # nexus anthropic delete-skill skill_01AbCdEfGhIjKlMnOpQrStUv"

echo ""
echo "==================================================================="
echo "PART 5: Batch Operations"
echo "==================================================================="

# Import multiple GitHub skills at once
echo ""
echo "25. Importing multiple GitHub skills..."
for skill in canvas-design artifacts-builder theme-factory; do
    echo "Importing $skill..."
    nexus anthropic import-github "$skill" --tier agent
done

# Publish a skill to tenant library
echo ""
echo "26. Publishing skill to tenant library..."
nexus skills publish $SKILL_NAME --from-tier agent --to-tier tenant

# Search across all tiers
echo ""
echo "27. Searching across all skills..."
nexus skills search "art" --limit 10

echo ""
echo "==================================================================="
echo "Examples completed successfully!"
echo "==================================================================="
echo ""
echo "Cleanup (optional):"
echo "  rm -rf ./nexus-examples-workspace"
echo "  rm -rf ./nexus-examples-data"
echo "  rm ./code-analyzer.zip"
