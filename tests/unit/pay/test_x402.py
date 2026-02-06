"""Tests for x402 protocol integration.

These tests follow TDD principles and verify:
1. X402Client initialization and configuration
2. Data models (X402Receipt, X402PaymentVerification)
3. Payment required response generation (402)
4. Payment verification
5. Outgoing payments (pay external services)
6. HTTP request with automatic 402 handling
7. Webhook processing for credit topups

Tests use mock HTTP responses and mock CreditsService for isolation.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_credits_service():
    """Mock CreditsService for testing."""
    service = AsyncMock()
    service.topup = AsyncMock(return_value="transfer-123")
    service.provision_wallet = AsyncMock()
    return service


@pytest.fixture
def x402_config():
    """Default x402 configuration."""
    return {
        "facilitator_url": "https://x402.org/facilitator",
        "wallet_address": "0x1234567890123456789012345678901234567890",
        "network": "base",
        "webhook_secret": "test-secret-123",
    }


@pytest.fixture
def sample_payment_required():
    """Sample PaymentRequired payload as would be base64 encoded in header."""
    return {
        "amount": "1.00",
        "currency": "USDC",
        "address": "0x1234567890123456789012345678901234567890",
        "network": "eip155:8453",  # Base mainnet CAIP-2
        "description": "API access",
        "validFor": 300,  # 5 minutes
    }


@pytest.fixture
def sample_payment_payload():
    """Sample payment payload as would be sent by client."""
    return {
        "network": "eip155:8453",
        "amount": "1000000",  # 1 USDC in micro units
        "asset": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",  # USDC on Base
        "from": "0xabcdef1234567890abcdef1234567890abcdef12",
        "to": "0x1234567890123456789012345678901234567890",
        "validAfter": 1704067200,
        "validBefore": 1704067500,
        "nonce": "abc123",
        "signature": "0x" + "ab" * 65,
    }


@pytest.fixture
def sample_webhook_payload():
    """Sample x402 webhook payload for payment confirmation."""
    return {
        "event": "payment.confirmed",
        "tx_hash": "0x" + "cd" * 32,
        "network": "eip155:8453",
        "amount": "1000000",
        "currency": "USDC",
        "from": "0xabcdef1234567890abcdef1234567890abcdef12",
        "to": "0x1234567890123456789012345678901234567890",
        "timestamp": "2025-01-01T00:00:00Z",
        "metadata": {
            "agent_id": "agent-123",
            "tenant_id": "default",
        },
        "signature": "valid-signature",
    }


# =============================================================================
# Data Model Tests
# =============================================================================


class TestX402DataModels:
    """Test x402 data models."""

    def test_x402_receipt_creation(self):
        """X402Receipt should store payment confirmation details."""
        from nexus.pay.x402 import X402Receipt

        receipt = X402Receipt(
            tx_hash="0x" + "ab" * 32,
            network="eip155:8453",
            amount=Decimal("1.00"),
            currency="USDC",
            timestamp=datetime.now(UTC),
        )

        assert receipt.tx_hash.startswith("0x")
        assert receipt.network == "eip155:8453"
        assert receipt.amount == Decimal("1.00")
        assert receipt.currency == "USDC"
        assert receipt.timestamp is not None

    def test_x402_payment_verification_valid(self):
        """X402PaymentVerification should represent successful verification."""
        from nexus.pay.x402 import X402PaymentVerification

        verification = X402PaymentVerification(
            valid=True,
            tx_hash="0x" + "ab" * 32,
            amount=Decimal("1.00"),
            error=None,
        )

        assert verification.valid is True
        assert verification.tx_hash is not None
        assert verification.amount == Decimal("1.00")
        assert verification.error is None

    def test_x402_payment_verification_invalid(self):
        """X402PaymentVerification should represent failed verification."""
        from nexus.pay.x402 import X402PaymentVerification

        verification = X402PaymentVerification(
            valid=False,
            tx_hash=None,
            amount=None,
            error="Invalid signature",
        )

        assert verification.valid is False
        assert verification.tx_hash is None
        assert verification.error == "Invalid signature"

    def test_x402_error_exception(self):
        """X402Error should be raisable with message."""
        from nexus.pay.x402 import X402Error

        error = X402Error("Payment failed")
        assert str(error) == "Payment failed"

        with pytest.raises(X402Error, match="Payment failed"):
            raise X402Error("Payment failed")


# =============================================================================
# X402Client Initialization Tests
# =============================================================================


class TestX402ClientInit:
    """Test X402Client initialization."""

    def test_init_with_defaults(self):
        """X402Client should initialize with default Coinbase facilitator."""
        from nexus.pay.x402 import X402Client

        client = X402Client()

        assert client.facilitator_url == "https://x402.org/facilitator"
        assert client.network == "base"
        assert client.wallet_address is None

    def test_init_with_custom_config(self, x402_config):
        """X402Client should accept custom configuration."""
        from nexus.pay.x402 import X402Client

        client = X402Client(
            facilitator_url=x402_config["facilitator_url"],
            wallet_address=x402_config["wallet_address"],
            network=x402_config["network"],
            webhook_secret=x402_config["webhook_secret"],
        )

        assert client.facilitator_url == x402_config["facilitator_url"]
        assert client.wallet_address == x402_config["wallet_address"]
        assert client.network == x402_config["network"]
        assert client._webhook_secret == x402_config["webhook_secret"]

    def test_init_with_solana_network(self):
        """X402Client should support Solana network."""
        from nexus.pay.x402 import X402Client

        client = X402Client(network="solana")

        assert client.network == "solana"

    def test_network_to_caip2_conversion(self):
        """X402Client should convert network names to CAIP-2 format."""
        from nexus.pay.x402 import X402Client

        client = X402Client(network="base")
        assert client.caip2_network == "eip155:8453"

        client_eth = X402Client(network="ethereum")
        assert client_eth.caip2_network == "eip155:1"

        client_sol = X402Client(network="solana")
        assert client_sol.caip2_network == "solana:mainnet"


# =============================================================================
# Payment Required Response Tests
# =============================================================================


class TestPaymentRequiredResponse:
    """Test 402 Payment Required response generation."""

    def test_payment_required_response_status(self, x402_config):
        """payment_required_response should return 402 status."""
        from nexus.pay.x402 import X402Client

        client = X402Client(
            wallet_address=x402_config["wallet_address"],
            network=x402_config["network"],
        )

        response = client.payment_required_response(
            amount=Decimal("1.00"),
            description="API access",
        )

        assert response.status_code == 402

    def test_payment_required_response_header_present(self, x402_config):
        """Response should include X-Payment-Required header."""
        from nexus.pay.x402 import X402Client

        client = X402Client(
            wallet_address=x402_config["wallet_address"],
            network=x402_config["network"],
        )

        response = client.payment_required_response(
            amount=Decimal("1.00"),
            description="API access",
        )

        assert "X-Payment-Required" in response.headers

    def test_payment_required_response_header_is_base64_json(self, x402_config):
        """X-Payment-Required header should be base64-encoded JSON."""
        from nexus.pay.x402 import X402Client

        client = X402Client(
            wallet_address=x402_config["wallet_address"],
            network=x402_config["network"],
        )

        response = client.payment_required_response(
            amount=Decimal("1.00"),
            description="API access",
        )

        header_value = response.headers["X-Payment-Required"]
        decoded = base64.b64decode(header_value).decode()
        payload = json.loads(decoded)

        assert "amount" in payload
        assert "currency" in payload
        assert "address" in payload
        assert "network" in payload

    def test_payment_required_response_contains_correct_data(self, x402_config):
        """Header payload should contain correct payment details."""
        from nexus.pay.x402 import X402Client

        client = X402Client(
            wallet_address=x402_config["wallet_address"],
            network=x402_config["network"],
        )

        response = client.payment_required_response(
            amount=Decimal("5.50"),
            description="Premium API access",
        )

        header_value = response.headers["X-Payment-Required"]
        decoded = base64.b64decode(header_value).decode()
        payload = json.loads(decoded)

        assert payload["amount"] == "5.50"
        assert payload["currency"] == "USDC"
        assert payload["address"] == x402_config["wallet_address"]
        assert payload["description"] == "Premium API access"

    def test_payment_required_response_without_wallet_raises(self):
        """Should raise error if wallet address not configured."""
        from nexus.pay.x402 import X402Client, X402Error

        client = X402Client()  # No wallet address

        with pytest.raises(X402Error, match="wallet address"):
            client.payment_required_response(
                amount=Decimal("1.00"),
                description="API access",
            )


# =============================================================================
# Payment Verification Tests
# =============================================================================


class TestVerifyPayment:
    """Test incoming payment verification."""

    @pytest.mark.asyncio
    async def test_verify_payment_valid(self, x402_config, sample_payment_payload):
        """verify_payment should return valid verification for correct payment."""
        from nexus.pay.x402 import X402Client

        client = X402Client(
            facilitator_url=x402_config["facilitator_url"],
            wallet_address=x402_config["wallet_address"],
            network=x402_config["network"],
        )

        # Mock the facilitator verify endpoint
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = MagicMock(
            return_value={
                "valid": True,
                "tx_hash": "0x" + "ab" * 32,
                "amount": "1000000",
            }
        )

        with patch("httpx.AsyncClient.post", return_value=mock_response):
            payment_header = base64.b64encode(json.dumps(sample_payment_payload).encode()).decode()
            verification = await client.verify_payment(
                payment_header=payment_header,
                expected_amount=Decimal("1.00"),
            )

            assert verification.valid is True
            assert verification.tx_hash is not None
            assert verification.error is None

    @pytest.mark.asyncio
    async def test_verify_payment_invalid_signature(self, x402_config, sample_payment_payload):
        """verify_payment should return invalid for bad signature."""
        from nexus.pay.x402 import X402Client

        client = X402Client(
            facilitator_url=x402_config["facilitator_url"],
            wallet_address=x402_config["wallet_address"],
        )

        mock_response = AsyncMock()
        mock_response.status_code = 400
        mock_response.json = MagicMock(
            return_value={
                "valid": False,
                "error": "Invalid signature",
            }
        )

        with patch("httpx.AsyncClient.post", return_value=mock_response):
            payment_header = base64.b64encode(json.dumps(sample_payment_payload).encode()).decode()
            verification = await client.verify_payment(
                payment_header=payment_header,
                expected_amount=Decimal("1.00"),
            )

            assert verification.valid is False
            assert verification.error is not None

    @pytest.mark.asyncio
    async def test_verify_payment_amount_mismatch(self, x402_config, sample_payment_payload):
        """verify_payment should reject if amount doesn't match expected."""
        from nexus.pay.x402 import X402Client

        client = X402Client(
            facilitator_url=x402_config["facilitator_url"],
            wallet_address=x402_config["wallet_address"],
        )

        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = MagicMock(
            return_value={
                "valid": True,
                "tx_hash": "0x" + "ab" * 32,
                "amount": "500000",  # 0.5 USDC, less than expected
            }
        )

        with patch("httpx.AsyncClient.post", return_value=mock_response):
            payment_header = base64.b64encode(json.dumps(sample_payment_payload).encode()).decode()
            verification = await client.verify_payment(
                payment_header=payment_header,
                expected_amount=Decimal("1.00"),  # Expects 1 USDC
            )

            assert verification.valid is False
            assert "amount" in verification.error.lower()

    @pytest.mark.asyncio
    async def test_verify_payment_invalid_header_format(self, x402_config):
        """verify_payment should handle malformed payment header."""
        from nexus.pay.x402 import X402Client

        client = X402Client(
            facilitator_url=x402_config["facilitator_url"],
            wallet_address=x402_config["wallet_address"],
        )

        verification = await client.verify_payment(
            payment_header="not-valid-base64!!!",
            expected_amount=Decimal("1.00"),
        )

        assert verification.valid is False
        assert verification.error is not None


