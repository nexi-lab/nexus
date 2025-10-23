"""Tests for Firecrawl plugin."""

import pytest

from nexus_firecrawl.plugin import FirecrawlPlugin


def test_plugin_metadata() -> None:
    """Test plugin metadata."""
    plugin = FirecrawlPlugin()
    metadata = plugin.metadata()

    assert metadata.name == "firecrawl"
    assert metadata.version == "0.1.0"
    assert "scraping" in metadata.description.lower()


def test_plugin_commands() -> None:
    """Test plugin commands registration."""
    plugin = FirecrawlPlugin()
    commands = plugin.commands()

    assert "scrape" in commands
    assert "crawl" in commands
    assert "map" in commands
    assert "search" in commands
    assert "extract" in commands
    assert "pipe" in commands


def test_url_to_path() -> None:
    """Test URL to path conversion."""
    plugin = FirecrawlPlugin()

    # Test simple domain
    path = plugin._url_to_path("https://example.com")
    assert path == "example_com/index.md"

    # Test with path
    path = plugin._url_to_path("https://docs.stripe.com/api/charges")
    assert path == "docs_stripe_com/api_charges.md"

    # Test with trailing slash
    path = plugin._url_to_path("https://example.com/docs/")
    assert path == "example_com/docs.md"


def test_get_client_missing_api_key() -> None:
    """Test error handling when API key is missing."""
    plugin = FirecrawlPlugin()

    with pytest.raises(ValueError, match="API key not found"):
        plugin._get_client()


def test_get_client_with_config() -> None:
    """Test client creation with config."""
    plugin = FirecrawlPlugin()
    plugin._config = {
        "api_key": "test-key",
        "base_url": "https://api.test.com",
        "timeout": 30,
        "max_retries": 5,
    }

    client = plugin._get_client()

    assert client.api_key == "test-key"
    assert client.base_url == "https://api.test.com"
    assert client.timeout == 30
    assert client.max_retries == 5


def test_is_enabled() -> None:
    """Test plugin enabled status."""
    plugin = FirecrawlPlugin()

    assert plugin.is_enabled() is True

    plugin.disable()
    assert plugin.is_enabled() is False

    plugin.enable()
    assert plugin.is_enabled() is True
