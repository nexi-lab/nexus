# Quick Start Guide

## ⚠️ Important: CLI Commands Not Available Yet

The `nexus firecrawl` CLI commands don't work yet due to a bug in Nexus core. **But the plugin itself works perfectly!**

Use these Python wrapper scripts instead:

## Installation

```bash
# 1. Install the plugin
cd nexus-plugin-firecrawl
pip install -e .

# 2. Set your API key
export FIRECRAWL_API_KEY="fc-9771e78213894311b30051d354c5dee3"

# 3. Verify
nexus plugins list  # Should show 'firecrawl'
```

## Usage (Working Methods)

### Method 1: Python Wrapper Scripts ✅ **RECOMMENDED**

```bash
# Scrape a single page
python examples/scrape_cli.py https://example.com

# Scrape with output file
python examples/scrape_cli.py https://docs.stripe.com/api --output api.md

# Crawl a website
python examples/crawl_cli.py https://docs.stripe.com --max-pages 50
```

**These work perfectly and save to NexusFS automatically!**

### Method 2: Python SDK ✅

```python
import asyncio
import os
from nexus import connect
from nexus_firecrawl import FirecrawlPlugin

async def scrape_example():
    # Connect to Nexus
    nx = connect()

    # Create plugin
    plugin = FirecrawlPlugin(nexus_fs=nx)
    await plugin.initialize({"api_key": os.getenv("FIRECRAWL_API_KEY")})

    # Scrape (auto-saves to NexusFS)
    await plugin.scrape("https://example.com", save_to_nexus=True)

    print("✓ Content saved to /workspace/scraped/example_com/index.md")

asyncio.run(scrape_example())
```

### Method 3: Direct API Client ✅

```python
import asyncio
from nexus_firecrawl import FirecrawlClient

async def main():
    async with FirecrawlClient(api_key="fc-xxx") as client:
        result = await client.scrape("https://example.com")
        print(result.markdown)

asyncio.run(main())
```

## What Works ✅

- ✅ Plugin installation
- ✅ Plugin discovery (`nexus plugins list`)
- ✅ Python wrapper scripts
- ✅ Python SDK
- ✅ NexusFS integration (auto-save)
- ✅ All 6 commands (via Python)
- ✅ Real API integration
- ✅ All tests passing

## What Doesn't Work ❌

- ❌ CLI commands: `nexus firecrawl scrape` (Nexus core bug)

## Accessing Scraped Content

```bash
# List scraped content
nexus ls /workspace/scraped/

# Read a file
nexus cat /workspace/scraped/example_com/index.md

# Search across content
nexus grep "authentication" /workspace/scraped/
```

## Examples

### Example 1: Scrape Documentation

```bash
python examples/scrape_cli.py https://docs.stripe.com/api
# Saves to: /workspace/scraped/docs_stripe_com/api.md
```

### Example 2: Crawl a Site

```bash
python examples/crawl_cli.py https://docs.stripe.com --max-pages 50
# Saves to: /workspace/crawled/docs.stripe.com/...
```

### Example 3: Use with Other Plugins

```bash
# 1. Scrape docs
python examples/scrape_cli.py https://docs.stripe.com/api

# 2. Generate skill
nexus skill-seekers generate \
  --input /workspace/scraped/docs_stripe_com/api.md \
  --name stripe-api

# 3. Upload to Claude
nexus anthropic upload-skill stripe-api
```

## Verified Working ✅

Just tested successfully:
```bash
$ python examples/scrape_cli.py https://example.com
Scraping: https://example.com
✓ Done!

# Content automatically saved to NexusFS!
```

## Storage Locations

- **Scrape**: `/workspace/scraped/`
- **Crawl**: `/workspace/crawled/`

URLs are converted to paths:
- `https://example.com` → `/workspace/scraped/example_com/index.md`
- `https://docs.stripe.com/api` → `/workspace/scraped/docs_stripe_com/api.md`

## Troubleshooting

### Python Wrapper Fails

```bash
# Make sure plugin is installed
pip list | grep nexus-plugin-firecrawl

# Reinstall if needed
pip install -e .
```

### API Key Error

```bash
# Set the environment variable
export FIRECRAWL_API_KEY="fc-9771e78213894311b30051d354c5dee3"

# Or add to ~/.bashrc or ~/.zshrc
echo 'export FIRECRAWL_API_KEY="fc-xxx"' >> ~/.bashrc
```

### NexusFS Not Found

```bash
# Make sure you're in a Nexus workspace or have Nexus initialized
nexus init ./my-workspace
cd my-workspace
```

## Complete Workflow Example

```bash
# Set API key
export FIRECRAWL_API_KEY="fc-9771e78213894311b30051d354c5dee3"

# Scrape Stripe API docs
python examples/scrape_cli.py https://docs.stripe.com/api

# Verify content is saved
nexus ls /workspace/scraped/docs_stripe_com/

# Read the content
nexus cat /workspace/scraped/docs_stripe_com/api.md

# Search for specific terms
nexus grep "authentication" /workspace/scraped/

# Use with skill-seekers to create a Claude skill
nexus skill-seekers generate \
  --input /workspace/scraped/docs_stripe_com/api.md \
  --name stripe-api

# Upload to Claude
nexus anthropic upload-skill stripe-api
```

## Summary

| Feature | Status | How to Use |
|---------|--------|------------|
| Plugin installation | ✅ Works | `pip install -e .` |
| Plugin discovery | ✅ Works | `nexus plugins list` |
| Scraping | ✅ Works | `python examples/scrape_cli.py <url>` |
| Crawling | ✅ Works | `python examples/crawl_cli.py <url>` |
| NexusFS storage | ✅ Works | Automatic |
| Python SDK | ✅ Works | See examples/ |
| CLI commands | ❌ Broken | Use Python wrappers |

**Bottom line:** Everything works except the `nexus firecrawl` CLI commands. Use the Python wrapper scripts instead!

## Next Steps

1. **Start scraping**: `python examples/scrape_cli.py <url>`
2. **Check examples**: See `examples/` directory
3. **Read docs**: Check `HOW_IT_WORKS.md`, `INTEGRATION.md`
4. **Build workflows**: Combine with other plugins

The plugin is **production-ready** for Python/SDK usage! 🎉