# =============================================================================
# Outgoing Payment Tests
# =============================================================================


class TestOutgoingPayments:
    """Test outgoing payment operations."""

    @pytest.mark.asyncio
    async def test_pay_success(self, x402_config):
        """pay() should return receipt on successful payment."""
        from nexus.pay.x402 import X402Client

        client = X402Client(
            facilitator_url=x402_config["facilitator_url"],
            wallet_address=x402_config["wallet_address"],
            network=x402_config["network"],
        )

        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = MagicMock(
            return_value={
                "success": True,
                "tx_hash": "0x" + "ab" * 32,
                "network": "eip155:8453",
                "amount": "1000000",
                "timestamp": "2025-01-01T00:00:00Z",
            }
        )

        with patch("httpx.AsyncClient.post", return_value=mock_response):
            receipt = await client.pay(
                to_address="0x9876543210987654321098765432109876543210",
                amount=Decimal("1.00"),
                currency="USDC",
            )

            assert receipt is not None
            assert receipt.tx_hash.startswith("0x")
            assert receipt.amount == Decimal("1.00")

    @pytest.mark.asyncio
    async def test_pay_insufficient_funds(self, x402_config):
        """pay() should raise error on insufficient funds."""
        from nexus.pay.x402 import X402Client, X402Error

        client = X402Client(
            facilitator_url=x402_config["facilitator_url"],
            wallet_address=x402_config["wallet_address"],
        )

        mock_response = AsyncMock()
        mock_response.status_code = 402
        mock_response.json = MagicMock(
            return_value={
                "success": False,
                "error": "Insufficient funds",
            }
        )

        with (
            patch("httpx.AsyncClient.post", return_value=mock_response),
            pytest.raises(X402Error, match="Insufficient"),
        ):
            await client.pay(
                to_address="0x9876543210987654321098765432109876543210",
                amount=Decimal("1000000.00"),
                currency="USDC",
            )


