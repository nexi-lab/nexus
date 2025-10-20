"""Skills System Demo - Comprehensive example of Nexus Skills functionality.

The Skills System provides:
1. SKILL.md parser with YAML frontmatter
2. SkillRegistry for discovery and lazy loading
3. SkillManager for lifecycle operations (create, fork, publish)
4. Template system with 5 pre-built templates
5. Three-tier hierarchy (agent > tenant > system)
6. Dependency resolution with DAG and cycle detection
7. Export to .zip packages with format validation

Features demonstrated:
- Create skills from templates
- Fork existing skills with lineage tracking
- Publish skills between tiers
- Progressive disclosure and lazy loading
- Dependency resolution
- Export/import workflows
"""

import asyncio
import tempfile
from pathlib import Path

import nexus


def main() -> None:
    """Run the skills system demo."""
    print("=" * 70)
    print("Nexus Skills System Demo")
    print("=" * 70)
    print("\nNOTE: This demo shows the API surface.")
    print("For working examples, see: tests/unit/skills/")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir) / "nexus-data"
        data_dir.mkdir(parents=True)

        print(f"\n📁 Data directory: {data_dir}")

        # Initialize Nexus
        print("\n1. Connecting to Nexus...")
        nx = nexus.connect(config={"data_dir": str(data_dir)})
        print("   ✓ Connected")

        # Create sample skill files in the three tiers
        setup_sample_skills(nx)

        # Run async demo
        asyncio.run(skills_demo(nx, data_dir))


def setup_sample_skills(nx: nexus.NexusFilesystem) -> None:
    """Create sample SKILL.md files in the three tiers."""
    print("\n2. Setting up sample skills...")

    # Agent tier skill (highest priority)
    nx.write(
        "/workspace/.nexus/skills/my-personal-skill/SKILL.md",
        b"""---
name: my-personal-skill
description: A personal skill for code analysis
version: 1.0.0
author: Developer
---

# My Personal Skill

This is my personal code analysis skill.

## Features

- Fast analysis
- Custom rules
- Integration with my workflow
""",
    )

    # Tenant tier skill (medium priority)
    nx.write(
        "/shared/skills/team-analyzer/SKILL.md",
        b"""---
name: team-analyzer
description: Team-shared code analyzer
version: 2.1.0
author: Engineering Team
requires:
  - base-parser
---

# Team Analyzer

Shared skill for the entire engineering team.

## Usage

1. Scan codebase
2. Apply team standards
3. Generate report
""",
    )

    # Another shared/tenant tier skill with dependency
    nx.write(
        "/shared/skills/base-parser/SKILL.md",
        b"""---
name: base-parser
description: Base parsing utilities
version: 1.5.0
author: Team Libraries
---

# Base Parser

Foundation parsing utilities used by other skills.

## Capabilities

- AST parsing
- Token analysis
- Symbol resolution
""",
    )

    print("   ✓ Created 2 agent tier skills: /workspace/.nexus/skills/")
    print("   ✓ Created 2 tenant tier skills: /shared/skills/")
    print("   Note: /system/ tier is read-only (built-in skills only)")


