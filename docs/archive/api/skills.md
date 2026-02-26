# Skills API

← [API Documentation](README.md)

Nexus provides a comprehensive skills system for managing AI agent capabilities with progressive disclosure, lazy loading, and three-tier hierarchy.

## Documentation

<div class="grid cards" markdown>

- :material-school: **[Overview](skills/index.md)**

    Skills system overview and quick start

- :material-database: **[Skill Registry](skills/registry.md)**

    Discover and load skills

- :material-cog: **[Skill Manager](skills/manager.md)**

    Create, fork, and publish skills

- :material-export: **[Export & Import](skills/exporter.md)**

    Package and distribute skills

- :material-book: **[Complete Reference](skills/reference.md)**

    Full API reference (single page)

</div>

## Quick Example

```python
from nexus import connect
from nexus.skills import SkillRegistry, SkillManager

nx = connect()

# Discover skills
registry = SkillRegistry(nx)
await registry.discover()

# Get a skill
skill = await registry.get_skill("analyze-code")
print(skill.content)

# Create new skill
manager = SkillManager(nx, registry)
await manager.create_skill(
    name="my-skill",
    description="Custom skill",
    template="basic"
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
---

# Code Analysis Skill

[Skill content in markdown...]
```

## See Also

- [Plugins API](plugins.md) - Plugin system
- [CLI Reference](cli-reference.md) - Skills CLI commands