# =============================================================================
# Pay For Request Tests
# =============================================================================


class TestPayForRequest:
    """Test HTTP request with automatic 402 handling."""

    @pytest.mark.asyncio
    async def test_pay_for_request_no_payment_needed(self, x402_config):
        """pay_for_request should return response directly if no 402."""
        from nexus.pay.x402 import X402Client

        client = X402Client(
            facilitator_url=x402_config["facilitator_url"],
            wallet_address=x402_config["wallet_address"],
        )

        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.content = b'{"data": "value"}'
        mock_response.headers = {}

        with patch("httpx.AsyncClient.request", return_value=mock_response):
            response, receipt = await client.pay_for_request(
                url="https://api.example.com/data",
                method="GET",
            )

            assert response.status_code == 200
            assert receipt is None

    @pytest.mark.asyncio
    async def test_pay_for_request_handles_402(self, x402_config, sample_payment_required):
        """pay_for_request should automatically handle 402 responses."""
        from nexus.pay.x402 import X402Client

        client = X402Client(
            facilitator_url=x402_config["facilitator_url"],
            wallet_address=x402_config["wallet_address"],
        )

        # First request returns 402
        mock_402_response = AsyncMock()
        mock_402_response.status_code = 402
        mock_402_response.headers = {
            "X-Payment-Required": base64.b64encode(
                json.dumps(sample_payment_required).encode()
            ).decode()
        }

        # Payment request succeeds
        mock_payment_response = AsyncMock()
        mock_payment_response.status_code = 200
        mock_payment_response.json = MagicMock(
            return_value={
                "success": True,
                "tx_hash": "0x" + "ab" * 32,
                "network": "eip155:8453",
                "amount": "1000000",
                "timestamp": "2025-01-01T00:00:00Z",
            }
        )

        # Retry after payment succeeds
        mock_success_response = AsyncMock()
        mock_success_response.status_code = 200
        mock_success_response.content = b'{"data": "value"}'
        mock_success_response.headers = {}

        with (
            patch("httpx.AsyncClient.request") as mock_request,
            patch("httpx.AsyncClient.post", return_value=mock_payment_response),
        ):
            mock_request.side_effect = [mock_402_response, mock_success_response]

            response, receipt = await client.pay_for_request(
                url="https://api.example.com/paid-data",
                method="GET",
            )

            assert response.status_code == 200
            assert receipt is not None
            assert receipt.tx_hash.startswith("0x")


