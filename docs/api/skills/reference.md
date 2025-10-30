# Skills API

← [API Documentation](README.md)

Nexus provides a comprehensive skills system for managing AI agent capabilities. Skills are markdown-based knowledge units that can be discovered, versioned, shared, and exported across different tiers (agent, tenant, system).

## Overview

The Skills API provides:
- **Progressive disclosure**: Load metadata first, full content on-demand
- **Lazy loading**: Load skills only when needed
- **Three-tier hierarchy**: agent > tenant > system (with priority override)
- **Dependency resolution**: Automatic DAG resolution with cycle detection
- **Version control**: Leverages Nexus CAS for content deduplication
- **Export/Import**: Package skills for distribution
- **Search**: Text-based and semantic search capabilities

## Core Concepts

### Skill Structure

Skills are stored as `SKILL.md` files with YAML frontmatter:

```markdown
---
name: analyze-code
description: Analyzes code quality and suggests improvements
version: 1.0.0
author: Alice
created_at: 2025-01-15T10:00:00Z
modified_at: 2025-01-20T15:30:00Z
requires:
  - base-parser
  - ast-tools
---

# Code Analysis Skill

This skill analyzes code for quality issues...

## Usage

[Skill content in markdown format]
```

### Tier Hierarchy

Skills are organized in three tiers with priority ordering:

```
/workspace/.nexus/skills/   # Agent tier (highest priority)
/shared/skills/             # Tenant tier (shared across team)
/system/skills/             # System tier (global/built-in)
```

When a skill exists in multiple tiers, the higher priority tier wins:
- Agent skills override tenant skills
- Tenant skills override system skills

## SkillRegistry

The `SkillRegistry` is the primary interface for discovering and loading skills.

### Basic Usage

```python
from nexus import connect
from nexus.skills import SkillRegistry

nx = connect()
registry = SkillRegistry(nx)

# Discover skills (loads metadata only)
count = await registry.discover()
print(f"Discovered {count} skills")

# Get a skill (loads full content on-demand)
skill = await registry.get_skill("analyze-code")
print(skill.content)  # Full markdown content

# List available skills
skill_names = registry.list_skills()
print(f"Available skills: {skill_names}")

# Get metadata without loading full content
metadata = registry.get_metadata("analyze-code")
print(f"{metadata.name}: {metadata.description}")
```

### Discovery

Discovery loads only metadata for fast startup:

```python
# Discover from all tiers
count = await registry.discover()

# Discover from specific tiers
count = await registry.discover(tiers=["agent", "tenant"])

# Discover from agent tier only
count = await registry.discover(tiers=["agent"])
```

### Loading Skills

Skills are loaded lazily - full content is only loaded when requested:

```python
# Load a skill (loads content + caches it)
skill = await registry.get_skill("analyze-code")

# Access metadata
print(skill.metadata.name)
print(skill.metadata.description)
print(skill.metadata.version)
print(skill.metadata.author)

# Access full content
print(skill.content)  # Markdown content

# Load with dependencies
skill = await registry.get_skill("analyze-code", load_dependencies=True)
```

### Listing Skills

```python
# List all skills (returns names)
skill_names = registry.list_skills()

# List skills from specific tier
agent_skills = registry.list_skills(tier="agent")

# List with metadata (returns SkillMetadata objects)
metadata_list = registry.list_skills(include_metadata=True)
for metadata in metadata_list:
    print(f"{metadata.name} v{metadata.version}")
    print(f"  {metadata.description}")
    print(f"  Tier: {metadata.tier}")
```

### Dependency Resolution

Resolve dependencies in correct order (dependencies first):

```python
# Resolve all dependencies for a skill
deps = await registry.resolve_dependencies("analyze-code")
# Returns: ['base-parser', 'ast-tools', 'analyze-code']

# Load skills in dependency order
for skill_name in deps:
    skill = await registry.get_skill(skill_name)
    print(f"Loading: {skill_name}")
```

Dependency resolution detects circular dependencies:

```python
try:
    deps = await registry.resolve_dependencies("skill-with-cycle")
except SkillDependencyError as e:
    print(f"Circular dependency: {e}")
    # Error: Circular dependency detected: skill-a -> skill-b -> skill-c -> skill-a
```

