"""Payment service protocol (Issue #1357).

Defines the contract for payment protocol implementations.
Concrete implementations live in ``nexus.pay.protocol``.

Storage Affinity: **RecordStore** — transaction records + audit trail.

References:
    - docs/architecture/KERNEL-ARCHITECTURE.md §3
    - docs/architecture/data-storage-matrix.md (Four Pillars)
    - Issue #1357: Extensible protocol dispatch for agent commerce
"""

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from nexus.pay.audit_types import TransactionProtocol
if TYPE_CHECKING:
    from nexus.pay.protocol import ProtocolTransferRequest, ProtocolTransferResult

@runtime_checkable
class PaymentProtocol(Protocol):
    """Protocol for payment protocol implementations.

    Each protocol must provide:
        protocol_name: TransactionProtocol enum value
        can_handle(to, metadata): sync detection (no I/O)
        transfer(request): async payment execution
    """

    @property
    def protocol_name(self) -> TransactionProtocol: ...

    def can_handle(self, to: str, metadata: dict[str, Any] | None = None) -> bool: ...

    async def transfer(self, request: "ProtocolTransferRequest") -> "ProtocolTransferResult": ...