# =============================================================================
# Webhook Processing Tests
# =============================================================================


class TestWebhookProcessing:
    """Test x402 webhook processing for credit topups."""

    @pytest.mark.asyncio
    async def test_process_topup_webhook_success(
        self, x402_config, sample_webhook_payload, mock_credits_service
    ):
        """process_topup_webhook should credit agent in TigerBeetle."""
        from nexus.pay.x402 import X402Client

        client = X402Client(
            facilitator_url=x402_config["facilitator_url"],
            wallet_address=x402_config["wallet_address"],
            webhook_secret=x402_config["webhook_secret"],
        )

        # Mock webhook signature verification
        with patch.object(client, "_verify_webhook_signature", return_value=True):
            tx_id = await client.process_topup_webhook(
                webhook_payload=sample_webhook_payload,
                credits_service=mock_credits_service,
            )

            assert tx_id == "transfer-123"
            mock_credits_service.topup.assert_called_once()
            call_args = mock_credits_service.topup.call_args
            assert call_args.kwargs["agent_id"] == "agent-123"
            assert call_args.kwargs["source"] == "x402"

    @pytest.mark.asyncio
    async def test_process_topup_webhook_invalid_signature(
        self, x402_config, sample_webhook_payload, mock_credits_service
    ):
        """process_topup_webhook should reject invalid webhook signature."""
        from nexus.pay.x402 import X402Client, X402Error

        client = X402Client(
            facilitator_url=x402_config["facilitator_url"],
            wallet_address=x402_config["wallet_address"],
            webhook_secret=x402_config["webhook_secret"],
        )

        with patch.object(client, "_verify_webhook_signature", return_value=False):
            with pytest.raises(X402Error, match="signature"):
                await client.process_topup_webhook(
                    webhook_payload=sample_webhook_payload,
                    credits_service=mock_credits_service,
                )

            mock_credits_service.topup.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_topup_webhook_missing_agent_id(self, x402_config, mock_credits_service):
        """process_topup_webhook should reject webhook without agent_id."""
        from nexus.pay.x402 import X402Client, X402Error

        client = X402Client(
            facilitator_url=x402_config["facilitator_url"],
            wallet_address=x402_config["wallet_address"],
            webhook_secret=x402_config["webhook_secret"],
        )

        bad_payload = {
            "event": "payment.confirmed",
            "tx_hash": "0x" + "cd" * 32,
            "amount": "1000000",
            "metadata": {},  # Missing agent_id
            "signature": "valid-signature",
        }

        with (
            patch.object(client, "_verify_webhook_signature", return_value=True),
            pytest.raises(X402Error, match="agent_id"),
        ):
            await client.process_topup_webhook(
                webhook_payload=bad_payload,
                credits_service=mock_credits_service,
            )

    @pytest.mark.asyncio
    async def test_process_topup_webhook_provisions_wallet_if_needed(
        self, x402_config, sample_webhook_payload, mock_credits_service
    ):
        """process_topup_webhook should provision wallet before topup."""
        from nexus.pay.x402 import X402Client

        client = X402Client(
            facilitator_url=x402_config["facilitator_url"],
            wallet_address=x402_config["wallet_address"],
            webhook_secret=x402_config["webhook_secret"],
        )

        with patch.object(client, "_verify_webhook_signature", return_value=True):
            await client.process_topup_webhook(
                webhook_payload=sample_webhook_payload,
                credits_service=mock_credits_service,
            )

            mock_credits_service.provision_wallet.assert_called_once_with(
                agent_id="agent-123",
                zone_id="default",
            )


