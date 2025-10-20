#!/bin/bash
# Nexus Plugin System CLI Demo
# Demonstrates plugin management and usage via CLI

set -e  # Exit on error

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}Nexus Plugin System CLI Demo${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""

# ============================================================
# Part 1: Plugin Discovery and Management
# ============================================================
echo -e "${CYAN}Part 1: Plugin Discovery and Management${NC}"
echo ""

echo -e "${GREEN}1. List all installed plugins${NC}"
nexus plugins list
echo ""

echo -e "${GREEN}2. View detailed plugin information${NC}"
echo "   Checking anthropic plugin..."
if nexus plugins list 2>&1 | grep -q "anthropic"; then
    nexus plugins info anthropic
else
    echo -e "${YELLOW}   Note: anthropic plugin not installed${NC}"
    echo "   Install with: pip install nexus-ai-fs[anthropic]"
fi
echo ""

echo -e "${GREEN}3. Check skill-seekers plugin${NC}"
if nexus plugins list 2>&1 | grep -q "skill-seekers"; then
    nexus plugins info skill-seekers
else
    echo -e "${YELLOW}   Note: skill-seekers plugin not installed${NC}"
    echo "   Install with: pip install nexus-ai-fs[skill-seekers]"
fi
echo ""

# ============================================================
# Part 2: Plugin Installation (Demonstration Only)
# ============================================================
echo -e "${CYAN}Part 2: Plugin Installation${NC}"
echo ""

echo -e "${GREEN}4. Install plugin from PyPI (demonstration)${NC}"
echo "   Command: nexus plugins install anthropic"
echo "   This runs: pip install nexus-plugin-anthropic"
echo ""
echo -e "${YELLOW}   Note: Skipping actual installation in demo${NC}"
echo "   To install manually:"
echo "     Method 1: pip install nexus-ai-fs[anthropic]"
echo "     Method 2: pip install nexus-plugin-anthropic"
echo "     Method 3: nexus plugins install anthropic"
echo ""

# ============================================================
# Part 3: Plugin Enable/Disable
# ============================================================
echo -e "${CYAN}Part 3: Plugin Enable/Disable${NC}"
echo ""

if nexus plugins list 2>&1 | grep -q "anthropic"; then
    echo -e "${GREEN}5. Disable a plugin${NC}"
    echo "   Command: nexus plugins disable anthropic"
    # Uncomment to actually disable:
    # nexus plugins disable anthropic
    echo -e "${YELLOW}   Note: Skipping actual disable in demo${NC}"
    echo ""

    echo -e "${GREEN}6. Enable a plugin${NC}"
    echo "   Command: nexus plugins enable anthropic"
    # Uncomment to actually enable:
    # nexus plugins enable anthropic
    echo -e "${YELLOW}   Note: Skipping actual enable in demo${NC}"
    echo ""
else
    echo -e "${YELLOW}5-6. Enable/disable commands (plugin not installed)${NC}"
    echo ""
fi

# ============================================================
# Part 4: Using Plugin Commands - Anthropic Plugin
# ============================================================
echo -e "${CYAN}Part 4: Using Plugin Commands - Anthropic Plugin${NC}"
echo ""

if nexus plugins list 2>&1 | grep -q "anthropic"; then
    echo -e "${GREEN}7. Anthropic plugin commands${NC}"
    echo ""

    echo "   Available commands:"
    echo "   • nexus anthropic upload-skill <skill-name>"
    echo "   • nexus anthropic download-skill <skill-id> [--tier agent]"
    echo "   • nexus anthropic list-skills [--source custom|anthropic]"
    echo "   • nexus anthropic delete-skill <skill-id>"
    echo "   • nexus anthropic browse-github [--category development]"
    echo "   • nexus anthropic import-github <skill-name> [--tier agent]"
    echo ""

    echo -e "${GREEN}8. Browse GitHub skills (demonstration)${NC}"
    echo "   Command: nexus anthropic browse-github --category development"
    echo -e "${YELLOW}   Note: This requires network access to GitHub${NC}"
    echo ""

    echo -e "${GREEN}9. List skills in Claude API (demonstration)${NC}"
    echo "   Command: nexus anthropic list-skills"
    echo -e "${YELLOW}   Note: This requires ANTHROPIC_API_KEY to be set${NC}"
    echo "   Set key with: export ANTHROPIC_API_KEY=sk-ant-api03-..."
    echo ""
else
    echo -e "${YELLOW}7-9. Anthropic plugin commands (plugin not installed)${NC}"
    echo "   Install with: pip install nexus-ai-fs[anthropic]"
    echo ""
fi

# ============================================================
# Part 5: Using Plugin Commands - Skill Seekers Plugin
# ============================================================
echo -e "${CYAN}Part 5: Using Plugin Commands - Skill Seekers Plugin${NC}"
echo ""

if nexus plugins list 2>&1 | grep -q "skill-seekers"; then
    echo -e "${GREEN}10. Skill Seekers plugin commands${NC}"
    echo ""

    echo "   Available commands:"
    echo "   • nexus skill-seekers generate <url> --name <skill-name>"
    echo "   • nexus skill-seekers import <file> --tier agent"
    echo "   • nexus skill-seekers batch <urls-file>"
    echo "   • nexus skill-seekers list"
    echo ""

    echo -e "${GREEN}11. Generate skill from documentation (demonstration)${NC}"
    echo "   Command: nexus skill-seekers generate https://react.dev/ --name react-basics"
    echo -e "${YELLOW}   Note: This will scrape the website and generate SKILL.md${NC}"
    echo "   Skipping in demo to avoid long download times"
    echo ""

    echo -e "${GREEN}12. List generated skills${NC}"
    echo "   Command: nexus skill-seekers list"
    # Uncomment to actually list:
    # nexus skill-seekers list
    echo ""
else
    echo -e "${YELLOW}10-12. Skill Seekers plugin commands (plugin not installed)${NC}"
    echo "   Install with: pip install nexus-ai-fs[skill-seekers]"
    echo ""
fi

# ============================================================
# Part 6: Plugin Help and Documentation
# ============================================================
echo -e "${CYAN}Part 6: Plugin Help and Documentation${NC}"
echo ""

echo -e "${GREEN}13. Show plugin management help${NC}"
nexus plugins --help
echo ""

echo -e "${GREEN}14. Show plugin-specific help (if installed)${NC}"
if nexus plugins list 2>&1 | grep -q "anthropic"; then
    echo "   Anthropic plugin help:"
    # Note: This would show help if implemented
    echo "   Command: nexus anthropic --help"
    echo ""
fi

if nexus plugins list 2>&1 | grep -q "skill-seekers"; then
    echo "   Skill Seekers plugin help:"
    echo "   Command: nexus skill-seekers --help"
    echo ""
fi

# ============================================================
# Part 7: Plugin Uninstallation (Demonstration Only)
# ============================================================
echo -e "${CYAN}Part 7: Plugin Uninstallation${NC}"
echo ""

echo -e "${GREEN}15. Uninstall a plugin (demonstration)${NC}"
echo "   Command: nexus plugins uninstall skill-seekers"
echo "   This runs: pip uninstall -y nexus-plugin-skill-seekers"
echo ""
echo -e "${YELLOW}   Note: Skipping actual uninstall in demo${NC}"
echo "   To uninstall manually:"
echo "     Method 1: pip uninstall nexus-plugin-skill-seekers"
echo "     Method 2: nexus plugins uninstall skill-seekers"
echo ""

# ============================================================
# Summary
# ============================================================
echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}Demo Summary${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""

echo -e "${GREEN}Plugin Management Commands:${NC}"
echo "  ✓ nexus plugins list               - List installed plugins"
echo "  ✓ nexus plugins info <name>        - Show plugin details"
echo "  ✓ nexus plugins install <name>     - Install plugin from PyPI"
echo "  ✓ nexus plugins uninstall <name>   - Remove plugin"
echo "  ✓ nexus plugins enable <name>      - Enable plugin"
echo "  ✓ nexus plugins disable <name>     - Disable plugin"
echo ""

echo -e "${GREEN}First-Party Plugins:${NC}"
echo "  • anthropic     - Claude Skills API integration"
echo "  • skill-seekers - Generate skills from documentation"
echo ""

echo -e "${GREEN}Installation Options:${NC}"
echo "  Option 1: pip install nexus-ai-fs[plugins]       # All plugins"
echo "  Option 2: pip install nexus-ai-fs[anthropic]     # Just Anthropic"
echo "  Option 3: nexus plugins install anthropic        # Via CLI"
echo ""

echo -e "${GREEN}Example Plugin Usage:${NC}"
echo "  # Anthropic plugin"
echo "  nexus anthropic upload-skill my-skill"
echo "  nexus anthropic list-skills"
echo "  nexus anthropic import-github canvas-design"
echo ""
echo "  # Skill Seekers plugin"
echo "  nexus skill-seekers generate https://react.dev/ --name react-basics"
echo "  nexus skill-seekers import /path/to/SKILL.md"
echo "  nexus skill-seekers list"
echo ""

echo -e "${GREEN}Next Steps:${NC}"
echo "  • Install plugins: pip install nexus-ai-fs[plugins]"
echo "  • Configure API keys (for Anthropic plugin)"
echo "  • Try plugin commands with real data"
echo "  • Create your own plugin: see docs/PLUGIN_DEVELOPMENT.md"
echo ""

echo -e "${GREEN}Documentation:${NC}"
echo "  • Plugin Development Guide: docs/PLUGIN_DEVELOPMENT.md"
echo "  • Plugin System Overview: docs/PLUGIN_SYSTEM.md"
echo "  • Anthropic Plugin: nexus-plugin-anthropic/README.md"
echo "  • Skill Seekers Plugin: nexus-plugin-skill-seekers/README.md"
echo ""

echo -e "${GREEN}Demo complete!${NC}"
