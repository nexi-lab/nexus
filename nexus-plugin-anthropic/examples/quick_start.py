"""
Nexus Anthropic Plugin - Quick Start

A minimal example showing the most common workflows.
"""

import asyncio
import os

# Set your API key - replace with your actual key or set via environment variable
# Get your API key from: https://console.anthropic.com/settings/keys
if "ANTHROPIC_API_KEY" not in os.environ:
    raise ValueError(
        "Please set ANTHROPIC_API_KEY environment variable. "
        "Get your key from: https://console.anthropic.com/settings/keys"
    )
os.environ["NEXUS_DATA_DIR"] = "./quick-start-data"


async def main() -> None:
    import time

    from nexus import connect
    from nexus.plugins.registry import PluginRegistry
    from nexus.skills import SkillManager, SkillRegistry

    print("🚀 Nexus Anthropic Quick Start\n")

    # Use timestamp for unique skill names
    timestamp = str(int(time.time()))

    # Connect to Nexus
    nx = connect()
    skill_registry = SkillRegistry(nx)
    skill_manager = SkillManager(nx, skill_registry)

    # Get Anthropic plugin
    plugin_registry = PluginRegistry(nx)  # type: ignore[arg-type]
    plugin_registry.discover()
    anthropic = plugin_registry.get_plugin("anthropic")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Example 1: Import a skill from GitHub
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    print("📥 1. Importing skill from GitHub...")
    await anthropic.import_github_skill("algorithmic-art", tier="agent")  # type: ignore[union-attr]
    print("   ✓ Imported: algorithmic-art\n")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Example 2: Create a custom skill
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    skill_name = f"my-analyzer-{timestamp}"
    print("✏️  2. Creating custom skill...")
    await skill_manager.create_skill(
        name=skill_name, description="Custom data analysis tool", tier="agent"
    )
    print(f"   ✓ Created: {skill_name}\n")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Example 3: Upload to Claude Skills API
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    print("☁️  3. Uploading to Claude Skills API...")
    await anthropic.upload_skill(  # type: ignore[union-attr]
        skill_name=skill_name, display_title=f"My Custom Analyzer {timestamp}"
    )
    print("   ✓ Uploaded to Claude\n")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Example 4: List all skills
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    print("📋 4. Listing all local skills...")
    await skill_registry.discover()
    skill_list = skill_registry.list_skills()
    for name in skill_list:
        print(f"   • {name}")
    print()

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Example 5: List Claude API skills
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    print("☁️  5. Listing skills in Claude API...")
    await anthropic.list_skills()  # type: ignore[union-attr]
    print()

    nx.close()

    print("✅ Quick start complete!")
    print("\nNext steps:")
    print("  • Explore examples/README.md for detailed usage")
    print("  • Run examples/cli_examples.sh for CLI workflows")
    print("  • Run examples/python_sdk_examples.py for advanced usage")


if __name__ == "__main__":
    asyncio.run(main())
