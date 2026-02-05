"""Pytest configuration for TigerBeetle integration tests.

These tests require:
1. TigerBeetle Python client: pip install tigerbeetle
2. TigerBeetle server running:
    docker compose --profile pay up -d tigerbeetle

Or use the full test profile:
    docker compose --profile test up -d
"""

from __future__ import annotations

import os

import pytest

# TigerBeetle connection settings
TIGERBEETLE_ADDRESS = os.environ.get("TIGERBEETLE_ADDRESS", "127.0.0.1:3000")
TIGERBEETLE_CLUSTER_ID = int(os.environ.get("TIGERBEETLE_CLUSTER_ID", "0"))


def is_tigerbeetle_module_available() -> bool:
    """Check if TigerBeetle Python client is installed."""
    try:
        import tigerbeetle  # noqa: F401

        return True
    except ImportError:
        return False


def is_tigerbeetle_server_available() -> bool:
    """Check if TigerBeetle server is running and accessible."""
    try:
        import socket

        host, port = TIGERBEETLE_ADDRESS.split(":")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        result = sock.connect_ex((host, int(port)))
        sock.close()
        return result == 0
    except Exception:
        return False


def get_skip_reason() -> str | None:
    """Get reason for skipping tests, or None if tests should run."""
    if not is_tigerbeetle_module_available():
        return "TigerBeetle Python client not installed. Run: pip install tigerbeetle"
    if not is_tigerbeetle_server_available():
        return (
            f"TigerBeetle server not available at {TIGERBEETLE_ADDRESS}. "
            "Run: docker compose --profile pay up -d tigerbeetle"
        )
    return None


_skip_reason = get_skip_reason()

# Skip all tests in this module if TigerBeetle is not available
pytestmark = pytest.mark.skipif(
    _skip_reason is not None,
    reason=_skip_reason or "",
)


@pytest.fixture
def tigerbeetle_address() -> str:
    """Get TigerBeetle address."""
    return TIGERBEETLE_ADDRESS


@pytest.fixture
def tigerbeetle_cluster_id() -> int:
    """Get TigerBeetle cluster ID."""
    return TIGERBEETLE_CLUSTER_ID
