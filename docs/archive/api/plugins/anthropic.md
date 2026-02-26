# Anthropic Plugin

← [Plugins API](index.md)

Official plugin for integrating Nexus with Claude Skills API.

## Overview

The Anthropic plugin provides seamless integration with Claude's Skills API, allowing you to:

- Upload Nexus skills to Claude Skills API
- Download skills from Claude Skills API
- List and manage Claude skills
- Import skills from the official Anthropic skills GitHub repository

## Installation

```bash
pip install nexus-plugin-anthropic
```

The plugin is automatically discovered by Nexus.

## Configuration

Create `~/.nexus/plugins/anthropic/config.yaml`:

```yaml
api_key: "sk-ant-..."  # Your Anthropic API key
```

Or set the environment variable:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

## Commands

### upload-skill

Upload a Nexus skill to Claude Skills API:

```bash
nexus anthropic upload-skill <skill-name> [options]
```

**Options:**
- `--api-key TEXT`: Anthropic API key (overrides config/env)
- `--format TEXT`: Export format (default: claude)
- `--display-title TEXT`: Display title for the skill

**Example:**
```bash
# Upload a skill
nexus anthropic upload-skill my-analyzer

# Upload with custom title
nexus anthropic upload-skill my-analyzer --display-title "Code Analyzer Pro"
```

**What it does:**
1. Exports the skill from Nexus using SkillExporter
2. Filters frontmatter to Claude-compatible fields
3. Uploads to Claude Skills API via `/beta/skills` endpoint
4. Returns skill ID and version

### list-skills

List all skills in Claude Skills API:

```bash
nexus anthropic list-skills [options]
```

**Options:**
- `--api-key TEXT`: Anthropic API key
- `--source TEXT`: Filter by source ("custom" or "anthropic")

**Example:**
```bash
# List all skills
nexus anthropic list-skills

# List only custom skills
nexus anthropic list-skills --source custom

# List only Anthropic-provided skills
nexus anthropic list-skills --source anthropic
```

**Output:**
```
┏━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━┓
┃ ID             ┃ Display Title ┃ Latest Ver   ┃ Source ┃ Created At ┃
┡━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━┩
│ skill_01AbCd   │ My Analyzer   │ 1            │ custom │ 2025-01-15 │
│ skill_02EfGh   │ Data Parser   │ 2            │ custom │ 2025-01-20 │
└────────────────┴───────────────┴──────────────┴────────┴────────────┘
```

### download-skill

Download a skill from Claude Skills API:

```bash
nexus anthropic download-skill <skill-id> [options]
```

**Options:**
- `--api-key TEXT`: Anthropic API key
- `--tier TEXT`: Target tier (agent, tenant, system). Default: agent
- `--version TEXT`: Skill version. Default: latest

**Example:**
```bash
# Download a skill
nexus anthropic download-skill skill_01AbCdEfGhIjKlMn

# Download to specific tier
nexus anthropic download-skill skill_01AbCdEfGhIjKlMn --tier tenant
```

### delete-skill

Delete a skill from Claude Skills API:

```bash
nexus anthropic delete-skill <skill-id> [options]
```

**Options:**
- `--api-key TEXT`: Anthropic API key
- `--confirm`: Skip confirmation prompt

**Example:**
```bash
# Delete with confirmation
nexus anthropic delete-skill skill_01AbCdEfGhIjKlMn

# Delete without confirmation
nexus anthropic delete-skill skill_01AbCdEfGhIjKlMn --confirm
```

### browse-github

Browse skills from the official Anthropic skills repository:

```bash
nexus anthropic browse-github [options]
```

**Options:**
- `--category TEXT`: Filter by category

**Example:**
```bash
# Browse all skills
nexus anthropic browse-github

# Browse creative skills
nexus anthropic browse-github --category creative
```

