"""
Nexus Anthropic Plugin - Python SDK Examples

This script demonstrates how to use custom skills and Anthropic skills
programmatically using the Nexus Python SDK and Anthropic plugin.

Requirements:
    pip install nexus-ai-fs nexus-plugin-anthropic anthropic
"""

import asyncio
import os
from pathlib import Path

# Set up environment - replace with your actual key or set via environment variable
# Get your API key from: https://console.anthropic.com/settings/keys
if "ANTHROPIC_API_KEY" not in os.environ:
    raise ValueError(
        "Please set ANTHROPIC_API_KEY environment variable. "
        "Get your key from: https://console.anthropic.com/settings/keys"
    )
os.environ["NEXUS_DATA_DIR"] = "./nexus-sdk-examples-data"


def print_section(title: str) -> None:
    """Print a section header."""
    print("\n" + "=" * 70)
    print(f"{title}")
    print("=" * 70)


async def example_1_custom_nexus_skills() -> None:
    """Example 1: Working with Custom Nexus Skills."""
    print_section("EXAMPLE 1: Working with Custom Nexus Skills")

    import time

    from nexus import connect
    from nexus.skills import SkillExporter, SkillManager, SkillRegistry

    # Use timestamp for unique skill names
    timestamp = str(int(time.time()))

    # Connect to Nexus
    nx = connect()

    # Initialize skill registry and manager
    registry = SkillRegistry(nx)
    manager = SkillManager(nx, registry)

    # 1. Create a custom skill
    skill_name = f"data-processor-{timestamp}"
    print("\n1. Creating a custom skill...")
    await manager.create_skill(
        name=skill_name,
        description="Process and transform data using pandas and numpy",
        tier="agent",
        version="1.0.0",
    )
    print(f"✓ Created skill: {skill_name}")

    # 2. Discover all skills
    print("\n2. Discovering all skills...")
    await registry.discover()
    skill_list = registry.list_skills()
    print(f"✓ Found {len(skill_list)} skills: {skill_list}")

    # 3. Get skill details
    print("\n3. Getting skill details...")
    skill = await registry.get_skill(skill_name)
    print(f"✓ Skill: {skill.metadata.name}")
    print(f"  Description: {skill.metadata.description}")
    print(f"  Tier: {skill.metadata.tier}")
    print(f"  Version: {skill.metadata.version}")

    # 4. Search for skills
    print("\n4. Searching for skills...")
    results = await manager.search_skills(query="data", limit=5)
    print(f"✓ Found {len(results)} matching skills:")
    for name, score in results:
        print(f"  - {name} (score: {score:.2f})")

    # 5. Fork a skill
    fork_name = f"ml-data-processor-{timestamp}"
    print("\n5. Forking skill...")
    await manager.fork_skill(source_name=skill_name, target_name=fork_name, tier="agent")
    print(f"✓ Forked: {skill_name} → {fork_name}")

    # 6. Resolve dependencies
    print("\n6. Resolving dependencies...")
    deps = await registry.resolve_dependencies(skill_name)
    print(f"✓ Dependency resolution order: {' → '.join(deps)}")

    # 7. Export skill to zip
    print("\n7. Exporting skill to .zip package...")
    exporter = SkillExporter(registry)
    zip_bytes = await exporter.export_skill(
        name=skill_name, format="claude", include_dependencies=True
    )
    assert zip_bytes is not None
    print(f"✓ Exported to .zip ({len(zip_bytes)} bytes)")

    # Save to file
    output_path = Path("./data-processor.zip")
    output_path.write_bytes(zip_bytes)
    print(f"✓ Saved to {output_path}")

    # 8. Validate skill for Claude API
    print("\n8. Validating skill for Claude API...")
    is_valid, message, size = await exporter.validate_export(
        name=skill_name, format="claude", include_dependencies=True
    )
    print(f"✓ Validation: {message}")
    print(f"  Size: {size / 1024:.2f} KB")

    nx.close()


