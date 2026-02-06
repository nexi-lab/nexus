"""x402 protocol integration for Nexus Pay.

This module provides the X402Client for handling x402 HTTP-native payments:
- Outgoing payments (pay external x402-enabled services)
- Incoming payments (accept x402 for Nexus API endpoints)
- Credit topups (x402 → TigerBeetle via webhooks)

x402 Protocol Overview:
1. Server responds with 402 + PAYMENT-REQUIRED header (base64 JSON)
2. Client constructs payment, signs it, sends X-PAYMENT header
3. Server verifies via facilitator /verify endpoint
4. Server settles via facilitator /settle endpoint
5. Server returns PAYMENT-RESPONSE header with confirmation

References:
- https://www.x402.org
- https://docs.cdp.coinbase.com/x402/welcome
- https://github.com/coinbase/x402

Related: Issue #1206 (x402 protocol integration)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from starlette.responses import Response

if TYPE_CHECKING:
    import httpx

    from nexus.pay.credits import CreditsService

logger = logging.getLogger(__name__)

# =============================================================================
# Constants
# =============================================================================

# USDC has 6 decimal places
USDC_DECIMALS = 6
USDC_SCALE = 10**USDC_DECIMALS

# Default facilitator URL (Coinbase x402 facilitator)
DEFAULT_FACILITATOR_URL = "https://x402.org/facilitator"

# Supported networks with CAIP-2 identifiers
NETWORK_CAIP2_MAP = {
    "base": "eip155:8453",
    "ethereum": "eip155:1",
    "polygon": "eip155:137",
    "arbitrum": "eip155:42161",
    "optimism": "eip155:10",
    "solana": "solana:mainnet",
}

SUPPORTED_NETWORKS = frozenset(NETWORK_CAIP2_MAP.keys())

# Pre-compiled regex for wallet validation (2.5x faster)
_WALLET_REGEX = re.compile(r"^0x[0-9a-fA-F]{40}$")

# Default cache TTL for payment verifications
DEFAULT_CACHE_TTL_SECONDS = 60.0


# =============================================================================
# Exceptions
# =============================================================================


class X402Error(Exception):
    """Base exception for x402 operations."""

    pass


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class X402Receipt:
    """Receipt for a completed x402 payment."""

    tx_hash: str
    network: str
    amount: Decimal
    currency: str
    timestamp: datetime


@dataclass
class X402PaymentVerification:
    """Result of verifying an x402 payment."""

    valid: bool
    tx_hash: str | None
    amount: Decimal | None
    error: str | None


# =============================================================================
# Utility Functions
# =============================================================================


def usdc_to_micro(amount: Decimal) -> int:
    """Convert USDC amount to micro units (6 decimals).

    Args:
        amount: Amount in USDC (e.g., 1.50)

    Returns:
        Amount in micro units (e.g., 1_500_000)
    """
    return int(amount * USDC_SCALE)


def micro_to_usdc(micro: int) -> Decimal:
    """Convert micro units to USDC amount.

    Args:
        micro: Amount in micro units

    Returns:
        Amount in USDC
    """
    return Decimal(micro) / Decimal(USDC_SCALE)


def validate_wallet_address(address: str) -> bool:
    """Validate an EVM wallet address.

    Uses pre-compiled regex for 2.5x faster validation.

    Args:
        address: Wallet address to validate

    Returns:
        True if valid EVM address, False otherwise
    """
    return bool(address and _WALLET_REGEX.match(address))


def validate_network(network: str) -> bool:
    """Validate network is supported.

    Args:
        network: Network name to validate

    Returns:
        True if supported, False otherwise
    """
    return network in SUPPORTED_NETWORKS


# =============================================================================
# X402Client
# =============================================================================


class X402Client:
    """x402 protocol client for agent payments.

    Supports both outgoing payments (paying external services) and
    incoming payments (accepting x402 for Nexus APIs).

    Performance Features:
        - Payment verification caching (60s TTL by default)
        - Connection pooling for facilitator HTTP calls
        - Pre-compiled regex for address validation

    Example:
        >>> client = X402Client(
        ...     wallet_address="0x1234...",
        ...     network="base",
        ... )
        >>> # Generate 402 response for paid endpoint
        >>> response = client.payment_required_response(
        ...     amount=Decimal("1.00"),
        ...     description="API access",
        ... )
        >>> # Verify incoming payment
        >>> verification = await client.verify_payment(
        ...     payment_header=request.headers["X-Payment"],
        ...     expected_amount=Decimal("1.00"),
        ... )
    """

    def __init__(
        self,
        facilitator_url: str = DEFAULT_FACILITATOR_URL,
        wallet_address: str | None = None,
        network: str = "base",
        webhook_secret: str | None = None,
        cache_ttl: float = DEFAULT_CACHE_TTL_SECONDS,
    ):
        """Initialize X402Client.

        Args:
            facilitator_url: x402 facilitator service URL.
            wallet_address: EVM wallet address for receiving payments.
            network: Network name (base, ethereum, solana, etc.).
            webhook_secret: Secret for webhook signature verification.
            cache_ttl: TTL for payment verification cache in seconds.
        """
        self.facilitator_url = facilitator_url
        self.wallet_address = wallet_address
        self.network = network
        self._webhook_secret = webhook_secret
        self._cache_ttl = cache_ttl

        # Payment verification cache: {cache_key: (verification, timestamp)}
        self._verification_cache: dict[str, tuple[X402PaymentVerification, float]] = {}

        # Lazy-initialized persistent HTTP client with connection pooling
        self._http_client: httpx.AsyncClient | None = None

    @property
    def caip2_network(self) -> str:
        """Get CAIP-2 formatted network identifier."""
        return NETWORK_CAIP2_MAP.get(self.network, f"eip155:{self.network}")

    async def _get_http_client(self) -> httpx.AsyncClient:
        """Get or create persistent HTTP client with connection pooling.

        Connection pooling reduces TCP/TLS handshake overhead for
        repeated facilitator calls.
        """
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
        """Close the HTTP client. Call on application shutdown."""
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

    def _get_cached_verification(self, cache_key: str) -> X402PaymentVerification | None:
        """Get cached verification if still valid."""
        cached = self._verification_cache.get(cache_key)
        if cached:
            verification, timestamp = cached
            if (datetime.now(UTC).timestamp() - timestamp) < self._cache_ttl:
                logger.debug(f"Cache hit for payment verification: {cache_key[:20]}...")
                return verification
        return None

    def _cache_verification(self, cache_key: str, verification: X402PaymentVerification) -> None:
        """Cache a verification result."""
        self._verification_cache[cache_key] = (
            verification,
            datetime.now(UTC).timestamp(),
        )
        # Prune cache if too large
        if len(self._verification_cache) > 10000:
            self._prune_cache()

    def _prune_cache(self) -> None:
        """Remove expired cache entries."""
        now = datetime.now(UTC).timestamp()
        expired = [
            k for k, (_, ts) in self._verification_cache.items() if (now - ts) > self._cache_ttl
        ]
        for k in expired:
            del self._verification_cache[k]
        logger.debug(f"Pruned {len(expired)} expired cache entries")

    # =========================================================================
    # Incoming Payments (Accept x402 for Nexus APIs)
    # =========================================================================

    def payment_required_response(
        self,
        amount: Decimal,
        description: str = "API access",
        valid_for: int = 300,
    ) -> Response:
        """Generate 402 Payment Required response with payment details.

        Args:
            amount: Amount in USDC to charge.
            description: Human-readable description of what payment is for.
            valid_for: How long the payment request is valid (seconds).

        Returns:
            Starlette Response with 402 status and X-Payment-Required header.

        Raises:
            X402Error: If wallet address not configured or amount invalid.
        """
        if not self.wallet_address:
            raise X402Error("Cannot generate payment required: wallet address not configured")

        if amount <= 0:
            raise X402Error("Cannot generate payment required: amount must be positive")

        payload = {
            "amount": str(amount),
            "currency": "USDC",
            "address": self.wallet_address,
            "network": self.caip2_network,
            "description": description,
            "validFor": valid_for,
        }

        encoded = base64.b64encode(json.dumps(payload).encode()).decode()

        return Response(
            status_code=402,
            content=json.dumps({"error": "Payment required", "description": description}),
            media_type="application/json",
            headers={"X-Payment-Required": encoded},
        )

    async def verify_payment(
        self,
        payment_header: str,
        expected_amount: Decimal,
    ) -> X402PaymentVerification:
        """Verify incoming x402 payment header.

        Uses caching to prevent duplicate facilitator calls for the same
        payment (60s TTL by default). Uses connection pooling for efficiency.

        Args:
            payment_header: Base64-encoded payment payload from X-Payment header.
            expected_amount: Expected payment amount in USDC.

        Returns:
            X402PaymentVerification with verification result.
        """
        # Check cache first (prevents duplicate facilitator calls)
        cache_key = f"{payment_header}:{expected_amount}"
        cached = self._get_cached_verification(cache_key)
        if cached:
            return cached

        # Decode payment header
        try:
            decoded = base64.b64decode(payment_header).decode()
            payment_payload = json.loads(decoded)
        except (ValueError, json.JSONDecodeError) as e:
            return X402PaymentVerification(
                valid=False,
                tx_hash=None,
                amount=None,
                error=f"Invalid payment header format: {e}",
            )

        # Call facilitator verify endpoint with connection pooling
        try:
            client = await self._get_http_client()
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
                # Verify amount matches expected
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
            self._cache_verification(cache_key, verification)
            return verification

        except TimeoutError:
            return X402PaymentVerification(
                valid=False,
                tx_hash=None,
                amount=None,
                error="Verification timeout: facilitator did not respond",
            )
        except Exception as e:
            return X402PaymentVerification(
                valid=False,
                tx_hash=None,
                amount=None,
                error=f"Verification error: {e}",
            )

    # =========================================================================
    # Outgoing Payments (Pay external x402 services)
    # =========================================================================

    async def pay(
        self,
        to_address: str,
        amount: Decimal,
        currency: str = "USDC",
    ) -> X402Receipt:
        """Send payment via x402 to external service.

        Args:
            to_address: Recipient wallet address.
            amount: Amount in USDC to send.
            currency: Currency (default USDC).

        Returns:
            X402Receipt with payment confirmation.

        Raises:
            X402Error: If payment fails.
        """
        import httpx

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.facilitator_url}/settle",
                    json={
                        "from": self.wallet_address,
                        "to": to_address,
                        "amount": str(usdc_to_micro(amount)),
                        "currency": currency,
                        "network": self.caip2_network,
                    },
                    timeout=60.0,
                )

            result = response.json()

            if response.status_code != 200 or not result.get("success"):
                error_msg = result.get("error", "Payment failed")
                raise X402Error(f"Payment failed: {error_msg}")

            # Parse timestamp, handling 'Z' suffix for Python 3.10 compatibility
            timestamp_str = result.get("timestamp")
            if timestamp_str:
                # Replace 'Z' with '+00:00' for Python 3.10 compatibility
                timestamp_str = timestamp_str.replace("Z", "+00:00")
                timestamp = datetime.fromisoformat(timestamp_str)
            else:
                timestamp = datetime.now(UTC)

            return X402Receipt(
                tx_hash=result["tx_hash"],
                network=result.get("network", self.caip2_network),
                amount=amount,
                currency=currency,
                timestamp=timestamp,
            )

        except httpx.TimeoutException as e:
            raise X402Error("Payment failed: network timeout") from e
        except httpx.HTTPError as e:
            raise X402Error(f"Payment failed: network error - {e}") from e
        except ConnectionError as e:
            raise X402Error(f"Payment failed: connection error - {e}") from e

    async def pay_for_request(
        self,
        url: str,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> tuple[Any, X402Receipt | None]:
        """Make HTTP request, automatically handling 402 responses.

        Args:
            url: URL to request.
            method: HTTP method.
            headers: Optional headers.
            body: Optional request body.

        Returns:
            Tuple of (response, receipt). Receipt is None if no payment needed.
        """
        import httpx

        headers = headers or {}

        async with httpx.AsyncClient() as client:
            # First request
            response = await client.request(
                method=method,
                url=url,
                headers=headers,
                content=body,
                timeout=30.0,
            )

            # If not 402, return directly
            if response.status_code != 402:
                return response, None

            # Parse payment required header
            payment_required = response.headers.get("X-Payment-Required")
            if not payment_required:
                return response, None

            try:
                decoded = base64.b64decode(payment_required).decode()
                payment_details = json.loads(decoded)
            except (ValueError, json.JSONDecodeError):
                return response, None

            # Make payment
            receipt = await self.pay(
                to_address=payment_details["address"],
                amount=Decimal(payment_details["amount"]),
                currency=payment_details.get("currency", "USDC"),
            )

            # Retry request with payment proof
            # In real x402, this would include the signed payment in X-Payment header
            # For now, we assume the facilitator handled settlement
            retry_response = await client.request(
                method=method,
                url=url,
                headers={
                    **headers,
                    "X-Payment": base64.b64encode(
                        json.dumps({"tx_hash": receipt.tx_hash}).encode()
                    ).decode(),
                },
                content=body,
                timeout=30.0,
            )

            return retry_response, receipt

    # =========================================================================
    # Credit Topup (x402 → TigerBeetle)
    # =========================================================================

    async def process_topup_webhook(
        self,
        webhook_payload: dict[str, Any],
        credits_service: CreditsService,
    ) -> str:
        """Process x402 payment webhook and credit agent in TigerBeetle.

        Args:
            webhook_payload: Webhook payload from x402 facilitator.
            credits_service: CreditsService for TigerBeetle operations.

        Returns:
            Transaction ID from TigerBeetle.

        Raises:
            X402Error: If webhook validation fails or topup fails.
        """
        # 1. Verify webhook signature
        if not self._verify_webhook_signature(webhook_payload):
            raise X402Error("Invalid webhook signature")

        # 2. Extract payment details
        metadata = webhook_payload.get("metadata", {})
        agent_id = metadata.get("agent_id")
        tenant_id = metadata.get("tenant_id", "default")

        if not agent_id:
            raise X402Error("Missing agent_id in webhook metadata")

        tx_hash = webhook_payload.get("tx_hash", "")
        amount_micro = int(webhook_payload.get("amount", 0))
        amount = micro_to_usdc(amount_micro)

        # 3. Provision wallet if needed (idempotent)
        await credits_service.provision_wallet(
            agent_id=agent_id,
            tenant_id=tenant_id,
        )

        # 4. Credit agent in TigerBeetle
        # Use tx_hash as idempotency key to prevent double-crediting
        tx_id = await credits_service.topup(
            agent_id=agent_id,
            amount=amount,
            source="x402",
            external_tx_id=tx_hash,
            tenant_id=tenant_id,
        )

        return tx_id

    def _verify_webhook_signature(self, payload: dict[str, Any]) -> bool:
        """Verify webhook signature using HMAC-SHA256.

        Args:
            payload: Webhook payload with signature field.

        Returns:
            True if signature is valid, False otherwise.
        """
        if not self._webhook_secret:
            # If no secret configured, skip verification (not recommended for production)
            return True

        signature = payload.get("signature")
        if not signature:
            return False

        # Create payload without signature for verification
        payload_copy = {k: v for k, v in payload.items() if k != "signature"}
        payload_bytes = json.dumps(payload_copy, sort_keys=True).encode()

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
    "X402Client",
    "X402Error",
    "X402Receipt",
    "X402PaymentVerification",
    "usdc_to_micro",
    "micro_to_usdc",
    "validate_wallet_address",
    "validate_network",
]