**Output:**
```
┏━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Name               ┃ Path                       ┃
┡━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ project-manager    │ project-manager            │
│ code-reviewer      │ code-reviewer              │
│ creative-writer    │ creative-writer            │
└────────────────────┴────────────────────────────┘

Use nexus anthropic import-github <skill-name> to import a skill
```

### import-github

Import a skill from the Anthropic skills GitHub repository:

```bash
nexus anthropic import-github <skill-name> [options]
```

**Options:**
- `--tier TEXT`: Target tier (agent, tenant, system). Default: agent

**Example:**
```bash
# Import a skill
nexus anthropic import-github project-manager

# Import to tenant tier
nexus anthropic import-github code-reviewer --tier tenant
```

**What it does:**
1. Fetches SKILL.md from `https://github.com/anthropics/skills`
2. Creates the skill directory in Nexus
3. Writes the SKILL.md file
4. Displays skill info

## Python API

You can also use the plugin programmatically:

```python
from nexus import connect
from nexus.plugins import PluginRegistry

nx = connect()
registry = PluginRegistry(nx)
registry.discover()

plugin = registry.get_plugin("anthropic")

# Upload a skill
await plugin.upload_skill("my-analyzer")

# List skills
await plugin.list_skills()

# Import from GitHub
await plugin.import_github_skill("project-manager", tier="agent")
```

## Workflow Examples

### 1. Create and Upload a Skill

```bash
# Create a skill in Nexus
nexus skills create my-analyzer --description "Code quality analyzer"

# Edit the skill...
# (Add content to /workspace/.nexus/skills/my-analyzer/SKILL.md)

# Upload to Claude
nexus anthropic upload-skill my-analyzer
```

### 2. Import and Customize GitHub Skills

```bash
# Browse available skills
nexus anthropic browse-github

# Import a skill
nexus anthropic import-github project-manager

# Customize it
# (Edit /workspace/.nexus/skills/project-manager/SKILL.md)

# Upload customized version to Claude
nexus anthropic upload-skill project-manager --display-title "My Project Manager"
```

### 3. Share Skills with Team

```bash
# Upload personal skill to Claude
nexus anthropic upload-skill my-tool --display-title "Team Tool"

# Team members can list and download
nexus anthropic list-skills
nexus anthropic download-skill skill_01AbCd
```

## Implementation Details

### Skill Frontmatter Filtering

Claude Skills API only allows specific frontmatter fields:
- `name`
- `description`
- `license`
- `allowed-tools`
- `metadata`

The plugin automatically filters out other fields (like `version`, `author`, `created_at`) when uploading.

### Export Format

The plugin exports skills in Claude format:
```
skill-name.zip
├── skill-name/
│   ├── SKILL.md (filtered frontmatter)
│   └── manifest.json
└── [dependencies if needed]
```

### API Version

The plugin uses the Claude Skills API beta version:
- Skills API: `skills-2025-10-02`
- Code Execution: `code-execution-2025-08-25`

## Troubleshooting

### API Key Not Found

```bash
# Set environment variable
export ANTHROPIC_API_KEY="sk-ant-..."

# Or create config file
mkdir -p ~/.nexus/plugins/anthropic
echo "api_key: sk-ant-..." > ~/.nexus/plugins/anthropic/config.yaml
```

### Upload Fails with Size Error

Claude format has an 8MB limit. Try:

```bash
# Export without dependencies
nexus skills export my-skill --no-deps

# Or split large skills into smaller ones
```

### Skill Not Found

Ensure the skill exists in Nexus:

```bash
# List Nexus skills
nexus skills list

# Check skill location
nexus skills info my-skill
```

## See Also

- [Skills API](../skills/index.md) - Nexus skills system
- [Skill Manager](../skills/manager.md) - Creating and managing skills
- [Claude Skills Documentation](https://docs.anthropic.com/claude/docs/skills)

## Source Code

- GitHub: [nexus-plugin-anthropic](https://github.com/nexi-lab/nexus-plugin-anthropic)
- PyPI: [nexus-plugin-anthropic](https://pypi.org/project/nexus-plugin-anthropic/)
