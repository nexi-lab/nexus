# Nexus Plugin: Skill Seekers

Skill Seekers integration plugin for Nexus - automatically generate SKILL.md files from documentation.

## Features

- **Scrape Documentation** - Extract content from documentation URLs
- **Generate Skills** - Automatically generate SKILL.md from documentation
- **Import Skills** - Import generated skills into Nexus filesystem
- **Batch Processing** - Generate multiple skills from a list of URLs

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

1. **Scrape** - Fetch documentation from URL
2. **Extract** - Parse HTML/Markdown content
3. **Generate** - Use LLM to create SKILL.md metadata and description
4. **Import** - Write to NexusFS at appropriate tier

## License

Apache 2.0 - See LICENSE file for details

## Contributing

Contributions welcome! Please see CONTRIBUTING.md for guidelines.

## Support

- **Issues**: [GitHub Issues](https://github.com/nexi-lab/nexus-plugin-skill-seekers/issues)
- **Documentation**: [Nexus Documentation](https://github.com/nexi-lab/nexus)
- **Main Project**: [Nexus](https://github.com/nexi-lab/nexus)
