# Nexus Plugin: Firecrawl

Production-grade web scraping integration for Nexus using [Firecrawl](https://firecrawl.dev).

> **🚀 Quick Start**: See [QUICK_START.md](./QUICK_START.md) for immediate usage (working Python wrappers)
>
> **⚠️  CLI Note**: The `nexus firecrawl` commands don't work yet due to a Nexus core bug, but the plugin itself works perfectly via Python wrappers.
>
> **📚 How It Works**: See [HOW_IT_WORKS.md](./HOW_IT_WORKS.md) for architecture and integration details

## Features

- **Single URL Scraping**: Scrape individual pages with JS rendering and anti-bot protection
- **Website Crawling**: Crawl entire websites with configurable depth and filtering
- **URL Mapping**: Discover all URLs on a website
- **Web Search**: Search and scrape results
- **Structured Extraction**: Extract data using JSON schemas
- **Unix Pipe Support**: Compose with other Nexus commands via pipes
- **NexusFS Integration**: Automatically save scraped content to Nexus filesystem

### Why Firecrawl?

Traditional web scraping with BeautifulSoup fails on modern websites. Firecrawl provides:

- ✅ JavaScript rendering (Puppeteer/Playwright)
- ✅ Anti-bot detection handling
- ✅ Proxy rotation
- ✅ PDF and complex document support
- ✅ LLM-ready markdown output
- ✅ Batch processing

## Installation

```bash
# Install the plugin
pip install nexus-plugin-firecrawl

# Or install from source
cd nexus-plugin-firecrawl
pip install -e .
```

## Configuration

Set your Firecrawl API key:

```bash
export FIRECRAWL_API_KEY="fc-your-api-key-here"
```

Or configure via Nexus plugin settings:

```yaml
# ~/.nexus/plugins/firecrawl/config.yaml
api_key: fc-your-api-key-here
base_url: https://api.firecrawl.dev/v1
timeout: 60
max_retries: 3
```

Get your API key at [firecrawl.dev](https://firecrawl.dev).

## Quick Start

```bash
# 1. Set your API key
export FIRECRAWL_API_KEY="fc-your-api-key-here"

# 2. Scrape a single page
nexus firecrawl scrape https://example.com

# 3. Crawl a documentation site
nexus firecrawl crawl https://docs.stripe.com --max-pages 20

# 4. Use in pipelines
nexus firecrawl pipe https://docs.stripe.com/api | jq '.content'
```

See [examples/](./examples/) for Python SDK usage and more examples.

## Usage

### 1. Scrape a Single URL

```bash
# Basic scraping
nexus firecrawl scrape https://docs.stripe.com/api

# Save to specific file
nexus firecrawl scrape https://docs.stripe.com/api --output stripe-api.md

# Get JSON output
nexus firecrawl scrape https://docs.stripe.com/api --json
```

### 2. Crawl a Website

```bash
# Crawl entire site (up to 100 pages)
nexus firecrawl crawl https://docs.stripe.com --max-pages 100

# Crawl with path filtering
nexus firecrawl crawl https://docs.stripe.com \
  --include-paths "/api/**" \
  --exclude-paths "/blog/**" \
  --max-pages 50

# Control crawl depth
nexus firecrawl crawl https://example.com --max-depth 2 --max-pages 50
```

### 3. Map URLs

Discover all URLs on a website:

```bash
# Map all URLs
nexus firecrawl map https://docs.stripe.com

# Filter with search
nexus firecrawl map https://docs.stripe.com --search "payment"
```

### 4. Web Search

```bash
# Search and scrape results
nexus firecrawl search "stripe api python" --limit 5

# Search without scraping
nexus firecrawl search "documentation" --no-scrape
```

### 5. Structured Data Extraction

Extract structured data using JSON schema:

```bash
# Create schema file
cat > schema.json <<EOF
{
  "type": "object",
  "properties": {
    "title": {"type": "string"},
    "author": {"type": "string"},
    "publishDate": {"type": "string"}
  }
}
EOF

# Extract with schema
nexus firecrawl extract https://example.com/article --schema schema.json

# Or use inline schema
nexus firecrawl extract https://example.com/article --schema '{"type":"object","properties":{"title":{"type":"string"}}}'
```

### 6. Unix Pipe Mode

Compose with other commands:

```bash
# Pipe to skill creation (when available)
nexus firecrawl pipe https://docs.stripe.com/api | \
  nexus skills create-from-web --name stripe-api

# Multi-stage pipeline
nexus firecrawl pipe https://docs.stripe.com/api | \
  jq '.content' | \
  nexus skills create-from-web | \
  nexus anthropic upload-skill

# Batch processing
cat urls.txt | while read url; do
  nexus firecrawl pipe "$url" >> scraped.jsonl
done
```

## Python SDK Usage

Use the Firecrawl client directly in your Python code:

```python
import asyncio
from nexus_firecrawl import FirecrawlClient

async def main():
    # Simple scraping
    async with FirecrawlClient(api_key="fc-your-key") as client:
        result = await client.scrape("https://example.com")
        print(result.markdown)

        # Crawl a website
        job = await client.crawl("https://docs.example.com", limit=50)
        while job.status not in ["completed", "failed"]:
            await asyncio.sleep(2)
            job = await client.get_crawl_status(job.id)

        print(f"Crawled {len(job.data)} pages")

asyncio.run(main())
```

See [examples/python_sdk_test.py](./examples/python_sdk_test.py) for complete examples.

## How It Works with Nexus

The Firecrawl plugin **integrates directly into Nexus**:

- ✅ Discovered automatically via entry points
- ✅ Commands available as `nexus firecrawl <command>`
- ✅ Content saved to NexusFS (not just files)
- ✅ Works with other plugins (skill-seekers, anthropic, etc.)
- ✅ Supports Unix pipelines

See [HOW_IT_WORKS.md](./HOW_IT_WORKS.md) for architecture diagrams and detailed explanations.

## Examples

See the [examples](./examples) directory for:

- `python_sdk_test.py` - Python SDK usage examples
- `cli_test.sh` - CLI command examples script
- `nexus_integration_demo.py` - Full Nexus integration demo

Or read:
- [INTEGRATION.md](./INTEGRATION.md) - Integration guide
- [EXAMPLES.md](./EXAMPLES.md) - Examples documentation

## Configuration Options

| Option | Environment Variable | Default | Description |
|--------|---------------------|---------|-------------|
| `api_key` | `FIRECRAWL_API_KEY` | None | Firecrawl API key (required) |
| `base_url` | - | `https://api.firecrawl.dev/v1` | API base URL |
| `timeout` | - | `60` | Request timeout (seconds) |
| `max_retries` | - | `3` | Maximum retry attempts |

## Commands Reference

### `scrape`

Scrape a single URL.

**Arguments:**
- `url` (required) - URL to scrape
- `--output PATH` - Output file path
- `--json` - Output as JSON
- `--api-key KEY` - Override API key
- `--no-main-content` - Include all page content
- `--no-save-nexus` - Don't save to NexusFS

### `crawl`

Crawl a website.

**Arguments:**
- `url` (required) - Base URL to crawl
- `--max-pages N` - Maximum pages (default: 100)
- `--max-depth N` - Maximum crawl depth
- `--include-paths PATTERNS` - Include URL patterns
- `--exclude-paths PATTERNS` - Exclude URL patterns
- `--output-dir PATH` - Output directory
- `--api-key KEY` - Override API key

### `map`

Map all URLs on a website.

**Arguments:**
- `url` (required) - Base URL to map
- `--search QUERY` - Filter URLs by search query
- `--limit N` - Maximum URLs (default: 5000)
- `--api-key KEY` - Override API key

### `search`

Search the web and scrape results.

**Arguments:**
- `query` (required) - Search query
- `--limit N` - Number of results (default: 10)
- `--no-scrape` - Don't scrape results
- `--save-nexus` - Save to NexusFS
- `--api-key KEY` - Override API key

### `extract`

Extract structured data using a schema.

**Arguments:**
- `url` (required) - URL to extract from
- `--schema SCHEMA` - JSON schema file or inline JSON
- `--prompt PROMPT` - Extraction prompt
- `--api-key KEY` - Override API key

### `pipe`

Scrape and output JSON for piping.

**Arguments:**
- `url` (required) - URL to scrape
- `--api-key KEY` - Override API key

## Testing

### Unit Tests

Run the automated test suite:

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run all unit tests
pytest tests/

# Run with coverage
pytest --cov=nexus_firecrawl tests/

# Run specific test
pytest tests/test_client.py::test_scrape_success
```

### Integration Tests

Test with real API:

```bash
# Set your API key
export FIRECRAWL_API_KEY="fc-your-api-key-here"

# Test Python SDK
python examples/python_sdk_test.py

# Test CLI (requires Nexus installed)
./examples/cli_test.sh
```

**Note:** Integration tests use real API credits. The Python SDK test uses minimal credits (~3 scrapes).

## Development

```bash
# Install in editable mode
pip install -e ".[dev]"

# Format code
ruff format src/ tests/

# Lint
ruff check src/ tests/

# Type check
mypy src/
```

## Troubleshooting

### API Key Issues

```bash
# Verify API key is set
echo $FIRECRAWL_API_KEY

# Test with explicit key
nexus firecrawl scrape https://example.com --api-key "fc-xxx"
```

### Rate Limiting

Firecrawl has rate limits. If you hit limits:

1. Reduce `--max-pages` for crawls
2. Add delays between requests
3. Upgrade your Firecrawl plan

### Timeout Errors

For slow pages, increase timeout:

```python
# In plugin config
timeout: 120  # seconds

# Or programmatically
client = FirecrawlClient(api_key="fc-xxx", timeout=120)
```

## Contributing

Contributions welcome! Please:

1. Fork the repository
2. Create a feature branch
3. Add tests for new functionality
4. Submit a pull request

## License

Apache-2.0

## Related

- [Firecrawl](https://firecrawl.dev) - The underlying scraping service
- [Nexus](https://github.com/nexi-lab/nexus) - The filesystem and plugin system
- [nexus-plugin-skill-seekers](https://github.com/nexi-lab/nexus-plugin-skill-seekers) - Generate skills from docs

## Support

- [GitHub Issues](https://github.com/nexi-lab/nexus-plugin-firecrawl/issues)
- [Documentation](https://docs.nexus.ai/plugins/firecrawl)
- [Firecrawl Docs](https://docs.firecrawl.dev)
