# Skills Management

**Organize, discover, and share AI capabilities as reusable skills**

⏱️ **Time:** 20 minutes | 💡 **Difficulty:** Medium

## What You'll Learn

- Create and manage AI skills in Markdown format
- Use the three-tier skill hierarchy (agent/tenant/system)
- Auto-generate skills from documentation URLs
- Search and discover skills
- Share skills across your organization
- Integrate skills with AI agents

## Prerequisites

✅ Python 3.8+ installed
✅ Nexus installed (`pip install nexus-ai-fs`)
✅ Basic understanding of Markdown and YAML
✅ (Optional) API key for AI enhancement (OpenRouter, Anthropic, or OpenAI)

## Overview

**Skills** are reusable AI capabilities packaged as Markdown files with structured metadata. They enable:

- **📦 Reusability** - Package knowledge once, use everywhere
- **🔍 Discoverability** - Search and find skills across your organization
- **🏢 Governance** - Control skill access with three-tier hierarchy
- **🤖 AI Integration** - Auto-generate skills from any documentation
- **📚 Knowledge Sharing** - Collaborate on skills across teams

**Three-Tier Hierarchy:**

```
┌──────────────────────────────────────────────────────┐
│  System Tier (/system/skills/)                      │
│  ✓ Global skills available to all                   │
│  ✓ Admin-only creation                              │
│  ✓ Examples: Python stdlib, REST APIs               │
└──────────────────────────────────────────────────────┘
              ↑ Promoted by admins
┌──────────────────────────────────────────────────────┐
│  Tenant Tier (/shared/skills/)                      │
│  ✓ Organization-wide skills                         │
│  ✓ ReBAC permissions control                        │
│  ✓ Examples: Internal APIs, team processes          │
└──────────────────────────────────────────────────────┘
              ↑ Published by users
┌──────────────────────────────────────────────────────┐
│  Agent Tier (/workspace/.nexus/skills/)             │
│  ✓ Personal skills (highest priority)               │
│  ✓ Private to agent/user                            │
│  ✓ Examples: Custom workflows, preferences          │
└──────────────────────────────────────────────────────┘
```

**Discovery Priority:** Agent → Tenant → System (agent skills override tenant/system)

---

## Step 1: Start Nexus Server

Start Nexus server with database authentication:

```bash
# Initialize server with admin user (first time only)
nexus serve --host 0.0.0.0 --port 2026 \
  --database-url "postgresql://postgres:nexus@localhost/nexus" \
  --auth-type database --init

# Server will output:
# ✓ Admin user created: admin
# ✓ API key: nxk_abc123...
# Save this API key!

# For subsequent starts:
nexus serve --host 0.0.0.0 --port 2026 \
  --database-url "postgresql://postgres:nexus@localhost/nexus" \
  --auth-type database
```

**Quick setup script:**
```bash
# Use the convenience script
./scripts/init-nexus-with-auth.sh

# Load credentials
source .nexus-admin-env

# Verify
echo $NEXUS_URL      # http://localhost:2026
echo $NEXUS_API_KEY  # nxk_abc123...
```

**Verify server:**
```bash
curl http://localhost:2026/health
# {"status":"ok","version":"0.5.2"}
```

---

## Step 2: Create Your First Skill

Skills are Markdown files with YAML frontmatter. Let's create one:

```python
# create_skill.py
import nexus

# Connect to server
nx = nexus.connect(config={
    "url": "http://localhost:2026",
    "api_key": "your-api-key"
})

# Create skill content
skill_content = """---
name: git-best-practices
description: Git workflow and best practices for team collaboration
version: 1.0.0
author: DevOps Team
created_at: 2025-01-15T10:00:00Z
tier: tenant
---

# Git Best Practices

## Overview
Guidelines for effective Git workflows in team environments.

## Branch Naming
- `feature/` - New features
- `bugfix/` - Bug fixes
- `hotfix/` - Production hotfixes
- `release/` - Release preparation

## Commit Messages
Follow conventional commits:
- `feat:` - New feature
- `fix:` - Bug fix
- `docs:` - Documentation
- `refactor:` - Code refactoring

## Example Workflow

\```bash
# Create feature branch
git checkout -b feature/add-login

# Make changes and commit
git add .
git commit -m "feat: add user login page"

# Push and create PR
git push origin feature/add-login
\```

## Code Review Guidelines
- Review within 24 hours
- Two approvals required
- Run tests before approval
- Check for security issues
"""

# Create skill directory
nx.mkdir("/workspace/.nexus/skills/git-best-practices", parents=True, exist_ok=True)

# Write skill file
nx.write("/workspace/.nexus/skills/git-best-practices/SKILL.md", skill_content.encode())

print("✅ Skill created: git-best-practices")
```

