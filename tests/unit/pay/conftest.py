"""Shared fixtures for Nexus Pay unit tests.

Provides common mock services and test app configurations
used across pay router and SDK tests.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def mock_credits_service():
    """Mock CreditsService with sensible defaults."""
    service = AsyncMock()
    service.get_balance = AsyncMock(return_value=Decimal("100.0"))
    service.get_balance_with_reserved = AsyncMock(return_value=(Decimal("100.0"), Decimal("5.0")))
    service.transfer = AsyncMock(return_value="tx-123")
    service.topup = AsyncMock(return_value="topup-123")
    service.reserve = AsyncMock(return_value="res-123")
    service.commit_reservation = AsyncMock()
    service.release_reservation = AsyncMock()
    service.deduct_fast = AsyncMock(return_value=True)
    service.check_budget = AsyncMock(return_value=True)
    service.transfer_batch = AsyncMock(return_value=["tx-1", "tx-2"])
    service.provision_wallet = AsyncMock()
    return service


@pytest.fixture
def mock_x402_client():
    """Mock X402Client for testing."""
    client = MagicMock()
    client.network = "base"
    client.facilitator_url = "https://x402.org/facilitator"
    client.wallet_address = "0x1234567890123456789012345678901234567890"
    return client


@pytest.fixture
def mock_auth_result():
    """Mock auth result dict matching require_auth output."""
    return {
        "authenticated": True,
        "subject_type": "agent",
        "subject_id": "test-agent",
        "zone_id": "default",
        "is_admin": False,
        "x_agent_id": None,
        "metadata": {},
    }
