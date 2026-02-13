"""Protocol Abstraction Layer for multi-protocol payments.

Issue #1357 Phase 1: Extensible protocol dispatch for agent commerce.
Replaces hardcoded dual-routing (x402/credits) with a registry pattern
that supports future protocols (ACP, AP2).

Architecture:
    PaymentProtocol (ABC) → concrete implementations (X402, Credits)
    ProtocolDetector → ordered chain detection (auto-routing)
    ProtocolRegistry → name-based lookup + auto-detection

Detection chain order:
    1. X402PaymentProtocol — wallet addresses (0x...)
    2. CreditsPaymentProtocol — catch-all (agent IDs)
    Future: ACP/AP2 insert between x402 and credits via metadata checks.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from nexus.pay.audit_types import TransactionProtocol
from nexus.pay.x402 import validate_wallet_address

if TYPE_CHECKING:
    from nexus.pay.x402 import X402Client

logger = logging.getLogger(__name__)

# Legacy method name mapping: old name → TransactionProtocol value
_LEGACY_METHOD_MAP: dict[str, str] = {
    "credits": "internal",
}

# Reverse mapping: TransactionProtocol → user-facing method name for Receipt
_PROTOCOL_TO_METHOD: dict[TransactionProtocol, str] = {
    TransactionProtocol.INTERNAL: "credits",
    TransactionProtocol.X402: "x402",
    TransactionProtocol.ACP: "acp",
    TransactionProtocol.AP2: "ap2",
}


# =============================================================================
# Exceptions
# =============================================================================


class ProtocolError(Exception):
    """Base exception for protocol operations."""


class ProtocolNotFoundError(ProtocolError):
    """Raised when a requested protocol is not registered."""


class ProtocolDetectionError(ProtocolError):
    """Raised when no protocol matches the destination."""


# =============================================================================
# Data Classes
# =============================================================================


@dataclass(frozen=True)
class ProtocolTransferRequest:
    """Immutable request for a protocol transfer."""

    from_agent: str
    to: str
    amount: Decimal
    memo: str = ""
    idempotency_key: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProtocolTransferResult:
    """Immutable result from a protocol transfer."""

    protocol: TransactionProtocol
    tx_id: str
    amount: Decimal
    from_agent: str
    to: str
    tx_hash: str | None = None
    timestamp: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# =============================================================================
# ABC
# =============================================================================


class PaymentProtocol(ABC):
    """Abstract base for payment protocol implementations.

    Each protocol must provide:
        protocol_name: TransactionProtocol enum value
        can_handle(to, metadata): sync detection (no I/O)
        transfer(request): async payment execution
    """

    @property
    @abstractmethod
    def protocol_name(self) -> TransactionProtocol:
        """Return the protocol identifier."""

    @abstractmethod
    def can_handle(self, to: str, metadata: dict[str, Any] | None = None) -> bool:
        """Check if this protocol can handle the given destination."""

    @abstractmethod
    async def transfer(self, request: ProtocolTransferRequest) -> ProtocolTransferResult:
        """Execute a transfer using this protocol."""


# =============================================================================
# Detector
# =============================================================================


class ProtocolDetector:
    """Ordered chain detector that finds the first matching protocol.

    Protocols are checked in registration order. The first protocol
    whose can_handle() returns True is selected.
    """

    def __init__(self, protocols: list[PaymentProtocol]) -> None:
        self._protocols = list(protocols)

    def detect(
        self,
        to: str,
        metadata: dict[str, Any] | None = None,
    ) -> PaymentProtocol:
        """Detect the appropriate protocol for a destination.

        Raises:
            ProtocolDetectionError: If no protocol matches.
        """
        for protocol in self._protocols:
            if protocol.can_handle(to, metadata):
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "Detected protocol %s for destination %s",
                        protocol.protocol_name,
                        to,
                    )
                return protocol

        raise ProtocolDetectionError(f"No protocol can handle destination '{to}'")


# =============================================================================
# Registry
# =============================================================================


class ProtocolRegistry:
    """Registry for payment protocols with name-based lookup and auto-detection.

    Supports:
        - register/get/unregister by protocol name
        - resolve() with explicit method or auto-detection
        - Legacy name mapping (e.g., 'credits' → 'internal')
    """

    def __init__(self) -> None:
        self._protocols: dict[str, PaymentProtocol] = {}

    def register(self, protocol: PaymentProtocol) -> None:
        """Register a protocol by its name."""
        name = str(protocol.protocol_name)
        self._protocols[name] = protocol
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Registered protocol: %s", name)

    def get(self, name: str) -> PaymentProtocol:
        """Get a protocol by name.

        Raises:
            ProtocolNotFoundError: If protocol not found.
        """
        # Apply legacy mapping
        resolved_name = _LEGACY_METHOD_MAP.get(name, name)
        protocol = self._protocols.get(resolved_name)
        if protocol is None:
            raise ProtocolNotFoundError(
                f"Protocol '{name}' not found. Available: {', '.join(self._protocols.keys())}"
            )
        return protocol

    def unregister(self, name: str) -> None:
        """Remove a protocol by name. No-op if not registered."""
        resolved_name = _LEGACY_METHOD_MAP.get(name, name)
        self._protocols.pop(resolved_name, None)

    def resolve(
        self,
        method: str,
        to: str,
        metadata: dict[str, Any] | None = None,
    ) -> PaymentProtocol:
        """Resolve a protocol by method name or auto-detect.

        Args:
            method: Protocol method name, or 'auto' for detection.
            to: Destination address/agent ID.
            metadata: Optional metadata for detection hints.

        Raises:
            ProtocolNotFoundError: If method not found.
            ProtocolDetectionError: If auto-detect fails.
        """
        if method == "auto":
            detector = ProtocolDetector(list(self._protocols.values()))
            return detector.detect(to, metadata)

        return self.get(method)

    def list_protocols(self) -> list[str]:
        """Return list of registered protocol names."""
        return list(self._protocols.keys())


# =============================================================================
# Concrete: X402
# =============================================================================


class X402PaymentProtocol(PaymentProtocol):
    """x402 protocol implementation wrapping X402Client.

    Handles payments to EVM wallet addresses (0x...).
    """

    def __init__(self, client: X402Client) -> None:
        self._client = client

    @property
    def protocol_name(self) -> TransactionProtocol:
        return TransactionProtocol.X402

    def can_handle(
        self,
        to: str,
        metadata: dict[str, Any] | None = None,  # noqa: ARG002
    ) -> bool:
        return validate_wallet_address(to)

    async def transfer(self, request: ProtocolTransferRequest) -> ProtocolTransferResult:
        try:
            receipt = await self._client.pay(
                to_address=request.to,
                amount=request.amount,
            )
            return ProtocolTransferResult(
                protocol=TransactionProtocol.X402,
                tx_id=receipt.tx_hash,
                amount=request.amount,
                from_agent=request.from_agent,
                to=request.to,
                tx_hash=receipt.tx_hash,
                timestamp=receipt.timestamp,
                metadata={"network": receipt.network, "currency": receipt.currency},
            )
        except Exception as e:
            raise ProtocolError(f"x402 transfer failed: {e}") from e


# =============================================================================
# Concrete: Credits (Internal)
# =============================================================================


class CreditsPaymentProtocol(PaymentProtocol):
    """Internal credits protocol wrapping CreditsService.

    Catch-all for agent-to-agent transfers (non-wallet destinations).
    """

    def __init__(self, service: Any, zone_id: str = "default") -> None:
        self._service = service
        self._zone_id = zone_id

    @property
    def protocol_name(self) -> TransactionProtocol:
        return TransactionProtocol.INTERNAL

    def can_handle(
        self,
        to: str,
        metadata: dict[str, Any] | None = None,  # noqa: ARG002
    ) -> bool:
        return not validate_wallet_address(to)

    async def transfer(self, request: ProtocolTransferRequest) -> ProtocolTransferResult:
        try:
            tx_id = await self._service.transfer(
                from_id=request.from_agent,
                to_id=request.to,
                amount=request.amount,
                memo=request.memo,
                idempotency_key=request.idempotency_key,
                zone_id=self._zone_id,
            )
            return ProtocolTransferResult(
                protocol=TransactionProtocol.INTERNAL,
                tx_id=tx_id,
                amount=request.amount,
                from_agent=request.from_agent,
                to=request.to,
                tx_hash=None,
                timestamp=datetime.now(UTC),
                metadata={},
            )
        except Exception as e:
            raise ProtocolError(f"Credits transfer failed: {e}") from e


# =============================================================================
# Module Exports
# =============================================================================


def get_protocol_method_name(protocol: TransactionProtocol) -> str:
    """Get user-facing method name for a protocol enum value.

    Maps protocol enums to backwards-compatible method names
    (e.g., INTERNAL → "credits").
    """
    return _PROTOCOL_TO_METHOD.get(protocol, str(protocol))


__all__ = [
    "CreditsPaymentProtocol",
    "PaymentProtocol",
    "ProtocolDetectionError",
    "ProtocolDetector",
    "ProtocolError",
    "ProtocolNotFoundError",
    "ProtocolRegistry",
    "ProtocolTransferRequest",
    "ProtocolTransferResult",
    "X402PaymentProtocol",
    "get_protocol_method_name",
]