async def example_2_anthropic_skills_api() -> None:
    """Example 2: Using Anthropic Skills API."""
    print_section("EXAMPLE 2: Using Anthropic Skills API")

    import time

    from nexus import connect
    from nexus.plugins.registry import PluginRegistry
    from nexus.skills import SkillManager, SkillRegistry

    # Use timestamp for unique names
    timestamp = str(int(time.time()))
    skill_name = f"data-processor-{timestamp}"

    # Connect to Nexus
    nx = connect()

    # Create a skill first
    registry = SkillRegistry(nx)
    manager = SkillManager(nx, registry)
    await manager.create_skill(
        name=skill_name, description="Process and transform data", tier="agent"
    )

    # Get the Anthropic plugin
    plugin_registry = PluginRegistry(nx)  # type: ignore[arg-type]
    plugin_registry.discover()
    anthropic_plugin = plugin_registry.get_plugin("anthropic")

    if not anthropic_plugin:
        print("❌ Anthropic plugin not installed")
        print("Install with: pip install nexus-plugin-anthropic")
        nx.close()
        return

    # 1. Upload a custom skill to Claude Skills API
    print("\n1. Uploading custom skill to Claude Skills API...")
    await anthropic_plugin.upload_skill(  # type: ignore[attr-defined]
        skill_name=skill_name, display_title=f"Data Processor {timestamp}", format="claude"
    )
    print("✓ Uploaded to Claude Skills API")

    # 2. List all skills in Claude API
    print("\n2. Listing skills from Claude Skills API...")
    await anthropic_plugin.list_skills()  # type: ignore[attr-defined]

    # 3. List only custom skills
    print("\n3. Listing only custom skills...")
    await anthropic_plugin.list_skills(source="custom")  # type: ignore[attr-defined]

    # 4. Download a skill from Claude API (example)
    print("\n4. Downloading skill from Claude API (example)...")
    print("   # await anthropic_plugin.download_skill(")
    print("   #     skill_id='skill_01AbCdEfGhIjKlMnOpQrStUv',")
    print("   #     tier='agent',")
    print("   #     version='latest'")
    print("   # )")

    nx.close()


async def example_3_github_skills() -> None:
    """Example 3: Importing Skills from GitHub."""
    print_section("EXAMPLE 3: Importing Skills from GitHub")

    from nexus import connect
    from nexus.plugins.registry import PluginRegistry
    from nexus.skills import SkillRegistry

    # Connect to Nexus
    nx = connect()

    # Get the Anthropic plugin
    plugin_registry = PluginRegistry(nx)  # type: ignore[arg-type]
    plugin_registry.discover()
    anthropic_plugin = plugin_registry.get_plugin("anthropic")

    # 1. Browse GitHub skills
    print("\n1. Browsing Anthropic skills from GitHub...")
    await anthropic_plugin.browse_github_skills()  # type: ignore[union-attr]

    # 2. Import a specific skill
    print("\n2. Importing 'algorithmic-art' from GitHub...")
    await anthropic_plugin.import_github_skill(skill_name="algorithmic-art", tier="agent")  # type: ignore[union-attr]
    print("✓ Imported algorithmic-art")

    # 3. Import multiple skills
    print("\n3. Importing multiple skills...")
    github_skills = ["canvas-design", "mcp-builder", "theme-factory"]

    for skill in github_skills:
        print(f"  Importing {skill}...")
        await anthropic_plugin.import_github_skill(skill_name=skill, tier="agent")  # type: ignore[union-attr]

    print(f"✓ Imported {len(github_skills)} skills from GitHub")

    # 4. Verify imported skills
    print("\n4. Verifying imported skills...")
    skill_registry = SkillRegistry(nx)
    await skill_registry.discover()

    for skill in ["algorithmic-art", "canvas-design", "mcp-builder"]:
        try:
            skill_obj = await skill_registry.get_skill(skill)
            print(f"✓ {skill}: {skill_obj.metadata.description[:60]}...")
        except Exception as e:
            print(f"✗ {skill}: {e}")

    nx.close()


