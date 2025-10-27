# Skill Seekers Plugin

← [Plugins API](index.md)

Generate Nexus skills from documentation URLs using AI.

## Overview

The Skill Seekers plugin automates skill creation from online documentation:

- Scrape documentation from any URL
- Generate SKILL.md files automatically
- Import existing SKILL.md files
- Batch process multiple URLs
- Auto-extract keywords and metadata

## Installation

```bash
pip install nexus-plugin-skill-seekers
```

## Commands

### generate

Generate a skill from a documentation URL:

```bash
nexus skill-seekers generate <url> [options]
```

**Options:**
- `--name TEXT`: Skill name (auto-generated if not provided)
- `--tier TEXT`: Target tier (agent, tenant, system). Default: agent
- `--output-dir TEXT`: Output directory for SKILL.md file

**Examples:**
```bash
# Generate from URL
nexus skill-seekers generate https://docs.stripe.com/api

# Specify custom name
nexus skill-seekers generate https://docs.example.com/api --name example-api

# Save to file only (don't import to Nexus)
nexus skill-seekers generate https://docs.example.com --output-dir ./skills

# Generate to tenant tier
nexus skill-seekers generate https://docs.example.com --tier tenant
```

**What it does:**
1. Fetches documentation from URL
2. Extracts main content
3. Generates skill name (if not provided)
4. Creates SKILL.md with frontmatter
5. Imports to Nexus (if available)
6. Optionally saves to file

**Generated SKILL.md structure:**
```markdown
---
name: stripe-api
version: 1.0.0
description: Skill generated from https://docs.stripe.com/api
author: Skill Seekers
created: 2025-01-26T12:00:00Z
source_url: https://docs.stripe.com/api
tier: agent
---

# Stripe Api

## Overview

This skill was automatically generated from documentation at https://docs.stripe.com/api.

## Description

[Extracted content...]

## Keywords

api, data, request, response, object...
```

### import

Import an existing SKILL.md file into Nexus:

```bash
nexus skill-seekers import <file-path> [options]
```

**Options:**
- `--tier TEXT`: Target tier (agent, tenant, system). Default: agent
- `--name TEXT`: Override skill name (uses filename if not provided)

**Examples:**
```bash
# Import skill file
nexus skill-seekers import ./my-skill.md

# Import to specific tier
nexus skill-seekers import ./team-skill.md --tier tenant

# Import with custom name
nexus skill-seekers import ./skill.md --name custom-name
```

### batch

Generate multiple skills from a URLs file:

```bash
nexus skill-seekers batch <urls-file> [options]
```

**Options:**
- `--tier TEXT`: Target tier for all skills. Default: agent

**URLs file format:**
```
# Lines starting with # are comments
https://docs.stripe.com/api stripe-api
https://docs.github.com/api github-api
https://docs.example.com/guide example-guide
```

Format: `<url> [optional-name]`

**Example:**
```bash
# Create urls.txt
cat > urls.txt << 'EOL'
https://docs.stripe.com/api stripe-api
https://docs.github.com/rest github-rest-api
https://docs.anthropic.com/claude claude-api
EOL

# Batch generate
nexus skill-seekers batch urls.txt

# Generate to tenant tier
nexus skill-seekers batch urls.txt --tier tenant
```

**Output:**
```
Processing 3 URLs...
Generating skills... ━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100% 0:00:10
✓ Generated 3 skills
```

### list

List all generated skills:

```bash
nexus skill-seekers list
```

**Output:**
```
┏━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Tier   ┃ Name         ┃ Path                                ┃
┡━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ Agent  │ stripe-api   │ /workspace/.nexus/skills/stripe-api │
│ Agent  │ github-api   │ /workspace/.nexus/skills/github-api │
│ Tenant │ team-skill   │ /shared/skills/team-skill           │
└────────┴──────────────┴─────────────────────────────────────┘
```

## Use Cases

### 1. API Documentation Skills

Generate skills from API documentation:

```bash
# Stripe API
nexus skill-seekers generate https://docs.stripe.com/api --name stripe-api

# GitHub API
nexus skill-seekers generate https://docs.github.com/rest --name github-api

# Use the skill
nexus skills info stripe-api
```

### 2. Framework Documentation

Create skills from framework docs:

```bash
# Next.js
nexus skill-seekers generate https://nextjs.org/docs

# FastAPI
nexus skill-seekers generate https://fastapi.tiangolo.com
```

