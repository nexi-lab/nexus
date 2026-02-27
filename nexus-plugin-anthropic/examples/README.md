# Nexus Anthropic Plugin - Examples

Comprehensive examples demonstrating how to use custom Nexus skills and Anthropic skills through both CLI and Python SDK.

## Overview

This directory contains practical examples for:

1. **Custom Nexus Skills** - Creating, managing, and exporting your own skills
2. **Anthropic Skills API** - Uploading and managing skills in Claude
3. **GitHub Skills** - Importing official Anthropic skills
4. **Advanced Workflows** - Complete end-to-end skill lifecycle

## Prerequisites

### Installation

```bash
# Install core Nexus
pip install nexus-ai-fs

# Install Anthropic plugin
pip install nexus-plugin-anthropic

# Or install from source
cd nexus-plugin-anthropic
pip install -e .
```

### API Key Setup

Set your Anthropic API key:

```bash
export ANTHROPIC_API_KEY="sk-ant-api03-..."
```

Or create a config file at `~/.nexus/plugins/anthropic/config.yaml`:

```yaml
api_key: sk-ant-api03-...
```

## Examples

### 0. Quick Start (Recommended)

**File:** `quick_start.py`

The fastest way to get started with both custom skills and Anthropic integration.

**Run it:**

```bash
# Set your API key
cp .env.example .env
# Edit .env and add your API key

# Run
python quick_start.py
```

**What it does:**
- ✅ Imports a skill from GitHub
- ✅ Creates a custom skill
- ✅ Uploads to Claude Skills API
- ✅ Lists all skills

Perfect for learning the basics in 5 minutes!

---

### 1. CLI Examples

**File:** `cli_examples.sh`

Demonstrates command-line usage of Nexus skills and Anthropic integration.

**Run the examples:**

```bash
# Make it executable
chmod +x cli_examples.sh

# Run all examples
./cli_examples.sh

# Or run specific sections by editing the script
```

**What it covers:**

- ✅ Creating custom skills with `nexus skills create`
- ✅ Managing skills (list, search, info, fork)
- ✅ Viewing dependency trees with `nexus skills deps`
- ✅ Validating and exporting skills
- ✅ Uploading to Claude Skills API
- ✅ Browsing and importing GitHub skills
- ✅ Advanced workflows (fork, customize, upload)

### 2. Python SDK Examples

**File:** `python_sdk_examples.py`

Demonstrates programmatic usage using the Nexus Python SDK.

**Run the examples:**

```bash
# Install dependencies
pip install nexus-ai-fs nexus-plugin-anthropic anthropic

# Run all examples
python python_sdk_examples.py

# Or run specific examples in your own script
```

**What it covers:**

- ✅ **Example 1:** Custom Nexus Skills
  - Creating skills programmatically
  - Skill discovery and search
  - Dependency resolution
  - Export and validation

- ✅ **Example 2:** Anthropic Skills API
  - Uploading skills to Claude
  - Listing and filtering skills
  - Downloading skills

- ✅ **Example 3:** GitHub Skills Integration
  - Browsing available skills
  - Importing multiple skills
  - Verifying imported skills

- ✅ **Example 4:** Advanced Workflow
  - Import from GitHub
  - Fork and customize
  - Validate and export
  - Upload to Claude API
  - Publish to team tier

- ✅ **Example 5:** Using Skills with Claude API
  - Integration with Messages API
  - Custom skill usage
  - Anthropic skill usage

## Quick Start Guide

### Creating and Using Custom Skills

**CLI:**

```bash
# 1. Create a skill
nexus skills create my-analyzer --description "Analyze data patterns"

# 2. View it
nexus skills info my-analyzer

# 3. Export it
nexus skills export my-analyzer --output my-analyzer.zip

# 4. Upload to Claude
nexus anthropic upload-skill my-analyzer --display-title "My Analyzer v1.0"
```

**Python:**

```python
import asyncio
from nexus import connect
from nexus.skills import SkillManager, SkillRegistry

async def main():
    nx = connect()
    registry = SkillRegistry(nx)
    manager = SkillManager(nx, registry)

    # Create skill
    await manager.create_skill(
        name="my-analyzer",
        description="Analyze data patterns",
        tier="agent"
    )

    # Get skill details
    skill = await registry.get_skill("my-analyzer")
    print(f"Created: {skill.metadata.name}")

    nx.close()

asyncio.run(main())
```

### Importing GitHub Skills

**CLI:**

```bash
# 1. Browse available skills
nexus anthropic browse-github

# 2. Import a skill
nexus anthropic import-github algorithmic-art

# 3. Verify it's imported
nexus skills info algorithmic-art

# 4. Use it (fork, customize, etc.)
nexus skills fork algorithmic-art my-art-tool
```

**Python:**

```python
import asyncio
from nexus import connect
from nexus.plugins.registry import PluginRegistry

async def main():
    nx = connect()

    # Get plugin
    plugin_registry = PluginRegistry(nx)
    plugin_registry.discover()
    plugin = plugin_registry.get_plugin("anthropic")

    # Import skill
    await plugin.import_github_skill("algorithmic-art", tier="agent")

    nx.close()

asyncio.run(main())
```

### Using Skills with Claude Messages API

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-ant-api03-...")

