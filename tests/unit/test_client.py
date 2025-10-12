"""Tests for Nexus client."""

import pytest

from nexus import NexusClient


@pytest.mark.asyncio
async def test_client_initialization():
    """Test client can be initialized."""
    client = NexusClient(api_key="test_key", base_url="http://localhost:8080")
    assert client.api_key == "test_key"
    assert client.base_url == "http://localhost:8080"


@pytest.mark.asyncio
async def test_client_context_manager():
    """Test client works as context manager."""
    async with NexusClient(api_key="test_key") as client:
        assert client._client is not None
