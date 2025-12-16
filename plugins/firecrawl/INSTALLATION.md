# Installation Guide

## Quick Install

```bash
# Navigate to the plugin directory
cd nexus-plugin-firecrawl

# Install the plugin
pip install -e .

# Verify installation
nexus plugins list
```

You should see:
```
Installed Plugins
┏━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┓
┃ Name     ┃ Version ┃ Description                            ┃ Status   ┃
┡━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━┩
│ firecrawl│ 0.1.0   │ Production-grade web scraping...       │ ✓ Enabled│
└──────────┴─────────┴────────────────────────────────────────┴──────────┘
```

## Step-by-Step Installation

### 1. Install the Plugin

```bash
cd /Users/tafeng/nexus/nexus-plugin-firecrawl
pip install -e .
```

This installs the plugin in "editable" mode, so any code changes are immediately available.

### 2. Verify Nexus Can See It

```bash
nexus plugins list
```

If you see `firecrawl` in the list, it's installed correctly!

### 3. Check Plugin Details

```bash
nexus plugins info firecrawl
```

Should show:
```
firecrawl v0.1.0
Production-grade web scraping integration using Firecrawl

Author: Nexus Team
Homepage: https://github.com/nexi-lab/nexus-plugin-firecrawl

Commands:
  • nexus firecrawl scrape
  • nexus firecrawl crawl
  • nexus firecrawl map
  • nexus firecrawl search
  • nexus firecrawl extract
  • nexus firecrawl pipe

Status: ✓ Enabled
```

### 4. Configure API Key

```bash
# Option A: Environment variable (recommended)
export FIRECRAWL_API_KEY="fc-9771e78213894311b30051d354c5dee3"

# Option B: Config file
mkdir -p ~/.nexus/plugins/firecrawl
cat > ~/.nexus/plugins/firecrawl/config.yaml << EOF
api_key: fc-9771e78213894311b30051d354c5dee3
base_url: https://api.firecrawl.dev/v1
timeout: 60
max_retries: 3
EOF
```

### 5. Test It Works

```bash
# Simple test
nexus firecrawl scrape https://example.com

# Should output:
# ✓ Saved to NexusFS: /workspace/scraped/example_com/index.md
```

## Troubleshooting

### Plugin Not Found

**Error:** `No such command 'firecrawl'`

**Solution:**
```bash
# Make sure it's installed
pip list | grep nexus-plugin-firecrawl

# If not found, install it
pip install -e .

# Verify Nexus sees it
nexus plugins list
```

### Import Errors

**Error:** `ModuleNotFoundError: No module named 'nexus_firecrawl'`

**Solution:**
```bash
# Reinstall the plugin
pip uninstall nexus-plugin-firecrawl -y
pip install -e .
```

### Entry Point Issues

**Error:** Plugin doesn't show up in `nexus plugins list`

**Solution:**
```bash
# Check entry points are correct
cat pyproject.toml | grep -A 2 "entry-points"

# Should show:
# [project.entry-points."nexus.plugins"]
# firecrawl = "nexus_firecrawl.plugin:FirecrawlPlugin"

# Reinstall to register entry points
pip install -e . --force-reinstall --no-deps
```

### API Key Not Found

**Error:** `Firecrawl API key not found`

**Solution:**
```bash
# Set environment variable
export FIRECRAWL_API_KEY="fc-your-key-here"

# Or create config file
mkdir -p ~/.nexus/plugins/firecrawl
echo "api_key: fc-your-key-here" > ~/.nexus/plugins/firecrawl/config.yaml
```

## Development Installation

If you're developing the plugin:

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest tests/

# Run with coverage
pytest --cov=nexus_firecrawl tests/

# Lint
ruff check src/

# Type check
mypy src/
```

## Uninstalling

```bash
# Uninstall via pip
pip uninstall nexus-plugin-firecrawl

# Or via Nexus
nexus plugins uninstall firecrawl
```

## Verification Checklist

After installation, verify everything works:

- [ ] `pip list | grep nexus-plugin-firecrawl` shows the package
- [ ] `nexus plugins list` shows firecrawl
- [ ] `nexus plugins info firecrawl` shows details
- [ ] `nexus firecrawl scrape https://example.com` works
- [ ] Content appears in NexusFS: `nexus ls /workspace/scraped/`

## Next Steps

Once installed:

1. **Test basic scraping**: `nexus firecrawl scrape https://example.com`
2. **Run examples**: `python examples/python_sdk_test.py`
3. **Read documentation**: Check README.md and HOW_IT_WORKS.md
4. **Try workflows**: Combine with other plugins

## Installation Summary

```bash
# 1. Install
cd nexus-plugin-firecrawl && pip install -e .

# 2. Verify
nexus plugins list

# 3. Configure
export FIRECRAWL_API_KEY="fc-xxx"

# 4. Test
nexus firecrawl scrape https://example.com

# 5. Success!
nexus ls /workspace/scraped/
```

That's it! The plugin is now integrated with Nexus and ready to use.
