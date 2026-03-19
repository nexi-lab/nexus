"""Payment service protocol (Issue #1357).

Defines the contract for payment protocol implementations, including
the transfer request/result data classes used across the protocol boundary.

Concrete implementations live in ``nexus.bricks.pay.protocol``.

Storage Affinity: **RecordStore** — transaction records + audit trail.

References:
    - docs/architecture/KERNEL-ARCHITECTURE.md §3
    - docs/architecture/data-storage-matrix.md (Four Pillars)
    - Issue #1357: Extensible protocol dispatch for agent commerce
    - Issue #2286: Protocol types moved here from bricks/pay/protocol.py
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.contracts.types import TransactionProtocol

# =============================================================================
# Protocol Data Classes
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

    protocol: "TransactionProtocol"
    tx_id: str
    amount: Decimal
    from_agent: str
    to: str
    tx_hash: str | None = None
    timestamp: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# =============================================================================
# Protocol Interface
# =============================================================================


@runtime_checkable
class PaymentProtocol(Protocol):
    """Protocol for payment protocol implementations.

    Each protocol must provide:
        protocol_name: TransactionProtocol enum value
        can_handle(to, metadata): sync detection (no I/O)
        transfer(request): async payment execution
    """

    @property
    def protocol_name(self) -> "TransactionProtocol": ...

    def can_handle(self, to: str, metadata: dict[str, Any] | None = None) -> bool: ...

    async def transfer(self, request: ProtocolTransferRequest) -> ProtocolTransferResult: ...


__all__ = [
    "PaymentProtocol",
    "ProtocolTransferRequest",
    "ProtocolTransferResult",
]