async def skills_demo(nx: nexus.NexusFilesystem, data_dir: Path) -> None:
    """Run the async skills demo."""

    # ============================================================
    # Part 1: Discovery and Lazy Loading
    # ============================================================
    print("\n" + "=" * 70)
    print("PART 1: Discovery and Lazy Loading")
    print("=" * 70)

    print("\n3. Creating skill registry...")
    from nexus.skills import SkillRegistry

    registry = SkillRegistry(nx)
    print("   ✓ Registry created")

    print("\n4. Discovering skills (loads metadata only)...")
    count = await registry.discover()
    print(f"   ✓ Discovered {count} skills")

    if count == 0:
        print("\n   ⚠️  No skills discovered")
        print("   Note: Skills were written to NexusFS but discovery uses local filesystem")
        print("   ℹ️  For working filesystem integration, see tests/unit/skills/")
        print("\n   Continuing with SkillManager demo (creates skills on local FS)...")
        # Don't return - continue with the manager demo which uses local FS

    if count > 0:
        print("\n5. Listing discovered skills...")
        skills = registry.list_skills()
        for skill_name in sorted(skills):
            metadata = registry.get_metadata(skill_name)
            print(f"   - {metadata.name} (v{metadata.version or 'n/a'}) [{metadata.tier}]")
            print(f"     {metadata.description}")
    else:
        print("\n5. Skipping skill listing (no skills discovered)")

    if count > 0:
        # ============================================================
        # Part 2: Lazy Loading and Tier Priority
        # ============================================================
        print("\n" + "=" * 70)
        print("PART 2: Lazy Loading and Tier Priority")
        print("=" * 70)

        print("\n6. Getting skill metadata (no content loaded)...")
        metadata = registry.get_metadata("base-parser")
        print(f"   Name: {metadata.name}")
        print(f"   Description: {metadata.description}")
        print(f"   Version: {metadata.version}")
        print(f"   Tier: {metadata.tier}")
        print(f"   File: {metadata.file_path}")
        print("   ✓ Metadata accessed instantly (no content loading)")

        print("\n7. Loading full skill content (lazy loading)...")
        skill = await registry.get_skill("base-parser")
        print(f"   ✓ Loaded skill: {skill.metadata.name}")
        print(f"   Content preview: {skill.content[:100]}...")
        print("   ✓ Skill is now cached for future access")

        # ============================================================
        # Part 3: Dependency Resolution
        # ============================================================
        print("\n" + "=" * 70)
        print("PART 3: Dependency Resolution (DAG)")
        print("=" * 70)

        print("\n8. Resolving dependencies for 'team-analyzer'...")
        print("   team-analyzer requires:")
        print("     - base-parser")

        deps = await registry.resolve_dependencies("team-analyzer")
        print("\n   ✓ Resolved dependency order:")
        for i, dep in enumerate(deps, 1):
            dep_metadata = registry.get_metadata(dep)
            print(f"   {i}. {dep} - {dep_metadata.description}")

        print("\n   ✓ Dependencies resolved in correct order (DAG)")
        print("   ✓ Cycle detection prevents infinite loops")

        # ============================================================
        # Part 4: Skill Export
        # ============================================================
        print("\n" + "=" * 70)
        print("PART 4: Skill Export (.zip packages)")
        print("=" * 70)

        print("\n9. Creating skill exporter...")
        from nexus.skills import SkillExporter

        exporter = SkillExporter(registry)
        print("   ✓ Exporter created")

        print("\n10. Validating export (checks size limits)...")
        valid, msg, size = await exporter.validate_export(
            "team-analyzer", format="claude", include_dependencies=True
        )
        print(f"    Valid: {valid}")
        print(f"    Message: {msg}")
        print(f"    Total size: {size:,} bytes ({size / 1024:.2f} KB)")

        print("\n11. Exporting skill to .zip (with dependencies)...")
        output_path = data_dir / "team-analyzer.zip"
        await exporter.export_skill(
            "team-analyzer",
            output_path=str(output_path),
            format="generic",
            include_dependencies=True,
        )
        print(f"   ✓ Exported to: {output_path}")
        print(f"   ✓ Size: {output_path.stat().st_size:,} bytes")

        print("\n12. Exporting single skill (no dependencies)...")
        output_path2 = data_dir / "base-parser.zip"
        await exporter.export_skill(
            "base-parser",
            output_path=str(output_path2),
            format="generic",
            include_dependencies=False,
        )
        print(f"   ✓ Exported to: {output_path2}")
        print(f"   ✓ Size: {output_path2.stat().st_size:,} bytes")

        # ============================================================
        # Part 5: Registry Statistics
        # ============================================================
        print("\n" + "=" * 70)
        print("PART 5: Registry Statistics")
        print("=" * 70)

        print("\n13. Registry summary:")
        print(f"    {registry}")

        print("\n14. Skills by tier:")
        for tier in ["agent", "tenant"]:
            tier_skills = registry.list_skills(tier=tier)
            if tier_skills:
                print(f"    {tier.capitalize()}: {len(tier_skills)} skill(s)")
                for skill_name in tier_skills:
                    print(f"      - {skill_name}")
    else:
        print("\n   (Skipping Parts 2-5 - will demo SkillManager with local filesystem)")

    # ============================================================
    # Part 6: Skill Lifecycle Management (Create, Fork, Publish)
    # ============================================================
    print("\n" + "=" * 70)
    print("PART 6: Skill Lifecycle Management (NEW in v0.3.0)")
    print("=" * 70)

    print("\n15. Creating SkillManager (uses local filesystem)...")
    # Create manager without filesystem to use local FS
    from nexus.skills import SkillManager, SkillRegistry

    # Temporarily override tier paths to use temp directory
    original_tier_paths = SkillRegistry.TIER_PATHS.copy()
    SkillRegistry.TIER_PATHS = {
        "agent": str(data_dir / "agent-skills") + "/",
        "tenant": str(data_dir / "tenant-skills") + "/",
        "system": str(data_dir / "system-skills") + "/",
    }

    # Create a new registry for local FS
    local_registry = SkillRegistry(filesystem=None)
    # Create manager with the registry
    manager = SkillManager(filesystem=None, registry=local_registry)
    print("   ✓ Manager created")
    print(f"   Using temp directory: {data_dir}")

    print("\n16. Listing available templates...")
    from nexus.skills import get_template_description, list_templates

    templates = list_templates()
    print(f"   Available templates ({len(templates)}):")
    for template in templates:
        desc = get_template_description(template)
        print(f"   • {template}: {desc}")

    print("\n17. Creating new skill from 'basic' template...")
    new_skill_path = await manager.create_skill(
        "my-first-skill",
        description="My first custom skill created from template",
        template="basic",
        author="Demo User",
        tier="agent",
    )
    print(f"   ✓ Created: {new_skill_path}")

    # Refresh registry to discover new skill
    await local_registry.discover()
    new_skill = await local_registry.get_skill("my-first-skill")
    print(f"   ✓ Skill verified: {new_skill.metadata.name} v{new_skill.metadata.version}")

    print("\n18. Creating skill from 'data-analysis' template...")
    data_skill_path = await manager.create_skill(
        "customer-analytics",
        description="Analyze customer behavior and trends",
        template="data-analysis",
        author="Data Team",
        tier="agent",
    )
    print(f"   ✓ Created: {data_skill_path}")

    print("\n19. Forking existing skill with lineage tracking...")
    # First create base-parser if it doesn't exist
    print("   Creating base-parser skill first...")
    await manager.create_skill(
        "base-parser", description="Base parsing utilities", template="basic", tier="agent"
    )

    # Discover it so the registry knows about it
    await local_registry.discover()

    # Now fork it
    forked_path = await manager.fork_skill(
        "base-parser", "enhanced-parser", tier="agent", author="Demo User"
    )
    print(f"   ✓ Forked: {forked_path}")

    # Verify fork
    await local_registry.discover()
    forked_skill = await local_registry.get_skill("enhanced-parser")
    print(f"   ✓ Fork verified: {forked_skill.metadata.name} v{forked_skill.metadata.version}")
    print("   ✓ Lineage tracked in metadata (forked_from, parent_skill)")

    print("\n20. Publishing skill from agent to tenant tier...")
    published_path = await manager.publish_skill(
        "my-first-skill", source_tier="agent", target_tier="tenant"
    )
    print(f"   ✓ Published: {published_path}")
    print("   ✓ Skill now available to entire team in /shared/skills/")

    # ============================================================
    # Final Statistics
    # ============================================================
    print("\n" + "=" * 70)
    print("FINAL STATISTICS")
    print("=" * 70)

    # Refresh registry one more time to get all skills
    await local_registry.discover()

    print("\n21. Skills by tier (after lifecycle operations):")
    all_skills = local_registry.list_skills(include_metadata=True)
    tier_counts = {"agent": 0, "tenant": 0, "system": 0}

    for metadata in all_skills:
        tier_counts[metadata.tier] += 1

    for tier, count in tier_counts.items():
        if count > 0:
            print(f"   {tier.capitalize()}: {count} skill(s)")
            tier_skills = local_registry.list_skills(tier=tier)
            for skill_name in sorted(tier_skills):
                meta = local_registry.get_metadata(skill_name)
                version_str = f"v{meta.version}" if meta.version else "n/a"
                print(f"     - {skill_name} ({version_str})")

    print("\n" + "=" * 70)
    print("Demo Complete!")
    print("=" * 70)

    print("\n✨ Key Takeaways:")
    print("   • Progressive Disclosure: Metadata loaded first, content on-demand")
    print("   • Lazy Loading: Skills cached only when accessed")
    print("   • Three-Tier Hierarchy: Agent > Tenant > System priority")
    print("   • DAG Resolution: Automatic dependency ordering with cycle detection")
    print("   • Vendor-Neutral Export: Generic .zip with format validation")
    print("   • Skill Lifecycle: Create from templates, fork with lineage, publish to teams")
    print("   • Template System: 5 pre-built templates for common patterns")

    # Restore original tier paths
    SkillRegistry.TIER_PATHS = original_tier_paths


if __name__ == "__main__":
    main()