# =============================================================================
# Amount Conversion Tests
# =============================================================================


class TestAmountConversion:
    """Test USDC micro-unit conversion utilities."""

    def test_usdc_to_micro(self):
        """USDC should convert to micro units (6 decimals)."""
        from nexus.pay.x402 import usdc_to_micro

        assert usdc_to_micro(Decimal("1.00")) == 1_000_000
        assert usdc_to_micro(Decimal("0.50")) == 500_000
        assert usdc_to_micro(Decimal("0.000001")) == 1
        assert usdc_to_micro(Decimal("100.123456")) == 100_123_456

    def test_micro_to_usdc(self):
        """Micro units should convert back to USDC."""
        from nexus.pay.x402 import micro_to_usdc

        assert micro_to_usdc(1_000_000) == Decimal("1.00")
        assert micro_to_usdc(500_000) == Decimal("0.50")
        assert micro_to_usdc(1) == Decimal("0.000001")


# =============================================================================
# Configuration Validation Tests
# =============================================================================


class TestConfigValidation:
    """Test x402 configuration validation."""

    def test_validate_wallet_address_valid(self):
        """Valid EVM address should pass validation."""
        from nexus.pay.x402 import validate_wallet_address

        # Valid EVM address (40 hex chars + 0x prefix)
        assert validate_wallet_address("0x1234567890123456789012345678901234567890") is True

    def test_validate_wallet_address_invalid_length(self):
        """Invalid length address should fail validation."""
        from nexus.pay.x402 import validate_wallet_address

        assert validate_wallet_address("0x1234") is False
        assert validate_wallet_address("") is False

    def test_validate_wallet_address_invalid_chars(self):
        """Address with invalid characters should fail validation."""
        from nexus.pay.x402 import validate_wallet_address

        assert validate_wallet_address("0xGGGG567890123456789012345678901234567890") is False

    def test_validate_network_supported(self):
        """Supported networks should pass validation."""
        from nexus.pay.x402 import validate_network

        assert validate_network("base") is True
        assert validate_network("ethereum") is True
        assert validate_network("solana") is True

    def test_validate_network_unsupported(self):
        """Unsupported networks should fail validation."""
        from nexus.pay.x402 import validate_network

        assert validate_network("bitcoin") is False
        assert validate_network("unknown") is False


