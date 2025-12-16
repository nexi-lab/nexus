"""Skill Seekers Plugin SDK Demo - Comprehensive Python API examples.

The Skill Seekers plugin enables automatic generation of SKILL.md files from
documentation websites, making it easy to import knowledge into Nexus.

Features demonstrated:
- Generate skills from documentation URLs programmatically
- Import existing SKILL.md files
- Batch processing of multiple URLs
- Custom configuration and metadata
- Integration with Nexus filesystem
- Error handling and validation
- Advanced scraping options

This demo shows both direct plugin usage and integration with Nexus SDK.
"""

import asyncio
import tempfile
from pathlib import Path

import nexus


async def main() -> None:
    """Run the Skill Seekers plugin SDK demo."""
    print("=" * 70)
    print("Nexus Skill Seekers Plugin - Python SDK Demo")
    print("=" * 70)
    print()

    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir) / "nexus-data"
        data_dir.mkdir(parents=True)

        print(f"📁 Data directory: {data_dir}")

        # ============================================================
        # Part 1: Plugin Setup and Configuration
        # ============================================================
        print("\n" + "=" * 70)
        print("PART 1: Plugin Setup and Configuration")
        print("=" * 70)

        print("\n1. Connecting to Nexus with plugin support...")
        nx = nexus.connect(config={"data_dir": str(data_dir)})
        print("   ✓ Connected to Nexus")

        # Import plugin
        print("\n2. Importing Skill Seekers plugin...")
        from nexus_skill_seekers.plugin import SkillSeekersPlugin

        print("   ✓ Plugin imported")

        # Create plugin instance
        print("\n3. Creating plugin instance...")
        plugin = SkillSeekersPlugin(nexus_fs=nx)  # type: ignore[arg-type]
        print("   ✓ Plugin instance created")

        # Get plugin metadata
        print("\n4. Plugin metadata:")
        metadata = plugin.metadata()
        print(f"   Name: {metadata.name}")
        print(f"   Version: {metadata.version}")
        print(f"   Description: {metadata.description}")
        print(f"   Author: {metadata.author}")
        if metadata.homepage:
            print(f"   Homepage: {metadata.homepage}")

        # List available commands
        print("\n5. Available plugin commands:")
        commands = plugin.commands()
        for cmd_name in sorted(commands.keys()):
            print(f"   • {cmd_name}")

        # ============================================================
        # Part 2: Generate Skills from Documentation
        # ============================================================
        print("\n" + "=" * 70)
        print("PART 2: Generate Skills from Documentation")
        print("=" * 70)

        print("\n6. Generate skill from React documentation...")
        await plugin.generate_skill(
            url="https://react.dev/",
            name="react-basics",
            tier="agent",
            output_dir=None,  # Will import to NexusFS
        )
        print("   ✓ Generated react-basics skill")

        print("\n7. Generate skill from FastAPI docs (tenant tier)...")
        await plugin.generate_skill(
            url="https://fastapi.tiangolo.com/",
            name="fastapi-guide",
            tier="tenant",
        )
        print("   ✓ Generated fastapi-guide skill in tenant tier")

        print("\n8. Generate skill with auto-generated name...")
        await plugin.generate_skill(
            url="https://docs.python.org/3/library/asyncio.html",
            # name will be auto-generated from URL
            tier="agent",
        )
        print("   ✓ Generated skill with auto-generated name")

        print("\n9. Generate skill and save to file (not imported)...")
        output_path = Path(tmpdir) / "local-skills"
        output_path.mkdir(exist_ok=True)
        await plugin.generate_skill(
            url="https://docs.pytest.org/",
            name="pytest-testing",
            tier="agent",
            output_dir=str(output_path),
        )
        print(f"   ✓ Saved skill to: {output_path / 'pytest-testing.md'}")

        # ============================================================
        # Part 3: Import Existing SKILL.md Files
        # ============================================================
        print("\n" + "=" * 70)
        print("PART 3: Import Existing SKILL.md Files")
        print("=" * 70)

        # Create a sample SKILL.md
        print("\n10. Creating sample SKILL.md file...")
        sample_skill_path = Path(tmpdir) / "custom-skill.md"
        sample_skill_content = """---
name: custom-api-skill
version: 1.0.0
description: Custom API integration skill
author: SDK Demo
created: 2025-01-15T10:00:00Z
tier: agent
---

# Custom API Skill

## Overview

This is a custom skill for API integration patterns.

## Features

- RESTful API design
- GraphQL integration
- WebSocket communication
- Authentication strategies

## Usage

Use this skill when working with API integrations in your projects.

## Keywords

api, rest, graphql, websocket, authentication, oauth
"""
        sample_skill_path.write_text(sample_skill_content)
        print(f"   ✓ Created: {sample_skill_path}")

        print("\n11. Importing SKILL.md file into Nexus...")
        await plugin.import_skill(
            file_path=str(sample_skill_path),
            tier="agent",
            name=None,  # Will use name from file
        )
        print("   ✓ Imported custom-api-skill")

        print("\n12. Importing with custom name override...")
        await plugin.import_skill(
            file_path=str(sample_skill_path),
            tier="tenant",
            name="shared-api-skill",  # Override name
        )
        print("   ✓ Imported as shared-api-skill in tenant tier")

        # ============================================================
        # Part 4: Batch Processing
        # ============================================================
        print("\n" + "=" * 70)
        print("PART 4: Batch Processing from URLs File")
        print("=" * 70)

        # Create URLs file
        print("\n13. Creating URLs file for batch processing...")
        urls_file = Path(tmpdir) / "docs-urls.txt"
        urls_content = """# Documentation URLs for batch processing
# Format: url name
https://docs.djangoproject.com/ django-framework
https://vuejs.org/guide/ vue-guide
https://docs.sqlalchemy.org/ sqlalchemy-orm
"""
        urls_file.write_text(urls_content)
        print(f"   ✓ Created: {urls_file}")
        print("   URLs:")
        for line in urls_content.strip().split("\n"):
            if line and not line.startswith("#"):
                print(f"     - {line}")

        print("\n14. Running batch generation...")
        await plugin.batch_generate(
            urls_file=str(urls_file),
            tier="agent",
        )
        print("   ✓ Batch generation complete")

        # ============================================================
        # Part 5: List and Verify Skills
        # ============================================================
        print("\n" + "=" * 70)
        print("PART 5: List and Verify Generated Skills")
        print("=" * 70)

        print("\n15. Listing all generated skills...")
        await plugin.list_skills()
        print()

        # Verify skills in filesystem
        print("\n16. Verifying skills in Nexus filesystem...")
        print("\n   Agent tier skills (/workspace/.nexus/skills/):")
        try:
            agent_skills = nx.list("/workspace/.nexus/skills/")
            for skill_file in agent_skills[:5]:  # Show first 5
                print(f"   • {skill_file}")
            if len(agent_skills) > 5:
                print(f"   ... and {len(agent_skills) - 5} more")
        except Exception:
            print("   (No agent skills found)")

        print("\n   Tenant tier skills (/shared/skills/):")
        try:
            tenant_skills = nx.list("/shared/skills/")
            for skill_file in tenant_skills[:5]:  # Show first 5
                print(f"   • {skill_file}")
            if len(tenant_skills) > 5:
                print(f"   ... and {len(tenant_skills) - 5} more")
        except Exception:
            print("   (No tenant skills found)")

        # ============================================================
        # Part 6: Advanced Usage - Direct API Access
        # ============================================================
        print("\n" + "=" * 70)
        print("PART 6: Advanced Usage - Direct API Access")
        print("=" * 70)

        print("\n17. Directly scraping documentation content...")
        # Access internal scraping method
        url = "https://docs.python.org/3/library/json.html"
        print(f"   URL: {url}")
        content = plugin._fetch_documentation(url)
        print(f"   ✓ Fetched {len(content)} characters")
        print(f"   Preview: {content[:200]}...")

        print("\n18. Generating skill name from URL...")
        generated_name = plugin._generate_skill_name(url)
        print(f"   URL: {url}")
        print(f"   Generated name: {generated_name}")

        print("\n19. Creating SKILL.md content...")
        skill_md = plugin._generate_skill_md(
            name="json-library",
            url=url,
            content=content[:2000],  # Use truncated content for demo
        )
        print("   ✓ Generated SKILL.md content")
        print("   Preview:")
        print("   " + "\n   ".join(skill_md.split("\n")[:15]))

        # ============================================================
        # Part 7: Integration with Nexus Skills System
        # ============================================================
        print("\n" + "=" * 70)
        print("PART 7: Integration with Nexus Skills System")
        print("=" * 70)

        print("\n20. Discovering skills using Nexus SkillRegistry...")
        from nexus.skills import SkillRegistry

        registry = SkillRegistry(nx)
        count = await registry.discover()
        print(f"   ✓ Discovered {count} skills total")

        print("\n21. Listing skills by tier...")
        for tier in ["agent", "tenant"]:
            tier_skills = registry.list_skills(tier=tier)
            if tier_skills:
                print(f"\n   {tier.capitalize()} tier ({len(tier_skills)} skills):")
                # Type assertion: we know list_skills returns list[str]
                skill_names = sorted(tier_skills)[:5]  # type: ignore[type-var]
                for skill_name in skill_names:
                    assert isinstance(skill_name, str)
                    skill_metadata = registry.get_metadata(skill_name)
                    print(f"   • {skill_name} - {skill_metadata.description or 'No description'}")
                if len(tier_skills) > 5:
                    print(f"   ... and {len(tier_skills) - 5} more")

        print("\n22. Loading a generated skill...")
        if count > 0:
            first_skill = registry.list_skills()[0]
            assert isinstance(first_skill, str)
            skill = await registry.get_skill(first_skill)
            print(f"   ✓ Loaded: {skill.metadata.name}")
            print(f"   Version: {skill.metadata.version}")
            print(f"   Description: {skill.metadata.description}")
            print(f"   Tier: {skill.metadata.tier}")
            print(f"   Content length: {len(skill.content)} chars")
            print(f"   Content preview: {skill.content[:150]}...")

        # ============================================================
        # Part 8: Error Handling
        # ============================================================
        print("\n" + "=" * 70)
        print("PART 8: Error Handling and Validation")
        print("=" * 70)

        print("\n23. Handling invalid URLs...")
        try:
            await plugin.generate_skill(
                url="https://invalid-url-that-does-not-exist-12345.com/",
                name="test-invalid",
                tier="agent",
            )
        except Exception as e:
            print(f"   ✓ Caught expected error: {type(e).__name__}")
            print(f"   Message: {str(e)[:100]}...")

        print("\n24. Handling missing files...")
        try:
            await plugin.import_skill(
                file_path="/nonexistent/path/to/skill.md",
                tier="agent",
            )
        except FileNotFoundError as e:
            print(f"   ✓ Caught expected error: {type(e).__name__}")
            print(f"   Message: {str(e)}")

        print("\n25. Validating plugin configuration...")
        # Check if NexusFS is available
        if plugin.nx:
            print("   ✓ NexusFS available for skill import")
        else:
            print("   ⚠ NexusFS not available - skills will be saved to files only")

        # ============================================================
        # Final Summary
        # ============================================================
        print("\n" + "=" * 70)
        print("Demo Complete!")
        print("=" * 70)

        print("\n✨ Key Takeaways:")
        print("   • Generate skills from any documentation URL")
        print("   • Batch process multiple documentation sites")
        print("   • Import existing SKILL.md files")
        print("   • Multi-tier support (agent, tenant, system)")
        print("   • Seamless integration with Nexus Skills System")
        print("   • Automatic metadata extraction and generation")
        print("   • Error handling and validation")

        print("\n📚 Plugin Commands:")
        print("   • generate_skill() - Generate from documentation URL")
        print("   • import_skill()   - Import existing SKILL.md file")
        print("   • batch_generate() - Process multiple URLs from file")
        print("   • list_skills()    - List all generated skills")

        print("\n🔧 Configuration:")
        print("   • Set OpenAI API key for enhanced skill generation")
        print("   • Configure default tier and output directory")
        print("   • Customize scraping and parsing behavior")

        print("\n🚀 Next Steps:")
        print("   • Generate skills from your team's documentation")
        print("   • Share skills across teams using tenant tier")
        print("   • Integrate generated skills into AI workflows")
        print("   • Build custom templates for specific documentation types")

        # Close Nexus connection
        nx.close()


if __name__ == "__main__":
    asyncio.run(main())
