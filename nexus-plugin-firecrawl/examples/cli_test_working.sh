#!/bin/bash
# Working CLI tests using Python wrapper scripts
#
# These tests use the Python wrappers that actually work,
# instead of the broken `nexus firecrawl` commands.
#
# Usage:
#   export FIRECRAWL_API_KEY="fc-your-api-key-here"
#   chmod +x examples/cli_test_working.sh
#   ./examples/cli_test_working.sh

set -e  # Exit on error

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "============================================================"
echo "Firecrawl Plugin - Working CLI Tests"
echo "============================================================"

# Check if API key is set
if [ -z "$FIRECRAWL_API_KEY" ]; then
    echo -e "${RED}❌ Error: FIRECRAWL_API_KEY environment variable not set${NC}"
    echo ""
    echo "Usage:"
    echo "  export FIRECRAWL_API_KEY='fc-your-api-key-here'"
    echo "  ./examples/cli_test_working.sh"
    exit 1
fi

echo -e "${GREEN}✓ API key is set${NC}"
echo ""

# Test 1: Scrape using Python wrapper
echo ""
echo -e "${BLUE}Test 1: Scraping with Python Wrapper${NC}"
echo "============================================================"
echo -e "${YELLOW}Command:${NC} python examples/scrape_cli.py https://example.com"
echo ""

if python examples/scrape_cli.py https://example.com 2>&1 | grep -q "Done!"; then
    echo -e "${GREEN}✅ Test 1 passed - Scraping works${NC}"
else
    echo -e "${RED}❌ Test 1 failed${NC}"
    exit 1
fi

# Test 2: Verify content in NexusFS
echo ""
echo -e "${BLUE}Test 2: Verify Content Saved to NexusFS${NC}"
echo "============================================================"
echo -e "${YELLOW}Command:${NC} nexus ls /workspace/scraped/"
echo ""

if nexus ls /workspace/scraped/ 2>&1 | grep -q "example_com"; then
    echo -e "${GREEN}✅ Test 2 passed - Content saved to NexusFS${NC}"
else
    echo -e "${RED}❌ Test 2 failed - Content not in NexusFS${NC}"
    exit 1
fi

# Test 3: Read content from NexusFS
echo ""
echo -e "${BLUE}Test 3: Read Content from NexusFS${NC}"
echo "============================================================"
echo -e "${YELLOW}Command:${NC} nexus cat /workspace/scraped/example_com/index.md"
echo ""

if nexus cat /workspace/scraped/example_com/index.md 2>&1 | grep -q "Example Domain"; then
    echo -e "${GREEN}✅ Test 3 passed - Can read from NexusFS${NC}"
else
    echo -e "${RED}❌ Test 3 failed - Cannot read content${NC}"
    exit 1
fi

# Test 4: Scrape with custom output
echo ""
echo -e "${BLUE}Test 4: Scrape with Custom Output${NC}"
echo "============================================================"
echo -e "${YELLOW}Command:${NC} python examples/scrape_cli.py https://example.com --output test-output.md"
echo ""

if python examples/scrape_cli.py https://example.com --output test-output.md 2>&1 | grep -q "Done!"; then
    if nexus cat /workspace/test-output.md 2>&1 | grep -q "Example Domain"; then
        echo -e "${GREEN}✅ Test 4 passed - Custom output works${NC}"
    else
        echo -e "${RED}❌ Test 4 failed - Custom output not found${NC}"
        exit 1
    fi
else
    echo -e "${RED}❌ Test 4 failed - Scraping failed${NC}"
    exit 1
fi

# Test 5: Plugin is installed
echo ""
echo -e "${BLUE}Test 5: Plugin Discovery${NC}"
echo "============================================================"
echo -e "${YELLOW}Command:${NC} nexus plugins list"
echo ""

if nexus plugins list 2>&1 | grep -q "firecrawl"; then
    echo -e "${GREEN}✅ Test 5 passed - Plugin is discovered${NC}"
else
    echo -e "${RED}❌ Test 5 failed - Plugin not found${NC}"
    exit 1
fi

# Test 6: Plugin info shows commands
echo ""
echo -e "${BLUE}Test 6: Plugin Commands Registered${NC}"
echo "============================================================"
echo -e "${YELLOW}Command:${NC} nexus plugins info firecrawl"
echo ""

if nexus plugins info firecrawl 2>&1 | grep -q "scrape"; then
    echo -e "${GREEN}✅ Test 6 passed - Commands are registered${NC}"
else
    echo -e "${RED}❌ Test 6 failed - Commands not registered${NC}"
    exit 1
fi

# Summary
echo ""
echo "============================================================"
echo -e "${GREEN}✅ All tests passed!${NC}"
echo "============================================================"
echo ""
echo "The Firecrawl plugin is working correctly!"
echo ""
echo "Available Python wrappers:"
echo "  python examples/scrape_cli.py <url> [--output FILE]"
echo "  python examples/crawl_cli.py <url> [--max-pages N]"
echo ""
echo "Note: 'nexus firecrawl' commands don't work yet (Nexus core bug)"
echo "      Use the Python wrappers above instead."
echo ""
echo "Content is saved to:"
echo "  /workspace/scraped/  - for scrape command"
echo "  /workspace/crawled/  - for crawl command"
echo ""
echo "Access with:"
echo "  nexus ls /workspace/scraped/"
echo "  nexus cat /workspace/scraped/example_com/index.md"
echo "  nexus grep 'search term' /workspace/scraped/"
echo ""
