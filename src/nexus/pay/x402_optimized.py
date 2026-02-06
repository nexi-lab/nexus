"""Optimized x402 protocol implementation for high performance.

Performance optimizations applied:
1. LRU cache for verified payments (prevents duplicate facilitator calls)
2. Connection pooling with persistent httpx client
3. Pre-computed payment required responses
4. Faster JSON with orjson (fallback to standard json)
5. Compiled regex for address validation

This module provides the same API as x402.py but with better performance.

Related: Issue #1206 (x402 protocol integration)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from starlette.responses import Response

# Try orjson for faster JSON, fallback to standard json
try:
    import orjson

    def json_dumps(obj: Any) -> str:
        return orjson.dumps(obj).decode()

    def json_loads(s: str | bytes) -> Any:
        return orjson.loads(s)

except ImportError:
    import json

    def json_dumps(obj: Any) -> str:
        return json.dumps(obj)

    def json_loads(s: str | bytes) -> Any:
        return json.loads(s)


if TYPE_CHECKING:
    import httpx

    from nexus.pay.credits import CreditsService


# =============================================================================
# Constants
# =============================================================================

USDC_DECIMALS = 6
USDC_SCALE = 10**USDC_DECIMALS
DEFAULT_FACILITATOR_URL = "https://x402.org/facilitator"

# Pre-compiled regex for wallet validation
WALLET_REGEX = re.compile(r"^0x[0-9a-fA-F]{40}$")

# Network mapping (cached)
NETWORK_CAIP2_MAP = {
    "base": "eip155:8453",
    "ethereum": "eip155:1",
    "polygon": "eip155:137",
    "arbitrum": "eip155:42161",
    "optimism": "eip155:10",
    "solana": "solana:mainnet",
}

SUPPORTED_NETWORKS = frozenset(NETWORK_CAIP2_MAP.keys())


# =============================================================================
# Exceptions
# =============================================================================


class X402Error(Exception):
    """Base exception for x402 operations."""

    pass


# =============================================================================
# Data Classes
# =============================================================================


@dataclass(frozen=True, slots=True)
class X402Receipt:
    """Receipt for a completed x402 payment. Immutable and memory-efficient."""

    tx_hash: str
    network: str
    amount: Decimal
    currency: str
    timestamp: datetime


@dataclass(frozen=True, slots=True)
class X402PaymentVerification:
    """Result of verifying an x402 payment. Immutable and memory-efficient."""

    valid: bool
    tx_hash: str | None
    amount: Decimal | None
    error: str | None


# =============================================================================
# Optimized Utility Functions
# =============================================================================


def usdc_to_micro(amount: Decimal) -> int:
    """Convert USDC to micro units. No cache - computation is cheaper than lookup."""
    return int(amount * USDC_SCALE)


def micro_to_usdc(micro: int) -> Decimal:
    """Convert micro units to USDC. No cache - computation is cheaper than lookup."""
    return Decimal(micro) / Decimal(USDC_SCALE)


def validate_wallet_address(address: str) -> bool:
    """Validate EVM wallet address using pre-compiled regex."""
    return bool(address and WALLET_REGEX.match(address))


def validate_network(network: str) -> bool:
    """Validate network using frozenset lookup."""
    return network in SUPPORTED_NETWORKS


# =============================================================================
# Optimized X402Client
# =============================================================================


class X402ClientOptimized:
    """High-performance x402 protocol client.

    Optimizations:
    - Persistent httpx client with connection pooling
    - LRU cache for payment verifications
    - Pre-computed payment required responses
    - Efficient JSON serialization
    """

    # Class-level cache for verified payments
    _verification_cache: dict[str, tuple[X402PaymentVerification, float]] = {}
    _cache_ttl: float = 60.0  # 60 second TTL

    def __init__(
        self,
        facilitator_url: str = DEFAULT_FACILITATOR_URL,
        wallet_address: str | None = None,
        network: str = "base",
        webhook_secret: str | None = None,
        cache_ttl: float = 60.0,
    ):
        self.facilitator_url = facilitator_url
        self.wallet_address = wallet_address
        self.network = network
        self._webhook_secret = webhook_secret
        self._cache_ttl = cache_ttl

        # Pre-compute CAIP-2 network
        self._caip2_network = NETWORK_CAIP2_MAP.get(network, f"eip155:{network}")

        # Lazy-initialized persistent client
        self._http_client: httpx.AsyncClient | None = None

        # Pre-computed base payment required payload (for speed)
        self._base_payment_payload = {
            "currency": "USDC",
            "address": wallet_address,
            "network": self._caip2_network,
        }

    @property
    def caip2_network(self) -> str:
        return self._caip2_network

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create persistent HTTP client with connection pooling."""
        if self._http_client is None:
            import httpx

            self._http_client = httpx.AsyncClient(
                timeout=30.0,
                limits=httpx.Limits(
                    max_keepalive_connections=20,
                    max_connections=100,
                    keepalive_expiry=30.0,
                ),
            )
        return self._http_client

    async def close(self) -> None:
        """Close the HTTP client. Call on shutdown."""
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

    # =========================================================================
    # Incoming Payments
    # =========================================================================

    def payment_required_response(
        self,
        amount: Decimal,
        description: str = "API access",
        valid_for: int = 300,
    ) -> Response:
        """Generate 402 response. Optimized with pre-computed base payload."""
        if not self.wallet_address:
            raise X402Error("Cannot generate payment required: wallet address not configured")

        if amount <= 0:
            raise X402Error("Cannot generate payment required: amount must be positive")

        # Build payload efficiently
        payload = {
            **self._base_payment_payload,
            "amount": str(amount),
            "description": description,
            "validFor": valid_for,
        }

        encoded = base64.b64encode(json_dumps(payload).encode()).decode()

        return Response(
            status_code=402,
            content=json_dumps({"error": "Payment required", "description": description}),
            media_type="application/json",
            headers={"X-Payment-Required": encoded},
        )

    async def verify_payment(
        self,
        payment_header: str,
        expected_amount: Decimal,
    ) -> X402PaymentVerification:
        """Verify payment with caching for deduplication."""
        # Check cache first
        cache_key = f"{payment_header}:{expected_amount}"
        cached = self._verification_cache.get(cache_key)
        if cached:
            verification, timestamp = cached
            if (datetime.now(UTC).timestamp() - timestamp) < self._cache_ttl:
                return verification

        # Parse header
        try:
            decoded = base64.b64decode(payment_header).decode()
            payment_payload = json_loads(decoded)
        except (ValueError, Exception) as e:
            return X402PaymentVerification(
                valid=False,
                tx_hash=None,
                amount=None,
                error=f"Invalid payment header format: {e}",
            )

        # Call facilitator
        try:
            client = await self._get_client()
            response = await client.post(
                f"{self.facilitator_url}/verify",
                json={
                    "payment": payment_payload,
                    "expected_amount": str(usdc_to_micro(expected_amount)),
                    "recipient": self.wallet_address,
                },
            )

            result = response.json()

            if response.status_code != 200 or not result.get("valid"):
                verification = X402PaymentVerification(
                    valid=False,
                    tx_hash=None,
                    amount=None,
                    error=result.get("error", "Verification failed"),
                )
            else:
                received_micro = int(result.get("amount", 0))
                expected_micro = usdc_to_micro(expected_amount)

                if received_micro < expected_micro:
                    verification = X402PaymentVerification(
                        valid=False,
                        tx_hash=result.get("tx_hash"),
                        amount=micro_to_usdc(received_micro),
                        error=f"Amount mismatch: received {received_micro}, expected {expected_micro}",
                    )
                else:
                    verification = X402PaymentVerification(
                        valid=True,
                        tx_hash=result.get("tx_hash"),
                        amount=micro_to_usdc(received_micro),
                        error=None,
                    )

            # Cache the result
            self._verification_cache[cache_key] = (
                verification,
                datetime.now(UTC).timestamp(),
            )

            # Prune old cache entries periodically
            if len(self._verification_cache) > 10000:
                self._prune_cache()

            return verification

        except Exception as e:
            return X402PaymentVerification(
                valid=False,
                tx_hash=None,
                amount=None,
                error=f"Verification error: {e}",
            )

    def _prune_cache(self) -> None:
        """Remove expired cache entries."""
        now = datetime.now(UTC).timestamp()
        expired = [
            k for k, (_, ts) in self._verification_cache.items() if (now - ts) > self._cache_ttl
        ]
        for k in expired:
            del self._verification_cache[k]

    # =========================================================================
    # Outgoing Payments
    # =========================================================================

    async def pay(
        self,
        to_address: str,
        amount: Decimal,
        currency: str = "USDC",
    ) -> X402Receipt:
        """Send payment via x402."""
        try:
            client = await self._get_client()
            response = await client.post(
                f"{self.facilitator_url}/settle",
                json={
                    "from": self.wallet_address,
                    "to": to_address,
                    "amount": str(usdc_to_micro(amount)),
                    "currency": currency,
                    "network": self._caip2_network,
                },
            )

            result = response.json()

            if response.status_code != 200 or not result.get("success"):
                raise X402Error(f"Payment failed: {result.get('error', 'Unknown')}")

            timestamp_str = result.get("timestamp", "")
            if timestamp_str:
                timestamp_str = timestamp_str.replace("Z", "+00:00")
                timestamp = datetime.fromisoformat(timestamp_str)
            else:
                timestamp = datetime.now(UTC)

            return X402Receipt(
                tx_hash=result["tx_hash"],
                network=result.get("network", self._caip2_network),
                amount=amount,
                currency=currency,
                timestamp=timestamp,
            )

        except Exception as e:
            raise X402Error(f"Payment failed: {e}") from e

    # =========================================================================
    # Webhook Processing
    # =========================================================================

    async def process_topup_webhook(
        self,
        webhook_payload: dict[str, Any],
        credits_service: CreditsService,
    ) -> str:
        """Process webhook with efficient signature verification."""
        if not self._verify_webhook_signature(webhook_payload):
            raise X402Error("Invalid webhook signature")

        metadata = webhook_payload.get("metadata", {})
        agent_id = metadata.get("agent_id")
        tenant_id = metadata.get("tenant_id", "default")

        if not agent_id:
            raise X402Error("Missing agent_id in webhook metadata")

        tx_hash = webhook_payload.get("tx_hash", "")
        amount = micro_to_usdc(int(webhook_payload.get("amount", 0)))

        await credits_service.provision_wallet(agent_id=agent_id, tenant_id=tenant_id)

        return await credits_service.topup(
            agent_id=agent_id,
            amount=amount,
            source="x402",
            external_tx_id=tx_hash,
            tenant_id=tenant_id,
        )

    def _verify_webhook_signature(self, payload: dict[str, Any]) -> bool:
        """Verify webhook signature using HMAC-SHA256."""
        if not self._webhook_secret:
            return True

        signature = payload.get("signature")
        if not signature:
            return False

        payload_copy = {k: v for k, v in payload.items() if k != "signature"}
        payload_bytes = json_dumps(dict(sorted(payload_copy.items()))).encode()

        expected_sig = hmac.new(
            self._webhook_secret.encode(),
            payload_bytes,
            hashlib.sha256,
        ).hexdigest()

        return hmac.compare_digest(signature, expected_sig)


# =============================================================================
# Module Exports
# =============================================================================

__all__ = [
    "X402ClientOptimized",
    "X402Error",
    "X402Receipt",
    "X402PaymentVerification",
    "usdc_to_micro",
    "micro_to_usdc",
    "validate_wallet_address",
    "validate_network",
]