### 3. Library References

Build skill library from multiple sources:

```bash
# Create batch file
cat > libraries.txt << 'EOL'
https://pandas.pydata.org/docs pandas
https://numpy.org/doc numpy
https://docs.python.org/3/library python-stdlib
EOL

# Batch generate
nexus skill-seekers batch libraries.txt
```

### 4. Knowledge Base Migration

Import existing documentation:

```bash
# Generate from URLs
nexus skill-seekers generate https://internal-docs.company.com/api

# Or import existing markdown
nexus skill-seekers import ./existing-docs/*.md
```

## Python API

Use the plugin programmatically:

```python
from nexus import connect
from nexus.plugins import PluginRegistry

nx = connect()
registry = PluginRegistry(nx)
registry.discover()

plugin = registry.get_plugin("skill-seekers")

# Generate from URL
await plugin.generate_skill(
    url="https://docs.example.com/api",
    name="example-api",
    tier="agent"
)

# Import skill file
await plugin.import_skill(
    file_path="./my-skill.md",
    tier="tenant"
)

# List skills
await plugin.list_skills()
```

## Workflow with Other Plugins

### With Firecrawl Plugin

Use Firecrawl for better scraping:

```bash
# Scrape with Firecrawl (handles JS, anti-bot)
nexus firecrawl scrape https://docs.example.com/api --output api.md

# Import as skill
nexus skill-seekers import api.md --name example-api
```

### With Anthropic Plugin

Generate and upload to Claude:

```bash
# Generate skill
nexus skill-seekers generate https://docs.stripe.com/api --name stripe-api

# Upload to Claude
nexus anthropic upload-skill stripe-api
```

## Implementation Details

### Content Extraction

The plugin uses BeautifulSoup for HTML parsing:
1. Fetches URL content
2. Removes scripts and styles
3. Extracts text content
4. Cleans whitespace
5. Generates structured SKILL.md

### Name Generation

Auto-generates names from URLs:
```
https://docs.stripe.com/api → stripe-api
https://github.com/docs/rest → rest
https://example.com/guide/auth → auth
```

Rules:
- Uses last path component or domain
- Replaces special chars with `-`
- Removes consecutive dashes

### Keyword Extraction

Simple keyword extraction algorithm:
1. Finds common technical terms
2. Filters for programming/API keywords
3. Returns top 10 unique keywords

## Limitations

### Current Implementation

- Basic content extraction (no LLM)
- Simple keyword extraction
- Single-page scraping only
- No JavaScript rendering

### Future Enhancements

Planned improvements:
- LLM-powered content summarization
- Multi-page documentation handling
- Better keyword extraction with NLP
- JavaScript rendering support
- Schema extraction for APIs

## Comparison with Firecrawl

| Feature                  | Skill Seekers      | Firecrawl         |
|-------------------------|-------------------|-------------------|
| JavaScript rendering    | ❌ No             | ✅ Yes            |
| Anti-bot detection      | ❌ No             | ✅ Yes            |
| Multi-page crawl        | ❌ No             | ✅ Yes            |
| Skill generation        | ✅ Yes            | ❌ No             |
| Free                    | ✅ Yes            | ⚠️ Limited        |
| Dependencies            | Minimal           | Requires API key  |

**Recommendation**: Use Firecrawl for scraping, Skill Seekers for skill generation.

## Troubleshooting

### Failed to Fetch Documentation

Check URL accessibility:

```bash
curl -I https://docs.example.com
```

Some sites may block scraping.

### Empty Content

The site might be JavaScript-heavy. Use Firecrawl instead:

```bash
nexus firecrawl scrape https://example.com --output content.md
nexus skill-seekers import content.md
```

### Skill Already Exists

Remove or rename existing skill:

```bash
# Remove from Nexus
nexus skills delete old-skill

# Or use different name
nexus skill-seekers generate https://url --name new-name
```

## See Also

- [Firecrawl Plugin](firecrawl.md) - Production-grade web scraping
- [Anthropic Plugin](anthropic.md) - Upload to Claude Skills API
- [Skills API](../skills/index.md) - Nexus skills system
- [Skill Manager](../skills/manager.md) - Managing skills

## Source Code

- GitHub: [nexus-plugin-skill-seekers](https://github.com/nexi-lab/nexus-plugin-skill-seekers)
- PyPI: [nexus-plugin-skill-seekers](https://pypi.org/project/nexus-plugin-skill-seekers/)