async def example_4_advanced_workflow() -> None:
    """Example 4: Advanced Workflow - GitHub Import + Customization + Upload."""
    print_section("EXAMPLE 4: Advanced Workflow")

    import time

    from nexus import connect
    from nexus.plugins.registry import PluginRegistry
    from nexus.skills import SkillExporter, SkillManager, SkillRegistry

    # Use timestamp for unique names
    timestamp = str(int(time.time()))
    custom_name = f"my-custom-builder-{timestamp}"

    # Connect to Nexus
    nx = connect()

    # Initialize components
    skill_registry = SkillRegistry(nx)
    skill_manager = SkillManager(nx, skill_registry)
    plugin_registry = PluginRegistry(nx)  # type: ignore[arg-type]
    plugin_registry.discover()
    anthropic_plugin = plugin_registry.get_plugin("anthropic")

    # 1. Import a skill from GitHub
    print("\n1. Importing skill from GitHub...")
    await anthropic_plugin.import_github_skill(skill_name="artifacts-builder", tier="agent")  # type: ignore[union-attr]
    print("✓ Imported artifacts-builder from GitHub")

    # 2. Fork it to create a customized version
    print("\n2. Creating customized version...")
    await skill_registry.discover()
    await skill_manager.fork_skill(
        source_name="artifacts-builder", target_name=custom_name, tier="agent"
    )
    print(f"✓ Forked: artifacts-builder → {custom_name}")

    # 3. Modify the skill (in real usage, you'd edit the SKILL.md content)
    print("\n3. Customizing skill content...")
    skill_path = f"/workspace/.nexus/skills/{custom_name}/SKILL.md"
    raw_content = nx.read(skill_path)
    assert isinstance(raw_content, bytes)
    content = raw_content.decode("utf-8")

    # Add a note about customization
    custom_note = "\n\n## Customization Note\n\nThis is a customized version of artifacts-builder with enhanced features.\n"
    content += custom_note

    nx.write(skill_path, content.encode("utf-8"))
    print("✓ Added customization notes")

    # 4. Validate the customized skill
    print("\n4. Validating customized skill...")
    exporter = SkillExporter(skill_registry)
    await skill_registry.discover()  # Refresh to get updated content

    is_valid, message, size = await exporter.validate_export(
        name=custom_name, format="claude", include_dependencies=True
    )
    print(f"✓ {message}")
    print(f"  Size: {size / 1024:.2f} KB / {8 * 1024:.0f} KB limit")

    # 5. Export to zip
    print("\n5. Exporting customized skill...")
    await exporter.export_skill(
        name=custom_name,
        output_path="./my-custom-builder.zip",
        format="claude",
        include_dependencies=True,
    )
    print("✓ Exported to my-custom-builder.zip")

    # 6. Upload to Claude Skills API
    print("\n6. Uploading to Claude Skills API...")
    await anthropic_plugin.upload_skill(  # type: ignore[union-attr]
        skill_name=custom_name, display_title=f"My Custom Builder {timestamp}", format="claude"
    )
    print("✓ Uploaded to Claude Skills API")

    # 7. Publish to tenant tier for team access
    print("\n7. Publishing to tenant tier...")
    await skill_manager.publish_skill(name=custom_name, source_tier="agent", target_tier="tenant")
    print("✓ Published to tenant tier")

    nx.close()


async def example_5_using_with_claude() -> None:
    """Example 5: Using Skills with Claude API Messages."""
    print_section("EXAMPLE 5: Using Skills with Claude API")

    # This example shows how to use uploaded skills in Claude API calls
    print("\n1. Setting up Claude client...")
    print("   client = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))")

    print("\n2. Using a skill in a message (example)...")
    print("""
    # Example of using a custom skill with Claude Messages API
    message = client.messages.create(
        model="claude-3-5-sonnet-20241022",
        max_tokens=1024,
        messages=[
            {"role": "user", "content": "Process this data using the data processor skill"}
        ],
        # Enable code execution with skills
        tools=[{
            "type": "code_execution"
        }],
        # Specify which skills to use
        container={
            "skills": [
                {
                    "type": "custom",
                    "skill_id": "skill_01AbCdEfGhIjKlMnOpQrStUv",  # From upload
                    "version": "latest"
                }
            ]
        },
        # Required beta headers
        extra_headers={
            "anthropic-beta": "skills-2025-10-02,code-execution-2025-08-25"
        }
    )

    print(message.content)
    """)

    print("\n3. Using Anthropic-provided skills (example)...")
    print("""
    # Using official Anthropic skills
    message = client.messages.create(
        model="claude-3-5-sonnet-20241022",
        max_tokens=1024,
        messages=[
            {"role": "user", "content": "Create some algorithmic art"}
        ],
        tools=[{"type": "code_execution"}],
        container={
            "skills": [
                {
                    "type": "anthropic",
                    "name": "algorithmic-art",  # Official Anthropic skill
                    "version": "latest"
                }
            ]
        },
        extra_headers={
            "anthropic-beta": "skills-2025-10-02,code-execution-2025-08-25"
        }
    )
    """)


async def main() -> None:
    """Run all examples."""
    print("""
╔═══════════════════════════════════════════════════════════════════╗
║   Nexus Anthropic Plugin - Python SDK Examples                   ║
║                                                                   ║
║   This script demonstrates:                                       ║
║   1. Working with custom Nexus skills                            ║
║   2. Using the Anthropic Skills API                              ║
║   3. Importing skills from GitHub                                ║
║   4. Advanced workflows (import → customize → upload)            ║
║   5. Using skills with Claude Messages API                       ║
╚═══════════════════════════════════════════════════════════════════╝
    """)

    try:
        # Run examples
        await example_1_custom_nexus_skills()
        await example_2_anthropic_skills_api()
        await example_3_github_skills()
        await example_4_advanced_workflow()
        await example_5_using_with_claude()

        print_section("All Examples Completed Successfully!")

        print("\nGenerated files:")
        print("  - data-processor.zip")
        print("  - my-custom-builder.zip")
        print("  - nexus-sdk-examples-data/")

        print("\nCleanup:")
        print("  rm -rf nexus-sdk-examples-data/")
        print("  rm data-processor.zip my-custom-builder.zip")

    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
