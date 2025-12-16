# Nexus Plugin: Skill Seekers

Skill Seekers integration plugin for Nexus - automatically generate AI-enhanced SKILL.md files from documentation with Firecrawl and ReBAC integration.

## Features

### Core Capabilities
- **llms.txt Detection** - 10x faster scraping using llms.txt standard
- **Firecrawl Integration** - Multi-page documentation crawling with JavaScript rendering
- **AI Enhancement** - Claude-powered skill generation for high-quality content
- **Batch Processing** - Generate multiple skills from a list of URLs

### Security & Governance
- **ReBAC Integration** - Automatic permission tuple creation
- **Tier-Based Access** - Agent, tenant, and system tier support
- **Approval Workflow** - Auto-submit tenant skills for approval
- **Permission Checks** - Enforce tenant membership and admin requirements

## Installation

```bash
pip install nexus-plugin-skill-seekers
```

Or install from source:

```bash
cd nexus-plugin-skill-seekers
pip install -e .
```

## Configuration

### Option 1: Configuration File

Create `~/.nexus/plugins/skill-seekers/config.yaml`:

```yaml
# Default tier for imported skills
default_tier: agent

# Default output directory
output_dir: /tmp/nexus-skills

# OpenAI API key for skill generation (optional)
openai_api_key: sk-...
```

### Option 2: Environment Variable

```bash
export OPENAI_API_KEY=sk-...
```

## Usage

### List Installed Plugins

```bash
nexus plugins list
```

### View Plugin Info

```bash
nexus plugins info skill-seekers
```

### Scrape Documentation and Generate Skill

Generate a skill from a documentation URL:

```bash
nexus skill-seekers generate https://docs.example.com/api --name my-api-skill
```

With custom tier:

```bash
nexus skill-seekers generate https://docs.example.com/api --name my-api-skill --tier tenant
```

### Import Existing SKILL.md

Import a skill file into Nexus:

```bash
nexus skill-seekers import /path/to/SKILL.md --tier agent
```

### Batch Generate Skills

Generate multiple skills from a URLs file:

```bash
nexus skill-seekers batch urls.txt
```

Format of `urls.txt`:
```
https://docs.example.com/api api-docs
https://docs.example.com/guide user-guide
```

### List Generated Skills

```bash
nexus skill-seekers list
```

## Commands

| Command | Description |
|---------|-------------|
| `generate <url>` | Generate a skill from documentation URL |
| `import <file>` | Import a SKILL.md file into Nexus |
| `batch <file>` | Generate skills from a list of URLs |
| `list` | List all generated skills |

## Examples

### CLI Example

Run the comprehensive CLI demo:

```bash
cd nexus-plugin-skill-seekers
./examples/skill_seekers_cli_demo.sh
```

This demo demonstrates:
- Plugin installation and verification
- Generating skills from documentation URLs (React, FastAPI, Python)
- Importing existing SKILL.md files
- Batch processing from URLs file
- Integration with Nexus skills system

### Python SDK Example

Run the Python SDK demo:

```bash
cd nexus-plugin-skill-seekers
PYTHONPATH=../src python examples/skill_seekers_sdk_demo.py
```

This demo shows:
- Programmatic plugin usage
- Direct API access to scraping methods
- Custom skill generation and import
- Batch processing
- Error handling and validation
- Integration with SkillRegistry

See the [examples/](examples/) directory for full source code.

## Development

### Setup

```bash
git clone https://github.com/nexi-lab/nexus-plugin-skill-seekers
cd nexus-plugin-skill-seekers
pip install -e ".[dev]"
```

### Run Tests

```bash
pytest
```

### Type Checking

```bash
mypy src
```

### Linting

```bash
ruff check src
ruff format src
```

## Architecture

### Plugin Registration

The plugin is registered via entry points in `pyproject.toml`:

```toml
[project.entry-points."nexus.plugins"]
skill-seekers = "nexus_skill_seekers.plugin:SkillSeekersPlugin"
```

### Skill Generation Flow

1. **llms.txt Check** - Try fetching llms.txt (10x faster)
2. **Firecrawl Scraping** - Multi-page crawl with JS rendering (if llms.txt not found)
3. **Fallback** - Basic BeautifulSoup scraping (deprecated)
4. **AI Enhancement** - Claude API generates professional SKILL.md
5. **Permission Check** - Verify tier-specific permissions (ReBAC)
6. **Write to Nexus** - Create skill file at appropriate tier
7. **ReBAC Tuples** - Create ownership and access tuples
8. **Approval** - Auto-submit tenant skills for approval workflow

## How It Works

### Tiered Scraping Strategy

```
Priority 1: llms.txt (⚡ 10x faster)
  ↓ Not found
Priority 2: Firecrawl (Multi-page + JS rendering)
  ↓ Failed
Priority 3: Basic scraping (Single page, deprecated)
```

### ReBAC Integration

```
Agent Tier:
  → Creates ownership tuple: (agent, creator_id) owner-of (skill, name)
  → Private to creator

Tenant Tier:
  → Creates ownership tuple
  → Creates tenant association: (tenant, tenant_id) tenant (skill, name)
  → Auto-submits for approval
  → Accessible to all tenant members after approval

System Tier:
  → Creates ownership tuple
  → Creates public access: (*, *) public (skill, name)
  → Globally readable by all users
```

## License

Apache 2.0 - See LICENSE file for details

## Contributing

Contributions welcome! Please see CONTRIBUTING.md for guidelines.

## Support

- **Issues**: [GitHub Issues](https://github.com/nexi-lab/nexus-plugin-skill-seekers/issues)
- **Documentation**: [Nexus Documentation](https://github.com/nexi-lab/nexus)
- **Main Project**: [Nexus](https://github.com/nexi-lab/nexus)
