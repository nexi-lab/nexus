"""Pytest configuration and fixtures."""

import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def temp_dir():
    """Create a temporary directory for tests."""
    tmp = tempfile.mkdtemp()
    yield Path(tmp)
    shutil.rmtree(tmp)


@pytest.fixture
def sample_data():
    """Sample test data."""
    return {
        "text_file": b"Hello, Nexus!",
        "json_file": b'{"key": "value"}',
        "binary_file": bytes(range(256)),
    }


@pytest.fixture
async def nexus_client():
    """Create a test Nexus client."""
    # TODO: Implement when client is ready
    pass
