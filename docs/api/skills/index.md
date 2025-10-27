# Skills API Overview

← [API Documentation](../README.md)

Nexus provides a comprehensive skills system for managing AI agent capabilities with progressive disclosure, lazy loading, and hierarchical organization.

## What are Skills?

Skills are markdown-based knowledge units that define AI agent capabilities:

- **Progressive disclosure**: Load metadata first, full content on-demand
- **Lazy loading**: Load skills only when needed
- **Three-tier hierarchy**: agent > tenant > system (priority override)
- **Dependency resolution**: Automatic DAG resolution with cycle detection
- **Version control**: Leverage Nexus CAS for content deduplication
- **Export/Import**: Package skills for distribution

## Quick Start

### Discover Skills

```python
from nexus import connect
from nexus.skills import SkillRegistry

nx = connect()
registry = SkillRegistry(nx)

# Discover skills (loads metadata only)
count = await registry.discover()
print(f"Discovered {count} skills")
```

### Load a Skill

```python
# Get a skill (loads full content)
skill = await registry.get_skill("analyze-code")
print(skill.metadata.description)
print(skill.content[:200])  # Preview content
```

### Create a Skill

```python
from nexus.skills import SkillManager

manager = SkillManager(nx, registry)

# Create from template
path = await manager.create_skill(
    name="my-skill",
    description="Custom skill",
    template="basic",
    tier="agent"
)
```

## Skill Structure

Skills are stored as `SKILL.md` files with YAML frontmatter:

```markdown
---
name: analyze-code
description: Analyzes code quality
version: 1.0.0
author: Alice
requires:
  - base-parser
  - ast-tools
---

# Code Analysis Skill

[Skill content in markdown format...]
```

## Tier Hierarchy

Skills are organized in three tiers:

```
/workspace/.nexus/skills/   # Agent tier (highest priority)
/shared/skills/             # Tenant tier (team-shared)
/system/skills/             # System tier (global/built-in)
```

Priority: **Agent > Tenant > System**

## Documentation

<div class="grid cards" markdown>

- :material-database: **[Skill Registry](registry.md)**

    Discover, load, and manage skills

- :material-cog: **[Skill Manager](manager.md)**

    Create, fork, and publish skills

- :material-export: **[Export & Import](exporter.md)**

    Package and distribute skills

- :material-book: **[Complete Reference](reference.md)**

    Full API documentation

</div>

## Core Components

### SkillRegistry

Discover and load skills with progressive disclosure:

```python
registry = SkillRegistry(nx)
await registry.discover()

# Get metadata (lightweight)
metadata = registry.get_metadata("analyze-code")

# Get full skill (on-demand)
skill = await registry.get_skill("analyze-code")
```

[Learn more →](registry.md)

### SkillManager

Manage skill lifecycle:

```python
manager = SkillManager(nx, registry)

# Create
await manager.create_skill("my-skill", "Description")

# Fork
await manager.fork_skill("analyze-code", "my-analyzer")

# Publish
await manager.publish_skill("my-skill", target_tier="tenant")
```

[Learn more →](manager.md)

### SkillExporter

Export skills for distribution:

```python
from nexus.skills import SkillExporter

exporter = SkillExporter(registry)
await exporter.export_skill(
    "my-skill",
    "/tmp/my-skill.zip",
    format="claude"
)
```

[Learn more →](exporter.md)

## CLI Integration

```bash
# List skills
nexus skills list

# Show skill info
nexus skills info analyze-code

# Create skill
nexus skills create my-skill --template basic

# Fork skill
nexus skills fork analyze-code my-analyzer

# Export skill
nexus skills export my-skill --output /tmp/skill.zip
```

## Next Steps

1. **[Skill Registry](registry.md)** - Discover and load skills
2. **[Skill Manager](manager.md)** - Create and manage skills
3. **[Export & Import](exporter.md)** - Distribute skills

## See Also

- [Plugins API](../plugins/index.md) - Plugin system
- [CLI Reference](../cli-reference.md) - Skills CLI commands
- [Semantic Search](../semantic-search.md) - Search skills
