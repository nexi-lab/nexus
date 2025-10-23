#!/bin/bash
set -e

echo "=================================================="
echo "Nexus Unix Pipeline Integration Demo"
echo "=================================================="
echo ""

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Cleanup function
cleanup() {
    echo -e "\n${YELLOW}Cleaning up...${NC}"
    rm -f /tmp/test-pipeline-*.json
    rm -f /tmp/urls.txt
}
trap cleanup EXIT

echo -e "${BLUE}=== 1. Core Commands with Unix Tools ===${NC}"
echo "Core commands (cat, write) work with standard Unix tools like grep, jq, awk"
echo ""

# Test 1: nexus cat with Unix grep
echo -e "${GREEN}Test: nexus cat | grep${NC}"
echo "Command: nexus cat /workspace/data.txt | grep 'pattern'"
echo "This works because we use Unix grep, not nexus grep"
echo ""

# Test 2: nexus cat with jq
echo -e "${GREEN}Test: nexus cat | jq${NC}"
echo "Nexus cat outputs raw content, pipe to jq for JSON processing:"
echo "  nexus cat /workspace/config.json | jq '.items[]'"
echo ""

echo -e "${BLUE}=== 2. Plugin Commands with JSON Pipelines ===${NC}"
echo "Plugin commands support --json and --stdin for structured data exchange"
echo ""

# Test 3: Simulate scraped data
echo -e "${GREEN}Test: Simulating web scraping pipeline${NC}"
echo "Creating mock scraped data..."
cat > /tmp/test-pipeline-scraped.json <<'EOF'
{
  "type": "scraped_content",
  "url": "https://docs.example.com/api",
  "content": "# Example API Documentation\n\nThis is a test API documentation.\n\n## Authentication\nUse API keys for authentication.\n\n## Endpoints\n- GET /users\n- POST /users\n- DELETE /users/:id",
  "title": "Example API Docs",
  "metadata": {
    "scraped_at": "2025-10-23T12:00:00Z",
    "scraper": "test",
    "format": "markdown"
  }
}
EOF

echo "Mock data created at /tmp/test-pipeline-scraped.json"
echo ""

# Test 4: Skills create-from-web with stdin
echo -e "${GREEN}Test: nexus skills create-from-web --stdin${NC}"
echo "Command: cat scraped.json | nexus skills create-from-web --stdin --name example-api"
echo ""
echo "This command:"
echo "  ✓ Reads JSON from stdin"
echo "  ✓ Creates a SKILL.md file"
echo "  ✓ Auto-generates skill name from URL if not provided"
echo "  ✓ Outputs JSON for next command in pipeline"
echo ""

# Show what the output would look like
echo "Expected output JSON:"
cat <<'EOF'
{
  "type": "skill",
  "name": "example-api",
  "path": "/workspace/.nexus/skills/agent/example-api/SKILL.md",
  "tier": "agent",
  "source_url": "https://docs.example.com/api"
}
EOF
echo ""

echo -e "${BLUE}=== 3. Multi-Stage Pipelines ===${NC}"
echo "Chain multiple plugin commands together"
echo ""

echo -e "${GREEN}Example: Scrape → Create Skill → Upload${NC}"
cat <<'EOF'
nexus firecrawl scrape https://docs.stripe.com/api --json | \
  nexus skills create-from-web --stdin --name stripe-api --tier tenant | \
  nexus anthropic upload-skill --stdin
EOF
echo ""

echo -e "${BLUE}=== 4. Pipelines with Unix Tools ===${NC}"
echo "Combine plugin JSON pipelines with Unix tools"
echo ""

echo -e "${GREEN}Example: Filter with jq before creating skill${NC}"
cat <<'EOF'
nexus firecrawl scrape https://docs.example.com --json | \
  jq 'select(.content | length > 1000)' | \
  nexus skills create-from-web --stdin
EOF
echo ""

echo -e "${GREEN}Example: Batch processing with while loop${NC}"
cat <<'EOF'
cat urls.txt | while read url; do
  nexus firecrawl scrape "$url" --json | \
    nexus skills create-from-web --stdin
done
EOF
echo ""

echo -e "${GREEN}Example: Parallel processing with xargs${NC}"
cat <<'EOF'
cat urls.txt | xargs -P 4 -I {} sh -c \
  'nexus firecrawl scrape {} --json | nexus skills create-from-web --stdin'
EOF
echo ""

echo -e "${BLUE}=== 5. Standard JSON Formats ===${NC}"
echo ""

echo -e "${GREEN}Web Scraping Output Format:${NC}"
cat <<'EOF'
{
  "type": "scraped_content",
  "url": "https://docs.example.com/api",
  "content": "markdown content...",
  "title": "Page Title",
  "metadata": {
    "scraped_at": "2025-10-23T12:00:00Z",
    "scraper": "firecrawl",
    "format": "markdown"
  }
}
EOF
echo ""

echo -e "${GREEN}Skill Creation Output Format:${NC}"
cat <<'EOF'
{
  "type": "skill",
  "name": "example-api",
  "path": "/workspace/.nexus/skills/example-api/SKILL.md",
  "tier": "agent",
  "source_url": "https://docs.example.com/api",
  "metadata": {
    "created_at": "2025-10-23T12:00:00Z"
  }
}
EOF
echo ""

echo -e "${BLUE}=== Key Principles ===${NC}"
echo ""
echo "1. ✓ Core commands → Use Unix tools (grep, jq, awk, sed)"
echo "2. ✓ Plugin commands → Use JSON pipelines (--json, --stdin)"
echo "3. ✓ Include 'type' field in JSON for identification"
echo "4. ✓ Auto-detect pipes with is_piped_output()"
echo "5. ✗ Don't make core commands dual-mode (ambiguous)"
echo ""

echo -e "${YELLOW}=== What NOT to Do ===${NC}"
echo ""
echo "❌ nexus cat /file.txt | nexus grep 'pattern'"
echo "   (Ambiguous - is grep searching stdin or filesystem?)"
echo ""
echo "✅ nexus cat /file.txt | grep 'pattern'"
echo "   (Clear - using Unix grep on piped content)"
echo ""

echo "=================================================="
echo "Demo Complete!"
echo "=================================================="