**Run it:**
```bash
python create_skill.py
```

---

## Step 3: Discover and List Skills

Use the Skills Registry to find available skills:

```python
# discover_skills.py
import asyncio
import nexus
from nexus.skills.registry import SkillRegistry

async def main():
    # Connect to Nexus
    nx = nexus.connect(config={
        "url": "http://localhost:2026",
        "api_key": "your-api-key"
    })

    # Create skill registry
    registry = SkillRegistry(filesystem=nx)

    # Discover all skills
    count = await registry.discover(tiers=["agent", "tenant", "system"])
    print(f"📚 Discovered {count} total skills\n")

    # List skills by tier
    print("🔹 Agent Skills (Personal):")
    agent_skills = registry.list_skills(tier="agent")
    for skill_name in agent_skills:
        print(f"  • {skill_name}")

    print("\n🔸 Tenant Skills (Organization):")
    tenant_skills = registry.list_skills(tier="tenant")
    for skill_name in tenant_skills:
        print(f"  • {skill_name}")

    print("\n🔻 System Skills (Global):")
    system_skills = registry.list_skills(tier="system")
    for skill_name in system_skills:
        print(f"  • {skill_name}")

asyncio.run(main())
```

**Run it:**
```bash
python discover_skills.py
```

**Expected output:**
```
📚 Discovered 3 total skills

🔹 Agent Skills (Personal):
  • git-best-practices

🔸 Tenant Skills (Organization):
  • api-design-guide

🔻 System Skills (Global):
  • python-json-module
```

---

## Step 4: Read and Use Skills

Access skill content and metadata:

```python
# read_skill.py
import asyncio
import nexus
from nexus.skills.registry import SkillRegistry

async def main():
    nx = nexus.connect(config={
        "url": "http://localhost:2026",
        "api_key": "your-api-key"
    })

    registry = SkillRegistry(filesystem=nx)
    await registry.discover()

    # Get specific skill
    skill = await registry.get_skill("git-best-practices")

    # Access metadata
    print(f"📖 Skill: {skill.metadata.name}")
    print(f"   Version: {skill.metadata.version}")
    print(f"   Author: {skill.metadata.author}")
    print(f"   Description: {skill.metadata.description}")
    print(f"   Tier: {skill.metadata.tier}")

    # Read the content
    print(f"\n📄 Content Preview:")
    print(skill.content[:300] + "...")

asyncio.run(main())
```

**Output:**
```
📖 Skill: git-best-practices
   Version: 1.0.0
   Author: DevOps Team
   Description: Git workflow and best practices...
   Tier: tenant

📄 Content Preview:
# Git Best Practices

## Overview
Guidelines for effective Git workflows...
```

---

## Step 5: Auto-Generate Skills from Documentation

Use Nexus Skill Seekers plugin to generate skills from URLs:

**Install the plugin:**
```bash
pip install nexus-plugin-skill-seekers
```

**Generate skills:**
```python
# generate_skill.py
import asyncio
import os
import nexus
from nexus_skill_seekers.plugin import SkillSeekersPlugin

async def main():
    # Connect to Nexus server
    nx = nexus.connect(config={"mode": "remote", "url": "http://localhost:2026", "api_key": os.getenv("NEXUS_API_KEY"}))

    # Initialize Skill Seekers plugin
    plugin = SkillSeekersPlugin(nx)

    # Generate skill from Python docs
    print("🔍 Generating skill from Python json module docs...")

    skill_path = await plugin.generate_skill(
        url="https://docs.python.org/3/library/json.html",
        name="python-json-module",
        tier="agent",
        use_ai=True  # Enable AI enhancement for better structure
    )

    print(f"✅ Skill created: {skill_path}")
    print("   The skill is now available in your agent tier!")

asyncio.run(main())
```

**Run with AI enhancement (optional):**
```bash
# Set API key for AI enhancement
export OPENROUTER_API_KEY="sk-or-v1-..."
# or
export ANTHROPIC_API_KEY="sk-ant-..."
# or
export OPENAI_API_KEY="sk-..."

# Generate
python generate_skill.py
```