### Caching

The registry caches loaded skills for performance:

```python
# Clear skill cache (keeps metadata)
registry.clear_cache()

# Clear everything (metadata + cache)
registry.clear()

# Check registry state
print(registry)
# Output: SkillRegistry(skills=10, cached=3, tiers=['agent', 'tenant'])
```

## SkillManager

The `SkillManager` handles skill lifecycle operations: create, fork, publish.

### Creating Skills

Create new skills from templates:

```python
from nexus.skills import SkillManager

manager = SkillManager(nx)

# Create skill from template
path = await manager.create_skill(
    name="my-analyzer",
    description="Analyzes code quality",
    template="code-generation",
    tier="agent",
    author="Alice",
    version="1.0.0"
)
print(f"Created skill at: {path}")

# Available templates:
# - basic: Simple skill template
# - data-analysis: Data analysis skill
# - code-generation: Code generation skill
# - etc.
```

Create skills from custom content:

```python
# Create from scraped/custom content
path = await manager.create_skill_from_content(
    name="stripe-api",
    description="Stripe API Documentation",
    content="# Stripe API\n\nComplete API reference...",
    tier="agent",
    author="Auto-generated",
    source_url="https://docs.stripe.com/api",
    metadata={
        "category": "api-docs",
        "provider": "stripe"
    }
)
```

### Forking Skills

Fork existing skills with lineage tracking:

```python
# Fork a skill
path = await manager.fork_skill(
    source_name="analyze-code",
    target_name="my-code-analyzer",
    tier="agent",
    author="Bob"
)
print(f"Forked skill at: {path}")

# The forked skill will include:
# - New name
# - Updated metadata (forked_from, parent_skill)
# - Incremented version
# - New timestamps
```

### Publishing Skills

Publish skills from one tier to another:

```python
# Publish agent skill to tenant library
path = await manager.publish_skill(
    name="my-analyzer",
    source_tier="agent",
    target_tier="tenant"
)
print(f"Published to: {path}")

# Publish tenant skill to system library
path = await manager.publish_skill(
    name="shared-analyzer",
    source_tier="tenant",
    target_tier="system"
)
```

### Searching Skills

Simple text-based search across skill descriptions:

```python
# Search for skills
results = await manager.search_skills("code analysis")
for skill_name, score in results:
    print(f"{skill_name}: {score:.2f}")

# Search in specific tier
results = await manager.search_skills(
    query="data processing",
    tier="tenant",
    limit=5
)

# For semantic search, use Nexus semantic_search API
# (requires semantic search to be initialized)
```

## SkillExporter

Export skills to various formats for distribution.

### Exporting Skills

```python
from nexus.skills import SkillExporter

exporter = SkillExporter(registry)

# Export skill to zip file
await exporter.export_skill(
    skill_name="analyze-code",
    output_path="/tmp/analyze-code.zip",
    format="generic",           # or "claude", "openai"
    include_dependencies=True   # Include required skills
)

# Export for Claude API (8MB limit enforced)
await exporter.export_skill(
    skill_name="my-skill",
    output_path="/tmp/my-skill.zip",
    format="claude"
)

# Export to directory (not zip)
await exporter.export_skill(
    skill_name="my-skill",
    output_path="/tmp/my-skill/",
    format="generic"
)
```

### Export Formats

**Generic Format**:
```
skill-name.zip
├── SKILL.md          # Main skill file
├── manifest.json     # Export metadata
└── deps/             # Dependencies (if included)
    ├── dep1/
    │   └── SKILL.md
    └── dep2/
        └── SKILL.md
```

**Claude Format**:
```
skill-name.zip
├── skill-name/
│   ├── SKILL.md       # Filtered frontmatter (only Claude-allowed fields)
│   └── manifest.json
└── [dependencies if needed]
```

### Import Skills

Import skills from exported packages:

```python
# Import from zip file
await importer.import_skill(
    source_path="/tmp/analyze-code.zip",
    tier="agent"
)

# Import from directory
await importer.import_skill(
    source_path="/tmp/my-skill/",
    tier="tenant"
)
```

## Data Models

### SkillMetadata

Lightweight metadata loaded during discovery:

```python
@dataclass
class SkillMetadata:
    name: str                        # Skill name (required)
    description: str                 # Description (required)
    version: str | None             # Semantic version
    author: str | None              # Author name
    created_at: datetime | None     # Creation timestamp
    modified_at: datetime | None    # Last modified timestamp
    requires: list[str]             # Skill dependencies
    metadata: dict[str, Any]        # Additional metadata
    file_path: str | None           # Path to SKILL.md
    tier: str | None                # agent, tenant, or system
```

### Skill

Complete skill with metadata and content:

```python
@dataclass
class Skill:
    metadata: SkillMetadata    # Lightweight metadata
    content: str               # Full markdown content

    def validate(self) -> None:
        """Validate skill structure."""
        self.metadata.validate()
        if not self.content:
            raise ValidationError("skill content is required")
```

## Complete Example

Here's a complete example using the Skills API:

```python
from nexus import connect
from nexus.skills import SkillRegistry, SkillManager, SkillExporter

# Connect to Nexus
nx = connect()

# === Discovery ===
registry = SkillRegistry(nx)
count = await registry.discover()
print(f"Discovered {count} skills")

# === List Skills ===
skill_names = registry.list_skills()
print(f"Available skills: {', '.join(skill_names)}")

# === Get Skill Details ===
metadata = registry.get_metadata("analyze-code")
print(f"\nSkill: {metadata.name}")
print(f"Description: {metadata.description}")
print(f"Version: {metadata.version}")
print(f"Dependencies: {metadata.requires}")

# === Load Skill Content ===
skill = await registry.get_skill("analyze-code")
print(f"\nContent preview: {skill.content[:200]}...")

# === Resolve Dependencies ===
deps = await registry.resolve_dependencies("analyze-code")
print(f"\nDependency order: {' -> '.join(deps)}")

# === Create New Skill ===
manager = SkillManager(nx, registry)
new_skill_path = await manager.create_skill(
    name="my-analyzer",
    description="Custom code analyzer",
    template="code-generation",
    tier="agent",
    author="Alice"
)
print(f"\nCreated new skill: {new_skill_path}")

# === Fork Existing Skill ===
forked_path = await manager.fork_skill(
    source_name="analyze-code",
    target_name="alice-analyzer",
    tier="agent",
    author="Alice"
)
print(f"Forked skill: {forked_path}")

# === Search Skills ===
results = await manager.search_skills("code analysis", limit=5)
print("\nSearch results:")
for skill_name, score in results:
    print(f"  {skill_name}: {score:.2f}")

# === Export Skill ===
exporter = SkillExporter(registry)
export_path = "/tmp/my-analyzer.zip"
await exporter.export_skill(
    skill_name="my-analyzer",
    output_path=export_path,
    format="generic",
    include_dependencies=True
)
print(f"\nExported to: {export_path}")

# === Publish to Team ===
published_path = await manager.publish_skill(
    name="my-analyzer",
    source_tier="agent",
    target_tier="tenant"
)
print(f"Published to team: {published_path}")
```

## CLI Integration

The Skills API is also available via CLI:

```bash
# Create a skill
nexus skills create my-skill --description "My skill" --template basic

# List skills
nexus skills list
nexus skills list --tier agent

# Show skill info
nexus skills info analyze-code

# Fork a skill
nexus skills fork analyze-code my-analyzer

# Publish a skill
nexus skills publish my-skill --target-tier tenant

# Export a skill
nexus skills export my-skill --output /tmp/my-skill.zip

# Search skills
nexus skills search "code analysis"
```

## Integration with Plugins

Skills work seamlessly with plugins. For example, the Anthropic plugin:

```python
# Upload skill to Claude Skills API
nexus anthropic upload-skill my-skill

# Download skill from Claude Skills API
nexus anthropic download-skill skill_01AbCdEfGhIjKlMnOpQrStUv

# List Claude skills
nexus anthropic list-skills

# Import from GitHub (anthropics/skills repo)
nexus anthropic browse-github
nexus anthropic import-github project-manager
```

## Advanced Usage

### Custom Filesystem

Use skills with any filesystem implementation:

```python
from nexus.skills import SkillRegistry

# Use with remote Nexus server
nx = connect(remote_url="http://localhost:8080")
registry = SkillRegistry(nx)

# Use with GCS backend
nx = connect(backend="gcs", gcs_bucket="my-bucket")
registry = SkillRegistry(nx)

# Use without filesystem (local files only)
registry = SkillRegistry(filesystem=None)
```

### Manual Parsing

Parse skills manually using the parser:

```python
from nexus.skills import SkillParser

parser = SkillParser()

# Parse from file
skill = parser.parse_file("/path/to/SKILL.md", tier="agent")

# Parse from content string
content = """---
name: my-skill
description: My skill
---
# Content here
"""
skill = parser.parse_content(content, file_path="/virtual/path", tier="agent")

# Parse metadata only
metadata = parser.parse_metadata_only("/path/to/SKILL.md", tier="agent")
```

### Validation

Validate skills and metadata:

```python
from nexus.skills import Skill, SkillMetadata
from nexus.core.exceptions import ValidationError

metadata = SkillMetadata(
    name="test-skill",
    description="Test skill",
    tier="agent"
)

try:
    metadata.validate()
except ValidationError as e:
    print(f"Validation error: {e}")

skill = Skill(metadata=metadata, content="# Skill content")
skill.validate()
```

### Governance and Analytics

Track skill usage and governance:

```python
from nexus.skills import SkillGovernance, SkillAnalytics

# Track skill usage
governance = SkillGovernance(registry)
await governance.track_usage(
    skill_name="analyze-code",
    user="alice",
    action="load"
)

# Get usage analytics
analytics = SkillAnalytics(registry)
stats = await analytics.get_skill_stats("analyze-code")
print(f"Usage count: {stats['usage_count']}")
print(f"Last used: {stats['last_used']}")

# Get popular skills
popular = await analytics.get_popular_skills(limit=10)
for skill_name, usage_count in popular:
    print(f"{skill_name}: {usage_count} uses")
```

### Approval Workflow

Skills can be submitted for approval before publishing to tenant tier:

```python
from nexus.skills import SkillGovernance

# Initialize with database connection
governance = SkillGovernance(db_connection=db_conn, rebac_manager=rebac)

# Submit skill for approval
approval_id = await governance.submit_for_approval(
    skill_name="my-analyzer",
    submitted_by="alice",
    reviewers=["bob", "charlie"],
    comments="Ready for team-wide use"
)
print(f"Submitted for approval: {approval_id}")

# List pending approvals (as reviewer)
pending = await governance.list_approvals(status="pending")
for approval in pending:
    print(f"{approval.skill_name} by {approval.submitted_by}")

# Approve skill
await governance.approve_skill(
    approval_id=approval_id,
    reviewed_by="bob",
    reviewer_type="user",
    comments="Code quality looks excellent!"
)

# Or reject skill
await governance.reject_skill(
    approval_id=approval_id,
    reviewed_by="bob",
    reviewer_type="user",
    comments="Needs more input validation"
)

# Check if skill is approved
is_approved = await governance.is_approved("my-analyzer")
if is_approved:
    # Publish to tenant tier
    await manager.publish_skill("my-analyzer", tier="tenant")
```

#### CLI Commands

The approval workflow is also available via CLI:

```bash
# Submit skill for approval
nexus skills submit-approval my-analyzer \
    --submitted-by alice \
    --reviewers bob,charlie \
    --comments "Ready for team use"

# List pending approvals
nexus skills list-approvals --status pending

# Approve skill
nexus skills approve <approval-id> \
    --reviewed-by bob \
    --comments "Excellent work!"

# Reject skill
nexus skills reject <approval-id> \
    --reviewed-by bob \
    --comments "Needs improvements"
```

#### Database Setup

The approval workflow requires a database connection:

```bash
# Set database URL
export NEXUS_DATABASE_URL="postgresql://user:pass@localhost/nexus"

# Run migrations to create skill_approvals table
alembic upgrade head
```

#### ReBAC Integration

When ReBAC is enabled, the approval workflow checks permissions:

- Submitters need `write` permission on agent tier skills
- Reviewers need `approve` permission on skills
- Only approved skills can be published to tenant tier

## Error Handling

Handle common errors gracefully:

```python
from nexus.skills import (
    SkillRegistry,
    SkillNotFoundError,
    SkillDependencyError,
    SkillManagerError,
    SkillParseError
)

registry = SkillRegistry(nx)
await registry.discover()

try:
    # Try to get a skill
    skill = await registry.get_skill("non-existent")
except SkillNotFoundError as e:
    print(f"Skill not found: {e}")

try:
    # Try to resolve dependencies
    deps = await registry.resolve_dependencies("skill-with-cycle")
except SkillDependencyError as e:
    print(f"Dependency error: {e}")

try:
    # Try to create a skill
    manager = SkillManager(nx)
    await manager.create_skill("invalid name!", "Description")
except SkillManagerError as e:
    print(f"Manager error: {e}")

try:
    # Try to parse invalid skill
    from nexus.skills import SkillParser
    parser = SkillParser()
    skill = parser.parse_file("invalid.md", "agent")
except SkillParseError as e:
    print(f"Parse error: {e}")
```

## Best Practices

### 1. Progressive Discovery

Always discover before accessing skills:

```python
registry = SkillRegistry(nx)
await registry.discover()  # Load metadata

# Now access skills
skill = await registry.get_skill("my-skill")  # Load content on-demand
```

### 2. Dependency Management

Declare dependencies in skill frontmatter:

```yaml
---
name: advanced-analyzer
description: Advanced code analysis
requires:
  - base-parser    # Base dependency
  - ast-tools      # Another dependency
---
```

Load skills with dependencies:

```python
# Resolve and load in correct order
deps = await registry.resolve_dependencies("advanced-analyzer")
for skill_name in deps:
    skill = await registry.get_skill(skill_name)
    # Use skill...
```

### 3. Tier Organization

Organize skills by visibility:

- **Agent tier** (`/workspace/.nexus/skills/`): Personal skills, experiments
- **Tenant tier** (`/shared/skills/`): Team-shared skills, approved skills
- **System tier** (`/system/skills/`): Global skills, built-in capabilities

### 4. Version Management

Use semantic versioning:

```yaml
---
name: my-skill
version: 1.2.0
---
```

Leverage Nexus version control:

```python
# Skills are versioned automatically via Nexus CAS
versions = nx.list_versions("/workspace/.nexus/skills/my-skill/SKILL.md")
old_content = nx.get_version("/workspace/.nexus/skills/my-skill/SKILL.md", version=1)
```

### 5. Export and Share

Export skills with dependencies for distribution:

```python
exporter = SkillExporter(registry)
await exporter.export_skill(
    skill_name="my-skill",
    output_path="/tmp/my-skill.zip",
    format="generic",
    include_dependencies=True  # Include all required skills
)
```

## Troubleshooting

### Skill Not Found

```python
# Ensure discovery has been run
await registry.discover()

# Check which tiers were discovered
print(registry)  # Shows tiers

# List skills to verify
skills = registry.list_skills()
print(f"Available: {skills}")

# Check specific tier
agent_skills = registry.list_skills(tier="agent")
```

### Invalid Skill Format

```python
# Validate SKILL.md format
from nexus.skills import SkillParser

parser = SkillParser()
try:
    skill = parser.parse_file("/path/to/SKILL.md", "agent")
    skill.validate()
except SkillParseError as e:
    print(f"Parse error: {e}")
```

### Circular Dependencies

```python
# Detect circular dependencies
try:
    deps = await registry.resolve_dependencies("my-skill")
except SkillDependencyError as e:
    print(f"Circular dependency: {e}")
    # Fix: Update skill frontmatter to remove circular dependency
```

### Export Size Limit

```python
# Claude format has 8MB limit
try:
    await exporter.export_skill(
        "large-skill",
        "/tmp/skill.zip",
        format="claude"
    )
except ValidationError as e:
    print(f"Export too large: {e}")
    # Fix: Export without dependencies or use generic format
```

## See Also

- [Plugins API](plugins.md) - Plugin system
- [CLI Reference](cli-reference.md) - Skills CLI commands
- [Semantic Search](semantic-search.md) - Search skills semantically
- [Versioning](versioning.md) - Version control system

## Next Steps

1. Discover and explore available skills
2. Create your first skill using templates
3. Fork and customize existing skills
4. Share skills with your team via publishing
5. Integrate with plugins (e.g., Anthropic plugin)