# Use a custom skill you uploaded
message = client.messages.create(
    model="claude-3-5-sonnet-20241022",
    max_tokens=1024,
    messages=[
        {"role": "user", "content": "Analyze this data"}
    ],
    tools=[{"type": "code_execution"}],
    container={
        "skills": [
            {
                "type": "custom",
                "skill_id": "skill_01AbCdEfGhIjKlMnOpQrStUv",  # From upload
                "version": "latest"
            }
        ]
    },
    extra_headers={
        "anthropic-beta": "skills-2025-10-02,code-execution-2025-08-25"
    }
)

print(message.content)
```

## Common Workflows

### Workflow 1: Create Custom Skill → Upload to Claude

```bash
# 1. Create
nexus skills create data-transformer --description "Transform CSV data"

# 2. Validate for Claude (max 8MB)
nexus skills validate data-transformer --format claude

# 3. Upload
nexus anthropic upload-skill data-transformer
```

### Workflow 2: Import GitHub Skill → Customize → Share

```bash
# 1. Import from GitHub
nexus anthropic import-github mcp-builder

# 2. Fork to customize
nexus skills fork mcp-builder my-mcp-builder

# 3. Export for sharing
nexus skills export my-mcp-builder --output my-mcp-builder.zip

# 4. Share with team (publish to tenant tier)
nexus skills publish my-mcp-builder --from-tier agent --to-tier tenant
```

### Workflow 3: Download Claude Skill → Fork → Re-upload

```bash
# 1. List your Claude skills
nexus anthropic list-skills --source custom

# 2. Download one
nexus anthropic download-skill skill_01ABC... --tier agent

# 3. Fork it
nexus skills fork downloaded-skill improved-skill

# 4. Upload new version
nexus anthropic upload-skill improved-skill
```

## Available GitHub Skills

Import any of these official Anthropic skills:

| Skill Name | Description |
|------------|-------------|
| `algorithmic-art` | Create algorithmic art with p5.js |
| `artifacts-builder` | Build HTML artifacts |
| `canvas-design` | Design on HTML canvas |
| `mcp-builder` | Create MCP servers |
| `brand-guidelines` | Apply brand guidelines |
| `internal-comms` | Internal communications |
| `slack-gif-creator` | Create GIFs for Slack |
| `theme-factory` | Create and apply themes |
| `webapp-testing` | Test web applications |
| `skill-creator` | Create new skills |
| `document-skills` | Advanced document skills |
| `template-skill` | Template for new skills |

```bash
# Import any skill
nexus anthropic import-github <skill-name>
```

## Skill Tier System

Nexus organizes skills into three tiers:

- **Agent Tier** (`/workspace/.nexus/skills/`) - Personal, agent-specific skills
- **Tenant Tier** (`/shared/skills/`) - Shared across team/organization
- **System Tier** (`/system/skills/`) - System-wide, immutable skills

```bash
# Create at different tiers
nexus skills create my-skill --tier agent
nexus skills publish my-skill --from-tier agent --to-tier tenant
```

## Advanced Features

### Dependency Management

```bash
# View dependency tree
nexus skills deps my-skill

# View as list
nexus skills deps my-skill --no-visual

# Export with dependencies
nexus skills export my-skill --output skill.zip
```

### Skill Comparison

```bash
# Compare two skills
nexus skills diff skill-v1 skill-v2

# With more context
nexus skills diff skill-v1 skill-v2 --context 10
```

### Search and Discovery

```bash
# Search all skills
nexus skills search "data analysis"

# Filter by tier
nexus skills search "analysis" --tier tenant --limit 5
```

## Troubleshooting

### Issue: "NexusFS not available"

**Solution:** Plugin commands need to be run from a Nexus workspace:

```bash
# Initialize workspace first
nexus init ./my-workspace
cd my-workspace

# Set data directory
export NEXUS_DATA_DIR=./nexus-data

# Now run plugin commands
nexus anthropic browse-github
```

### Issue: "Anthropic API key not found"

**Solution:** Set your API key:

```bash
# Option 1: Environment variable
export ANTHROPIC_API_KEY="sk-ant-api03-..."

# Option 2: Config file
mkdir -p ~/.nexus/plugins/anthropic
echo "api_key: sk-ant-api03-..." > ~/.nexus/plugins/anthropic/config.yaml

# Option 3: Command-line argument
nexus anthropic upload-skill my-skill --api-key sk-ant-api03-...
```

### Issue: Skill validation fails (size limit)

**Solution:** Claude Skills API has an 8MB limit:

```bash
# Check size
nexus skills size my-skill --human

# Validate before upload
nexus skills validate my-skill --format claude

# Export without dependencies if too large
nexus skills export my-skill --output skill.zip --no-deps
```

## Security Notes

⚠️ **Important:**

1. **Never commit API keys to version control**
2. Use environment variables or secure config files
3. Add `config.yaml` to `.gitignore`
4. Rotate keys regularly
5. Use separate keys for development and production

## Additional Resources

- [Nexus Documentation](https://github.com/nexi-lab/nexus)
- [Anthropic Skills Documentation](https://docs.claude.com/en/docs/build-with-claude/skills)
- [Anthropic Skills GitHub Repository](https://github.com/anthropics/skills)
- [Claude API Documentation](https://docs.claude.com)

## Support

- **Issues:** [GitHub Issues](https://github.com/nexi-lab/nexus-plugin-anthropic/issues)
- **Nexus Main Project:** [nexi-lab/nexus](https://github.com/nexi-lab/nexus)

## License

Apache 2.0 - See LICENSE file for details