**Output:**
```
🔍 Generating skill from Python json module docs...
  → Checking for llms.txt...
  → Using AI enhancement with Claude...
✅ Skill created: /workspace/.nexus/skills/python-json-module/SKILL.md
   The skill is now available in your agent tier!
```

---

## Step 6: Search and Find Skills

Search skills by keyword using the CLI:

```bash
# Search for skills
nexus skills search json

# Expected output:
# 📚 Search results for "json":
#   • python-json-module (agent) - Python's json module for encoding/decoding
#   • api-json-formatting (tenant) - JSON formatting standards for APIs
```

**Via Python:**
```python
# search_skills.py
import asyncio
import nexus
from nexus.skills.registry import SkillRegistry

async def main():
    nx = nexus.connect(config={
        "url": "http://localhost:2026",
        "api_key": "your-api-key"
    })

    registry = SkillRegistry(filesystem=nx)
    await registry.discover()

    # Search by keyword
    query = "git"
    results = [
        name for name in registry.list_skills()
        if query.lower() in name.lower()
    ]

    print(f"🔍 Found {len(results)} skills matching '{query}':")
    for name in results:
        skill = await registry.get_skill(name)
        print(f"  • {name}")
        print(f"    Tier: {skill.metadata.tier}")
        print(f"    Description: {skill.metadata.description}")

asyncio.run(main())
```

---

## Step 7: Share Skills with Your Team

Publish agent-tier skills to tenant tier for team sharing:

```python
# publish_skill.py
import asyncio
import nexus

async def main():
    nx = nexus.connect(config={
        "url": "http://localhost:2026",
        "api_key": "your-api-key"
    })

    # Read agent-tier skill
    agent_skill = nx.read("/workspace/.nexus/skills/git-best-practices/SKILL.md")

    # Create in tenant tier
    nx.mkdir("/shared/skills/git-best-practices", parents=True, exist_ok=True)
    nx.write("/shared/skills/git-best-practices/SKILL.md", agent_skill)

    # Grant team read access
    nx.rebac_create(
        subject=("group", "engineering"),
        relation="can_read",
        object=("file", "/shared/skills/git-best-practices"),
        zone_id="default"
    )

    print("✅ Skill published to tenant tier")
    print("   Team members can now discover and use it!")

asyncio.run(main())
```

---

## Step 8: Use Skills CLI

Nexus provides a CLI for skill management:

**List all skills:**
```bash
nexus skills list

# Output:
# Agent Skills:
#   • git-best-practices
#   • python-json-module
#
# Tenant Skills:
#   • api-design-guide
```

**Get skill info:**
```bash
nexus skills info git-best-practices

# Output:
# Name: git-best-practices
# Version: 1.0.0
# Description: Git workflow and best practices...
# Tier: agent
# Author: DevOps Team
# Created: 2025-01-15T10:00:00Z
```

**Export skills:**
```bash
# Export single skill
nexus skills export git-best-practices --output git-skill.zip

# Export all agent skills
nexus skills export-all --tier agent --output my-skills.zip
```

---

## Complete Example: Team Skill Library

Here's a complete workflow for building a team skill library:

```python
#!/usr/bin/env python3
"""
Build a team skill library from documentation
"""
import asyncio
import os
import nexus
from nexus_skill_seekers.plugin import SkillSeekersPlugin

async def main():
    # Connect to server
    nx = nexus.connect(config={"mode": "remote", "url": "http://localhost:2026", "api_key": os.getenv("NEXUS_API_KEY"}))
    plugin = SkillSeekersPlugin(nx)

    # Team's tech stack documentation
    docs = {
        "fastapi": "https://fastapi.tiangolo.com/",
        "pydantic": "https://docs.pydantic.dev/",
        "sqlalchemy": "https://docs.sqlalchemy.org/",
        "pytest": "https://docs.pytest.org/",
    }

    print("🚀 Building team skill library...")
    print(f"   Generating {len(docs)} skills from documentation\n")

    for name, url in docs.items():
        print(f"📖 Processing: {name}")
        try:
            skill_path = await plugin.generate_skill(
                url=url,
                name=f"lib-{name}",
                tier="tenant",  # Share with team
                use_ai=True
            )
            print(f"   ✅ Created: {skill_path}\n")
        except Exception as e:
            print(f"   ❌ Failed: {e}\n")

    print("🎉 Team skill library complete!")
    print("\nTeam members can now:")
    print("  • nexus skills list --tier tenant")
    print("  • nexus skills info lib-fastapi")
    print("  • Use skills in AI agent conversations")

if __name__ == "__main__":
    asyncio.run(main())
```

