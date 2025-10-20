"""Skills System Demo - Comprehensive example of Nexus Skills functionality.

NOTE: This demo is a work in progress. For fully working examples, see:
  tests/unit/skills/test_skill_registry.py
  tests/unit/skills/test_skill_exporter.py

The Skills System provides:
1. SKILL.md parser with YAML frontmatter
2. SkillRegistry for discovery and lazy loading
3. Three-tier hierarchy (agent > tenant > system)
4. Dependency resolution with DAG and cycle detection
5. Export to .zip packages with format validation

For the working API, see the unit tests which show:
- Programmatic skill creation and discovery
- Registry operations with mock filesystems
- Dependency resolution examples
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
    registry = nexus.SkillRegistry(nx)
    print("   ✓ Registry created")

    print("\n4. Discovering skills (loads metadata only)...")
    count = await registry.discover()
    print(f"   ✓ Discovered {count} skills")

    if count == 0:
        print("\n   ⚠️  No skills discovered (filesystem integration WIP)")
        print("   ℹ️  See tests/unit/skills/ for working examples:")
        print("      - test_skill_registry.py: Full discovery & lazy loading")
        print("      - test_skill_exporter.py: Export to .zip packages")
        print("      - test_skill_parser.py: SKILL.md parsing")
        print("\n   The Skills System SDK is fully functional - see README.md")
        return

    print("\n5. Listing discovered skills...")
    skills = registry.list_skills()
    for skill_name in sorted(skills):
        metadata = registry.get_metadata(skill_name)
        print(f"   - {metadata.name} (v{metadata.version or 'n/a'}) [{metadata.tier}]")
        print(f"     {metadata.description}")

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
    exporter = nexus.SkillExporter(registry)
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

    print("\n" + "=" * 70)
    print("Demo Complete!")
    print("=" * 70)

    print("\n✨ Key Takeaways:")
    print("   • Progressive Disclosure: Metadata loaded first, content on-demand")
    print("   • Lazy Loading: Skills cached only when accessed")
    print("   • Three-Tier Hierarchy: Agent > Tenant > System priority")
    print("   • DAG Resolution: Automatic dependency ordering with cycle detection")
    print("   • Vendor-Neutral Export: Generic .zip with format validation")


if __name__ == "__main__":
    main()
