#!/usr/bin/env python3
"""
Nexus Integration Demo - Shows how Firecrawl plugin works with Nexus

This demonstrates:
1. How Nexus discovers and loads the plugin
2. How commands are registered and executed
3. How content is saved to NexusFS
4. How to use the plugin programmatically

Requirements:
    - nexus-ai-fs installed
    - nexus-plugin-firecrawl installed
    - FIRECRAWL_API_KEY environment variable set
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

# Try to import Nexus components
try:
    from nexus.plugins.registry import PluginRegistry

    NEXUS_AVAILABLE = True
except ImportError:
    print("⚠️  Nexus not installed - showing mock integration")
    NEXUS_AVAILABLE = False

# Always import the plugin
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from nexus_firecrawl import FirecrawlPlugin


def print_section(title: str) -> None:
    """Print a section header."""
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70 + "\n")


async def demo_plugin_discovery() -> None:
    """Demo 1: How Nexus discovers the Firecrawl plugin."""
    print_section("DEMO 1: Plugin Discovery")

    if not NEXUS_AVAILABLE:
        print("Nexus not available - skipping")
        return

    # Create a temporary Nexus data directory
    with tempfile.TemporaryDirectory() as temp_dir:
        print(f"Creating Nexus instance at: {temp_dir}")

        # Initialize NexusFS (simplified - actual init may vary)
        try:
            from nexus import connect

            nx = connect(data_dir=temp_dir)  # type: ignore[call-arg]
        except Exception:
            print("⚠️  Using mock NexusFS for demo")
            return

        print("\n1️⃣  Creating PluginRegistry...")
        registry = PluginRegistry(nexus_fs=nx)  # type: ignore[arg-type]

        print("2️⃣  Discovering plugins via entry points...")
        discovered = registry.discover()

        print(f"\n✅ Discovered {len(discovered)} plugin(s):")
        for name in discovered:
            plugin = registry.get_plugin(name)
            if plugin:
                meta = plugin.metadata()
                print(f"   • {meta.name} v{meta.version} - {meta.description}")

        # Check if firecrawl was found
        if "firecrawl" in discovered:
            print("\n✅ Firecrawl plugin successfully discovered!")

            plugin = registry.get_plugin("firecrawl")
            if plugin:
                print("\n3️⃣  Plugin commands registered:")
                for cmd_name in plugin.commands().keys():
                    print(f"   • nexus firecrawl {cmd_name}")
        else:
            print("\n⚠️  Firecrawl plugin not found in registry")
            print("   Make sure to install it: pip install -e .")


async def demo_cli_execution() -> None:
    """Demo 2: How CLI commands execute through the plugin."""
    print_section("DEMO 2: CLI Command Execution Flow")

    if not NEXUS_AVAILABLE:
        print("Nexus not available - skipping")
        return

    print("When you run: nexus firecrawl scrape https://example.com")
    print("\nExecution flow:")
    print("  1. Nexus CLI parses command: plugin=firecrawl, command=scrape")
    print("  2. PluginRegistry finds FirecrawlPlugin")
    print("  3. Creates NexusFS instance")
    print("  4. Instantiates plugin with NexusFS")
    print("  5. Loads plugin config from ~/.nexus/plugins/firecrawl/config.yaml")
    print("  6. Calls plugin.scrape(url='https://example.com')")
    print("  7. Plugin scrapes using Firecrawl API")
    print("  8. Plugin saves content to NexusFS")
    print("  9. Plugin displays result to user")

    print("\n📝 This is handled automatically by Nexus!")


async def demo_nexusfs_integration() -> None:
    """Demo 3: How content is saved to NexusFS."""
    print_section("DEMO 3: NexusFS Integration")

    api_key = os.getenv("FIRECRAWL_API_KEY")
    if not api_key:
        print("⚠️  FIRECRAWL_API_KEY not set - using mock data")
        mock_mode = True
    else:
        mock_mode = False

    if not NEXUS_AVAILABLE:
        print("Nexus not available - showing concept")
        print("\nWhen Firecrawl plugin saves content:")
        print("  1. Scrape https://example.com")
        print("  2. Convert URL to path: example_com/index.md")
        print("  3. Full path: /workspace/scraped/example_com/index.md")
        print("  4. Create directory: /workspace/scraped/example_com/")
        print("  5. Write markdown content to NexusFS")
        print("  6. Content accessible via: nexus cat /workspace/scraped/example_com/index.md")
        return

    # Real demonstration
    with tempfile.TemporaryDirectory() as temp_dir:
        print(f"Creating Nexus workspace at: {temp_dir}")

        # Initialize NexusFS
        try:
            from nexus import connect

            nx = connect(data_dir=temp_dir)  # type: ignore[call-arg]
        except Exception as e:
            print(f"⚠️  Cannot initialize NexusFS: {e}")
            return

        # Create and configure plugin
        plugin = FirecrawlPlugin(nexus_fs=nx)  # type: ignore[arg-type]
        await plugin.initialize({"api_key": api_key or "demo"})

        if not mock_mode:
            print("\n🌐 Scraping https://example.com...")

            try:
                # Scrape with NexusFS integration
                await plugin.scrape("https://example.com", save_to_nexus=True)

                # Show what was saved
                print("\n📁 Content saved to NexusFS:")

                # List files
                scraped_dir = "/workspace/scraped"
                if nx.exists(scraped_dir):
                    for file in nx.list(scraped_dir, recursive=True):
                        print(f"   • {file}")

                    # Read the content
                    file_path = "/workspace/scraped/example_com/index.md"
                    if nx.exists(file_path):
                        raw_content = nx.read(file_path)
                        content = (
                            raw_content.decode("utf-8")
                            if isinstance(raw_content, bytes)
                            else str(raw_content)
                        )
                        print(f"\n📄 Content preview ({file_path}):")
                        print("   " + content[:200].replace("\n", "\n   ") + "...")

            except Exception as e:
                print(f"\n❌ Error: {e}")
        else:
            print("\n💡 Set FIRECRAWL_API_KEY to run real scraping demo")


async def demo_programmatic_usage() -> None:
    """Demo 4: Using the plugin programmatically."""
    print_section("DEMO 4: Programmatic Usage")

    api_key = os.getenv("FIRECRAWL_API_KEY")
    if not api_key:
        print("⚠️  FIRECRAWL_API_KEY not set - showing example code")
        print("\n```python")
        print("from nexus.core import NexusFS")
        print("from nexus_firecrawl import FirecrawlPlugin")
        print("")
        print("# Initialize Nexus")
        print("nx = NexusFS(data_dir='./my-nexus-data')")
        print("")
        print("# Create plugin")
        print("plugin = FirecrawlPlugin(nexus_fs=nx)")
        print("await plugin.initialize({'api_key': 'fc-xxx'})")
        print("")
        print("# Use plugin")
        print("await plugin.scrape('https://example.com')")
        print("")
        print("# Content automatically in NexusFS!")
        print("content = nx.read('/workspace/scraped/example_com/index.md')")
        print("```")
        return

    if not NEXUS_AVAILABLE:
        print("Nexus not available - install with: pip install nexus-ai-fs")
        return

    with tempfile.TemporaryDirectory() as temp_dir:
        print("Running real example...\n")

        # The actual code from above
        try:
            from nexus import connect

            nx = connect(data_dir=temp_dir)  # type: ignore[call-arg]
        except Exception as e:
            print(f"⚠️  Cannot initialize NexusFS: {e}")
            return
        plugin = FirecrawlPlugin(nexus_fs=nx)  # type: ignore[arg-type]
        await plugin.initialize({"api_key": api_key})

        print("✅ Initialized NexusFS and FirecrawlPlugin")
        print("🌐 Scraping example.com...")

        try:
            await plugin.scrape("https://example.com", save_to_nexus=True)
            print("✅ Content scraped and saved!")

            # Access the content
            path = "/workspace/scraped/example_com/index.md"
            if nx.exists(path):
                raw_content = nx.read(path)
                content = (
                    raw_content.decode("utf-8")
                    if isinstance(raw_content, bytes)
                    else str(raw_content)
                )
                print(f"\n📄 Saved to: {path}")
                print(f"   Size: {len(content)} bytes")

        except Exception as e:
            print(f"❌ Error: {e}")


async def demo_full_workflow() -> None:
    """Demo 5: Complete workflow with multiple plugins."""
    print_section("DEMO 5: Multi-Plugin Workflow")

    print("Example: Scrape docs -> Create skill -> Upload to Claude")
    print("\n```bash")
    print("# 1. Scrape Stripe API docs")
    print("nexus firecrawl scrape https://docs.stripe.com/api")
    print("")
    print("# 2. Content saved to: /workspace/scraped/docs_stripe_com/api.md")
    print("")
    print("# 3. Generate skill from scraped content")
    print("nexus skill-seekers generate \\")
    print("  --input /workspace/scraped/docs_stripe_com/api.md \\")
    print("  --name stripe-api")
    print("")
    print("# 4. Upload skill to Claude")
    print("nexus anthropic upload-skill stripe-api")
    print("```")

    print("\n🔗 All plugins work together through NexusFS!")


async def main() -> None:
    """Run all demos."""
    print("=" * 70)
    print("  NEXUS INTEGRATION DEMO")
    print("  How Firecrawl Plugin Works with Nexus")
    print("=" * 70)

    if NEXUS_AVAILABLE:
        print("\n✅ Nexus detected - running full demos")
    else:
        print("\n⚠️  Nexus not installed - showing concepts")
        print("   Install with: pip install nexus-ai-fs")

    api_key = os.getenv("FIRECRAWL_API_KEY")
    if api_key:
        print("✅ Firecrawl API key detected")
    else:
        print("⚠️  FIRECRAWL_API_KEY not set - limited demos")

    # Run all demos
    await demo_plugin_discovery()
    await demo_cli_execution()
    await demo_nexusfs_integration()
    await demo_programmatic_usage()
    await demo_full_workflow()

    # Summary
    print_section("SUMMARY")
    print("The Firecrawl plugin integrates with Nexus through:")
    print("  1. ✅ Entry point registration (discovered automatically)")
    print("  2. ✅ CLI command registration (nexus firecrawl <command>)")
    print("  3. ✅ NexusFS integration (content saved to filesystem)")
    print("  4. ✅ Configuration system (~/.nexus/plugins/firecrawl/)")
    print("  5. ✅ Plugin API (programmatic usage)")
    print("  6. ✅ Multi-plugin workflows (works with other plugins)")

    print("\n📚 For more information:")
    print("   • README.md - Usage guide")
    print("   • INTEGRATION.md - Integration details")
    print("   • EXAMPLES.md - Code examples")

    print("\n" + "=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
