# Firecrawl Plugin

← [Plugins API](index.md)

Production-grade web scraping plugin powered by Firecrawl.

## Overview

The Firecrawl plugin provides enterprise-level web scraping capabilities:

- **JavaScript rendering**: Full support for dynamic sites (Puppeteer/Playwright)
- **Anti-bot detection**: Handles CAPTCHAs and bot protection
- **Proxy rotation**: Automatic proxy management
- **Complex documents**: PDFs, SPAs, and complex layouts
- **LLM-ready output**: Clean markdown optimized for AI consumption

## Installation

```bash
pip install nexus-plugin-firecrawl
```

## Configuration

Create `~/.nexus/plugins/firecrawl/config.yaml`:

```yaml
api_key: "fc-..."  # Your Firecrawl API key
base_url: "https://api.firecrawl.dev/v1"  # Optional
timeout: 60  # Request timeout in seconds
max_retries: 3  # Maximum retry attempts
```

Or set the environment variable:

```bash
export FIRECRAWL_API_KEY="fc-..."
```

## Commands

### scrape

Scrape a single URL:

```bash
nexus firecrawl scrape <url> [options]
```

**Options:**
- `--output TEXT`: Output file path
- `--json-output`: Output as JSON instead of markdown
- `--api-key TEXT`: Firecrawl API key
- `--only-main-content / --no-only-main-content`: Extract only main content (default: True)
- `--save-to-nexus / --no-save-to-nexus`: Save to NexusFS (default: True)

**Examples:**
```bash
# Scrape and display
nexus firecrawl scrape https://docs.example.com/api

# Save to file
nexus firecrawl scrape https://docs.example.com/api --output api-docs.md

# Save to Nexus
nexus firecrawl scrape https://docs.example.com/api
# → Saves to /workspace/scraped/docs_example_com/api.md

# Get full page (not just main content)
nexus firecrawl scrape https://example.com --no-only-main-content

# JSON output for piping
nexus firecrawl scrape https://example.com --json-output
```

### crawl

Crawl entire website:

```bash
nexus firecrawl crawl <url> [options]
```

**Options:**
- `--max-pages INT`: Maximum pages to crawl (default: 100)
- `--max-depth INT`: Maximum crawl depth
- `--include-paths TEXT`: URL patterns to include (can specify multiple)
- `--exclude-paths TEXT`: URL patterns to exclude (can specify multiple)
- `--api-key TEXT`: Firecrawl API key
- `--save-to-nexus / --no-save-to-nexus`: Save to NexusFS (default: True)
- `--output-dir TEXT`: Output directory for files

**Examples:**
```bash
# Crawl documentation site
nexus firecrawl crawl https://docs.example.com --max-pages 50

# Crawl with path filtering
nexus firecrawl crawl https://site.com \
  --include-paths "/api/**" \
  --include-paths "/docs/**" \
  --exclude-paths "/blog/**"

# Crawl with depth limit
nexus firecrawl crawl https://site.com --max-depth 3

# Save to directory
nexus firecrawl crawl https://site.com --output-dir ./scraped
```

**What it does:**
1. Starts async crawl job
2. Shows progress bar
3. Saves each page as markdown
4. Organizes by domain and path structure

### map

Map all URLs on a website:

```bash
nexus firecrawl map <url> [options]
```

**Options:**
- `--search TEXT`: Filter URLs by search query
- `--limit INT`: Maximum URLs to return (default: 5000)
- `--api-key TEXT`: Firecrawl API key

**Examples:**
```bash
# Map all URLs
nexus firecrawl map https://docs.example.com

# Filter by search term
nexus firecrawl map https://site.com --search "api"

# Pipe to file
nexus firecrawl map https://site.com > urls.txt
```

**Output:**
```
Found 150 URLs

┏━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ #  ┃ URL                                       ┃
┡━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ 1  │ https://docs.example.com/                 │
│ 2  │ https://docs.example.com/getting-started  │
│ 3  │ https://docs.example.com/api              │
...
```

### search

Search the web and optionally scrape results:

```bash
nexus firecrawl search <query> [options]
```

**Options:**
- `--limit INT`: Number of results (default: 10)
- `--scrape / --no-scrape`: Scrape each result (default: True)
- `--api-key TEXT`: Firecrawl API key
- `--save-to-nexus / --no-save-to-nexus`: Save to NexusFS (default: False)

**Examples:**
```bash
# Search and display results
nexus firecrawl search "python async tutorial"

# Search without scraping
nexus firecrawl search "news" --no-scrape

# Search and save to Nexus
nexus firecrawl search "API documentation" --save-to-nexus
```