# =============================================================================
# Edge Cases
# =============================================================================


class TestEdgeCases:
    """Test edge cases and error conditions."""

    @pytest.mark.asyncio
    async def test_verify_payment_network_timeout(self, x402_config, sample_payment_payload):
        """verify_payment should handle network timeouts gracefully."""
        from nexus.pay.x402 import X402Client

        client = X402Client(
            facilitator_url=x402_config["facilitator_url"],
            wallet_address=x402_config["wallet_address"],
        )

        with patch("httpx.AsyncClient.post", side_effect=TimeoutError("Connection timed out")):
            payment_header = base64.b64encode(json.dumps(sample_payment_payload).encode()).decode()
            verification = await client.verify_payment(
                payment_header=payment_header,
                expected_amount=Decimal("1.00"),
            )

            assert verification.valid is False
            assert "timeout" in verification.error.lower() or "error" in verification.error.lower()

    @pytest.mark.asyncio
    async def test_pay_network_error(self, x402_config):
        """pay() should handle network errors gracefully."""
        from nexus.pay.x402 import X402Client, X402Error

        client = X402Client(
            facilitator_url=x402_config["facilitator_url"],
            wallet_address=x402_config["wallet_address"],
        )

        with (
            patch("httpx.AsyncClient.post", side_effect=ConnectionError("Network error")),
            pytest.raises(X402Error, match="network|connection|error"),
        ):
            await client.pay(
                to_address="0x9876543210987654321098765432109876543210",
                amount=Decimal("1.00"),
                currency="USDC",
            )

    def test_payment_required_with_zero_amount(self, x402_config):
        """payment_required_response should handle zero amount."""
        from nexus.pay.x402 import X402Client, X402Error

        client = X402Client(
            wallet_address=x402_config["wallet_address"],
            network=x402_config["network"],
        )

        with pytest.raises(X402Error, match="amount"):
            client.payment_required_response(
                amount=Decimal("0"),
                description="Free tier",
            )

    def test_payment_required_with_negative_amount(self, x402_config):
        """payment_required_response should reject negative amount."""
        from nexus.pay.x402 import X402Client, X402Error

        client = X402Client(
            wallet_address=x402_config["wallet_address"],
            network=x402_config["network"],
        )

        with pytest.raises(X402Error, match="amount"):
            client.payment_required_response(
                amount=Decimal("-1.00"),
                description="Refund",
            )

    @pytest.mark.asyncio
    async def test_webhook_idempotency(
        self, x402_config, sample_webhook_payload, mock_credits_service
    ):
        """Duplicate webhook should be handled idempotently."""
        from nexus.pay.x402 import X402Client

        client = X402Client(
            facilitator_url=x402_config["facilitator_url"],
            wallet_address=x402_config["wallet_address"],
            webhook_secret=x402_config["webhook_secret"],
        )

        with patch.object(client, "_verify_webhook_signature", return_value=True):
            # First call
            await client.process_topup_webhook(
                webhook_payload=sample_webhook_payload,
                credits_service=mock_credits_service,
            )

            # Second call with same payload (duplicate)
            await client.process_topup_webhook(
                webhook_payload=sample_webhook_payload,
                credits_service=mock_credits_service,
            )

            # Should use tx_hash as idempotency key
            assert mock_credits_service.topup.call_count == 2
            # Both calls should use the same external_tx_id
            calls = mock_credits_service.topup.call_args_list
            assert calls[0].kwargs.get("external_tx_id") == calls[1].kwargs.get("external_tx_id")
