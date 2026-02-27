#!/bin/bash
# CLI test script for nexus-plugin-firecrawl
#
# This script demonstrates and tests all CLI commands.
#
# Requirements:
#   - nexus-plugin-firecrawl installed
#   - FIRECRAWL_API_KEY environment variable set
#
# Usage:
#   export FIRECRAWL_API_KEY="fc-your-api-key-here"
#   chmod +x examples/cli_test.sh
#   ./examples/cli_test.sh

set -e  # Exit on error

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "============================================================"
echo "Firecrawl Plugin - CLI Tests"
echo "============================================================"

# Check if plugin is installed
echo ""
echo "Checking if plugin is installed..."
if ! nexus plugins list 2>/dev/null | grep -q "firecrawl"; then
    echo -e "${RED}❌ Error: Firecrawl plugin not installed${NC}"
    echo ""
    echo "To install the plugin:"
    echo "  1. cd nexus-plugin-firecrawl"
    echo "  2. pip install -e ."
    echo "  3. Verify: nexus plugins list"
    echo ""
    echo "Then run this test script again."
    exit 1
fi

echo -e "${GREEN}✓ Plugin is installed${NC}"

# Check if API key is set
if [ -z "$FIRECRAWL_API_KEY" ]; then
    echo -e "${RED}❌ Error: FIRECRAWL_API_KEY environment variable not set${NC}"
    echo ""
    echo "Usage:"
    echo "  export FIRECRAWL_API_KEY='fc-your-api-key-here'"
    echo "  ./examples/cli_test.sh"
    exit 1
fi

echo -e "${GREEN}✓ API key is set${NC}"
echo ""

# Test 1: Scrape a single URL
echo ""
echo -e "${BLUE}Test 1: Scraping a Single URL${NC}"
echo "============================================================"
echo -e "${YELLOW}Command:${NC} nexus firecrawl scrape https://example.com"
echo ""

if nexus firecrawl scrape https://example.com 2>&1 | head -20; then
    echo -e "${GREEN}✅ Test 1 passed${NC}"
else
    echo -e "${RED}❌ Test 1 failed${NC}"
    exit 1
fi

# Test 2: Verify JSON output (default behavior)
echo ""
echo -e "${BLUE}Test 2: JSON Output${NC}"
echo "============================================================"
echo -e "${YELLOW}Command:${NC} nexus firecrawl scrape https://example.com (checking JSON format)"
echo ""

if nexus firecrawl scrape https://example.com 2>/dev/null | jq -r '.url' | grep -q "example.com"; then
    echo -e "${GREEN}✅ Test 2 passed - Valid JSON output${NC}"
else
    echo -e "${RED}❌ Test 2 failed - Invalid JSON${NC}"
    exit 1
fi

# Test 3: Verify scrape command succeeds
echo ""
echo -e "${BLUE}Test 3: Command Success${NC}"
echo "============================================================"
echo -e "${YELLOW}Command:${NC} Check that scrape returns valid data"
echo ""

if nexus firecrawl scrape https://example.com 2>/dev/null | jq -r '.markdown' | grep -q "Example Domain"; then
    echo -e "${GREEN}✅ Test 3 passed - Content scraped correctly${NC}"
else
    echo -e "${RED}❌ Test 3 failed${NC}"
    exit 1
fi

# Test 4: Map command
echo ""
echo -e "${BLUE}Test 4: Map URLs${NC}"
echo "============================================================"
echo -e "${YELLOW}Command:${NC} nexus firecrawl map https://example.com"
echo ""

if nexus firecrawl map https://example.com 2>/dev/null | grep -q "https://example.com"; then
    echo -e "${GREEN}✅ Test 4 passed - Map command works${NC}"
else
    echo -e "${RED}❌ Test 4 failed${NC}"
    exit 1
fi

# Test 5: Pipe command (JSON output for piping)
echo ""
echo -e "${BLUE}Test 5: Pipe Command${NC}"
echo "============================================================"
echo -e "${YELLOW}Command:${NC} nexus firecrawl pipe https://example.com | jq -r '.title'"
echo ""

TITLE=$(nexus firecrawl pipe https://example.com 2>/dev/null | jq -r '.title')
if [ ! -z "$TITLE" ]; then
    echo "Title: $TITLE"
    echo -e "${GREEN}✅ Test 5 passed - Pipe command works${NC}"
else
    echo -e "${RED}❌ Test 5 failed${NC}"
    exit 1
fi

# Optional Test 6: Small crawl (commented out to save credits)
# Uncomment to test crawling
# echo ""
# echo -e "${BLUE}Test 6: Small Crawl (3 pages)${NC}"
# echo "============================================================"
# echo -e "${YELLOW}Command:${NC} nexus firecrawl crawl https://example.com --max-pages 3"
# echo -e "${YELLOW}⚠️  Note: This uses API credits${NC}"
# echo ""
#
# if nexus firecrawl crawl https://example.com --max-pages 3; then
#     echo -e "${GREEN}✅ Test 6 passed${NC}"
# else
#     echo -e "${RED}❌ Test 6 failed${NC}"
#     exit 1
# fi

# Summary
echo ""
echo "============================================================"
echo -e "${GREEN}✅ All CLI tests passed!${NC}"
echo "============================================================"
echo ""
echo "The Firecrawl plugin CLI commands are working!"
echo ""
echo "✅ Tests passed:"
echo "  1. Basic scraping"
echo "  2. JSON output format"
echo "  3. Content verification"
echo "  4. Map command"
echo "  5. Pipe command"
echo ""
echo "Available commands:"
echo "  nexus firecrawl scrape <url>      - Scrape a single URL"
echo "  nexus firecrawl crawl <url>       - Crawl a website"
echo "  nexus firecrawl map <url>         - Map all URLs"
echo "  nexus firecrawl search <query>    - Search the web"
echo "  nexus firecrawl extract <url>     - Extract structured data"
echo "  nexus firecrawl pipe <url>        - Output JSON for piping"
echo ""
echo "Note: For file output and NexusFS integration, use Python wrappers:"
echo "  python examples/scrape_cli.py <url>         - Saves to NexusFS"
echo "  python examples/crawl_cli.py <url>          - Crawls and saves"
echo ""
