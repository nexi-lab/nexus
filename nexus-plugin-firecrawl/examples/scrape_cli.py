#!/usr/bin/env python3
"""CLI wrapper for Firecrawl scrape command.

Usage:
    python scrape_cli.py <url> [--output FILE]

Examples:
    python scrape_cli.py https://example.com
    python scrape_cli.py https://docs.stripe.com/api --output stripe-api.md
"""

import asyncio
import os
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nexus import connect

from nexus_firecrawl import FirecrawlPlugin


async def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ["-h", "--help"]:
        print(__doc__)
        sys.exit(0)

    url = sys.argv[1]
    output = None

    # Parse --output flag
    if "--output" in sys.argv:
        idx = sys.argv.index("--output")
        if idx + 1 < len(sys.argv):
            output = sys.argv[idx + 1]

    # Get API key
    api_key = os.getenv("FIRECRAWL_API_KEY")
    if not api_key:
        print("Error: FIRECRAWL_API_KEY environment variable not set")
        print("")
        print("Set it with:")
        print("  export FIRECRAWL_API_KEY='fc-your-key-here'")
        sys.exit(1)

    # Initialize Nexus and plugin
    print(f"Scraping: {url}")
    nx = connect()
    plugin = FirecrawlPlugin(nexus_fs=nx)  # type: ignore[arg-type]
    await plugin.initialize({"api_key": api_key})

    # Scrape
    await plugin.scrape(url, output=output, save_to_nexus=True)

    print("✓ Done!")


if __name__ == "__main__":
    asyncio.run(main())
