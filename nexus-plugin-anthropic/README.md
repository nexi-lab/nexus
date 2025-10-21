# Nexus Plugin: Anthropic

Anthropic Claude Skills API integration plugin for Nexus Skills System.

Integrates with the official Claude Skills API (launched October 2025) and the Anthropic skills GitHub repository.

## Features

### Claude Skills API Integration
- **Upload Skills** - Upload Nexus skills to Claude Skills API
- **Download Skills** - Download and import skills from Claude Skills API
- **List Skills** - View all skills uploaded to Claude Skills API
- **Delete Skills** - Remove skills from Claude Skills API

### GitHub Skills Repository
- **Browse GitHub Skills** - Browse official skills from anthropics/skills repository
- **Import GitHub Skills** - Import skills directly from GitHub into Nexus

## Installation

```bash
pip install nexus-plugin-anthropic
```

Or install from source:

```bash
cd nexus-plugin-anthropic
pip install -e .
```

## Configuration

### Option 1: Configuration File

Create `~/.nexus/plugins/anthropic/config.yaml`:

```yaml
api_key: sk-ant-api03-your-api-key-here
```

### Option 2: Environment Variable

```bash
export ANTHROPIC_API_KEY=sk-ant-api03-your-api-key-here
```

### Option 3: Command Line

Pass API key directly to commands:

```bash
nexus anthropic upload-skill my-skill --api-key sk-ant-api03-...
```

## Usage

### Plugin Management

```bash
# List installed plugins
nexus plugins list

# View plugin info
nexus plugins info anthropic
```

### Claude Skills API Commands

#### Upload a Skill

Upload a skill from your Nexus installation to Claude Skills API:

```bash
nexus anthropic upload-skill my-analyzer
```

With custom display title:

```bash
nexus anthropic upload-skill my-analyzer --display-title "My Code Analyzer"
```

#### List Skills

View all skills in Claude Skills API:

```bash
# List all skills
nexus anthropic list-skills

# List only custom skills
nexus anthropic list-skills --source custom

# List only Anthropic-provided skills
nexus anthropic list-skills --source anthropic
```

#### Download a Skill

Download a skill from Claude Skills API and import to Nexus:

```bash
nexus anthropic download-skill skill_01AbCdEfGhIjKlMnOpQrStUv --tier agent
```

Options:
- `--tier`: Target tier (agent, tenant, system). Default: agent
- `--version`: Skill version. Default: latest

#### Delete a Skill

Remove a skill from Claude Skills API:

```bash
nexus anthropic delete-skill skill_01AbCdEfGhIjKlMnOpQrStUv
```

Note: This will delete all versions of the skill.

### GitHub Skills Repository Commands

#### Browse Available Skills

Browse skills from the official Anthropic skills repository:

```bash
# List all skills
nexus anthropic browse-github

# Filter by category
nexus anthropic browse-github --category development
```

#### Import a GitHub Skill

Import a skill directly from GitHub into your Nexus:

```bash
# Import to agent tier
nexus anthropic import-github algorithmic-art-generation

# Import to tenant tier
nexus anthropic import-github canvas-design --tier tenant
```

Common skills available:
- `algorithmic-art-generation` - Create algorithmic art
- `canvas-design` - Design on HTML canvas
- `animated-gif-creation` - Create animated GIFs
- `mcp-server-creation` - Build MCP servers
- `advanced-word-skills` - Advanced Word document manipulation
- And many more!

## Commands

| Command | Description |
|---------|-------------|
| `upload-skill <name>` | Upload a skill to Claude Skills API |
| `download-skill <id>` | Download and import a skill from Claude Skills API |
| `list-skills` | List all skills in Claude Skills API |
| `delete-skill <id>` | Delete a skill from Claude Skills API |
| `browse-github` | Browse skills from anthropics/skills repository |
| `import-github <name>` | Import a skill from GitHub into Nexus |

## Examples

Comprehensive examples are available in the `examples/` directory:

- **CLI Examples** (`examples/cli_examples.sh`) - Command-line usage examples
- **Python SDK Examples** (`examples/python_sdk_examples.py`) - Programmatic usage examples
- **README** (`examples/README.md`) - Detailed documentation and quick start guide

```bash
# Run CLI examples
cd examples
./cli_examples.sh

# Run Python SDK examples
python python_sdk_examples.py
```

See the [examples README](examples/README.md) for detailed documentation.

## Development

### Setup

```bash
git clone https://github.com/nexi-lab/nexus-plugin-anthropic
cd nexus-plugin-anthropic
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
anthropic = "nexus_anthropic.plugin:AnthropicPlugin"
```

### Commands

Commands are exposed via the `NexusPlugin.commands()` method and are accessible as:

```bash
nexus <plugin-name> <command-name> [args]
```

For example:
```bash
nexus anthropic upload-skill my-skill
```

### Configuration

Plugin configuration is loaded from:
1. `~/.nexus/plugins/anthropic/config.yaml`
2. Environment variables (`ANTHROPIC_API_KEY`)
3. Command-line arguments

## Integration with Nexus Core

The plugin integrates with Nexus core features:

- **Skill Export** - Uses `nexus.skills.SkillExporter` to export skills
- **Skill Import** - Uses `NexusFS.write()` to import downloaded skills
- **Skill Registry** - Uses `nexus.skills.SkillRegistry` for skill discovery

## API Details

This plugin uses the official Claude Skills API endpoints:

- `POST /beta/skills` - Create/upload skills
- `GET /beta/skills` - List skills
- `GET /beta/skills/{skill_id}` - Retrieve skill details
- `DELETE /beta/skills/{skill_id}` - Delete skills
- `POST /beta/skills/{skill_id}/versions` - Create new version
- `GET /beta/skills/{skill_id}/versions` - List versions

All API calls include the required beta headers:
- `skills-2025-10-02`
- `code-execution-2025-08-25`

## GitHub Integration

The plugin can browse and import skills from the official [anthropics/skills](https://github.com/anthropics/skills) repository. This allows you to:

1. Discover pre-built skills created by Anthropic
2. Import them directly into your Nexus workspace
3. Fork and customize them for your needs

All GitHub skills are imported with their original metadata and can be used immediately.

## License

Apache 2.0 - See LICENSE file for details

## Contributing

Contributions welcome! Please see CONTRIBUTING.md for guidelines.

## Support

- **Issues**: [GitHub Issues](https://github.com/nexi-lab/nexus-plugin-anthropic/issues)
- **Documentation**: [Nexus Documentation](https://github.com/nexi-lab/nexus)
- **Main Project**: [Nexus](https://github.com/nexi-lab/nexus)
