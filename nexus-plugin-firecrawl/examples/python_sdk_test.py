#!/usr/bin/env python3
"""Python SDK test examples for nexus-plugin-firecrawl.

This demonstrates how to use the Firecrawl client directly in Python code.

Requirements:
    pip install nexus-plugin-firecrawl

Usage:
    export FIRECRAWL_API_KEY="fc-your-api-key-here"
    python examples/python_sdk_test.py
"""

import asyncio
import json
import os
import sys
from pathlib import Path

# Add src to path for local development
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nexus_firecrawl import FirecrawlClient


async def test_scrape() -> bool:
    """Test 1: Scrape a single URL."""
    print("\n" + "=" * 60)
    print("Test 1: Scraping a Single URL")
    print("=" * 60)

    api_key = os.getenv("FIRECRAWL_API_KEY")
    if not api_key:
        print("❌ Error: FIRECRAWL_API_KEY environment variable not set")
        return False

    try:
        async with FirecrawlClient(api_key=api_key) as client:
            print("📄 Scraping: https://example.com")
            result = await client.scrape("https://example.com")

            if result.success:
                print("✅ Success!")
                print(f"   Title: {result.metadata.get('title', 'N/A')}")
                print(f"   Content length: {len(result.markdown or '')} characters")
                print("\n   First 200 characters:")
                print(f"   {(result.markdown or '')[:200]}...")
                return True
            else:
                print("❌ Scraping failed")
                return False

    except Exception as e:
        print(f"❌ Error: {e}")
        return False


async def test_map() -> bool:
    """Test 2: Map URLs on a website."""
    print("\n" + "=" * 60)
    print("Test 2: Mapping URLs")
    print("=" * 60)

    api_key = os.getenv("FIRECRAWL_API_KEY")
    if not api_key:
        print("❌ Error: FIRECRAWL_API_KEY environment variable not set")
        return False

    try:
        async with FirecrawlClient(api_key=api_key) as client:
            print("🔍 Mapping URLs: https://example.com")
            result = await client.map_url("https://example.com", limit=20)

            if result.success:
                print("✅ Success!")
                print(f"   Found {len(result.links)} URLs:")
                for i, link in enumerate(result.links[:10], 1):
                    print(f"   {i:2d}. {link}")

                if len(result.links) > 10:
                    print(f"   ... and {len(result.links) - 10} more")
                return True
            else:
                print("❌ Mapping failed")
                return False

    except Exception as e:
        print(f"❌ Error: {e}")
        return False


async def test_crawl() -> bool:
    """Test 3: Crawl a website (small scale)."""
    print("\n" + "=" * 60)
    print("Test 3: Crawling a Website")
    print("=" * 60)
    print("⚠️  Note: This uses API credits. Limiting to 3 pages.")

    api_key = os.getenv("FIRECRAWL_API_KEY")
    if not api_key:
        print("❌ Error: FIRECRAWL_API_KEY environment variable not set")
        return False

    try:
        async with FirecrawlClient(api_key=api_key) as client:
            print("🕷️  Starting crawl: https://example.com")
            job = await client.crawl("https://example.com", limit=3)

            print(f"   Job ID: {job.id}")
            print(f"   Status: {job.status}")

            # Poll for completion
            max_wait = 30  # seconds
            waited = 0
            while job.status not in ["completed", "failed"] and waited < max_wait:
                await asyncio.sleep(2)
                waited += 2
                job = await client.get_crawl_status(job.id)
                print(f"   Status: {job.status} ({job.completed or 0}/{job.total or '?'} pages)")

            if job.status == "completed":
                print("✅ Crawl completed!")
                print(f"   Total pages: {len(job.data)}")
                for i, page in enumerate(job.data[:3], 1):
                    print(f"   {i}. {page.get('url', 'N/A')}")
                return True
            else:
                print(f"⚠️  Crawl status: {job.status}")
                return False

    except Exception as e:
        print(f"❌ Error: {e}")
        return False


async def test_json_output() -> bool:
    """Test 4: JSON output format (for piping)."""
    print("\n" + "=" * 60)
    print("Test 4: JSON Output Format")
    print("=" * 60)

    api_key = os.getenv("FIRECRAWL_API_KEY")
    if not api_key:
        print("❌ Error: FIRECRAWL_API_KEY environment variable not set")
        return False

    try:
        async with FirecrawlClient(api_key=api_key) as client:
            print("📋 Scraping for JSON output: https://example.com")
            result = await client.scrape("https://example.com")

            if result.success:
                # Format as JSON (like the pipe command does)
                output = {
                    "url": "https://example.com",
                    "content": result.markdown,
                    "title": result.metadata.get("title", ""),
                    "description": result.metadata.get("description", ""),
                    "metadata": result.metadata,
                }

                print("✅ JSON output generated:")
                print(json.dumps(output, indent=2)[:500] + "...")
                return True
            else:
                print("❌ Scraping failed")
                return False

    except Exception as e:
        print(f"❌ Error: {e}")
        return False


async def main() -> None:
    """Run all SDK tests."""
    print("=" * 60)
    print("Firecrawl Plugin - Python SDK Tests")
    print("=" * 60)

    # Check API key
    if not os.getenv("FIRECRAWL_API_KEY"):
        print("\n❌ Error: FIRECRAWL_API_KEY environment variable not set")
        print("\nUsage:")
        print("  export FIRECRAWL_API_KEY='fc-your-api-key-here'")
        print("  python examples/python_sdk_test.py")
        sys.exit(1)

    # Run tests
    results = []
    results.append(await test_scrape())
    results.append(await test_map())
    results.append(await test_json_output())

    # Optional: Uncomment to test crawling (uses more credits)
    # results.append(await test_crawl())

    # Summary
    print("\n" + "=" * 60)
    print("Test Summary")
    print("=" * 60)
    passed = sum(results)
    total = len(results)
    print(f"Passed: {passed}/{total}")

    if passed == total:
        print("✅ All tests passed!")
        sys.exit(0)
    else:
        print("❌ Some tests failed")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