### extract

Extract structured data using a schema:

```bash
nexus firecrawl extract <url> <schema> [options]
```

**Arguments:**
- `url`: URL to extract from
- `schema`: JSON schema file or inline JSON

**Options:**
- `--prompt TEXT`: Optional extraction prompt
- `--api-key TEXT`: Firecrawl API key

**Examples:**
```bash
# Extract using schema file
nexus firecrawl extract https://example.com/product schema.json

# Extract with inline schema
nexus firecrawl extract https://example.com/product '{
  "type": "object",
  "properties": {
    "title": {"type": "string"},
    "price": {"type": "number"},
    "description": {"type": "string"}
  }
}'

# Extract with custom prompt
nexus firecrawl extract https://example.com schema.json \
  --prompt "Extract product information from this page"
```

### pipe

Optimized for Unix pipelines:

```bash
nexus firecrawl pipe <url>
```

**Example:**
```bash
# Scrape and pipe to another command
nexus firecrawl pipe https://docs.stripe.com/api | jq '.content' > api.md

# Create skill from web content
nexus firecrawl pipe https://docs.example.com/api | \
  nexus skills create-from-web api-skill
```

## Use Cases

### 1. Documentation Scraping

Scrape API documentation for skills:

```bash
# Scrape documentation
nexus firecrawl scrape https://docs.stripe.com/api

# Create skill from it
nexus skills create stripe-api --from-file /workspace/scraped/...
```

### 2. Knowledge Base Building

Crawl entire documentation sites:

```bash
# Crawl docs site
nexus firecrawl crawl https://docs.example.com \
  --max-pages 200 \
  --include-paths "/api/**" \
  --include-paths "/guides/**"

# All pages saved to /workspace/crawled/docs_example_com/
```

### 3. Competitive Analysis

Extract structured data:

```bash
# Extract product info
nexus firecrawl extract https://competitor.com/product '{
  "type": "object",
  "properties": {
    "features": {"type": "array"},
    "pricing": {"type": "object"}
  }
}' > competitor-analysis.json
```

### 4. Content Aggregation

Search and scrape multiple sources:

```bash
# Search for specific topic
nexus firecrawl search "machine learning best practices" --limit 20

# All results scraped and saved
```

## Python API

Use the plugin programmatically:

```python
from nexus import connect
from nexus.plugins import PluginRegistry

nx = connect()
registry = PluginRegistry(nx)
registry.discover()

plugin = registry.get_plugin("firecrawl")

# Scrape a URL
await plugin.scrape("https://docs.example.com/api")

# Crawl a site
await plugin.crawl(
    "https://docs.example.com",
    max_pages=100,
    include_paths=["/api/**"]
)

# Search the web
await plugin.search("python tutorials", limit=10)
```

## Features

### JavaScript Rendering

Firecrawl handles dynamic content:
- React/Vue/Angular apps
- Lazy-loaded content
- AJAX requests
- WebSockets

### Anti-Bot Detection

Automatic handling of:
- CAPTCHAs
- Rate limiting
- Bot detection systems
- Fingerprinting

### Document Types

Supports:
- HTML pages
- Single-page applications (SPAs)
- PDFs
- Complex layouts
- Shadow DOM

### Output Quality

- Clean markdown
- Preserved formatting
- Semantic structure
- LLM-optimized

## Pricing

Firecrawl is a paid service. See [Firecrawl Pricing](https://firecrawl.dev/pricing).

Free tier includes:
- 500 credits/month
- Rate limits apply

## Troubleshooting

### API Key Not Found

```bash
export FIRECRAWL_API_KEY="fc-..."
```

### Rate Limit Errors

Configure retry settings:

```yaml
# ~/.nexus/plugins/firecrawl/config.yaml
max_retries: 5
timeout: 120
```

### Crawl Taking Too Long

Reduce scope:

```bash
nexus firecrawl crawl https://site.com --max-pages 50 --max-depth 2
```

## See Also

- [Skill Seekers Plugin](skill-seekers.md) - Generate skills from scraped content
- [Skills API](../skills/index.md) - Nexus skills system
- [Firecrawl Documentation](https://docs.firecrawl.dev/)

## Source Code

- GitHub: [nexus-plugin-firecrawl](https://github.com/nexi-lab/nexus-plugin-firecrawl)
- PyPI: [nexus-plugin-firecrawl](https://pypi.org/project/nexus-plugin-firecrawl/)