**Run it:**
```bash
# Set API key for AI enhancement
export OPENROUTER_API_KEY="your-key"
export NEXUS_API_KEY="your-nexus-key"

# Generate team library
python build_team_library.py
```

---

## Troubleshooting

### Issue: Skills Not Found

**Problem:** `registry.discover()` finds 0 skills

**Solution:**
```bash
# Check skill directories exist
nexus ls /workspace/.nexus/skills/
nexus ls /shared/skills/
nexus ls /system/skills/

# Verify SKILL.md files
nexus ls /workspace/.nexus/skills/*/SKILL.md
```

---

### Issue: AI Enhancement Fails

**Problem:** Skills generate without AI formatting

**Solution:**
```python
# Check API keys
import os
print(f"OpenRouter: {bool(os.getenv('OPENROUTER_API_KEY'))}")
print(f"Anthropic: {bool(os.getenv('ANTHROPIC_API_KEY'))}")
print(f"OpenAI: {bool(os.getenv('OPENAI_API_KEY'))}")

# Verify one is set
# Plugin auto-falls back to basic generation if no key
```

---

### Issue: Permission Denied

**Problem:** Cannot create tenant-tier skills

**Solution:**
```bash
# Check permissions
nexus rebac list-tuples --subject user:your-user

# Grant tenant skill creation permission
nexus rebac create \
  --subject user:your-user \
  --relation can_write \
  --object file:/shared/skills
```

---

## Best Practices

### 1. Use Meaningful Names

```python
# ✅ Good: Descriptive, clear
"api-authentication-guide"
"python-testing-pytest"
"deployment-kubernetes-helm"

# ❌ Bad: Vague, unclear
"stuff"
"notes"
"doc1"
```

### 2. Include Version Numbers

```yaml
---
name: api-auth
version: 2.1.0  # ✅ Semantic versioning
description: API authentication patterns
---
```

### 3. Add Dependencies

```yaml
---
name: advanced-api-testing
requires:
  - python-requests
  - api-authentication-guide
---
```

### 4. Use Appropriate Tiers

```
Agent Tier:  Personal preferences, custom workflows
Tenant Tier: Team processes, internal APIs
System Tier: Standard libraries, universal knowledge
```

---

## What's Next?

**Congratulations!** You've mastered Nexus skills management.

### 🔍 Recommended Next Steps

1. **[AI Agent Memory](ai-agent-memory.md)** (15 min)
   Combine skills with agent memory for smarter agents

2. **[Team Collaboration](team-collaboration.md)** (20 min)
   Share skills across your organization with permissions

3. **[Multi-Tenant SaaS](multi-zone-saas.md)** (30 min)
   Build skill libraries for multi-zone applications

### 📚 Related Concepts

- [Skills System Architecture](../concepts/skills-system.md)
- [Skill Seekers Plugin](../examples/skill-seekers.md)
- [ReBAC Permissions](../concepts/rebac-explained.md)

### 🔧 Advanced Topics

- [llms.txt Standard](https://llmstxt.org/) - Optimize documentation for AI
- [Firecrawl Integration](https://firecrawl.dev/) - Multi-page documentation scraping
- [Custom Skill Parsers](../api/skills.md) - Extend skill formats

---

## Summary

🎉 **You've completed the Skills Management tutorial!**

**What you learned:**
- ✅ Create skills with YAML frontmatter
- ✅ Use three-tier hierarchy (agent/tenant/system)
- ✅ Auto-generate skills from documentation
- ✅ Search and discover skills
- ✅ Share skills with your team
- ✅ Use Skills CLI for management

**Key Takeaways:**
- Skills package AI knowledge as reusable Markdown files
- Three-tier system provides governance and sharing
- Auto-generation makes skill creation effortless
- Skills integrate seamlessly with AI agents
- Use server mode for team collaboration

---

**Next:** [AI Agent Memory →](ai-agent-memory.md)

**Questions?** Check our [Skills System Guide](../concepts/skills-system.md) or [GitHub Discussions](https://github.com/nexi-lab/nexus/discussions)
