#!/usr/bin/env python3
"""CLI wrapper for Firecrawl crawl command.

Usage:
    python crawl_cli.py <url> [--max-pages N]

Examples:
    python crawl_cli.py https://docs.stripe.com
    python crawl_cli.py https://docs.stripe.com --max-pages 50
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
    max_pages = 100

    # Parse --max-pages flag
    if "--max-pages" in sys.argv:
        idx = sys.argv.index("--max-pages")
        if idx + 1 < len(sys.argv):
            max_pages = int(sys.argv[idx + 1])

    # Get API key
    api_key = os.getenv("FIRECRAWL_API_KEY")
    if not api_key:
        print("Error: FIRECRAWL_API_KEY environment variable not set")
        print("")
        print("Set it with:")
        print("  export FIRECRAWL_API_KEY='fc-your-key-here'")
        sys.exit(1)

    # Initialize Nexus and plugin
    print(f"Crawling: {url} (max {max_pages} pages)")
    nx = connect()
    plugin = FirecrawlPlugin(nexus_fs=nx)  # type: ignore[arg-type]
    await plugin.initialize({"api_key": api_key})

    # Crawl
    await plugin.crawl(url, max_pages=max_pages, save_to_nexus=True)

    print("✓ Done!")


if __name__ == "__main__":
    asyncio.run(main())
